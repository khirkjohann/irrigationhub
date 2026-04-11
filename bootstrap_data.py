"""
Module A — Bootstrap Data Generator
====================================
Causwagan Norte Irrigation System — ML Pipeline

Manual CLI utility to generate the initial training_data.csv rows in a
single day, before the Random Forest has ever been trained.

Usage:
    python3 bootstrap_data.py [--zone ZONE_ID] [--dummy]

    --zone   1-4    Only bootstrap one specific zone (default: loops all 4)
    --dummy         Skip real sensor reads; prompt for all values manually

Workflow per row:
  1. Prompt: Zone, Target_Crop_Moisture, Initial_Moisture, Temp, Humidity,
             Volume_Applied
  2. Wait 10 minutes (or SKIP_WAIT=1 env var for quick testing)
  3. Prompt: Post_Moisture
  4. Math:
       Moisture_Shift   = Post_Moisture − Initial_Moisture
       Moisture_Deficit = max(0.0, Moisture_Shift)  ← what the water fixed
       Target_Volume    = Volume_Applied
  5. Append one row to training_data.csv

Run it multiple times per zone to build a rich initial dataset.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

TRAINING_CSV   = os.getenv("IRRIGATION_TRAINING_CSV", "/home/pi/training_data.csv")
WAIT_SECONDS   = int(os.getenv("BOOTSTRAP_WAIT_SECONDS", "600"))   # 10 minutes
SKIP_WAIT      = os.getenv("SKIP_WAIT", "0") == "1"               # set for quick dev

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

VALID_ZONES = {1, 2, 3, 4}

CROP_DEFAULTS = {
    1: 50.0,   # Zone 1 — Generic probe
    2: 55.0,   # Zone 2 — SEN0308 #2
    3: 55.0,   # Zone 3 — SEN0308 #1
    4: 60.0,   # Zone 4 — SEN0193 premium
}


# ─────────────────────────────────────────────────────────────────────────────
#  CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv():
    """Create training_data.csv with header row if it does not exist."""
    if not os.path.exists(TRAINING_CSV):
        with open(TRAINING_CSV, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADER)
        print(f"[BOOTSTRAP] Created {TRAINING_CSV}")


def _append_row(row: dict):
    """Append a single data row to training_data.csv."""
    _ensure_csv()
    with open(TRAINING_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writerow(row)
    print(f"[BOOTSTRAP] Row written → {row}")


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware abstraction (abstracted so dummy mode works without I2C)
# ─────────────────────────────────────────────────────────────────────────────

def _read_bme280():
    """
    Read temperature and humidity from the BME280.
    Returns (temp_c, humidity_pct) or (None, None) on failure.
    """
    try:
        import board
        import busio
        from adafruit_bme280 import basic as adafruit_bme280

        i2c = busio.I2C(board.SCL, board.SDA)
        for addr in (0x76, 0x77):
            try:
                bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
                return round(float(bme.temperature), 1), round(float(bme.humidity), 1)
            except Exception:
                pass
        i2c.deinit()
    except Exception as exc:
        print(f"[BOOTSTRAP] BME280 read failed: {exc}")
    return None, None


def _read_soil_moisture_pct(zone_id: int):
    """
    Read calibrated soil moisture (%) for a zone via ADS1115.
    Returns float or None on failure.
    Reads the main app's soil_baseline from the DB to apply calibration.
    """
    try:
        import sqlite3
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        # Channel index: zone 1 → ch 0, zone 2 → ch 1, …
        ch = zone_id - 1

        # Load baseline from main DB
        db_path = "/home/pi/irrigation_data.db"
        conn = sqlite3.connect(db_path, timeout=5)
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
        # Discard first read (mux settling)
        _ = AnalogIn(ads, ch).voltage
        time.sleep(0.05)
        samples = [float(AnalogIn(ads, ch).voltage) for _ in range(8)]
        voltage = sum(samples) / len(samples)
        i2c.deinit()

        if dry_v is not None and wet_v is not None and (dry_v - wet_v) != 0:
            pct = (dry_v - voltage) / (dry_v - wet_v) * 100.0
            return round(max(0.0, min(100.0, pct)), 1)
        # Fallback: raw linear map assuming 3.3 V = 0 %, 0 V = 100 %
        return round(max(0.0, min(100.0, (1.0 - voltage / 3.3) * 100.0)), 1)
    except Exception as exc:
        print(f"[BOOTSTRAP] Soil read zone {zone_id} failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Input helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ask_float(prompt: str, lo: float, hi: float, default: float = None) -> float:
    while True:
        hint = f" [default {default}]" if default is not None else ""
        raw = input(f"{prompt}{hint}: ").strip()
        if raw == "" and default is not None:
            return float(default)
        try:
            val = float(raw)
            if lo <= val <= hi:
                return val
            print(f"  ✗ Enter a number between {lo} and {hi}.")
        except ValueError:
            print("  ✗ Please enter a numeric value.")


def _ask_int(prompt: str, choices: set, default: int = None) -> int:
    while True:
        hint = f" [default {default}]" if default is not None else ""
        raw = input(f"{prompt} ({'/'.join(str(c) for c in sorted(choices))}){hint}: ").strip()
        if raw == "" and default is not None:
            return int(default)
        try:
            val = int(raw)
            if val in choices:
                return val
            print(f"  ✗ Choose from {sorted(choices)}.")
        except ValueError:
            print("  ✗ Please enter an integer.")


# ─────────────────────────────────────────────────────────────────────────────
#  Core bootstrap session
# ─────────────────────────────────────────────────────────────────────────────

def run_session(zone_id: int, dummy_mode: bool):
    """
    Execute one full bootstrap session for a zone.
    Returns True on success, False if the user aborts.
    """
    print(f"\n{'═'*58}")
    print(f"  Bootstrap Session  —  Zone {zone_id}")
    print(f"{'═'*58}")

    # ── STEP 1: Collect pre-watering inputs ──────────────────────────────────
    target_crop_moisture = _ask_float(
        "Target crop moisture (%)", 0.0, 100.0,
        default=CROP_DEFAULTS.get(zone_id, 50.0),
    )

    if dummy_mode:
        initial_moisture = _ask_float("Initial moisture (%) — enter manually", 0.0, 100.0)
        temp             = _ask_float("Temperature (°C)", -10.0, 60.0, default=28.0)
        humidity         = _ask_float("Humidity (%)", 0.0, 100.0, default=70.0)
    else:
        print("  Reading BME280 …", end=" ", flush=True)
        temp, humidity = _read_bme280()
        if temp is None:
            print("FAILED — enter manually.")
            temp     = _ask_float("Temperature (°C)", -10.0, 60.0)
            humidity = _ask_float("Humidity (%)", 0.0, 100.0)
        else:
            print(f"Temp={temp} °C, Humidity={humidity} %")

        print(f"  Reading soil moisture Zone {zone_id} …", end=" ", flush=True)
        initial_moisture = _read_soil_moisture_pct(zone_id)
        if initial_moisture is None:
            print("FAILED — enter manually.")
            initial_moisture = _ask_float("Initial moisture (%) — enter manually", 0.0, 100.0)
        else:
            print(f"{initial_moisture} %")

    volume_applied = _ask_float("Volume applied (Litres)", 0.0, 100.0)

    # ── STEP 2: Computed pre-check ────────────────────────────────────────────
    pre_deficit = max(0.0, target_crop_moisture - initial_moisture)
    print(f"\n  Pre-watering deficit: {pre_deficit:.1f} %")
    if pre_deficit == 0.0 and volume_applied > 0.0:
        print("  ⚠  Soil is already at or above target — this row will teach"
              " the AI not to over-water.")

    # ── STEP 3: Wait ─────────────────────────────────────────────────────────
    if SKIP_WAIT:
        print("  [SKIP_WAIT=1] Skipping 10-minute wait.")
    else:
        end_time = time.time() + WAIT_SECONDS
        print(f"\n  Waiting {WAIT_SECONDS // 60} min for soil to absorb …"
              "  (Ctrl-C to abort)")
        try:
            while True:
                remaining = int(end_time - time.time())
                if remaining <= 0:
                    break
                mins, secs = divmod(remaining, 60)
                print(f"\r  Time remaining: {mins:02d}:{secs:02d}", end="", flush=True)
                time.sleep(1)
            print()
        except KeyboardInterrupt:
            print("\n  ✗ Aborted — no row written.")
            return False

    # ── STEP 4: Collect post-watering moisture ───────────────────────────────
    if dummy_mode:
        post_moisture = _ask_float("Post moisture (%) — enter manually", 0.0, 100.0)
    else:
        print(f"  Reading post-moisture Zone {zone_id} …", end=" ", flush=True)
        post_moisture = _read_soil_moisture_pct(zone_id)
        if post_moisture is None:
            print("FAILED — enter manually.")
            post_moisture = _ask_float("Post moisture (%) — enter manually", 0.0, 100.0)
        else:
            print(f"{post_moisture} %")

    # ── STEP 5: Compute row values ────────────────────────────────────────────
    moisture_shift   = post_moisture - initial_moisture
    moisture_deficit = max(0.0, moisture_shift)   # what the water corrected
    target_volume    = round(volume_applied, 4)

    print(f"\n  Moisture shift  : {moisture_shift:+.1f} %")
    print(f"  Moisture deficit: {moisture_deficit:.1f} %  (logged)")
    print(f"  Target volume   : {target_volume} L  (logged)")

    if moisture_shift < 0:
        print("  ⚠  Post-moisture < initial. Water may not have reached the sensor yet,"
              " or there is a calibration issue. This row is still logged.")

    # ── STEP 6: Write row ─────────────────────────────────────────────────────
    row = {
        "Timestamp":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Zone_ID":              zone_id,
        "Target_Crop_Moisture": round(target_crop_moisture, 2),
        "Initial_Moisture":     round(initial_moisture, 2),
        "Temp":                 round(temp, 2),
        "Humidity":             round(humidity, 2),
        "Moisture_Deficit":     round(moisture_deficit, 4),
        "Target_Volume":        target_volume,
    }
    _append_row(row)
    print("  ✓ Row appended to training_data.csv")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap initial ML training rows for the irrigation system."
    )
    parser.add_argument(
        "--zone", type=int, choices=[1, 2, 3, 4],
        help="Bootstrap a single zone (default: prompt for each session)"
    )
    parser.add_argument(
        "--dummy", action="store_true",
        help="Skip real sensor reads; enter all values manually (for testing)"
    )
    args = parser.parse_args()

    _ensure_csv()

    print("\nCauswagan Norte — ML Bootstrap Data Generator")
    print(f"Training CSV : {TRAINING_CSV}")
    if SKIP_WAIT:
        print("Mode         : FAST (SKIP_WAIT=1 — 10-min timer bypassed)")
    if args.dummy:
        print("Mode         : DUMMY (no hardware reads)")
    print()

    try:
        while True:
            if args.zone:
                zone = args.zone
            else:
                zone = _ask_int("Zone to bootstrap", VALID_ZONES)

            run_session(zone, dummy_mode=args.dummy)

            again = input("\n  Run another session? (y/n) [y]: ").strip().lower()
            if again not in ("", "y", "yes"):
                break

    except (KeyboardInterrupt, EOFError):
        pass

    # Count rows written (excluding header)
    with open(TRAINING_CSV, newline="") as fh:
        row_count = sum(1 for _ in csv.reader(fh)) - 1
    print(f"\n  Done. training_data.csv now has {row_count} data row(s).")
    print("  Run cron_retrain.py when you have ≥20 rows to train the model.\n")


if __name__ == "__main__":
    main()
