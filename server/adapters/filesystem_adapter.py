"""
FileSystemAdapter — non-blocking file scanner and safe cross-drive mover.

Design decisions:
  - Two-tier stability: (size, mtime) must be stable across two consecutive
    scans AND the file must be openable in 'rb' mode (rejects files another
    process is still writing over SMB).
  - Scan interval: 5 s (configurable). Avoids hammering network shares.
  - safe_move: atomic rename on same volume; stream-to-.part then rename on
    cross-device (catches only errno.EXDEV, not locked-file errors which
    propagate to the caller for retry).
  - Folder exclusions: checked at insert time via DB query; excluded files
    are never inserted into holding_files.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import sqlite3
from typing import Dict, Optional, Tuple

from server.models.prescription import (
    PrescriptionFile,
    extract_doctor_name,
    parse_folder_path,
    file_id,
)

logger = logging.getLogger(__name__)

# Snapshot entry: maps file_path → (size, mtime)
_Snapshot = Dict[str, Tuple[int, float]]


class FileSystemAdapter:
    """
    Stateful filesystem scanner for the source input folder.

    Maintains an internal snapshot of (size, mtime) across polling passes
    to implement the two-tier stability check without sleeping inside the loop.

    Usage:
        adapter = FileSystemAdapter(source_root, allowed_extensions, doctor_regex)
        # In background thread loop:
        new_stable, deleted = adapter.scan(db_conn)
    """

    def __init__(
        self,
        source_root: str,
        allowed_extensions: tuple,
        doctor_regex,
    ):
        self.source_root = source_root
        self.allowed_extensions = tuple(ext.lower() for ext in allowed_extensions)
        self.doctor_regex = doctor_regex

        # Internal state between polling passes
        self._prev_snapshot: _Snapshot = {}  # {path: (size, mtime)} from last pass
        self._candidate_snapshot: _Snapshot = {}  # path seen stable once already

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
        """
        Perform one scan pass. Must be called from the scanner daemon thread.

        Steps:
          1. Walk the source folder, collect all matching files.
          2. Apply two-tier stability check.
          3. Sync stable files into holding_files (insert new, skip locked).
          4. Prune ghost files (deleted from disk but still 'waiting' in DB).

        Returns:
            (inserted_paths, deleted_paths) — paths that changed this pass.
        """
        if not self.source_root or not os.path.isdir(self.source_root):
            return [], []

        current_on_disk = self._walk_source()
        stable_paths = self._apply_stability_check(current_on_disk)

        inserted = self._sync_to_db(stable_paths, conn)
        deleted  = self._prune_ghosts(current_on_disk, conn)

        self._prev_snapshot = current_on_disk
        return inserted, deleted

    # ------------------------------------------------------------------
    # Scanning internals
    # ------------------------------------------------------------------

    def _walk_source(self) -> _Snapshot:
        """Walk the source root and return {path: (size, mtime)} for all matching files."""
        result: _Snapshot = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.source_root):
                # Skip hidden folders
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in self.allowed_extensions:
                        continue
                    full_path = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full_path)
                        result[full_path] = (st.st_size, st.st_mtime)
                    except OSError:
                        pass  # File disappeared between walk and stat
        except Exception as exc:
            logger.warning("Error walking source folder %s: %s", self.source_root, exc)
        return result

    def _apply_stability_check(self, current: _Snapshot) -> list[str]:
        """
        Two-tier stability check:
          Tier 1 — (size, mtime) unchanged from the previous pass.
          Tier 2 — file can be opened in 'rb' mode (rejects SMB-locked files).

        Returns list of file paths that passed both tiers this pass.
        A file must be stable in TWO consecutive passes before it is
        declared ready for insertion.
        """
        stable: list[str] = []

        for path, (size, mtime) in current.items():
            prev = self._prev_snapshot.get(path)

            if prev is None:
                # Seen for the first time — record as candidate
                self._candidate_snapshot[path] = (size, mtime)
                continue

            prev_size, prev_mtime = prev
            if size != prev_size or mtime != prev_mtime:
                # File changed — reset candidate (possibly new version)
                self._candidate_snapshot[path] = (size, mtime)
                continue

            # Size and mtime are stable — now try exclusive open (Tier 2)
            if not self._can_open_exclusive(path):
                continue  # Still being written

            stable.append(path)

        # Clean up candidates that disappeared
        disappeared = set(self._candidate_snapshot) - set(current)
        for p in disappeared:
            del self._candidate_snapshot[p]

        return stable

    @staticmethod
    def _can_open_exclusive(path: str) -> bool:
        """
        Attempt to open the file for reading. Returns False if another process
        (e.g. SMB copy daemon) holds an exclusive write lock.
        """
        try:
            with open(path, "rb") as _:
                pass
            return True
        except PermissionError:
            return False
        except OSError:
            return False

    # ------------------------------------------------------------------
    # DB sync
    # ------------------------------------------------------------------

    def _sync_to_db(self, stable_paths: list[str], conn: sqlite3.Connection) -> list[str]:
        """
        Insert newly stable files into holding_files.
        Skips files that:
          - Are already in holding_files (any status).
          - Have a folder that matches a folder_exclusions entry.
          - Cannot be parsed for clinic / doctor name.

        If a file already exists in holding_files but its size or mtime changed
        (e.g. updated version), reset first_seen_at and is_skipped=0.
        """
        inserted: list[str] = []

        for path in stable_paths:
            try:
                inserted_path = self._upsert_file(path, conn)
                if inserted_path:
                    inserted.append(inserted_path)
            except Exception as exc:
                logger.warning("Failed to upsert %s: %s", path, exc)

        return inserted

    def _upsert_file(self, path: str, conn: sqlite3.Connection) -> Optional[str]:
        """
        Insert a new stable file or update metadata if the file changed.
        Returns the path if a new row was inserted, else None.
        """
        fname = os.path.basename(path)
        doctor_name = extract_doctor_name(fname, self.doctor_regex)
        if not doctor_name:
            logger.debug("Skipping unrecognised filename: %s", fname)
            return None

        clinic, paper_weight = parse_folder_path(path, self.source_root)

        # Check folder exclusion
        excluded = conn.execute(
            "SELECT 1 FROM folder_exclusions WHERE ? LIKE folder_path || '%' LIMIT 1;",
            (path.replace("\\", "/"),)
        ).fetchone()
        if excluded:
            logger.debug("Skipping excluded folder: %s", path)
            return None

        st = os.stat(path)
        fid = file_id(path)

        existing = conn.execute(
            "SELECT id, file_size, mtime, status FROM holding_files WHERE id = ?;",
            (fid,)
        ).fetchone()

        if existing is None:
            # New file — insert
            conn.execute(
                """
                INSERT INTO holding_files
                    (id, file_path, clinic, paper_weight, doctor_name,
                     file_size, mtime, is_skipped, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'waiting');
                """,
                (fid, path, clinic, paper_weight, doctor_name,
                 st.st_size, st.st_mtime)
            )
            conn.commit()
            logger.info("Inserted stable file: %s [%s / %s / %s]",
                        fname, clinic, paper_weight, doctor_name)
            return path

        # Existing file — check if metadata changed (new version of the file)
        if existing["status"] == "waiting" and (
            existing["file_size"] != st.st_size or existing["mtime"] != st.st_mtime
        ):
            conn.execute(
                """
                UPDATE holding_files
                SET file_size       = ?,
                    mtime           = ?,
                    first_seen_at   = CURRENT_TIMESTAMP,
                    is_skipped      = 0,
                    doctor_name     = ?,
                    clinic          = ?,
                    paper_weight    = ?
                WHERE id = ?;
                """,
                (st.st_size, st.st_mtime, doctor_name, clinic, paper_weight, fid)
            )
            conn.commit()
            logger.info("Updated changed file: %s", fname)

        return None  # Not a new insertion

    def _prune_ghosts(self, current_on_disk: _Snapshot, conn: sqlite3.Connection) -> list[str]:
        """
        Remove holding_files rows for files that no longer exist on disk,
        but ONLY if their status is 'waiting'. Locked files (being processed)
        are never auto-deleted.
        """
        deleted: list[str] = []
        try:
            waiting_rows = conn.execute(
                "SELECT id, file_path FROM holding_files WHERE status = 'waiting';"
            ).fetchall()

            for row in waiting_rows:
                if row["file_path"] not in current_on_disk:
                    conn.execute(
                        "DELETE FROM holding_files WHERE id = ?;", (row["id"],)
                    )
                    deleted.append(row["file_path"])
                    logger.info("Pruned ghost file: %s", row["file_path"])

            if deleted:
                conn.commit()
        except Exception as exc:
            logger.warning("Error pruning ghosts: %s", exc)
        return deleted


# ---------------------------------------------------------------------------
# safe_move
# ---------------------------------------------------------------------------

def safe_move(src: str, dst: str) -> None:
    """
    Atomically move src to dst across any drive boundary.

    Same volume:
        os.rename() — instant and atomic.

    Cross-volume (errno.EXDEV):
        1. Stream src → dst.part  (buffered copy, explicit flush + fsync)
        2. Rename dst.part → dst  (atomic on same volume)
        3. Delete src

    Only errno.EXDEV is caught for cross-volume detection. Any other
    OSError (e.g. src locked by Photoshop) propagates to the caller
    so it can retry later.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise  # Not a cross-device error — propagate

    # Cross-device: stream copy then atomic rename
    part_path = dst + ".part"
    try:
        with open(src, "rb") as fsrc, open(part_path, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
            fdst.flush()
            os.fsync(fdst.fileno())
        os.rename(part_path, dst)
        os.remove(src)
    except Exception:
        # Clean up partial file if anything went wrong
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        raise
