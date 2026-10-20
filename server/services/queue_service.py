"""
QueueService — thread-safe state machine and plan tree snapshot manager.

Phase 2 scope:
  - Owns the RLock and the immutable PlanTree snapshot.
  - rebuild_snapshot(): reads holding_files from DB, builds a new PlanTree,
    atomically swaps the reference. Called by ScannerService after each pass.
  - apply_selection(): toggles is_skipped in DB then rebuilds the snapshot.
  - get_snapshot(): returns the current snapshot (no lock needed; atomic ref).

Later phases (3–5) will add:
  - lease_batch(), record_job_result(), play/pause/stop controls, recovery.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Optional

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

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def rev(self) -> int:
        return self._rev

    @property
    def boot_id(self) -> str:
        return self._boot_id
