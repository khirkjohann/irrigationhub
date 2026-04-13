"""
routes/data.py — API routes for dashboard, trends, history, prediction, status, CSV export.
"""
import csv
import io
import os
import subprocess
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from core.config import (
    ADS_SAMPLES,
    ADS_SAMPLE_DELAY,
    ML_MODEL_PATH,
    SENSOR_POLL_SECONDS,
    VALID_ZONES,
)
from core.db import get_db
from hardware.gpio_control import get_gpio_status, get_fan_status
from hardware.irrigation import get_latest_env
from hardware.sensors import get_sensor_snapshot
from core.utils import clamp

bp = Blueprint("data", __name__)

# Module-level reference to APP_START_TIME — set by app.py after import
APP_START_TIME: datetime = datetime.now()


# ── Dashboard helpers ─────────────────────────────────────────────────────────

def _baseline_dict(row) -> dict:
    return {"id": row["id"], "name": row["name"],
            "dry_voltage": row["dry_voltage"], "wet_voltage": row["wet_voltage"],
            "created_at": row["created_at"]}


def _crop_target_dict(row) -> dict:
    return {"id": row["id"], "name": row["name"],
            "target_voltage": row["target_voltage"], "created_at": row["created_at"]}


def _zone_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _hotspot_status() -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", "hostapd"],
                           capture_output=True, text=True, timeout=2, check=False)
        return "UP" if r.stdout.strip() == "active" else "DOWN"
    except Exception:
        return "UNKNOWN"


def dashboard_payload() -> dict:
    conn    = get_db()
    latest  = conn.execute("SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT 1").fetchone()
    valves  = conn.execute("SELECT * FROM valve_status ORDER BY valve_id").fetchall()
    zones   = conn.execute("""
        SELECT zp.*, sb.name AS soil_baseline_name,
               sb.dry_voltage  AS soil_baseline_dry_voltage,
               sb.wet_voltage  AS soil_baseline_wet_voltage,
               ct.name         AS crop_target_name,
               ct.target_voltage AS crop_target_voltage
        FROM zone_profile zp
        LEFT JOIN soil_baseline sb ON sb.id = zp.soil_baseline_id
        LEFT JOIN crop_target   ct ON ct.id = zp.crop_target_id
        ORDER BY zp.zone_id
    """).fetchall()
    baselines    = conn.execute("SELECT * FROM soil_baseline ORDER BY name").fetchall()
    crop_targets = conn.execute("SELECT * FROM crop_target ORDER BY name").fetchall()
    conn.close()

    valve_map = {r["valve_id"]: r["status"] for r in valves}
    zone_map  = {z["zone_id"]: z for z in zones}

    zone_payload = []
    for zid in sorted(VALID_ZONES):
        _z = zone_map.get(zid)
        z  = dict(_z) if _z else {}
        zone_payload.append({
            "zone_id":             zid,
            "moisture":            latest[f"soil_moisture_{zid}"] if latest else None,
            "target_moisture":     z.get("target_moisture"),
            "disabled":            bool(z.get("disabled", 0)),
            "soil_baseline_id":    z.get("soil_baseline_id"),
            "crop_target_id":      z.get("crop_target_id"),
            "soil_baseline_name":  z.get("soil_baseline_name"),
            "crop_target_name":    z.get("crop_target_name"),
            "crop_target_voltage": z.get("crop_target_voltage"),
            "flow_rate_lpm":       z.get("flow_rate_lpm", 3.0),
            "threshold_gap":       z.get("threshold_gap", 5.0),
            "valve_status":        valve_map.get(zid, "OFF"),
            "irr_mode":            z.get("irr_mode", "ml"),
        })

    valve_payload = []
    for v in valves:
        valve_payload.append({
            "valve_id":      v["valve_id"],
            "status":        v["status"],
            "water_flowing": v["status"] == "ON",
        })

    uptime    = datetime.now() - APP_START_TIME
    h, rem    = divmod(int(uptime.total_seconds()), 3600)
    gpio_snap = get_gpio_status()
    fan_snap  = get_fan_status()

    return {
        "environment": {
            "temperature": latest["temperature"] if latest else None,
            "humidity":    latest["humidity"]    if latest else None,
        },
        "system_health": {
            "hotspot_status": _hotspot_status(),
            "db_uptime":      f"{h}h {rem // 60}m",
            "sensor_status":  get_sensor_snapshot(),
            "relay_status":   gpio_snap,
            "fan_status":     fan_snap,
        },
        "zones":          zone_payload,
        "valves":         valve_payload,
        "soil_baselines": [_baseline_dict(r) for r in baselines],
        "crop_targets":   [_crop_target_dict(r) for r in crop_targets],
        "runtime": {
            "sensor_poll_seconds": SENSOR_POLL_SECONDS,
        },
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/api/dashboard")
def api_dashboard():
    return jsonify(dashboard_payload())


@bp.route("/api/trends")
def api_trends():
    hours = request.args.get("hours", default=24, type=int)
    if hours not in {24, 48}:
        hours = 24
    conn = get_db()
    rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4
           FROM sensor_data
           WHERE timestamp >= datetime('now', ?)
           ORDER BY timestamp ASC""",
        (f"-{hours} hours",),
    ).fetchall()
    conn.close()
    return jsonify({"hours": hours, "data": [dict(r) for r in rows]})


@bp.route("/api/zone/<int:zone_id>/history")
def api_zone_history(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    limit = clamp(request.args.get("limit", default=20, type=int) or 20, 1, 100)
    conn  = get_db()
    rows  = conn.execute(
        f"""SELECT datetime(timestamp,'localtime') AS timestamp,
                   soil_moisture_{zone_id} AS moisture
            FROM sensor_data ORDER BY timestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    events = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  event_type, source, detail
           FROM control_events WHERE zone_id=? ORDER BY timestamp DESC LIMIT ?""",
        (zone_id, limit),
    ).fetchall()
    conn.close()
    return jsonify({
        "zone_id":         zone_id,
        "history":         [dict(r) for r in rows],
        "control_history": [dict(e) for e in events],
    })


@bp.route("/api/zone/<int:zone_id>/predict")
def api_zone_predict(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    conn = get_db()
    zp  = conn.execute("SELECT * FROM zone_profile WHERE zone_id=?", (zone_id,)).fetchone()
    row = conn.execute(
        f"SELECT soil_moisture_{zone_id} AS moisture FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not zp:
        return jsonify({"error": "Zone profile not found"}), 404
    current_moisture = row["moisture"] if row else None
    target_moisture  = zp["target_moisture"]
    flow_rate_lpm    = float(zp["flow_rate_lpm"] or 3.0)
    if current_moisture is None:
        return jsonify({"error": "No sensor data available"}), 400
    if target_moisture is None:
        return jsonify({"error": "No target moisture configured for this zone"}), 400
    deficit_pct = max(0.0, float(target_moisture) - float(current_moisture))

    recommended_liters = None
    model_used = "formula"
    if os.path.exists(ML_MODEL_PATH):
        try:
            import joblib
            import numpy as np
            model = joblib.load(ML_MODEL_PATH)
            temp_val, hum_val = get_latest_env()
            features = np.array([[temp_val or 25.0, hum_val or 60.0, deficit_pct]])
            predicted = float(model.predict(features)[0])
            recommended_liters = round(max(0.0, predicted), 2)
            model_used = "ml"
        except Exception as exc:
            print(f"[PREDICT] ML model error: {exc}")
    if recommended_liters is None:
        recommended_liters = round(deficit_pct * 0.2, 2)

    estimated_minutes = round(recommended_liters / flow_rate_lpm, 2) if flow_rate_lpm > 0 else 0
    return jsonify({
        "zone_id":            zone_id,
        "current_moisture":   round(float(current_moisture), 1),
        "target_moisture":    round(float(target_moisture), 1),
        "deficit_pct":        round(deficit_pct, 1),
        "recommended_liters": recommended_liters,
        "estimated_minutes":  estimated_minutes,
        "model_used":         model_used,
    })


@bp.route("/api/system/status")
def api_system_status():
    return jsonify({
        "sensor_mode":   "hardware-only",
        "sensor_status": get_sensor_snapshot(),
        "relay_status":  get_gpio_status(),
        "fan_status":    get_fan_status(),
    })


@bp.route("/export/csv")
def export_csv():
    conn = get_db()
    rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  temperature, humidity,
                  soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4
           FROM sensor_data ORDER BY timestamp DESC"""
    ).fetchall()
    conn.close()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["timestamp", "temperature", "humidity",
                "soil_moisture_1", "soil_moisture_2", "soil_moisture_3", "soil_moisture_4"])
    w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=irrigation_records.csv"})
