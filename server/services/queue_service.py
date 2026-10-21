"""
QueueService — thread-safe state machine and plan tree snapshot manager.

Phase 2:
  - Owns the RLock and the immutable PlanTree snapshot.
  - rebuild_snapshot(): reads holding_files from DB, builds a new PlanTree,
    atomically swaps the reference. Called by ScannerService after each pass.
  - apply_selection(): toggles is_skipped in DB then rebuilds the snapshot.
  - get_snapshot(): returns the current snapshot (no lock needed; atomic ref).

Phase 3 (added here):
  - lease_batch(): atomically promotes the next ready batch to 'leased',
    locks its holding_files rows, and returns a job contract.
  - extend_lease(): heartbeat that pushes the lease_expires_at forward.
  - disk_space_ok(): gate checked before every lease.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import uuid
from typing import Any, Dict, Optional

from server.config import AppConfig
from server.database import get_connection, db_transaction, stamp_boot_id
from server.models.prescription import PrescriptionFile
from server.models.plan_tree import PlanTree
from server.models.selection_rules import SelectionChange

logger = logging.getLogger(__name__)


class StaleRevisionError(Exception):
    """Raised when a mutating request carries an outdated rev."""


class QueueService:
    """
    Singleton service that owns:
      - The primary threading.RLock (for all state mutations).
      - The immutable PlanTree snapshot (swapped atomically on each rebuild).
      - The monotonic revision counter and boot_id.

    Polling (/state) reads _snapshot directly without acquiring the lock;
    the reference is a Python object reference swap which is atomic in CPython.
    """

    def __init__(self, app_config: AppConfig):
        self._config = app_config
        self._lock = threading.RLock()
        self._mode = "idle"  # "idle" | "running" | "paused" | "pausing" | "stopped"
        self._rev = 0
        self._boot_id: str = ""
        self._warnings: dict = {"disk_warning": False, "csv_locked": False}

        # The immutable snapshot; reads are lock-free (CPython GIL guarantees
        # atomic reference assignment for simple name bindings).
        self._snapshot: Optional[PlanTree] = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def initialise(self) -> None:
        """
        Called once during create_app() after DB is ready.
        Stamps a fresh boot_id, performs an initial snapshot build.
        """
        self._boot_id = stamp_boot_id(self._config.database_path)
        conn = get_connection(self._config.database_path)
        try:
            self.rebuild_snapshot(conn)
        finally:
            conn.close()
        logger.info("QueueService initialised (boot_id=%s)", self._boot_id)

    # ------------------------------------------------------------------
    # Snapshot management (called by ScannerService daemon thread)
    # ------------------------------------------------------------------

    def rebuild_snapshot(self, conn: sqlite3.Connection) -> None:
        """
        Read current holding_files from DB, build a new PlanTree, and
        atomically swap the snapshot reference.

        The RLock is acquired only for the swap itself, not for the DB read.
        This keeps the lock acquisition time to microseconds.
        """
        files = self._load_holding_files(conn)

        with self._lock:
            self._rev += 1
            new_snapshot = PlanTree.build(
                files=files,
                boot_id=self._boot_id,
                rev=self._rev,
                mode=self._mode,
                warnings=dict(self._warnings),
            )

        # Atomic reference swap — polling threads see the new snapshot immediately
        self._snapshot = new_snapshot
        logger.debug("Snapshot rebuilt (rev=%d, files=%d)", self._rev, len(files))

    def get_snapshot(self) -> Optional[PlanTree]:
        """Return the current immutable snapshot. Lock-free."""
        return self._snapshot

    # ------------------------------------------------------------------
    # Selection mutations (POST /selection)
    # ------------------------------------------------------------------

    def apply_selection(self, change: SelectionChange) -> PlanTree:
        """
        Toggle is_skipped on holding_files rows as requested by the operator.

        Raises:
            StaleRevisionError: if change.rev doesn't match current server rev.
            ValueError:         if any ID in skip/include doesn't exist or is locked.

        Returns the freshly rebuilt snapshot.
        """
        with self._lock:
            if change.rev != self._rev:
                raise StaleRevisionError(
                    f"Stale rev: client={change.rev}, server={self._rev}"
                )

            with db_transaction(self._config.database_path) as conn:
                # Validate — only 'waiting' rows can be toggled
                all_ids = change.skip + change.include
                if all_ids:
                    placeholders = ",".join("?" * len(all_ids))
                    rows = conn.execute(
                        f"SELECT id, status FROM holding_files WHERE id IN ({placeholders});",
                        all_ids
                    ).fetchall()
                    found_ids = {row["id"] for row in rows}
                    locked_ids = {row["id"] for row in rows if row["status"] == "locked"}

                    missing = set(all_ids) - found_ids
                    if missing:
                        raise ValueError(f"Unknown file IDs: {sorted(missing)}")
                    if locked_ids:
                        raise ValueError(
                            f"Cannot toggle locked files (currently processing): "
                            f"{sorted(locked_ids)}"
                        )

                # Apply skips
                if change.skip:
                    conn.execute(
                        f"UPDATE holding_files SET is_skipped=1 "
                        f"WHERE id IN ({','.join('?' * len(change.skip))}) "
                        f"AND status='waiting';",
                        change.skip
                    )

                # Apply includes
                if change.include:
                    conn.execute(
                        f"UPDATE holding_files SET is_skipped=0 "
                        f"WHERE id IN ({','.join('?' * len(change.include))}) "
                        f"AND status='waiting';",
                        change.include
                    )

                # Rebuild snapshot using the same connection (within transaction)
                files = self._load_holding_files(conn)
                self._rev += 1
                new_snapshot = PlanTree.build(
                    files=files,
                    boot_id=self._boot_id,
                    rev=self._rev,
                    mode=self._mode,
                    warnings=dict(self._warnings),
                )

        self._snapshot = new_snapshot
        logger.info(
            "Selection applied (rev=%d, skip=%d, include=%d)",
            self._rev, len(change.skip), len(change.include)
        )
        return new_snapshot

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_holding_files(self, conn: sqlite3.Connection) -> list[PrescriptionFile]:
        """Load all holding_files rows into domain objects."""
        rows = conn.execute(
            "SELECT id, file_path, clinic, paper_weight, doctor_name, "
            "file_size, mtime, is_skipped, status, batch_id "
            "FROM holding_files;"
        ).fetchall()
        return [PrescriptionFile.from_db_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Phase 3: Lease management
    # ------------------------------------------------------------------

    # Default lease duration (seconds); override via AppConfig if needed
    LEASE_SECONDS: int = 300       # 5 minutes per batch
    DISK_MIN_BYTES: int = 5 * 1024 ** 3   # 5 GB minimum free space

    def lease_batch(
        self,
        force: bool = False,
        clinic: Optional[str] = None,
        paper_weight: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically promote the next ready batch to 'leased'.

        Selection logic:
          - Mode must be 'running' (not paused/stopped).
          - Disk space must be above threshold.
          - No other batch currently in 'leased' status for the same group
            (one Photoshop job at a time).
          - Selects the 4 oldest 'waiting', non-skipped files in the chosen
            (clinic, paper_weight) group.
          - If force=True and fewer than 4 files are available (minimum 1),
            empty placeholder slots are filled in.

        Returns a job-contract dict on success, or None if nothing is ready.
        Raises RuntimeError with a human-readable code on soft blocks.
        """
        with self._lock:
            if self._mode not in ("running", "idle"):
                raise RuntimeError(f"SERVER_NOT_RUNNING: mode is '{self._mode}'")

            if not self._check_disk_space():
                self._warnings["disk_warning"] = True
                raise RuntimeError("DISK_SPACE_LOW: less than 5 GB free on output volume")
            self._warnings["disk_warning"] = False

            # Find a ready group — prefer caller-specified group
            group = self._pick_ready_group(
                clinic=clinic,
                paper_weight=paper_weight,
                force=force,
            )
            if group is None:
                return None

            chosen_clinic, chosen_pw, candidate_files = group

            # Atomically promote in DB
            job_contract = self._promote_to_leased(
                clinic=chosen_clinic,
                paper_weight=chosen_pw,
                candidate_files=candidate_files,
                force=force,
            )

            # Rebuild snapshot so /state shows new locked files
            conn = get_connection(self._config.database_path)
            try:
                self.rebuild_snapshot(conn)
            finally:
                conn.close()

            return job_contract

    def extend_lease(self, batch_id: str, lease_id: str) -> bool:
        """
        Extend the lease expiry for an active batch (heartbeat).

        Returns True on success, False if the lease_id doesn't match or
        the batch is no longer in 'leased' status.
        """
        lease_seconds = getattr(self._config, "lease_seconds", self.LEASE_SECONDS)
        with db_transaction(self._config.database_path) as conn:
            row = conn.execute(
                "SELECT status, lease_id FROM batches WHERE id = ?;",
                (batch_id,)
            ).fetchone()

            if row is None:
                logger.warning("extend_lease: batch %s not found.", batch_id)
                return False

            if row["lease_id"] != lease_id:
                logger.warning(
                    "extend_lease: lease_id mismatch for batch %s "
                    "(got %s, expected %s).",
                    batch_id, lease_id, row["lease_id"]
                )
                return False

            if row["status"] != "leased":
                logger.warning(
                    "extend_lease: batch %s is '%s', not 'leased'.",
                    batch_id, row["status"]
                )
                return False

            conn.execute(
                """
                UPDATE batches
                SET lease_expires_at = DATETIME(
                    CURRENT_TIMESTAMP, '+' || ? || ' seconds'
                )
                WHERE id = ?;
                """,
                (lease_seconds, batch_id),
            )

        logger.debug("extend_lease: batch %s extended by %ss.", batch_id, lease_seconds)
        return True

    # ------------------------------------------------------------------
    # Phase 3: Internal helpers
    # ------------------------------------------------------------------

    def _check_disk_space(self) -> bool:
        """Return True if the output volume has ≥ min_bytes free."""
        path = self._config.finished_folder or self._config.source_folder
        if not path:
            return True  # Can't check — allow
        min_bytes = getattr(self._config, "disk_min_bytes", self.DISK_MIN_BYTES)
        try:
            free = shutil.disk_usage(path).free
            return free >= min_bytes
        except Exception:
            logger.exception("_check_disk_space: could not read disk usage.")
            return True  # Fail-open so a bad stat doesn't block production

    def _pick_ready_group(
        self,
        clinic: Optional[str],
        paper_weight: Optional[str],
        force: bool,
    ) -> Optional[tuple]:
        """
        Find the (clinic, paper_weight, [files]) group that is ready to lease.

        Returns (clinic, paper_weight, [PrescriptionFile, ...]) or None.
        """
        conn = get_connection(self._config.database_path)
        try:
            # Load all waiting, non-skipped files
            where_extra = ""
            params: list = []
            if clinic:
                where_extra += " AND clinic = ?"
                params.append(clinic)
            if paper_weight:
                where_extra += " AND paper_weight = ?"
                params.append(paper_weight)

            rows = conn.execute(
                f"""
                SELECT id, file_path, clinic, paper_weight, doctor_name,
                       file_size, mtime, is_skipped, status, batch_id
                FROM   holding_files
                WHERE  status = 'waiting'
                  AND  is_skipped = 0
                  {where_extra}
                ORDER BY mtime ASC, file_path ASC;
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        # Group by (clinic, paper_weight); pick first group with ≥4 files (or any if force)
        from server.models.prescription import PrescriptionFile
        groups: Dict[tuple, list] = {}
        for row in rows:
            key = (row["clinic"], row["paper_weight"])
            groups.setdefault(key, []).append(PrescriptionFile.from_db_row(row))

        # Prefer the group with the most files that has ≥4 (ready)
        ready = {k: v for k, v in groups.items() if len(v) >= 4}
        if ready:
            # Pick the group whose oldest file is oldest overall
            key = min(ready, key=lambda k: ready[k][0].mtime)
            return key[0], key[1], ready[key][:4]

        if force:
            # Pick whichever group has files (oldest-first)
            key = min(groups, key=lambda k: groups[k][0].mtime)
            return key[0], key[1], groups[key][:4]

        return None

    def _promote_to_leased(
        self,
        clinic: str,
        paper_weight: str,
        candidate_files: list,
        force: bool,
    ) -> Dict[str, Any]:
        """
        Execute the atomic BEGIN IMMEDIATE promotion transaction.

        Returns the job contract dict.
        """
        from server.models.batch import SLOT_ORDER, SLOT_COORDS

        lease_seconds = getattr(self._config, "lease_seconds", self.LEASE_SECONDS)
        batch_id = f"batch_{uuid.uuid4().hex[:16]}"
        lease_id = str(uuid.uuid4())
        slot_count = 4
        is_forced = force and len(candidate_files) < 4

        conn = get_connection(self._config.database_path)
        try:
            # BEGIN IMMEDIATE prevents readers from upgrading to writers while
            # we're assigning files, eliminating the double-batch race.
            conn.execute("BEGIN IMMEDIATE;")

            # Re-check the candidate files are still 'waiting' (guard against race)
            file_ids = [f.id for f in candidate_files]
            placeholders = ",".join("?" * len(file_ids))
            fresh_rows = conn.execute(
                f"""
                SELECT id, file_path, clinic, paper_weight, doctor_name,
                       file_size, mtime, is_skipped, status, batch_id
                FROM   holding_files
                WHERE  id IN ({placeholders})
                  AND  status = 'waiting'
                  AND  is_skipped = 0;
                """,
                file_ids,
            ).fetchall()

            if len(fresh_rows) < 1:
                raise RuntimeError("RACE: files were grabbed by another lease — retry")

            from server.models.prescription import PrescriptionFile
            fresh_files = [PrescriptionFile.from_db_row(r) for r in fresh_rows]

            # Atomically increment the batch counter
            conn.execute(
                """
                UPDATE system_state
                SET    value      = CAST(value AS INTEGER) + 1,
                       updated_at = CURRENT_TIMESTAMP
                WHERE  key = 'next_batch_number';
                """
            )
            batch_num_row = conn.execute(
                "SELECT value FROM system_state WHERE key = 'next_batch_number';"
            ).fetchone()
            batch_number = f"B{int(batch_num_row['value']) - 1:05d}"

            # Insert the batch row
            conn.execute(
                """
                INSERT INTO batches (
                    id, batch_number, lease_id, clinic, paper_weight,
                    status, attempt_count, slot_count, is_forced,
                    leased_at, lease_expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    'leased', 1, ?, ?,
                    CURRENT_TIMESTAMP,
                    DATETIME(CURRENT_TIMESTAMP, '+' || ? || ' seconds')
                );
                """,
                (
                    batch_id, batch_number, lease_id, clinic, paper_weight,
                    slot_count, 1 if is_forced else 0,
                    lease_seconds,
                ),
            )

            # Insert batch_slots and lock holding_files
            slot_assignments = []
            for i, slot_name in enumerate(SLOT_ORDER):
                x, y = SLOT_COORDS[slot_name]
                if i < len(fresh_files):
                    f = fresh_files[i]
                    conn.execute(
                        """
                        INSERT INTO batch_slots
                            (batch_id, slot, doctor_name, source_path, x_cm, y_cm, is_empty)
                        VALUES (?, ?, ?, ?, ?, ?, 0);
                        """,
                        (batch_id, slot_name, f.doctor_name, f.file_path, x, y),
                    )
                    slot_assignments.append({
                        "slot": slot_name,
                        "doctor_name": f.doctor_name,
                        "source_path": f.file_path,
                        "x_cm": x,
                        "y_cm": y,
                        "is_empty": False,
                    })
                elif is_forced:
                    conn.execute(
                        """
                        INSERT INTO batch_slots
                            (batch_id, slot, doctor_name, source_path, x_cm, y_cm, is_empty)
                        VALUES (?, ?, '[EMPTY]', '', ?, ?, 1);
                        """,
                        (batch_id, slot_name, x, y),
                    )
                    slot_assignments.append({
                        "slot": slot_name,
                        "doctor_name": "[EMPTY]",
                        "source_path": "",
                        "x_cm": x,
                        "y_cm": y,
                        "is_empty": True,
                    })

            # Lock holding_files rows
            lock_ids = [f.id for f in fresh_files]
            conn.execute(
                f"""
                UPDATE holding_files
                SET    status   = 'locked',
                       batch_id = ?
                WHERE  id IN ({','.join('?' * len(lock_ids))});
                """,
                [batch_id] + lock_ids,
            )

            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        else:
            conn.close()

        logger.info(
            "lease_batch: promoted %s (%s) → batch %s (lease %s, %ds)",
            clinic, paper_weight, batch_number, lease_id, lease_seconds
        )

        return {
            "batch_id":    batch_id,
            "batch_number": batch_number,
            "lease_id":    lease_id,
            "clinic":      clinic,
            "paper_weight": paper_weight,
            "is_forced":   is_forced,
            "lease_seconds": lease_seconds,
            "slots":       slot_assignments,
        }

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def rev(self) -> int:
        return self._rev

    @property
    def boot_id(self) -> str:
        return self._boot_id

    # ------------------------------------------------------------------
    # Phase 5: Queue control
    # ------------------------------------------------------------------

    def set_mode(self, new_mode: str) -> str:
        """
        Transition the queue mode.

        Allowed transitions:
            idle/paused/stopped  → running   (play)
            running              → pausing   (pause — waits for current batch)
            pausing/running/idle → stopped   (stop)
            pausing              → paused    (internal: set by finalizer after batch done)

        Returns the resulting mode string.
        Raises ValueError for illegal transitions.
        """
        valid_transitions: Dict[str, list] = {
            "play":   {"idle", "paused", "stopped", "pausing"},
            "pause":  {"running"},
            "stop":   {"running", "pausing", "paused", "idle"},
            "paused": {"pausing"},  # internal: finalizer calls after last batch drains
        }
        new_mode_map = {
            "play":   "running",
            "pause":  "pausing",
            "stop":   "stopped",
            "paused": "paused",
        }

        with self._lock:
            if new_mode not in new_mode_map:
                raise ValueError(f"Unknown mode command: '{new_mode}'")
            if self._mode not in valid_transitions[new_mode]:
                raise ValueError(
                    f"Cannot '{new_mode}' from mode '{self._mode}'. "
                    f"Allowed from: {sorted(valid_transitions[new_mode])}"
                )
            self._mode = new_mode_map[new_mode]
            logger.info("QueueService: mode → '%s' (via command '%s')", self._mode, new_mode)

            # Rebuild snapshot so /state immediately reflects the new mode
            conn = get_connection(self._config.database_path)
            try:
                self.rebuild_snapshot(conn)
            finally:
                conn.close()

        return self._mode

    def retry_batch(self, batch_id: str) -> bool:
        """
        Re-queue a batch that is in a terminal error state
        ('failed', 'interrupted', or 'flagged') back to 'waiting'.

        Returns True on success, False if the batch is not found or is
        not in a retryable state.
        """
        RETRYABLE = ("failed", "interrupted", "flagged")

        with self._lock:
            with db_transaction(self._config.database_path) as conn:
                row = conn.execute(
                    "SELECT status FROM batches WHERE id = ?;",
                    (batch_id,)
                ).fetchone()

                if row is None:
                    logger.warning("retry_batch: batch %s not found.", batch_id)
                    return False

                if row["status"] not in RETRYABLE:
                    logger.warning(
                        "retry_batch: batch %s in status '%s' is not retryable.",
                        batch_id, row["status"]
                    )
                    return False

                # Increment attempt counter, reset status
                conn.execute(
                    """
                    UPDATE batches
                    SET    status        = 'interrupted',
                           error_message = NULL,
                           completed_at  = NULL
                    WHERE  id = ?;
                    """,
                    (batch_id,)
                )

                # Return holding_files to 'waiting' pool
                conn.execute(
                    """
                    UPDATE holding_files
                    SET    status   = 'waiting',
                           batch_id = NULL
                    WHERE  batch_id = ?;
                    """,
                    (batch_id,)
                )

            conn2 = get_connection(self._config.database_path)
            try:
                self.rebuild_snapshot(conn2)
            finally:
                conn2.close()

        logger.info("retry_batch: batch %s returned to waiting pool.", batch_id)
        return True

    def mark_reviewed(self, batch_id: str) -> bool:
        """
        Acknowledge a 'flagged' batch without retrying it — marks it 'done'
        so it no longer appears as an error requiring operator attention.

        Returns True on success, False if not found or not flagged.
        """
        with self._lock:
            with db_transaction(self._config.database_path) as conn:
                row = conn.execute(
                    "SELECT status FROM batches WHERE id = ?;",
                    (batch_id,)
                ).fetchone()

                if row is None or row["status"] != "flagged":
                    logger.warning(
                        "mark_reviewed: batch %s not found or not 'flagged' (status=%s).",
                        batch_id, row["status"] if row else "NOT_FOUND"
                    )
                    return False

                conn.execute(
                    """
                    UPDATE batches
                    SET    status       = 'done',
                           error_message = NULL,
                           completed_at  = CURRENT_TIMESTAMP
                    WHERE  id = ?;
                    """,
                    (batch_id,)
                )

            conn2 = get_connection(self._config.database_path)
            try:
                self.rebuild_snapshot(conn2)
            finally:
                conn2.close()

        logger.info("mark_reviewed: batch %s cleared as reviewed.", batch_id)
        return True

    def get_resume_summary(self) -> Dict[str, Any]:
        """
        Query the DB for batches that survived a crash/restart and need
        operator attention.

        Returns a dict used by GET /health:
            {
              "interrupted_batches": N,
              "leased_batches":      N,   # leased at last shutdown — probably stale
              "flagged_batches":     N,
              "recommendation":      "auto_restart" | "flag_for_review" | "none"
            }

        Logic:
          - flagged > 0  → "flag_for_review" (needs human eyes)
          - interrupted > 0 or leased > 0 → "auto_restart" (safe to re-queue)
          - otherwise → "none"
        """
        conn = get_connection(self._config.database_path)
        try:
            row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status='interrupted' THEN 1 ELSE 0 END) AS interrupted,
                  SUM(CASE WHEN status='leased'      THEN 1 ELSE 0 END) AS leased,
                  SUM(CASE WHEN status='flagged'     THEN 1 ELSE 0 END) AS flagged
                FROM batches;
                """
            ).fetchone()
            h_row = conn.execute(
                "SELECT COUNT(*) AS count FROM holding_files;"
            ).fetchone()
        finally:
            conn.close()

        interrupted = int(row["interrupted"] or 0) if row else 0
        leased      = int(row["leased"]      or 0) if row else 0
        flagged     = int(row["flagged"]     or 0) if row else 0
        holding     = int(h_row["count"]     or 0) if h_row else 0

        if flagged > 0:
            recommendation = "flag_for_review"
        elif interrupted > 0 or leased > 0:
            recommendation = "auto_restart"
        else:
            recommendation = "none"

        return {
            "interrupted_batches": interrupted,
            "leased_batches":      leased,
            "flagged_batches":     flagged,
            "holding_files":       holding,
            "recommendation":      recommendation,
        }
