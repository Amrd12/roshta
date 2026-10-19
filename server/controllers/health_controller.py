import time
import psutil
from flask import Blueprint
from server.database import get_connection
from server.views.health_view import make_health_response

health_bp = Blueprint("health", __name__)
SERVER_START_TIME = time.time()
SERVER_VERSION = "2.0.0"

def is_photoshop_running() -> bool:
    """Checks if Photoshop.exe is currently present in running processes."""
    try:
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name")
            if name and name.lower() == "photoshop.exe":
                return True
    except Exception:
        pass
    return False

@health_bp.route("/health", methods=["GET"])
def health_check():
    """GET /health - Verifies service status, uptime, PS status, and uncommitted crash recovery counts."""
    uptime = time.time() - SERVER_START_TIME
    ps_running = is_photoshop_running()
    
    interrupted_count = 0
    leased_count = 0
    holding_count = 0
    db_ok = True

    try:
        conn = get_connection()
        try:
            # Query recovery summary
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) as interrupted, "
                "SUM(CASE WHEN status = 'leased' THEN 1 ELSE 0 END) as leased "
                "FROM batches"
            ).fetchone()
            if row:
                interrupted_count = row["interrupted"] or 0
                leased_count = row["leased"] or 0

            # Query staging count
            h_row = conn.execute("SELECT COUNT(*) as count FROM holding_files").fetchone()
            if h_row:
                holding_count = h_row["count"] or 0
        finally:
            conn.close()
    except Exception as e:
        import logging
        logging.getLogger("health").warning(f"Health DB query failed: {e}")
        db_ok = False

    return make_health_response(
        version=SERVER_VERSION,
        uptime_seconds=uptime,
        photoshop_running=ps_running,
        db_connected=db_ok,
        interrupted_batches=interrupted_count,
        leased_batches=leased_count,
        holding_files_count=holding_count
    )
