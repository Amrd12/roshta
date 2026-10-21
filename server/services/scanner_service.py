"""
ScannerService — background daemon that drives FileSystemAdapter every 5 s
and rebuilds the PlanTree snapshot in QueueService after each scan.

Lifecycle:
    service = ScannerService(queue_service, app_config)
    service.start()   # Launches daemon thread; returns immediately
    service.stop()    # Signals thread to exit; joins with 10 s timeout

The scanner never holds the QueueService RLock while doing disk I/O;
it acquires the lock only for the brief atomic snapshot swap at the end.
"""
from __future__ import annotations

import logging
import threading
import time

from server.config import AppConfig
from server.database import get_connection
from server.adapters.filesystem_adapter import FileSystemAdapter

logger = logging.getLogger(__name__)


class ScannerService:
    """
    Background directory poller.

    Runs in its own daemon thread so the Flask/Waitress threads are never
    blocked by disk I/O or network share latency.
    """

    def __init__(self, queue_service, app_config: AppConfig):
        """
        Args:
            queue_service: The application's QueueService singleton.
                           ScannerService calls queue_service.rebuild_snapshot()
                           after each scan pass.
            app_config:    Application configuration (paths, intervals, regex).
        """
        self._queue_service = queue_service
        self._config = app_config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._adapter: FileSystemAdapter | None = None
        if self._config.source_folder:
            self._adapter = FileSystemAdapter(
                source_root=self._config.source_folder,
                allowed_extensions=self._config.allowed_extensions,
                doctor_regex=self._config.doctor_regex,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scanner daemon thread. Safe to call multiple times (idempotent)."""
        if self._thread and self._thread.is_alive():
            logger.debug("ScannerService already running.")
            return

        self._stop_event.clear()
        if self._adapter is None and self._config.source_folder:
            self._adapter = FileSystemAdapter(
                source_root=self._config.source_folder,
                allowed_extensions=self._config.allowed_extensions,
                doctor_regex=self._config.doctor_regex,
            )
        self._thread = threading.Thread(
            target=self._run,
            name="ScannerService",
            daemon=True,   # Exits automatically when main process exits
        )
        self._thread.start()
        logger.info(
            "ScannerService started (interval=%ss, source=%s)",
            self._config.scan_interval_seconds,
            self._config.source_folder or "<not configured>",
        )

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the scanner to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            logger.info("ScannerService stopped.")

    def trigger_rescan(self) -> None:
        """
        Wake the scanner immediately for a forced rescan.
        Used by POST /control {action: "rescan"}.
        """
        self._stop_event.set()   # Wake up the sleep
        self._stop_event.clear() # Re-arm for next iteration
        logger.info("ScannerService: forced rescan triggered.")

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main scanner loop. Runs on the daemon thread."""
        logger.info("ScannerService: loop started.")
        while not self._stop_event.is_set():
            try:
                self._scan_pass()
            except Exception:
                logger.exception("ScannerService: unexpected error in scan pass.")

            # Interruptible sleep: wakes early if stop_event is set
            self._stop_event.wait(timeout=self._config.scan_interval_seconds)

        logger.info("ScannerService: loop exited.")

    def _scan_pass(self) -> None:
        """
        Execute one scan pass:
          1. Open a per-thread SQLite connection.
          2. Run FileSystemAdapter.scan() (disk I/O, no lock held).
          3. If any files changed, ask QueueService to rebuild its snapshot.
          4. Close the connection.
        """
        if not self._config.source_folder:
            return  # Source folder not configured yet

        conn = get_connection(self._config.database_path)
        try:
            inserted, deleted = self._adapter.scan(conn)
            if inserted or deleted:
                logger.debug(
                    "ScannerService: +%d inserted, -%d deleted",
                    len(inserted), len(deleted)
                )
                # Ask QueueService to rebuild the plan tree snapshot.
                # rebuild_snapshot() is fast (in-memory); it acquires the
                # RLock internally for the atomic swap only.
                self._queue_service.rebuild_snapshot(conn)
        finally:
            conn.close()
