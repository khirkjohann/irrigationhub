"""
routes/irrigation.py — Irrigation queue, schedule, and log export routes.
"""
import csv
import io

from flask import Blueprint, Response, jsonify, request

from core.config import VALID_ZONES
from core.db import get_db
from hardware.irrigation import (
    _irr_queue,
    _irr_queue_lock,
    _zone_schedules,
    get_irr_snapshot,
    queue_add,
)

bp = Blueprint("irrigation", __name__)


@bp.route("/api/irrigation/queue", methods=["GET"])
def api_irr_queue_get():
    return jsonify(get_irr_snapshot())


@bp.route("/api/irrigation/queue", methods=["POST"])
def api_irr_queue_add():
    payload = request.get_json(silent=True) or {}
    try:
        zone_id       = int(payload["zone_id"])
        volume_liters = float(payload["volume_liters"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "zone_id and volume_liters are required"}), 400
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    if volume_liters <= 0:
        return jsonify({"error": "volume_liters must be positive"}), 400
    item = queue_add(zone_id, volume_liters)
    return jsonify({"success": True, "item": item}), 201


@bp.route("/api/irrigation/queue/<int:item_id>", methods=["DELETE"])
def api_irr_queue_delete(item_id):
    with _irr_queue_lock:
        before = len(_irr_queue)
        _irr_queue[:] = [i for i in _irr_queue if i["id"] != item_id]
        removed = len(_irr_queue) < before
    if not removed:
        return jsonify({"error": "Item not found or already running"}), 404
    return jsonify({"success": True})


@bp.route("/api/zone/<int:zone_id>/schedule", methods=["GET"])
def api_zone_schedule_get(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    return jsonify({"schedule": _zone_schedules.get(zone_id)})


@bp.route("/api/zone/<int:zone_id>/schedule", methods=["POST"])
def api_zone_schedule_save(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    payload = request.get_json(silent=True) or {}
    days    = payload.get("days")
    slots   = payload.get("slots")
    if not isinstance(days, list) or not isinstance(slots, list):
        return jsonify({"error": "days and slots are required"}), 400
    _zone_schedules[zone_id] = {"days": days, "slots": slots}
    return jsonify({"success": True})


@bp.route("/api/zone/<int:zone_id>/schedule", methods=["DELETE"])
def api_zone_schedule_delete(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    _zone_schedules.pop(zone_id, None)
    return jsonify({"success": True})


@bp.route("/export/irrigation-log.csv")
def export_irrigation_log_csv():
    conn = get_db()
    rows = conn.execute("""
        SELECT zone_id, source,
               added_at, started_at, completed_at,
               volume_liters, est_duration_minutes, actual_duration_minutes, flow_rate_lpm,
               initial_moisture, post_moisture,
               temperature, humidity,
               crop_target_name, target_moisture,
               day_of_week, hour_of_day
        FROM irrigation_log ORDER BY id DESC
    """).fetchall()
    conn.close()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["zone_id", "source", "added_at", "started_at", "completed_at",
                "volume_liters", "est_duration_minutes", "actual_duration_minutes", "flow_rate_lpm",
                "initial_moisture_pct", "post_moisture_pct",
                "temperature_c", "humidity_pct",
                "crop_target", "target_moisture_pct",
                "day_of_week", "hour_of_day"])
    w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=irrigation_log.csv"})
