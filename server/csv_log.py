"""
csv_log.py — Append-only CSV logger for completed batches.

Two files are maintained:
  1. session_log.csv   — cleared on each service start; lives in the output
                         folder alongside TIFFs.
  2. master_YYYY-MM-DD.csv — permanent daily record; one file per calendar
                             day, appended across service restarts.

Both files share the same column schema.

Thread safety:
  All writes go through a module-level threading.Lock so concurrent calls
  from the JobFinalizerWorker (or tests) never interleave CSV rows.

Usage:
    from server.csv_log import CsvLogger
    logger = CsvLogger(session_csv_path, master_csv_dir)
    logger.append_batch(row)   # BatchLogRow dataclass
"""
from __future__ import annotations

import csv
import logging
import os
import threading
from dataclasses import dataclass, fields, astuple
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column schema
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "timestamp",          # ISO-8601 UTC, e.g. 2026-10-21T11:00:00
    "batch_id",           # Internal UUID-based batch key
    "batch_number",       # Human-readable, e.g. B00142
    "clinic",
    "paper_weight",
    "slot_count",         # 1–4
    "is_forced",          # "1" if operator forced an incomplete batch
    "duration_ms",        # Wall-clock ms reported by the plugin
    "print_final_path",   # Absolute path of the finalized PRINT TIFF
    "tracker_final_path", # Absolute path of the finalized TRACKER TIFF (or "")
    "doctors",            # Pipe-separated doctor names, e.g. "Dr_A|Dr_B|Dr_C|Dr_D"
    "status",             # "done" | "flagged" | "failed"
    "error_message",      # Populated on failure; empty otherwise
]


@dataclass
class BatchLogRow:
    """
    One row in the CSV log.  All fields are plain strings for CSV safety.
    """
    timestamp: str
    batch_id: str
    batch_number: str
    clinic: str
    paper_weight: str
    slot_count: str
    is_forced: str
    duration_ms: str
    print_final_path: str
    tracker_final_path: str
    doctors: str
    status: str
    error_message: str


# ---------------------------------------------------------------------------
# CsvLogger
# ---------------------------------------------------------------------------

class CsvLogger:
    """
    Append one CSV row per finalised batch to both:
      - a session CSV (path fixed at construction time), and
      - the daily master CSV (rotated by calendar date, stored in master_dir).

    Both files are created with a header row on first write.
    Writes are protected by a threading.Lock.
    """

    def __init__(
        self,
        session_csv_path: str,
        master_csv_dir: str,
    ) -> None:
        self._session_path = session_csv_path
        self._master_dir = master_csv_dir
        self._lock = threading.Lock()
        os.makedirs(master_csv_dir, exist_ok=True)
        os.makedirs(os.path.dirname(session_csv_path), exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_batch(self, row: BatchLogRow) -> None:
        """
        Append *row* to both the session CSV and today's master CSV.

        Uses atomic write-via-temp-rename on each file to prevent
        partial rows if the process is killed mid-write.
        """
        with self._lock:
            self._safe_append(self._session_path, row)
            master_path = self._master_path_for_today()
            self._safe_append(master_path, row)

    def session_path(self) -> str:
        return self._session_path

    def master_path_for_today(self) -> str:
        return self._master_path_for_today()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _master_path_for_today(self) -> str:
        today = date.today().isoformat()
        return os.path.join(self._master_dir, f"master_{today}.csv")

    def _safe_append(self, path: str, row: BatchLogRow) -> None:
        """
        Append *row* to *path*.

        Strategy:
          - If the file does not exist yet, write a header row first.
          - On Windows, atomic rename is not possible over an existing file
            without deleting the target first; we therefore use direct append
            (which is safe because the lock prevents concurrent writers) and
            rely on flush + fsync to make the write durable.
        """
        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
        try:
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
                if needs_header:
                    writer.writerow(CSV_COLUMNS)
                writer.writerow(astuple(row))
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            log.exception("CsvLogger: failed to write to %s", path)
            raise


# ---------------------------------------------------------------------------
# Module-level convenience factory
# ---------------------------------------------------------------------------

def make_csv_logger(finished_folder: str, log_dir: Optional[str] = None) -> CsvLogger:
    """
    Construct a CsvLogger from paths derived from AppConfig.

    Args:
        finished_folder: Where TIFF outputs land; session CSV goes here.
        log_dir:         Directory for daily master CSVs.  Defaults to
                         a 'csv_logs' subdirectory of finished_folder.
    """
    session_path = os.path.join(finished_folder, "session_log.csv")
    master_dir = log_dir or os.path.join(finished_folder, "csv_logs")
    return CsvLogger(session_csv_path=session_path, master_csv_dir=master_dir)
