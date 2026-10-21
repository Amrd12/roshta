"""
LeaseReaperDaemon — background thread that reclaims expired batch leases.

Every 15 seconds it queries for 'leased' batches whose lease_expires_at
has passed and marks them 'interrupted'. The holding_files rows are
unlocked (status='waiting', batch_id=NULL) so they re-enter the queue.

Lifecycle:
    reaper = LeaseReaperDaemon(queue_service, app_config)
    reaper.start()   # Daemon thread; dies with main process
    reaper.stop()    # Graceful shutdown
"""
from __future__ import annotations

import logging
import threading

from server.config import AppConfig
from server.database import get_connection, db_transaction

logger = logging.getLogger(__name__)

_REAP_INTERVAL_SECONDS = 15.0


class LeaseReaperDaemon:
    """
    Daemon thread that finds and reclaims timed-out lease slots.

    A batch in 'leased' status with lease_expires_at < now is considered
    abandoned (Photoshop hung, modal dialog, crash, etc.).  The reaper:
      1. Marks the batch 'interrupted'.
      2. Unlocks all holding_files rows tied to that batch so the scanner
         can re-queue them on the next pass.
      3. Calls queue_service.rebuild_snapshot() so /state reflects the change.
    """

    def __init__(self, queue_service, app_config: AppConfig) -> None:
        self._queue_service = queue_service
        self._config = app_config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the reaper daemon thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            logger.debug("LeaseReaperDaemon already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="LeaseReaperDaemon",
            daemon=True,
        )
        self._thread.start()
        logger.info("LeaseReaperDaemon started (interval=%ss)", _REAP_INTERVAL_SECONDS)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the daemon to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            logger.info("LeaseReaperDaemon stopped.")

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        logger.info("LeaseReaperDaemon: loop started.")
        while not self._stop_event.is_set():
            try:
                reclaimed = self._reap_expired_leases()
                if reclaimed:
                    logger.info(
                        "LeaseReaperDaemon: reclaimed %d expired lease(s).", reclaimed
                    )
            except Exception:
                logger.exception("LeaseReaperDaemon: unexpected error during reap.")

            self._stop_event.wait(timeout=_REAP_INTERVAL_SECONDS)

        logger.info("LeaseReaperDaemon: loop exited.")

    def _reap_expired_leases(self) -> int:
        """
        Find all batches whose lease has expired and reclaim them.

        Returns the number of batches reclaimed (0 if nothing to do).
        """
        # Query outside the write transaction — hold no write lock during lookup
        conn = get_connection(self._config.database_path)
        try:
            expired_rows = conn.execute(
                """
                SELECT id FROM batches
                WHERE  status = 'leased'
                  AND  lease_expires_at < CURRENT_TIMESTAMP
                """
            ).fetchall()
        finally:
            conn.close()

        if not expired_rows:
            return 0

        expired_ids = [row["id"] for row in expired_rows]
        reclaimed = 0

        for batch_id in expired_ids:
            try:
                self._reclaim_one(batch_id)
                reclaimed += 1
            except Exception:
                logger.exception(
                    "LeaseReaperDaemon: failed to reclaim batch %s", batch_id
                )

        # Rebuild snapshot so /state reflects newly unlocked files
        if reclaimed:
            conn = get_connection(self._config.database_path)
            try:
                self._queue_service.rebuild_snapshot(conn)
            finally:
                conn.close()

        return reclaimed

    def _reclaim_one(self, batch_id: str) -> None:
        """
        Atomically mark one expired batch as 'interrupted' and unlock
        its holding_files rows so they re-enter the 'waiting' pool.
        """
        with db_transaction(self._config.database_path) as conn:
            # Re-check inside the write transaction to avoid race with /job-result
            row = conn.execute(
                "SELECT status FROM batches WHERE id = ?;", (batch_id,)
            ).fetchone()

            if row is None or row["status"] != "leased":
                logger.debug(
                    "LeaseReaperDaemon: batch %s no longer 'leased' (now: %s) — skip.",
                    batch_id, row["status"] if row else "not found"
                )
                return  # Already resolved by another path

            conn.execute(
                """
                UPDATE batches
                SET    status        = 'interrupted',
                       completed_at  = CURRENT_TIMESTAMP,
                       error_message = 'Lease expired — reclaimed by reaper'
                WHERE  id = ?;
                """,
                (batch_id,),
            )

            # Unlock the holding_files: return them to the 'waiting' pool
            conn.execute(
                """
                UPDATE holding_files
                SET    status   = 'waiting',
                       batch_id = NULL
                WHERE  batch_id = ?;
                """,
                (batch_id,),
            )

        logger.info("LeaseReaperDaemon: batch %s → interrupted.", batch_id)
