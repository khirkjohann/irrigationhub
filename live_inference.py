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

# GPIO BCM pin for each zone valve (active-LOW relay board assumed).
RELAY_GPIO_MAP = {1: 17, 2: 27, 3: 22, 4: 23}
RELAY_ACTIVE_LOW = os.getenv("IRRIGATION_RELAY_ACTIVE_LOW", "1") == "1"

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


def _actuate_valve(zone_id: int, duration_minutes: float, dry_run: bool):
    """
    Open the zone valve for duration_minutes, then close it.
    Skipped entirely when dry_run=True.
    """
    if dry_run:
        print(f"[INFERENCE] DRY-RUN — would open Zone {zone_id} valve "
              f"for {duration_minutes:.2f} min")
        return

    duration_seconds = duration_minutes * 60.0
    pin = RELAY_GPIO_MAP[zone_id]

    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(pin, GPIO.OUT)
        on_level  = GPIO.LOW  if RELAY_ACTIVE_LOW else GPIO.HIGH
        off_level = GPIO.HIGH if RELAY_ACTIVE_LOW else GPIO.LOW

        print(f"[INFERENCE] Zone {zone_id} valve ON (BCM {pin}) — "
              f"{duration_seconds:.0f} s …")
        GPIO.output(pin, on_level)
        time.sleep(duration_seconds)
        GPIO.output(pin, off_level)
        print(f"[INFERENCE] Zone {zone_id} valve OFF")
    except Exception as exc:
        raise RuntimeError(f"GPIO actuation failed: {exc}") from exc
    finally:
        try:
            GPIO.cleanup(pin)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_target_moisture(zone_id: int) -> float:
    """
    Return the target_crop_moisture (%) for a zone from the main DB.
    Raises ValueError if the zone has no target set.
    """
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT target_moisture FROM zone_profile WHERE zone_id = ?", (zone_id,)
    ).fetchone()
    conn.close()
    if row is None or row["target_moisture"] is None:
        raise ValueError(f"Zone {zone_id} has no target_moisture set in DB")
    return float(row["target_moisture"])


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
    Write a sidecar JSON so feedback_grader.py knows what happened.
    Overwrites any previous pending record for this zone.
    """
    pending = {
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
    with open(PENDING_JSON, "w") as fh:
        json.dump(pending, fh, indent=2)
    print(f"[INFERENCE] Pending feedback written → {PENDING_JSON}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_inference(zone_id: int, dummy_mode: bool, dry_run: bool,
                  dummy_temp=None, dummy_humidity=None, dummy_moisture=None):
    """
    Execute the full inference pipeline for one zone.

    In dummy_mode the caller must supply dummy_temp, dummy_humidity,
    dummy_moisture (used for unit-testing without real hardware).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[INFERENCE] ── Zone {zone_id}  @  {ts} ──")

    # ── STEP 1: Read sensors ──────────────────────────────────────────────────
    if dummy_mode:
        if any(v is None for v in (dummy_temp, dummy_humidity, dummy_moisture)):
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

    # ── STEP 2: Look up target moisture ───────────────────────────────────────
    target_moisture = _get_target_moisture(zone_id)
    print(f"[INFERENCE] Target crop moisture: {target_moisture} %")

    # ── STEP 3: Compute deficit; skip if already satisfied ───────────────────
    deficit = max(0.0, target_moisture - initial_moisture)
    print(f"[INFERENCE] Moisture deficit: {deficit:.2f} %")

    if deficit == 0.0:
        print("[INFERENCE] Soil at or above target — pump stays OFF.")
        # Log a negative-class row so the RF learns the idle state.
        _log_row(zone_id, target_moisture, initial_moisture,
                 temp, humidity, 0.0, 0.0)
        return {
            "zone_id":          zone_id,
            "deficit":          0.0,
            "predicted_volume": 0.0,
            "duration_minutes": 0.0,
            "actuated":         False,
            "reason":           "deficit_zero",
        }

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
            "predicted_volume": 0.0,
            "duration_minutes": 0.0,
            "actuated":         False,
            "reason":           "model_zero_volume",
        }

    # ── STEP 5: Calculate pump duration ──────────────────────────────────────
    duration_minutes = predicted_volume / FLOW_RATE_LPM
    print(f"[INFERENCE] Duration: {duration_minutes:.2f} min  "
          f"(flow={FLOW_RATE_LPM} L/min)")

    # ── STEP 6: Actuate valve ─────────────────────────────────────────────────
    actuation_ts = datetime.now().isoformat()
    _actuate_valve(zone_id, duration_minutes, dry_run)

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
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Edge inference: read sensors → predict irrigation volume → actuate."
    )
    parser.add_argument("--zone", type=int, required=True, choices=[1, 2, 3, 4],
                        help="Zone ID to irrigate (1-4)")
    parser.add_argument("--dummy", action="store_true",
                        help="Use manual/dummy sensor values (no hardware)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Predict but do not open the valve")
    parser.add_argument("--temp",     type=float, help="Dummy temperature (°C)")
    parser.add_argument("--humidity", type=float, help="Dummy humidity (%)")
    parser.add_argument("--moisture", type=float, help="Dummy soil moisture (%)")
    args = parser.parse_args()

    try:
        result = run_inference(
            zone_id      = args.zone,
            dummy_mode   = args.dummy,
            dry_run      = args.dry_run,
            dummy_temp   = args.temp,
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
