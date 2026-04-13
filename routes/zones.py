"""
routes/zones.py — Zone profile, mapping, disable/enable, mode, and ML status routes.
"""
import os

from flask import Blueprint, jsonify, request

from core.config import ML_MODEL_PATH, VALID_ZONES
from core.db import get_db
from hardware.irrigation import log_event
from core.utils import to_bool, voltage_to_pct

bp = Blueprint("zones", __name__)


def _zone_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


@bp.route("/api/zone/<int:zone_id>/mapping", methods=["POST"])
def api_zone_mapping(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    p = request.get_json(silent=True) or {}

    conn = get_db()
    existing = conn.execute(
        "SELECT soil_baseline_id, crop_target_id FROM zone_profile WHERE zone_id=?", (zone_id,)
    ).fetchone()

    # Only overwrite a field if it was explicitly present in the payload.
    # Sending null/empty clears it; omitting the key keeps the existing value.
    if "soil_baseline_id" in p:
        bl = int(p["soil_baseline_id"]) if p["soil_baseline_id"] not in (None, "") else None
    else:
        bl = existing["soil_baseline_id"] if existing else None

    if "crop_target_id" in p:
        ct = int(p["crop_target_id"]) if p["crop_target_id"] not in (None, "") else None
    else:
        ct = existing["crop_target_id"] if existing else None

    bls  = {r["id"]: r for r in conn.execute(
        "SELECT id, dry_voltage, wet_voltage FROM soil_baseline"
    ).fetchall()}
    cts  = {r["id"]: r for r in conn.execute(
        "SELECT id, target_voltage FROM crop_target"
    ).fetchall()}

    if bl is not None and bl not in bls:
        conn.close()
        return jsonify({"error": "Soil baseline not found"}), 400
    if ct is not None and ct not in cts:
        conn.close()
        return jsonify({"error": "Crop target not found"}), 400

    target_moisture = voltage_to_pct(
        cts[ct]["target_voltage"], bls[bl]["dry_voltage"], bls[bl]["wet_voltage"]
    ) if bl and ct else None

    threshold_gap = float(p.get("threshold_gap", 5.0))
    if not (0.0 < threshold_gap <= 50.0):
        conn.close()
        return jsonify({"error": "threshold_gap must be between 0.1 and 50 %"}), 400

    conn.execute(
        "UPDATE zone_profile SET soil_baseline_id=?,crop_target_id=?,target_moisture=?,threshold_gap=? WHERE zone_id=?",
        (bl, ct, target_moisture, threshold_gap, zone_id),
    )
    conn.commit()
    z = conn.execute("""
        SELECT zp.*, sb.name AS soil_baseline_name, ct.name AS crop_target_name
        FROM zone_profile zp
        LEFT JOIN soil_baseline sb ON sb.id=zp.soil_baseline_id
        LEFT JOIN crop_target   ct ON ct.id=zp.crop_target_id
        WHERE zp.zone_id=?""", (zone_id,)).fetchone()
    conn.close()
    return jsonify({"zone": _zone_dict(z)})


@bp.route("/api/zone/<int:zone_id>/disable", methods=["POST"])
def api_zone_disable(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    p = request.get_json(silent=True) or {}
    if "disabled" not in p:
        return jsonify({"error": "disabled is required"}), 400
    disabled = to_bool(p["disabled"])
    conn = get_db()
    conn.execute("UPDATE zone_profile SET disabled=? WHERE zone_id=?",
                 (1 if disabled else 0, zone_id))
    conn.commit()
    z = conn.execute("SELECT * FROM zone_profile WHERE zone_id=?", (zone_id,)).fetchone()
    conn.close()
    log_event(zone_id,
              "zone_disabled_manual" if disabled else "zone_enabled_manual",
              "manual-config")
    return jsonify({"zone": _zone_dict(z)})


@bp.route("/api/zone/<int:zone_id>/mode", methods=["GET", "POST"])
def api_zone_set_mode(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    if request.method == "GET":
        conn = get_db()
        row  = conn.execute(
            "SELECT irr_mode FROM zone_profile WHERE zone_id=?", (zone_id,)
        ).fetchone()
        conn.close()
        return jsonify({"zone_id": zone_id, "irr_mode": row["irr_mode"] if row else "manual"})
    p    = request.get_json(silent=True) or {}
    mode = p.get("mode", "manual")
    if mode not in ("ml", "scheduled", "manual"):
        return jsonify({"error": "mode must be ml, scheduled, or manual"}), 400
    if mode == "ml" and not os.path.exists(ML_MODEL_PATH):
        return jsonify({"error": "No trained ML model available. Collect data with Panel 6 on "
                                 "the thesis dashboard, then run cron_retrain.py."}), 400
    conn = get_db()
    conn.execute("UPDATE zone_profile SET irr_mode=? WHERE zone_id=?", (mode, zone_id))
    conn.commit()
    conn.close()
    return jsonify({"zone_id": zone_id, "irr_mode": mode})


@bp.route("/api/ml/model-status")
def api_ml_model_status():
    model_exists = os.path.exists(ML_MODEL_PATH)
    row_count    = 0
    training_csv = os.getenv("TRAINING_CSV", "/home/pi/training_data.csv")
    if os.path.exists(training_csv):
        try:
            import csv as _csv
            with open(training_csv, newline="") as fh:
                row_count = max(0, sum(1 for _ in _csv.reader(fh)) - 1)
        except Exception:
            pass
    return jsonify({
        "model_available": model_exists,
        "model_path":      ML_MODEL_PATH,
        "training_rows":   row_count,
    })
