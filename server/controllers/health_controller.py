"""
HealthController — GET /health endpoint.

Returns service vitality, Photoshop process status, queue mode, disk
space, and a resume summary with an Auto-Restart / Flag-for-Review
recommendation based on DB batch states from the previous run.
"""
from __future__ import annotations

import shutil
import time

import psutil
from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)
SERVER_START_TIME = time.time()
SERVER_VERSION = "2.0.0"


def _is_photoshop_running() -> bool:
    """Return True if Photoshop.exe is present in the running process list."""
    try:
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name")
            if name and name.lower() == "photoshop.exe":
                return True
    except Exception:
        pass
    return False


def _disk_free_gb(path: str) -> float:
    """Return free disk space in GB for the volume containing *path*."""
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except Exception:
        return -1.0


@health_bp.route("/health", methods=["GET"])
def health_check():
    """
    GET /health — open endpoint (no X-Token required).

    Response fields:
        status              "ok" | "degraded"
        version             Server version string
        uptime_seconds      Seconds since server start
        mode                Current queue mode
        photoshop_running   True if Photoshop.exe detected
        database            "connected" | "error"
        disk_free_gb        Free GB on finished_folder volume (-1 if unknown)
        disk_warning        True when free space < threshold
        resume_summary      { interrupted_batches, leased_batches,
                              flagged_batches, recommendation }
    """
    uptime = time.time() - SERVER_START_TIME
    ps_running = _is_photoshop_running()
    db_ok = True
    resume_summary = {
        "interrupted_batches": 0,
        "leased_batches": 0,
        "flagged_batches": 0,
        "holding_files": 0,
        "recommendation": "none",
    }
    mode = "idle"
    disk_free_gb = -1.0
    disk_warning = False

    try:
        qs = current_app.config.get("QUEUE_SERVICE")
        if qs is not None:
            mode = qs.mode
            resume_summary = qs.get_resume_summary()
            disk_warning = qs._warnings.get("disk_warning", False)

        cfg = current_app.config.get("APP_CONFIG")
        if cfg and cfg.finished_folder:
            disk_free_gb = _disk_free_gb(cfg.finished_folder)
    except Exception:
        import logging
        logging.getLogger("health").exception("Health check query failed.")
        db_ok = False

    payload = {
        "status": "ok" if db_ok else "degraded",
        "version": SERVER_VERSION,
        "uptime_seconds": round(uptime, 1),
        "mode": mode,
        "photoshop_running": ps_running,
        "database": "connected" if db_ok else "error",
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_warning": disk_warning,
        "resume_summary": resume_summary,
    }
    return jsonify(payload), (200 if db_ok else 503)
