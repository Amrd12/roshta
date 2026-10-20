"""
StateController — GET /state?rev=N and POST /selection endpoints.

GET /state:
    Returns the current immutable PlanTree snapshot as JSON.
    If the client's rev matches the server's rev AND the boot_id matches,
    returns 304 Not Modified (empty body).

POST /selection:
    Toggles is_skipped on holding_files rows.
    Payload: { "rev": N, "skip": ["id1", ...], "include": ["id2", ...] }
    Responds 409 on stale rev, 422 on bad payload, 423 on locked files.
"""
from __future__ import annotations

from flask import Blueprint, request, jsonify, current_app

from server.models.selection_rules import SelectionChange
from server.views.error_view import make_error_response

state_bp = Blueprint("state", __name__)


def _queue_service():
    """Retrieve the QueueService singleton from app context."""
    return current_app.config["QUEUE_SERVICE"]


# ---------------------------------------------------------------------------
# GET /state
# ---------------------------------------------------------------------------

@state_bp.route("/state", methods=["GET"])
def get_state():
    """
    Return the current plan tree snapshot.

    Query params:
        rev      (int, optional): Last rev the client holds.
        boot_id  (str, optional): Last boot_id the client holds.

    Returns:
        200 with full snapshot JSON.
        304 (empty body) if both rev and boot_id match server values.
    """
    qs = _queue_service()
    snapshot = qs.get_snapshot()

    if snapshot is None:
        # Service not yet initialised (race on startup)
        return jsonify({"boot_id": "", "rev": 0, "mode": "idle", "tree": [],
                        "counters": {}, "warnings": {}}), 200

    # Check if client is up to date
    try:
        client_rev = int(request.args.get("rev", -1))
    except (TypeError, ValueError):
        client_rev = -1

    client_boot_id = request.args.get("boot_id", "")

    if (client_rev == snapshot.rev and
            client_boot_id == snapshot.boot_id):
        return "", 304

    return jsonify(snapshot.to_dict()), 200


# ---------------------------------------------------------------------------
# POST /selection
# ---------------------------------------------------------------------------

@state_bp.route("/selection", methods=["POST"])
def post_selection():
    """
    Toggle operator skip/include flags on holding_files rows.

    Body: { "rev": <int>, "skip": [<id>, ...], "include": [<id>, ...] }

    Responses:
        200  — selection applied; returns new snapshot.
        409  — stale rev (client must refresh /state first).
        422  — malformed payload or invalid IDs.
        423  — one or more files are currently locked (being processed).
    """
    from server.services.queue_service import StaleRevisionError

    payload = request.get_json(silent=True)
    if payload is None:
        return make_error_response(
            "INVALID_PAYLOAD", "Request body must be valid JSON.", 422
        )

    try:
        change = SelectionChange.from_payload(payload)
    except ValueError as exc:
        return make_error_response("INVALID_PAYLOAD", str(exc), 422)

    qs = _queue_service()

    try:
        new_snapshot = qs.apply_selection(change)
    except StaleRevisionError as exc:
        return make_error_response(
            "CONFLICT",
            f"Stale revision. {exc} — refresh /state and retry.",
            409
        )
    except ValueError as exc:
        msg = str(exc)
        if "locked" in msg.lower():
            return make_error_response("LOCKED", msg, 423)
        return make_error_response("INVALID_PAYLOAD", msg, 422)

    return jsonify(new_snapshot.to_dict()), 200
