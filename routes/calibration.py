"""
routes/calibration.py — Soil baseline and crop target calibration routes.
"""
from flask import Blueprint, jsonify, request

from core.config import ADS_SAMPLES, ADS_SAMPLE_DELAY
from core.db import get_db
from hardware.sensors import i2c_open, read_smoothed_channel
from core.utils import clamp_voltage

bp = Blueprint("calibration", __name__)

_CH_MAP = {"A0": 0, "A1": 1, "A2": 2, "A3": 3}


def _baseline_dict(row) -> dict:
    return {"id": row["id"], "name": row["name"],
            "dry_voltage": row["dry_voltage"], "wet_voltage": row["wet_voltage"],
            "created_at": row["created_at"]}


def _crop_target_dict(row) -> dict:
    return {"id": row["id"], "name": row["name"],
            "target_voltage": row["target_voltage"], "created_at": row["created_at"]}


def _capture_live_voltage(channel: int) -> float:
    import adafruit_ads1x15.ads1115 as ADS
    with i2c_open() as i2c:
        ads = ADS.ADS1115(i2c, address=0x48)
        return round(read_smoothed_channel(ads, channel), 4)


@bp.route("/api/calibration/capture-live", methods=["POST"])
def api_capture_live():
    payload = request.get_json(silent=True) or {}
    raw     = payload.get("channel", "A0")
    if isinstance(raw, str):
        key = raw.strip().upper()
        if key not in _CH_MAP:
            return jsonify({"error": "channel must be A0–A3"}), 400
        ch, label = _CH_MAP[key], key
    else:
        try:
            ch = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "channel must be A0–A3"}), 400
        if ch not in range(4):
            return jsonify({"error": "channel must be A0–A3"}), 400
        label = f"A{ch}"
    try:
        voltage = _capture_live_voltage(ch)
    except Exception as exc:
        return jsonify({"error": f"Capture failed: {exc}"}), 500
    return jsonify({"channel": label, "averaged_voltage": voltage,
                    "samples": ADS_SAMPLES, "sample_delay_seconds": ADS_SAMPLE_DELAY})


@bp.route("/api/calibration/baseline", methods=["POST"])
def api_save_baseline():
    p    = request.get_json(silent=True) or {}
    name = str(p.get("name", "")).strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        dry_v = clamp_voltage(p["dry_voltage"])
        wet_v = clamp_voltage(p["wet_voltage"])
    except Exception:
        return jsonify({"error": "dry_voltage and wet_voltage must be numeric"}), 400
    if abs(dry_v - wet_v) < 1e-9:
        return jsonify({"error": "dry and wet voltages must differ"}), 400
    conn = get_db()
    conn.execute(
        """INSERT INTO soil_baseline (name, dry_voltage, wet_voltage) VALUES (?,?,?)
           ON CONFLICT(name) DO UPDATE SET dry_voltage=excluded.dry_voltage,
                                           wet_voltage=excluded.wet_voltage""",
        (name, dry_v, wet_v),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM soil_baseline WHERE name=?", (name,)).fetchone()
    conn.close()
    return jsonify({"baseline": _baseline_dict(row)})


@bp.route("/api/calibration/baseline/<int:baseline_id>", methods=["DELETE"])
def api_delete_baseline(baseline_id):
    conn = get_db()
    if not conn.execute("SELECT id FROM soil_baseline WHERE id=?", (baseline_id,)).fetchone():
        conn.close()
        return jsonify({"error": "Baseline not found"}), 404
    conn.execute("UPDATE zone_profile SET soil_baseline_id=NULL WHERE soil_baseline_id=?",
                 (baseline_id,))
    conn.execute("DELETE FROM soil_baseline WHERE id=?", (baseline_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted_id": baseline_id})


@bp.route("/api/calibration/crop-target", methods=["POST"])
def api_save_crop_target():
    p    = request.get_json(silent=True) or {}
    name = str(p.get("name", "")).strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        tv = clamp_voltage(p["target_voltage"])
    except Exception:
        return jsonify({"error": "target_voltage must be numeric"}), 400
    conn = get_db()
    conn.execute(
        """INSERT INTO crop_target (name, target_voltage) VALUES (?,?)
           ON CONFLICT(name) DO UPDATE SET target_voltage=excluded.target_voltage""",
        (name, tv),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM crop_target WHERE name=?", (name,)).fetchone()
    conn.close()
    return jsonify({"crop_target": _crop_target_dict(row)})


@bp.route("/api/calibration/crop-target/<int:target_id>", methods=["DELETE"])
def api_delete_crop_target(target_id):
    conn = get_db()
    if not conn.execute("SELECT id FROM crop_target WHERE id=?", (target_id,)).fetchone():
        conn.close()
        return jsonify({"error": "Crop target not found"}), 404
    conn.execute("UPDATE zone_profile SET crop_target_id=NULL WHERE crop_target_id=?", (target_id,))
    conn.execute("DELETE FROM crop_target WHERE id=?", (target_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted_id": target_id})
