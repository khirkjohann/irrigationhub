"""
Module C — Feedback Grader
============================
Causwagan Norte Irrigation System — ML Pipeline

Called exactly 10 minutes after live_inference.py actuates the pump to
grade the prediction and log a corrected training row.

Usage:
    python3 feedback_grader.py [--dummy] [--moisture VALUE]

    --dummy            Skip real sensor reads (for testing)
    --moisture VALUE   Override final moisture % when in dummy mode

The grader reads the pending sidecar written by live_inference.py
(irrigation_brain_pending.json) and then:

  Grade A  (|error| ≤ 2 %)  → Log the original predicted volume.
  Grade B  (error < −2 %)   → Under-watered.  Calculate remaining deficit,
                               call live_inference.run_inference() again,
                               sum the two volumes, log the combined total.
  Grade C  (error > +2 %)   → Over-watered.  Penalise logged volume by ×0.8
                               to steer the model down on the next retrain.

Environment variables (same as live_inference.py):
    IRRIGATION_BRAIN_PKL
    IRRIGATION_FLOW_RATE
    IRRIGATION_DB
    TRAINING_CSV
    IRRIGATION_PENDING_JSON
    IRRIGATION_RELAY_ACTIVE_LOW
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime

# Re-use the same paths from live_inference so everything stays consistent.
BRAIN_PKL    = os.getenv("IRRIGATION_BRAIN_PKL",  "/home/pi/irrigation_brain.pkl")
FLOW_RATE    = float(os.getenv("IRRIGATION_FLOW_RATE", "2.5"))
DB_PATH      = os.getenv("IRRIGATION_DB",          "/home/pi/irrigation_data.db")
TRAINING_CSV = os.getenv("TRAINING_CSV",            "/home/pi/training_data.csv")
PENDING_JSON = os.getenv("IRRIGATION_PENDING_JSON", "/home/pi/irrigation_brain_pending.json")

OVER_WATER_PENALTY = 0.8    # factor applied to over-watered predictions
TOLERANCE_PCT      = 2.0    # ± % band considered "perfect"

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
#  CSV helper
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv():
    if not os.path.exists(TRAINING_CSV):
        with open(TRAINING_CSV, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADER)


def _log_row(zone_id, target_crop_moisture, initial_moisture, temp, humidity,
             moisture_deficit, target_volume, label=""):
    """Append one confirmed training row to training_data.csv."""
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
    tag = f" ({label})" if label else ""
    print(f"[GRADER] ✓ Row logged{tag} → deficit={moisture_deficit:.2f}%, "
          f"volume={target_volume:.3f} L")


# ─────────────────────────────────────────────────────────────────────────────
#  Pending sidecar helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_pending() -> list:
    """
    Load the sidecar JSON written by live_inference.py.
    Returns a list of pending records (supports both old single-dict and new
    list format).  Raises FileNotFoundError if no pending session exists.
    """
    if not os.path.exists(PENDING_JSON):
        raise FileNotFoundError(
            f"No pending feedback file found at {PENDING_JSON}. "
            "Run live_inference.py first."
        )
    with open(PENDING_JSON) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return [data]   # migrate old single-dict format
    return data


def _clear_pending():
    """Remove the sidecar after grading so stale data is not reused."""
    try:
        os.remove(PENDING_JSON)
    except FileNotFoundError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware abstraction
# ─────────────────────────────────────────────────────────────────────────────

def _read_soil_moisture_pct(zone_id: int) -> float:
    """Read calibrated soil moisture for zone_id. Returns % or raises."""
    import sqlite3
    import time
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn

    ch = zone_id - 1

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
    dry_v = row["dry_voltage"]  if row else None
    wet_v = row["wet_voltage"]  if row else None

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, address=0x48)
    _ = AnalogIn(ads, ch).voltage
    time.sleep(0.05)
    samples = [float(AnalogIn(ads, ch).voltage) for _ in range(8)]
    i2c.deinit()

    voltage = sum(samples) / len(samples)
    if dry_v is not None and wet_v is not None and (dry_v - wet_v) != 0.0:
        pct = (dry_v - voltage) / (dry_v - wet_v) * 100.0
    else:
        pct = (1.0 - voltage / 3.3) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


# ─────────────────────────────────────────────────────────────────────────────
#  Core grader logic
# ─────────────────────────────────────────────────────────────────────────────

def grade(final_moisture: float, pending: dict, dummy_mode: bool, dry_run: bool):
    """
    Grade the previous irrigation event and log the corrected training row.

    Returns a dict describing the grading outcome.
    """
    zone_id           = pending["zone_id"]
    target_moisture   = pending["target_moisture"]
    initial_moisture  = pending["initial_moisture"]
    temp              = pending["temp"]
    humidity          = pending["humidity"]
    deficit           = pending["deficit"]
    predicted_volume  = pending["predicted_volume"]
    flow_rate         = pending.get("flow_rate_lpm", FLOW_RATE)

    error = final_moisture - target_moisture
    print(f"[GRADER] Zone {zone_id}:")
    print(f"  Target moisture  : {target_moisture:.1f} %")
    print(f"  Initial moisture : {initial_moisture:.1f} %")
    print(f"  Final moisture   : {final_moisture:.1f} %")
    print(f"  Error            : {error:+.2f} %  (tolerance ±{TOLERANCE_PCT:.0f} %)")
    print(f"  Predicted volume : {predicted_volume:.3f} L")

    # ── Grade A: Perfect ─────────────────────────────────────────────────────
    if abs(error) <= TOLERANCE_PCT:
        _log_row(zone_id, target_moisture, initial_moisture, temp, humidity,
                 deficit, predicted_volume, label="Grade-A")
        return {
            "grade": "A",
            "error_pct":      round(error, 2),
            "logged_volume":  predicted_volume,
            "description":    "Within tolerance — exact prediction logged.",
        }

    # ── Grade B: Under-watered ────────────────────────────────────────────────
    if error < -TOLERANCE_PCT:
        remaining_deficit = max(0.0, target_moisture - final_moisture)
        print(f"[GRADER] Under-watered — remaining deficit: {remaining_deficit:.2f} %")
        extra_volume = 0.0

        if remaining_deficit > 0.0:
            # Re-run the inference engine to water the remaining deficit.
            from live_inference import run_inference
            try:
                result = run_inference(
                    zone_id          = zone_id,
                    dummy_mode       = dummy_mode,
                    dry_run          = dry_run,
                    dummy_temp       = temp,
                    dummy_humidity   = humidity,
                    dummy_moisture   = final_moisture,   # post-first-water reading
                )
                extra_volume = result.get("predicted_volume", 0.0)
                print(f"[GRADER] Second-pass volume: {extra_volume:.3f} L")
            except Exception as exc:
                print(f"[GRADER] Second-pass inference failed: {exc}")

        combined_volume = predicted_volume + extra_volume
        _log_row(zone_id, target_moisture, initial_moisture, temp, humidity,
                 deficit, combined_volume, label="Grade-B")
        return {
            "grade": "B",
            "error_pct":         round(error, 2),
            "extra_volume":      round(extra_volume, 3),
            "logged_volume":     round(combined_volume, 3),
            "description":       "Under-watered — second pass applied; combined volume logged.",
        }

    # ── Grade C: Over-watered ─────────────────────────────────────────────────
    penalised_volume = predicted_volume * OVER_WATER_PENALTY
    print(f"[GRADER] Over-watered — penalised volume: {penalised_volume:.3f} L "
          f"(×{OVER_WATER_PENALTY})")
    _log_row(zone_id, target_moisture, initial_moisture, temp, humidity,
             deficit, penalised_volume, label="Grade-C")
    return {
        "grade": "C",
        "error_pct":      round(error, 2),
        "penalty_factor": OVER_WATER_PENALTY,
        "logged_volume":  round(penalised_volume, 3),
        "description":    "Over-watered — penalised volume logged to correct AI bias.",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Grade the last irrigation event(s) and log corrected training rows."
    )
    parser.add_argument("--dummy",    action="store_true",
                        help="Skip real sensor reads (for testing)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Do not actuate valve in any second-pass")
    parser.add_argument("--moisture", type=float, default=None,
                        help="Override final moisture %% when in dummy mode (single-zone)")

    # ── Manual mode: supply all fields directly, no pending file needed ───────
    parser.add_argument("--manual",      action="store_true",
                        help="Grade a single event manually without a pending file")
    parser.add_argument("--zone",        type=int,   default=None)
    parser.add_argument("--target",      type=float, default=None,
                        help="Target crop moisture %%")
    parser.add_argument("--initial",     type=float, default=None,
                        help="Initial moisture %% before irrigation")
    parser.add_argument("--temp",        type=float, default=None)
    parser.add_argument("--humidity",    type=float, default=None)
    parser.add_argument("--deficit",     type=float, default=None,
                        help="Moisture_Deficit used for prediction")
    parser.add_argument("--predicted-volume", type=float, default=None,
                        dest="predicted_volume")
    parser.add_argument("--post-moisture",    type=float, default=None,
                        dest="post_moisture",
                        help="Actual post-irrigation moisture %%")
    args = parser.parse_args()

    # ── Manual single-event grade ─────────────────────────────────────────────
    if args.manual:
        required = ["zone", "target", "initial", "temp", "humidity",
                    "deficit", "predicted_volume", "post_moisture"]
        missing = [f"--{r.replace('_','-')}" for r in required
                   if getattr(args, r) is None]
        if missing:
            print(f"[GRADER] ERROR: --manual requires: {', '.join(missing)}",
                  file=sys.stderr)
            sys.exit(1)
        pending = {
            "zone_id":          args.zone,
            "target_moisture":  args.target,
            "initial_moisture": args.initial,
            "temp":             args.temp,
            "humidity":         args.humidity,
            "deficit":          args.deficit,
            "predicted_volume": args.predicted_volume,
            "flow_rate_lpm":    FLOW_RATE,
        }
        print(f"[GRADER] MANUAL grade for Zone {args.zone}")
        outcome = grade(args.post_moisture, pending,
                        dummy_mode=True, dry_run=args.dry_run)
        print(f"\n[GRADER] Grade: {outcome['grade']}  —  {outcome['description']}")
        sys.exit(0)

    # ── Normal mode: read pending sidecar ─────────────────────────────────────
    try:
        pending_list = _load_pending()
    except FileNotFoundError as exc:
        print(f"[GRADER] ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    for pending in pending_list:
        zone_id = pending["zone_id"]

        if args.dummy:
            if args.moisture is None:
                print("[GRADER] ERROR: --moisture required with --dummy",
                      file=sys.stderr)
                sys.exit(1)
            final_moisture = float(args.moisture)
            print(f"[GRADER] DUMMY  Zone {zone_id} final moisture={final_moisture} %")
        else:
            print(f"[GRADER] Reading post-watering moisture Zone {zone_id} …",
                  end=" ", flush=True)
            try:
                final_moisture = _read_soil_moisture_pct(zone_id)
                print(f"{final_moisture} %")
            except Exception as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                print(f"[GRADER] Skipping Zone {zone_id} — sensor error.")
                continue

        outcome = grade(final_moisture, pending,
                        dummy_mode=args.dummy, dry_run=args.dry_run)
        print(f"\n[GRADER] Grade: {outcome['grade']}  —  {outcome['description']}")

    _clear_pending()
    print(f"[GRADER] Pending sidecar cleared.")
    sys.exit(0)


if __name__ == "__main__":
    main()
