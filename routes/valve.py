"""
routes/valve.py — Valve toggle (form POST) and valve set (JSON API).
"""
from flask import Blueprint, jsonify, redirect, request, url_for

from core.config import VALID_ZONES
from core.db import get_db
from hardware.irrigation import set_valve

bp = Blueprint("valve", __name__)


@bp.route("/toggle_valve/<int:valve_id>", methods=["POST"])
def toggle_valve(valve_id):
    conn = get_db()
    row  = conn.execute("SELECT status FROM valve_status WHERE valve_id=?", (valve_id,)).fetchone()
    conn.close()
    if not row:
        return redirect(url_for("pages.home_page"))
    new_state  = "ON" if row["status"] == "OFF" else "OFF"
    auto_close = request.form.get("auto_close_minutes", type=int)
    set_valve(valve_id, new_state, auto_close if new_state == "ON" else None)
    return redirect(url_for("pages.home_page"))


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
