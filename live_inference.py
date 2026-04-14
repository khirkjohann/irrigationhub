"""
Module B — Live Inference Engine
==================================
Causwagan Norte Irrigation System — ML Pipeline

Feature engineer and edge-inference runner.  Designed to be called by a
scheduler (cron, APScheduler, or systemd timer) on a regular cadence.

Usage:
    python3 live_inference.py --zone ZONE_ID [--dummy] [--dry-run]

    --zone     1-4     Zone to water
    --dummy            Use manually-entered sensor values (no hardware)
    --dry-run          Calculate duration but do NOT open the valve

Environment variables:
    IRRIGATION_BRAIN_PKL   Path to the trained model file
                           (default /home/pi/irrigation_brain.pkl)
    IRRIGATION_FLOW_RATE   Litres / minute  (default 2.5)
    IRRIGATION_DB          Path to main SQLite DB
                           (default /home/pi/irrigation_data.db)
    TRAINING_CSV           Path to append idle rows
                           (default /home/pi/training_data.csv)

Pipeline:
  1. Read sensors → temperature, humidity, current moisture
  2. Look up zone target_crop_moisture from DB
  3. Compute Moisture_Deficit = target − current  (floored at 0)
     → if deficit == 0 → log idle row (0 deficit / 0 volume) and exit
  4. Load irrigation_brain.pkl
  5. Predict Target_Volume from [Temp, Humidity, Moisture_Deficit]
     → volume is clamped ≥ 0
  6. Pump_Duration = volume / FLOW_RATE
  7. Actuate GPIO relay for Pump_Duration minutes
  8. Write a pending-feedback sidecar JSON for feedback_grader.py
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration — all overridable via environment variables
# ─────────────────────────────────────────────────────────────────────────────

BRAIN_PKL      = os.getenv("IRRIGATION_BRAIN_PKL",  "/home/pi/irrigation_brain.pkl")
FLOW_RATE_LPM  = float(os.getenv("IRRIGATION_FLOW_RATE", "2.5"))   # Litres/min
DB_PATH        = os.getenv("IRRIGATION_DB",          "/home/pi/irrigation_data.db")
TRAINING_CSV   = os.getenv("TRAINING_CSV",            "/home/pi/training_data.csv")
PENDING_JSON   = os.getenv("IRRIGATION_PENDING_JSON", "/home/pi/irrigation_brain_pending.json")

APP_QUEUE_URL = os.getenv("IRRIGATION_QUEUE_URL", "http://localhost:5000/api/irrigation/queue")

VALID_ZONES = {1, 2, 3, 4}

CSV_HEADER = [
    "Timestamp",
    "Zone_ID",
    "Target_Crop_Moisture",
    "Initial_Moisture",
    "Temp",
    "Humidity",
    "Moisture_Deficit",
    "Target_Volume",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware abstraction layer
# ─────────────────────────────────────────────────────────────────────────────

def _read_bme280():
    """Return (temp_c, humidity_pct) or raise RuntimeError."""
    import board
    import busio
    from adafruit_bme280 import basic as adafruit_bme280

    i2c = busio.I2C(board.SCL, board.SDA)
    for addr in (0x76, 0x77):
        try:
            bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
            t = round(float(bme.temperature), 1)
            h = round(float(bme.humidity), 1)
            try:
                i2c.deinit()
            except Exception:
                pass
            return t, h
        except Exception:
            pass
    try:
        i2c.deinit()
    except Exception:
        pass
    raise RuntimeError("BME280 not found at 0x76 or 0x77")


def _read_soil_moisture_pct(zone_id: int):
    """
    Return calibrated soil moisture % for zone_id, or raise RuntimeError.
    Loads the dry/wet baseline from the main DB.
    """
    import sqlite3
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn

    ch = zone_id - 1   # zone 1 → channel 0

    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT sb.dry_voltage, sb.wet_voltage
           FROM zone_profile zp
           LEFT JOIN soil_baseline sb ON zp.soil_baseline_id = sb.id
           WHERE zp.zone_id = ?""",
        (zone_id,),
    ).fetchone()
    conn.close()

    dry_v = row["dry_voltage"] if row else None
    wet_v = row["wet_voltage"] if row else None

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, address=0x48)
    _ = AnalogIn(ads, ch).voltage    # discard mux settling read
    time.sleep(0.05)
    samples = [float(AnalogIn(ads, ch).voltage) for _ in range(8)]
    i2c.deinit()

    voltage = sum(samples) / len(samples)

    if dry_v is not None and wet_v is not None and (dry_v - wet_v) != 0.0:
        pct = (dry_v - voltage) / (dry_v - wet_v) * 100.0
    else:
        # Raw fallback: 3.3 V = 0 %, 0 V = 100 %
        pct = (1.0 - voltage / 3.3) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


def _actuate_valve(zone_id: int, volume_liters: float, dry_run: bool):
    """
    Queue an irrigation job via the main app's HTTP API.
    The app's queue worker handles GPIO and the hardware failsafe timer.
    Skipped entirely when dry_run=True.
    """
    if dry_run:
        print(f"[INFERENCE] DRY-RUN — would queue Zone {zone_id}, "
              f"{volume_liters:.3f} L")
        return

    import json as _json
    import urllib.request

    payload = _json.dumps({
        "zone_id":       zone_id,
        "volume_liters": round(volume_liters, 3),
        "source":        "ml",
    }).encode()
    req = urllib.request.Request(
        APP_QUEUE_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[INFERENCE] Zone {zone_id} queued — {volume_liters:.3f} L "
                  f"(HTTP {resp.status})")
    except Exception as exc:
        raise RuntimeError(f"Failed to queue irrigation via app: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_zone_thresholds(zone_id: int) -> tuple[float, float]:
    """
    Return (target_moisture, threshold_gap) for a zone from the main DB.

    threshold_gap is the minimum deficit that must be present before the pump
    fires.  If current_deficit < threshold_gap the zone is skipped and no
    actuation occurs.  When the trigger IS met the FULL current_deficit is
    passed to the model so it solves for restoring to 100 % of the target,
    not just the 1 % that crossed the gap.

    Defaults: threshold_gap = 5.0 % (matches DB column default).
    Raises ValueError if the zone has no target_moisture set.
    """
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT target_moisture, threshold_gap FROM zone_profile WHERE zone_id = ?",
        (zone_id,),
    ).fetchone()
    conn.close()
    if row is None or row["target_moisture"] is None:
        raise ValueError(f"Zone {zone_id} has no target_moisture set in DB")
    target    = float(row["target_moisture"])
    threshold = float(row["threshold_gap"]) if row["threshold_gap"] is not None else 5.0
    return target, threshold


# ─────────────────────────────────────────────────────────────────────────────
#  CSV logging
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv():
    if not os.path.exists(TRAINING_CSV):
        with open(TRAINING_CSV, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADER)


def _log_row(zone_id, target_crop_moisture, initial_moisture, temp, humidity,
             moisture_deficit, target_volume):
    """Append one row to training_data.csv."""
    _ensure_csv()
    row = {
        "Timestamp":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Zone_ID":              zone_id,
        "Target_Crop_Moisture": round(target_crop_moisture, 2),
        "Initial_Moisture":     round(initial_moisture, 2),
        "Temp":                 round(temp, 2),
        "Humidity":             round(humidity, 2),
        "Moisture_Deficit":     round(moisture_deficit, 4),
        "Target_Volume":        round(target_volume, 4),
    }
    with open(TRAINING_CSV, "a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=CSV_HEADER).writerow(row)
    print(f"[INFERENCE] Logged → deficit={moisture_deficit:.2f}%, "
          f"volume={target_volume:.3f} L")


# ─────────────────────────────────────────────────────────────────────────────
#  ML inference
# ─────────────────────────────────────────────────────────────────────────────

def _load_model():
    """Load the RandomForest model from irrigation_brain.pkl."""
    import joblib
    if not os.path.exists(BRAIN_PKL):
        raise FileNotFoundError(
            f"Model not found at {BRAIN_PKL}. "
            "Run cron_retrain.py after collecting at least 20 bootstrap rows."
        )
    return joblib.load(BRAIN_PKL)


def _predict_volume(model, temp: float, humidity: float, deficit: float) -> float:
    """
    Run the Random Forest and return predicted volume (≥ 0 L).
    Features: [Temp, Humidity, Moisture_Deficit]
    """
    import numpy as np
    features = [[temp, humidity, deficit]]
    volume = float(model.predict(features)[0])
    return max(0.0, volume)   # defensive floor — RF should not produce negatives


# ─────────────────────────────────────────────────────────────────────────────
#  Pending-feedback sidecar
# ─────────────────────────────────────────────────────────────────────────────

def _write_pending(zone_id, target_moisture, initial_moisture, temp, humidity,
                   deficit, predicted_volume, actuation_ts):
    """
    Append a sidecar record so feedback_grader.py knows what happened.
    Multiple zones per batch are stored as a list — no zone overwrites another.
    """
    new_entry = {
        "zone_id":           zone_id,
        "target_moisture":   target_moisture,
        "initial_moisture":  initial_moisture,
        "temp":              temp,
        "humidity":          humidity,
        "deficit":           deficit,
        "predicted_volume":  predicted_volume,
        "flow_rate_lpm":     FLOW_RATE_LPM,
        "actuation_ts":      actuation_ts,
    }
    # Load existing list (if any), append, then write back.
    if os.path.exists(PENDING_JSON):
        with open(PENDING_JSON) as fh:
            existing = json.load(fh)
        if isinstance(existing, dict):
            existing = [existing]   # migrate old single-dict format
    else:
        existing = []
    existing.append(new_entry)
    with open(PENDING_JSON, "w") as fh:
        json.dump(existing, fh, indent=2)
    print(f"[INFERENCE] Pending feedback written → {PENDING_JSON}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_inference(zone_id: int, dummy_mode: bool, dry_run: bool,
                  dummy_temp: float | None = None,
                  dummy_humidity: float | None = None,
                  dummy_moisture: float | None = None,
                  _skip_queue: bool = False):
    """
    Execute the full inference pipeline for one zone.

    In dummy_mode the caller must supply dummy_temp, dummy_humidity,
    dummy_moisture (used for unit-testing without real hardware).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[INFERENCE] ── Zone {zone_id}  @  {ts} ──")

    # ── STEP 1: Read sensors ──────────────────────────────────────────────────
    if dummy_mode:
        if dummy_temp is None or dummy_humidity is None or dummy_moisture is None:
            raise ValueError("dummy_mode requires dummy_temp, dummy_humidity, dummy_moisture")
        temp             = float(dummy_temp)
        humidity         = float(dummy_humidity)
        initial_moisture = float(dummy_moisture)
        print(f"[INFERENCE] DUMMY  temp={temp}, humidity={humidity}, "
              f"moisture={initial_moisture}")
    else:
        print("[INFERENCE] Reading BME280 …", end=" ", flush=True)
        temp, humidity = _read_bme280()
        print(f"temp={temp} °C, humidity={humidity} %")

        print(f"[INFERENCE] Reading soil moisture Zone {zone_id} …", end=" ", flush=True)
        initial_moisture = _read_soil_moisture_pct(zone_id)
        print(f"{initial_moisture} %")

    # ── STEP 2: Look up target moisture and trigger threshold ─────────────────
    target_moisture, threshold_gap = _get_zone_thresholds(zone_id)
    print(f"[INFERENCE] Target crop moisture: {target_moisture} %  "
          f"(threshold_gap={threshold_gap} %)")

    # ── STEP 3: Compute deficit; skip if below trigger threshold ─────────────
    #
    # current_deficit is the FULL gap back to the target.  threshold_gap is
    # only used as the trip-wire condition — once it fires we always ask the
    # model to solve for the complete deficit so the pump runs long enough to
    # restore soil moisture all the way back to the target level.
    #
    # Example: target=60 %, live=49 %  →  current_deficit=11 %
    #          threshold_gap=10 %      →  11 ≥ 10  →  trigger fires
    #          Model receives deficit=11 %, not 1 % (the overshoot).
    current_deficit = max(0.0, target_moisture - initial_moisture)
    print(f"[INFERENCE] Current deficit: {current_deficit:.2f} %  "
          f"(trigger at ≥ {threshold_gap} %)")

    if current_deficit < threshold_gap:
        reason = "deficit_zero" if current_deficit == 0.0 else "below_threshold"
        if current_deficit == 0.0:
            print("[INFERENCE] Soil at or above target — pump stays OFF.")
        else:
            print(f"[INFERENCE] Deficit {current_deficit:.2f} % < threshold "
                  f"{threshold_gap:.2f} % — pump stays OFF.")
        # Log a negative-class row so the RF learns the idle state.
        _log_row(zone_id, target_moisture, initial_moisture,
                 temp, humidity, 0.0, 0.0)
        return {
            "zone_id":          zone_id,
            "deficit":          current_deficit,
            "threshold_gap":    threshold_gap,
            "predicted_volume": 0.0,
            "duration_minutes": 0.0,
            "actuated":         False,
            "reason":           reason,
        }

    # Trigger met — use the full deficit for the model.
    deficit = current_deficit

    # ── STEP 4: Load model and predict volume ─────────────────────────────────
    model = _load_model()
    predicted_volume = _predict_volume(model, temp, humidity, deficit)
    print(f"[INFERENCE] Predicted volume: {predicted_volume:.3f} L")

    if predicted_volume == 0.0:
        print("[INFERENCE] Model returned 0 L — no actuation.")
        _log_row(zone_id, target_moisture, initial_moisture,
                 temp, humidity, deficit, 0.0)
        return {
            "zone_id":          zone_id,
            "deficit":          deficit,
            "threshold_gap":    threshold_gap,
            "predicted_volume": 0.0,
            "duration_minutes": 0.0,
            "actuated":         False,
            "reason":           "model_zero_volume",
        }

    # ── STEP 5: Estimate duration for reporting (main app uses its own flow rate) ─
    duration_minutes = predicted_volume / FLOW_RATE_LPM
    print(f"[INFERENCE] Estimated duration: {duration_minutes:.2f} min  "
          f"(flow={FLOW_RATE_LPM} L/min)")

    # ── STEP 6: Queue via main app (handles GPIO + failsafe timer) ────────────
    if _skip_queue:
        # Batch mode: caller will queue all zones after all predictions are done.
        result = {
            "zone_id":          zone_id,
            "target_moisture":  target_moisture,
            "initial_moisture": initial_moisture,
            "threshold_gap":    threshold_gap,
            "temp":             temp,
            "humidity":         humidity,
            "deficit":          deficit,
            "predicted_volume": predicted_volume,
            "duration_minutes": duration_minutes,
            "flow_rate_lpm":    FLOW_RATE_LPM,
            "actuated":         False,
            "_needs_queue":     True,
        }
        print(f"[INFERENCE] Prediction ready — Zone {zone_id}, {predicted_volume:.3f} L (queuing deferred)")
        return result

    actuation_ts = datetime.now().isoformat()
    _actuate_valve(zone_id, predicted_volume, dry_run)

    # ── STEP 7: Write pending-feedback sidecar ────────────────────────────────
    _write_pending(zone_id, target_moisture, initial_moisture, temp, humidity,
                   deficit, predicted_volume, actuation_ts)

    # ── STEP 8: Log the pre-feedback row (feedback_grader may update volume) ──
    # A confirmed row is written by feedback_grader after moisture check.
    # Here we only write the pending actuation; the grader writes the final CSV row.

    result = {
        "zone_id":          zone_id,
        "target_moisture":  target_moisture,
        "initial_moisture": initial_moisture,
        "threshold_gap":    threshold_gap,
        "temp":             temp,
        "humidity":         humidity,
        "deficit":          deficit,
        "predicted_volume": predicted_volume,
        "duration_minutes": duration_minutes,
        "flow_rate_lpm":    FLOW_RATE_LPM,
        "actuated":         not dry_run,
        "actuation_ts":     actuation_ts,
    }
    print(f"[INFERENCE] Complete — result={result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Batch runner — sense all zones first, then queue all
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_BATCH_ZONES = [2, 3]

def run_batch(zone_ids=None, dummy_mode=False, dry_run=False):
    """
    Two-phase batch inference.
    Phase 1: read sensors + predict for every zone (no pump running).
    Phase 2: queue all zones that need irrigation.

    This prevents the pump from zone N being on while zone N+1's sensor
    is being read, which caused false 0 % readings.
    """
    if zone_ids is None:
        zone_ids = DEFAULT_BATCH_ZONES

    predictions = []

    # ── Phase 1: sense + predict (skip_queue=True) ───────────────────────────
    print(f"\n[BATCH] ── Phase 1: sensing + predicting zones {zone_ids} ──")
    for zid in zone_ids:
        try:
            result = run_inference(zid, dummy_mode=dummy_mode, dry_run=dry_run,
                                   _skip_queue=True)
            predictions.append(result)
        except Exception as exc:
            print(f"[BATCH] FATAL Zone {zid}: {exc}", file=sys.stderr)

    # ── Phase 2: queue all predictions that need irrigation ──────────────────
    needs_queue = [p for p in predictions if p.get("_needs_queue")]
    if not needs_queue:
        print("[BATCH] Phase 2 — no zones need queuing.")
        return predictions

    print(f"\n[BATCH] ── Phase 2: queuing {len(needs_queue)} zone(s) ──")
    for pred in needs_queue:
        zid = pred["zone_id"]
        vol = pred["predicted_volume"]
        try:
            actuation_ts = datetime.now().isoformat()
            _actuate_valve(zid, vol, dry_run)
            _write_pending(zid, pred["target_moisture"], pred["initial_moisture"],
                           pred["temp"], pred["humidity"], pred["deficit"],
                           vol, actuation_ts)
            pred["actuated"]     = not dry_run
            pred["actuation_ts"] = actuation_ts
        except Exception as exc:
            print(f"[BATCH] Queue failed Zone {zid}: {exc}", file=sys.stderr)

    return predictions


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Edge inference: read sensors → predict irrigation volume → actuate."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--zone",  type=int, choices=[1, 2, 3, 4],
                       help="Single zone ID (legacy; prefer --zones for batch)")
    group.add_argument("--zones", type=int, nargs="+", metavar="N",
                       help="One or more zone IDs — senses all first, then queues all")
    parser.add_argument("--dummy", action="store_true",
                        help="Use manual/dummy sensor values (no hardware)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Predict but do not open the valve")
    parser.add_argument("--temp",     type=float, help="Dummy temperature (°C)")
    parser.add_argument("--humidity", type=float, help="Dummy humidity (%)")
    parser.add_argument("--moisture", type=float, help="Dummy soil moisture (%)")
    args = parser.parse_args()

    if args.zones:
        # Batch mode: validate zone IDs then run two-phase batch
        for zid in args.zones:
            if zid not in VALID_ZONES:
                print(f"[INFERENCE] ERROR: Invalid zone_id {zid}", file=sys.stderr)
                sys.exit(1)
        run_batch(zone_ids=args.zones, dummy_mode=args.dummy, dry_run=args.dry_run)
        sys.exit(0)
    else:
        # Legacy single-zone mode
        try:
            result = run_inference(
                zone_id        = args.zone,
                dummy_mode     = args.dummy,
                dry_run        = args.dry_run,
                dummy_temp     = args.temp,
                dummy_humidity = args.humidity,
                dummy_moisture = args.moisture,
            )
            sys.exit(0 if result.get("actuated") or result.get("reason") else 1)
        except FileNotFoundError as exc:
            print(f"[INFERENCE] ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"[INFERENCE] FATAL: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
