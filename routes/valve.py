"""
routes/valve.py — Valve set (JSON API), used by diagnostics routes.
"""
from flask import Blueprint, jsonify, request

from core.config import VALID_ZONES
from hardware.irrigation import set_valve

bp = Blueprint("valve", __name__)


@bp.route("/api/valve/<int:valve_id>", methods=["POST"])
def api_set_valve(valve_id):
    if valve_id not in VALID_ZONES:
        return jsonify({"error": "Invalid valve_id"}), 400
    payload = request.get_json(silent=True) or {}
    state   = payload.get("state")
    if state not in {"ON", "OFF"}:
        return jsonify({"error": "state must be ON or OFF"}), 400
    acm = payload.get("auto_close_minutes")
    if acm is not None:
        try:
            acm = float(acm)
        except (TypeError, ValueError):
            return jsonify({"error": "auto_close_minutes must be a number"}), 400
        if acm < 0:
            return jsonify({"error": "auto_close_minutes cannot be negative"}), 400
    set_valve(valve_id, state, acm if state == "ON" else None, source="manual")
    return jsonify({"success": True, "valve_id": valve_id, "state": state})
