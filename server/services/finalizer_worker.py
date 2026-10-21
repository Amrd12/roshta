"""
JobFinalizerWorker — single background worker for post-batch finalization.

POST /job-result enqueues a FinalizationTask and returns 202 Accepted in
< 5 ms.  This worker then:
  1. Verifies the output image (TIFF or JPEG) using ImageVerifierFactory.
  2. On success:
       a. Moves each source PSD from Input/ to Finished/ via safe_move().
       b. Deletes the holding_files rows for those PSDs.
       c. Marks the batch 'done'.
  3. On verification failure:
       a. Keeps source PSDs in place (they are NOT moved).
       b. Marks the batch 'flagged' with an error message.

In both paths the worker rebuilds the QueueService snapshot so /state
reflects the change.

Thread safety:
  The worker is a single consumer thread reading from a queue.Queue.
  It opens its own SQLite connection per task (no shared connections).

Lifecycle:
    worker = JobFinalizerWorker(queue_service, app_config)
    worker.start()
    worker.enqueue(task)   # Called by /job-result controller
    worker.stop()
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

from server.config import AppConfig
from server.database import get_connection, db_transaction
from server.adapters.filesystem_adapter import safe_move
from server.adapters.image_verifier.factory import ImageVerifierFactory

logger = logging.getLogger(__name__)

# How long the worker waits for the next task before looping
_QUEUE_TIMEOUT_SECONDS = 1.0


@dataclass
class FinalizationTask:
    """
    Payload enqueued by POST /job-result for async processing.

    Fields:
        batch_id:         Primary key of the batch row.
        lease_id:         Must match batches.lease_id (prevents stale results).
        success:          Did Photoshop report success (True) or failure (False)?
        print_temp_path:  Absolute path to the output print file (TIFF/JPEG).
        tracker_temp_path: Absolute path to the tracker/low-res output (optional).
        duration_ms:      Wall-clock ms Photoshop reported for the render.
        error_message:    Populated when success=False; propagated to DB.
        expected_width_px:  Expected pixel width of the print file.
        expected_height_px: Expected pixel height of the print file.
        expected_dpi:       Expected DPI of the print file.
    """
    batch_id: str
    lease_id: str
    success: bool
    print_temp_path: Optional[str] = None
    tracker_temp_path: Optional[str] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    expected_width_px: int = 0
    expected_height_px: int = 0
    expected_dpi: int = 300


class JobFinalizerWorker:
    """
    Single-consumer background worker for post-batch finalization.

    Usage:
        worker = JobFinalizerWorker(queue_service, app_config)
        worker.start()
        worker.enqueue(task)   # from /job-result HTTP handler
        worker.stop()
    """

    def __init__(self, queue_service, app_config: AppConfig) -> None:
        self._queue_service = queue_service
        self._config = app_config
        self._task_queue: queue.Queue[FinalizationTask] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            logger.debug("JobFinalizerWorker already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="JobFinalizerWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("JobFinalizerWorker started.")

    def stop(self, timeout: float = 15.0) -> None:
        """Signal the worker to drain the queue and exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            logger.info("JobFinalizerWorker stopped.")

    def enqueue(self, task: FinalizationTask) -> None:
        """Enqueue a finalization task. Non-blocking; returns immediately."""
        self._task_queue.put_nowait(task)
        logger.debug("JobFinalizerWorker: task enqueued for batch %s.", task.batch_id)

    @property
    def queue_depth(self) -> int:
        """Current number of pending finalization tasks."""
        return self._task_queue.qsize()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        logger.info("JobFinalizerWorker: loop started.")
        while not self._stop_event.is_set() or not self._task_queue.empty():
            try:
                task = self._task_queue.get(timeout=_QUEUE_TIMEOUT_SECONDS)
            except queue.Empty:
                continue

            try:
                self._process(task)
            except Exception:
                logger.exception(
                    "JobFinalizerWorker: unhandled exception for batch %s.",
                    task.batch_id
                )
            finally:
                self._task_queue.task_done()

        logger.info("JobFinalizerWorker: loop exited.")

    # ------------------------------------------------------------------
    # Core finalization logic
    # ------------------------------------------------------------------

    def _process(self, task: FinalizationTask) -> None:
        """Process one finalization task end-to-end."""
        logger.info(
            "JobFinalizerWorker: processing batch %s (success=%s).",
            task.batch_id, task.success
        )

        # ── Guard: validate lease still matches ─────────────────────────
        conn = get_connection(self._config.database_path)
        try:
            batch_row = conn.execute(
                "SELECT status, lease_id FROM batches WHERE id = ?;",
                (task.batch_id,)
            ).fetchone()
        finally:
            conn.close()

        if batch_row is None:
            logger.error(
                "JobFinalizerWorker: batch %s not found in DB — discarding task.",
                task.batch_id
            )
            return

        if batch_row["lease_id"] != task.lease_id:
            logger.warning(
                "JobFinalizerWorker: stale lease for batch %s "
                "(db=%s, task=%s) — discarding.",
                task.batch_id, batch_row["lease_id"], task.lease_id
            )
            return

        if batch_row["status"] not in ("leased",):
            logger.warning(
                "JobFinalizerWorker: batch %s already in status '%s' — skipping.",
                task.batch_id, batch_row["status"]
            )
            return

        # ── Plugin reported failure — mark flagged immediately ───────────
        if not task.success:
            self._mark_failed(task, reason=task.error_message or "Plugin reported failure.")
            self._rebuild_snapshot()
            return

        # ── Verify output image ──────────────────────────────────────────
        if task.print_temp_path and task.expected_width_px and task.expected_height_px:
            ok, reason = ImageVerifierFactory.verify_file(
                path=task.print_temp_path,
                expected_width_px=task.expected_width_px,
                expected_height_px=task.expected_height_px,
                expected_dpi=task.expected_dpi,
            )
            if not ok:
                logger.warning(
                    "JobFinalizerWorker: verification FAILED for batch %s: %s",
                    task.batch_id, reason
                )
                self._mark_flagged(task, reason=f"Verification failed: {reason}")
                self._rebuild_snapshot()
                return
            logger.info(
                "JobFinalizerWorker: verification passed for batch %s.", task.batch_id
            )
        else:
            logger.warning(
                "JobFinalizerWorker: batch %s skipping verification "
                "(no path or dimensions provided).", task.batch_id
            )

        # ── Move files and finalise ──────────────────────────────────────
        try:
            self._finalise(task)
        except Exception as exc:
            logger.exception(
                "JobFinalizerWorker: finalise step failed for batch %s.", task.batch_id
            )
            self._mark_failed(task, reason=f"Finalisation error: {exc}")
        finally:
            self._rebuild_snapshot()

    def _finalise(self, task: FinalizationTask) -> None:
        """
        Move source PSDs to Finished/ and mark batch 'done' in one transaction.
        """
        # Load batch slots to know which source paths to move
        conn = get_connection(self._config.database_path)
        try:
            slot_rows = conn.execute(
                "SELECT source_path FROM batch_slots WHERE batch_id = ? AND is_empty = 0;",
                (task.batch_id,)
            ).fetchall()
        finally:
            conn.close()

        source_paths = [row["source_path"] for row in slot_rows]

        # Derive print_final_path from print_temp_path if provided
        print_final_path: Optional[str] = None
        tracker_final_path: Optional[str] = None

        if task.print_temp_path and os.path.exists(task.print_temp_path):
            fname = os.path.basename(task.print_temp_path)
            if self._config.finished_folder:
                print_final_path = os.path.join(self._config.finished_folder, fname)
                safe_move(task.print_temp_path, print_final_path)
            else:
                print_final_path = task.print_temp_path  # leave in place

        if task.tracker_temp_path and os.path.exists(task.tracker_temp_path):
            fname = os.path.basename(task.tracker_temp_path)
            if self._config.finished_folder:
                tracker_final_path = os.path.join(self._config.finished_folder, fname)
                safe_move(task.tracker_temp_path, tracker_final_path)
            else:
                tracker_final_path = task.tracker_temp_path

        # Move each source PSD to Finished/
        finished_paths: dict[str, str] = {}
        for src in source_paths:
            if src and os.path.exists(src) and self._config.finished_folder:
                dst = os.path.join(self._config.finished_folder, os.path.basename(src))
                safe_move(src, dst)
                finished_paths[src] = dst
                logger.info("JobFinalizerWorker: moved %s → %s", src, dst)

        # Commit everything atomically
        with db_transaction(self._config.database_path) as conn:
            # Mark batch done
            conn.execute(
                """
                UPDATE batches
                SET    status             = 'done',
                       completed_at       = CURRENT_TIMESTAMP,
                       duration_ms        = ?,
                       print_final_path   = ?,
                       tracker_final_path = ?
                WHERE  id = ?;
                """,
                (task.duration_ms, print_final_path, tracker_final_path, task.batch_id),
            )

            # Update finished_path on each slot
            for src, dst in finished_paths.items():
                conn.execute(
                    "UPDATE batch_slots SET finished_path = ? WHERE batch_id = ? AND source_path = ?;",
                    (dst, task.batch_id, src),
                )

            # Delete holding_files for moved PSDs (they are now in Finished/)
            if source_paths:
                placeholders = ",".join("?" * len(source_paths))
                conn.execute(
                    f"DELETE FROM holding_files WHERE file_path IN ({placeholders});",
                    source_paths,
                )

        logger.info(
            "JobFinalizerWorker: batch %s → done (moved %d file(s)).",
            task.batch_id, len(finished_paths)
        )

    def _mark_failed(self, task: FinalizationTask, reason: str) -> None:
        """Mark batch as 'failed' and unlock holding_files."""
        self._mark_terminal(task, status="failed", reason=reason)

    def _mark_flagged(self, task: FinalizationTask, reason: str) -> None:
        """Mark batch as 'flagged' (needs operator review) and unlock holding_files."""
        self._mark_terminal(task, status="flagged", reason=reason)

    def _mark_terminal(
        self, task: FinalizationTask, status: str, reason: str
    ) -> None:
        """
        Atomically set batch to a terminal/error status and unlock holding_files.
        Source PSDs are NOT moved; they stay in Input/ for the next attempt.
        """
        with db_transaction(self._config.database_path) as conn:
            conn.execute(
                """
                UPDATE batches
                SET    status        = ?,
                       completed_at  = CURRENT_TIMESTAMP,
                       error_message = ?
                WHERE  id = ?;
                """,
                (status, reason, task.batch_id),
            )
            # Return files to waiting pool
            conn.execute(
                """
                UPDATE holding_files
                SET    status   = 'waiting',
                       batch_id = NULL
                WHERE  batch_id = ?;
                """,
                (task.batch_id,),
            )

        logger.warning(
            "JobFinalizerWorker: batch %s → %s. Reason: %s",
            task.batch_id, status, reason
        )

    def _rebuild_snapshot(self) -> None:
        """Trigger a snapshot rebuild so /state is up to date."""
        conn = get_connection(self._config.database_path)
        try:
            self._queue_service.rebuild_snapshot(conn)
        except Exception:
            logger.exception("JobFinalizerWorker: failed to rebuild snapshot.")
        finally:
            conn.close()
