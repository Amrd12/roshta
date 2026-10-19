from flask import jsonify, Response

def make_health_response(
    version: str,
    uptime_seconds: float,
    photoshop_running: bool,
    db_connected: bool,
    interrupted_batches: int = 0,
    leased_batches: int = 0,
    holding_files_count: int = 0
) -> Response:
    """Formats the /health endpoint response with system vitality and resume summary."""
    payload = {
        "status": "ok" if db_connected else "degraded",
        "version": version,
        "uptime_seconds": round(uptime_seconds, 1),
        "photoshop_running": photoshop_running,
        "database": "connected" if db_connected else "error",
        "resume_summary": {
            "interrupted_batches": interrupted_batches,
            "leased_batches": leased_batches,
            "holding_files": holding_files_count
        }
    }
    response = jsonify(payload)
    response.status_code = 200 if db_connected else 503
    return response
