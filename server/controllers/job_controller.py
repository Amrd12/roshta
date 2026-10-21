"""
JobController — POST /lease, POST /job-heartbeat, POST /job-result endpoints.

POST /lease
    Atomically promotes the next ready batch to 'leased'.
    Returns 200 with job contract, or 204 if nothing is ready.

POST /job-heartbeat
    Extends the lease expiry for an active batch.
    Body: { "batch_id": "...", "lease_id": "..." }

POST /job-result
    Accepts a job completion report from the Photoshop plugin.
    Enqueues finalization and returns 202 Accepted immediately.
    Body: {
        "batch_id": "...",
        "lease_id": "...",
        "success": true/false,
        "print_temp_path": "...",     # optional
        "tracker_temp_path": "...",   # optional
        "duration_ms": 12345,         # optional
        "error_message": "...",       # optional (on failure)
        "expected_width_px": 5906,    # optional (skip verify if absent)
        "expected_height_px": 4016,   # optional
        "expected_dpi": 300,          # optional, default 300
    }
"""
from __future__ import annotations

from flask import Blueprint, request, jsonify, current_app

from server.services.finalizer_worker import FinalizationTask
from server.views.error_view import make_error_response

job_bp = Blueprint("job", __name__)


def _qs():
    return current_app.config["QUEUE_SERVICE"]


def _finalizer():
    return current_app.config.get("FINALIZER_WORKER")


# ---------------------------------------------------------------------------
# POST /lease
# ---------------------------------------------------------------------------

@job_bp.route("/lease", methods=["POST"])
def post_lease():
    """
    Lease the next ready batch.

    Optional JSON body:
        { "force": true, "clinic": "ClinicA", "paper_weight": "90gsm" }

    Responses:
        200 — job contract
        204 — nothing ready to lease
        409 — server paused/stopped
        507 — disk space low
    """
    payload = request.get_json(silent=True) or {}
    force        = bool(payload.get("force", False))
    clinic       = payload.get("clinic") or None
    paper_weight = payload.get("paper_weight") or None

    qs = _qs()
    try:
        contract = qs.lease_batch(force=force, clinic=clinic, paper_weight=paper_weight)
    except RuntimeError as exc:
        code = str(exc).split(":")[0]
        if code == "DISK_SPACE_LOW":
            return make_error_response(code, str(exc), 507)
        if code == "SERVER_NOT_RUNNING":
            return make_error_response(code, str(exc), 409)
        return make_error_response("LEASE_ERROR", str(exc), 500)

    if contract is None:
        return "", 204

    return jsonify(contract), 200


# ---------------------------------------------------------------------------
# POST /job-heartbeat
# ---------------------------------------------------------------------------

@job_bp.route("/job-heartbeat", methods=["POST"])
def post_job_heartbeat():
    """
    Extend the active lease expiry (heartbeat).

    Body: { "batch_id": "...", "lease_id": "..." }

    Responses:
        200 — extended successfully
        404 — batch not found or wrong lease_id / status
        422 — missing required fields
    """
    payload = request.get_json(silent=True)
    if not payload:
        return make_error_response("INVALID_PAYLOAD", "JSON body required.", 422)

    batch_id = payload.get("batch_id")
    lease_id  = payload.get("lease_id")
    if not batch_id or not lease_id:
        return make_error_response(
            "INVALID_PAYLOAD",
            "Required fields: batch_id, lease_id.",
            422
        )

    qs = _qs()
    ok = qs.extend_lease(batch_id=batch_id, lease_id=lease_id)

    if not ok:
        return make_error_response(
            "LEASE_NOT_FOUND",
            f"Batch '{batch_id}' not found, lease_id mismatch, or batch not leased.",
            404
        )

    return jsonify({"status": "extended", "batch_id": batch_id}), 200


# ---------------------------------------------------------------------------
# POST /job-result
# ---------------------------------------------------------------------------

@job_bp.route("/job-result", methods=["POST"])
def post_job_result():
    """
    Receive the Photoshop plugin's completion report.

    Returns 202 Accepted immediately; actual finalization happens in the
    JobFinalizerWorker background thread.

    Body: {
        "batch_id": "...",
        "lease_id": "...",
        "success": true/false,
        ... (see module docstring)
    }

    Responses:
        202 — enqueued for finalization
        422 — missing required fields
    """
    payload = request.get_json(silent=True)
    if not payload:
        return make_error_response("INVALID_PAYLOAD", "JSON body required.", 422)

    batch_id = payload.get("batch_id")
    lease_id  = payload.get("lease_id")
    success   = payload.get("success")

    if not batch_id or not lease_id or success is None:
        return make_error_response(
            "INVALID_PAYLOAD",
            "Required fields: batch_id, lease_id, success.",
            422
        )

    task = FinalizationTask(
        batch_id=batch_id,
        lease_id=lease_id,
        success=bool(success),
        print_temp_path=payload.get("print_temp_path"),
        tracker_temp_path=payload.get("tracker_temp_path"),
        duration_ms=payload.get("duration_ms"),
        error_message=payload.get("error_message"),
        expected_width_px=int(payload.get("expected_width_px", 0)),
        expected_height_px=int(payload.get("expected_height_px", 0)),
        expected_dpi=int(payload.get("expected_dpi", 300)),
    )

    finalizer = _finalizer()
    if finalizer is None:
        return make_error_response(
            "FINALIZER_UNAVAILABLE",
            "JobFinalizerWorker is not running.",
            503
        )

    finalizer.enqueue(task)

    return jsonify({
        "status": "accepted",
        "batch_id": batch_id,
        "message": "Finalization enqueued.",
    }), 202
