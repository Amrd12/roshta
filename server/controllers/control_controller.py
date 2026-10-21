"""
ControlController — POST /control endpoint.

Handles all operator queue-control commands:

    play          Resume processing after idle/paused/stopped.
    pause         Graceful pause — current batch finishes, no new leases issued.
    stop          Hard stop — no new leases; queue enters 'stopped'.
    force_batch   Immediately lease whatever files are available even if < 4.
    retry         Re-queue a failed/interrupted/flagged batch by batch_id.
    mark_reviewed Acknowledge a flagged batch (clears it to 'done').
    rescan        Trigger an immediate re-scan of the source folder.

Request body:
    { "command": "<cmd>", "batch_id": "<id>" }   -- batch_id required for retry/mark_reviewed
    { "command": "force_batch", "clinic": "..", "paper_weight": ".." }

Responses:
    200  { "ok": true, "mode": "<new_mode>", "message": "..." }
    400  Bad command or illegal state transition
    404  batch_id not found (retry / mark_reviewed)
    422  Missing required fields
"""
from __future__ import annotations

from flask import Blueprint, request, jsonify, current_app

from server.views.error_view import make_error_response

control_bp = Blueprint("control", __name__)


def _qs():
    return current_app.config["QUEUE_SERVICE"]


def _scanner():
    return current_app.config.get("SCANNER_SERVICE")


# ---------------------------------------------------------------------------
# POST /control
# ---------------------------------------------------------------------------

@control_bp.route("/control", methods=["POST"])
def post_control():
    """
    Execute an operator queue-control command.

    Body: { "command": "<cmd>", ...optional fields... }

    Commands:
        play          — start / resume the queue
        pause         — graceful pause (waits for current batch)
        stop          — immediate stop (no new leases)
        force_batch   — lease with < 4 files; optional: clinic, paper_weight
        retry         — re-queue a batch; requires: batch_id
        mark_reviewed — clear a flagged batch; requires: batch_id
        rescan        — trigger an immediate folder re-scan
    """
    payload = request.get_json(silent=True)
    if not payload:
        return make_error_response("INVALID_PAYLOAD", "JSON body required.", 422)

    command = payload.get("command", "").strip().lower()
    if not command:
        return make_error_response("INVALID_PAYLOAD", "Field 'command' is required.", 422)

    qs = _qs()

    # ── play / pause / stop ─────────────────────────────────────────────────
    if command in ("play", "pause", "stop"):
        try:
            new_mode = qs.set_mode(command)
        except ValueError as exc:
            return make_error_response("INVALID_TRANSITION", str(exc), 400)
        return jsonify({
            "ok": True,
            "mode": new_mode,
            "message": f"Queue {command} accepted. Mode is now '{new_mode}'.",
        }), 200

    # ── force_batch ─────────────────────────────────────────────────────────
    if command == "force_batch":
        clinic       = payload.get("clinic") or None
        paper_weight = payload.get("paper_weight") or None
        try:
            contract = qs.lease_batch(force=True, clinic=clinic, paper_weight=paper_weight)
        except RuntimeError as exc:
            code = str(exc).split(":")[0]
            status = 507 if code == "DISK_SPACE_LOW" else 409
            return make_error_response(code, str(exc), status)

        if contract is None:
            return make_error_response(
                "QUEUE_EMPTY",
                "No files available to force-batch.",
                404,
            )
        return jsonify({
            "ok": True,
            "mode": qs.mode,
            "message": f"Force-batch leased: {contract['batch_number']}",
            "contract": contract,
        }), 200

    # ── retry ────────────────────────────────────────────────────────────────
    if command == "retry":
        batch_id = payload.get("batch_id", "").strip()
        if not batch_id:
            return make_error_response("INVALID_PAYLOAD", "Field 'batch_id' is required.", 422)
        ok = qs.retry_batch(batch_id)
        if not ok:
            return make_error_response(
                "BATCH_NOT_RETRYABLE",
                f"Batch '{batch_id}' not found or not in a retryable state "
                "(must be failed, interrupted, or flagged).",
                404,
            )
        return jsonify({
            "ok": True,
            "mode": qs.mode,
            "message": f"Batch '{batch_id}' re-queued for retry.",
        }), 200

    # ── mark_reviewed ────────────────────────────────────────────────────────
    if command == "mark_reviewed":
        batch_id = payload.get("batch_id", "").strip()
        if not batch_id:
            return make_error_response("INVALID_PAYLOAD", "Field 'batch_id' is required.", 422)
        ok = qs.mark_reviewed(batch_id)
        if not ok:
            return make_error_response(
                "BATCH_NOT_FLAGGED",
                f"Batch '{batch_id}' not found or not in 'flagged' state.",
                404,
            )
        return jsonify({
            "ok": True,
            "mode": qs.mode,
            "message": f"Batch '{batch_id}' marked as reviewed.",
        }), 200

    # ── rescan ────────────────────────────────────────────────────────────────
    if command == "rescan":
        scanner = _scanner()
        if scanner and hasattr(scanner, "trigger_scan"):
            scanner.trigger_scan()
            msg = "Rescan triggered."
        else:
            msg = "No scanner configured; rescan skipped."
        return jsonify({"ok": True, "mode": qs.mode, "message": msg}), 200

    # ── unknown ───────────────────────────────────────────────────────────────
    return make_error_response(
        "UNKNOWN_COMMAND",
        f"Unknown command '{command}'. Valid: play, pause, stop, force_batch, "
        "retry, mark_reviewed, rescan.",
        400,
    )
