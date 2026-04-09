"""
Causwagan Norte Automated Irrigation System — Main App
Port 5000  (User Dashboard)
Port 5001  (Logs Viewer)

Hardware:
  ADS1115 @ I2C 0x48  →  A0=Zone1 (SEN0308 #1), A1=Zone2 (SEN0308 #2),
                          A2=Zone3 (SEN0193),     A3=Zone4 (Generic v1.2)
  BME280  @ I2C 0x76/0x77  →  Temperature, Humidity
  GPIO (BCM): 17=Valve1, 27=Valve2, 22=Valve3, 23=Valve4
  Pump SSR fired automatically via hardware diode interlock.
"""

import csv
import io
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH         = "/home/pi/irrigation_data.db"
MAIN_APP_PORT   = int(os.getenv("IRRIGATION_MAIN_PORT", "5000"))
LOG_VIEWER_PORT = int(os.getenv("IRRIGATION_LOG_VIEWER_PORT", "5001"))
APP_START_TIME  = datetime.now()

# Zone / GPIO
VALID_ZONES      = {1, 2, 3, 4}
RELAY_GPIO_MAP   = {1: 17, 2: 27, 3: 22, 4: 23}   # zone_id → BCM pin
RELAY_ACTIVE_LOW = os.getenv("IRRIGATION_RELAY_ACTIVE_LOW", "1") == "1"

# Sensor
BME280_ADDRESSES       = (0x76, 0x77)
REQUIRED_ADS_ADDRESSES = {0x48}
ADS_SAMPLES            = 10      # averages per channel read
ADS_SAMPLE_DELAY       = 0.05    # seconds between samples

# Auto-control
AUTO_CONTROL_ENABLED  = os.getenv("IRRIGATION_ENABLE_AUTO_CONTROL", "1") == "1"
SENSOR_POLL_SECONDS   = 300.0
CONTROL_LOOP_SECONDS  = float(os.getenv("IRRIGATION_CONTROL_LOOP_SECONDS", "10"))
AUTO_HYSTERESIS       = float(os.getenv("IRRIGATION_AUTO_HYSTERESIS", "3"))
AUTO_PREDICT_MINUTES  = float(os.getenv("IRRIGATION_AUTO_PREDICT_MINUTES", "20"))
AUTO_FAILSAFE_MINUTES = int(os.getenv("IRRIGATION_AUTO_FAILSAFE_MINUTES", "10"))

CROP_TARGETS = {
    "Corn":    30,
    "Cassava": 35,
    "Peanuts": 25,
    "Custom":  None,
}

# ─────────────────────────────────────────────────────────────────────────────
#  Flask apps
# ─────────────────────────────────────────────────────────────────────────────

app      = Flask(__name__)
logs_app = Flask("irrigation-logs", template_folder="templates", static_folder="static")

# ─────────────────────────────────────────────────────────────────────────────
#  Thread-safe runtime state
# ─────────────────────────────────────────────────────────────────────────────

_workers_lock     = threading.Lock()
_workers_started  = False

# Valve failsafe timers
_valve_lock         = threading.Lock()
_valve_timers       = {}   # zone_id → threading.Timer
_valve_manual_until = {}   # zone_id → datetime  (blocks auto_control)

# Sensor status
_sensor_lock   = threading.Lock()
_sensor_status = {
    "last_poll":      None,
    "last_success":   None,
    "last_error":     None,
    "bme280":         {"ok": False, "message": "Not read yet"},
    "ads1115_0x48":   {"ok": False, "message": "Not read yet"},
    "missing_inputs": [],
}

# GPIO / relay status
_gpio_lock    = threading.Lock()
_gpio_status  = {"available": False, "initialized": False, "message": "Not initialized"}
_GPIO_BACKEND = None   # RPi.GPIO module once loaded

# ─────────────────────────────────────────────────────────────────────────────
#  Database
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperature     REAL,
            humidity        REAL,
            soil_moisture_1 REAL,
            soil_moisture_2 REAL,
            soil_moisture_3 REAL,
            soil_moisture_4 REAL
        );
        CREATE TABLE IF NOT EXISTS valve_status (
            valve_id     INTEGER PRIMARY KEY,
            status       TEXT    NOT NULL DEFAULT 'OFF',
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS zone_profile (
            zone_id          INTEGER PRIMARY KEY,
            crop             TEXT    NOT NULL DEFAULT 'Corn',
            target_moisture  REAL,
            disabled         INTEGER NOT NULL DEFAULT 0,
            soil_baseline_id INTEGER,
            crop_target_id   INTEGER
        );
        CREATE TABLE IF NOT EXISTS control_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP,
            zone_id    INTEGER  NOT NULL,
            event_type TEXT     NOT NULL,
            source     TEXT     NOT NULL,
            detail     TEXT
        );
        CREATE TABLE IF NOT EXISTS soil_baseline (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            dry_voltage REAL    NOT NULL,
            wet_voltage REAL    NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crop_target (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL UNIQUE,
            target_voltage REAL    NOT NULL,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS testing_lock (
            zone_id      INTEGER PRIMARY KEY,
            locked_until DATETIME NOT NULL
        );
    """)

    # Seed valve rows and zone profiles once.
    conn.executemany(
        "INSERT OR IGNORE INTO valve_status (valve_id, status) VALUES (?, 'OFF')",
        [(z,) for z in VALID_ZONES],
    )
    for zone_id in VALID_ZONES:
        conn.execute(
            "INSERT OR IGNORE INTO zone_profile (zone_id, crop, target_moisture) VALUES (?,?,?)",
            (zone_id, "Corn", CROP_TARGETS["Corn"]),
        )

    # Idempotent column additions for older DBs.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(zone_profile)")}
    for col, defn in [
        ("disabled",         "INTEGER NOT NULL DEFAULT 0"),
        ("soil_baseline_id", "INTEGER"),
        ("crop_target_id",   "INTEGER"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE zone_profile ADD COLUMN {col} {defn}")

    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return False


def voltage_to_pct(voltage, dry_v, wet_v):
    """Two-point capacitive calibration: high voltage = dry, low voltage = wet."""
    if None in (voltage, dry_v, wet_v):
        return None
    span = float(dry_v) - float(wet_v)
    if abs(span) < 1e-9:
        return None
    return round(clamp(((float(dry_v) - float(voltage)) / span) * 100.0, 0.0, 100.0), 1)


def clamp_voltage(v):
    return clamp(float(v), 0.0, 6.5)

# ─────────────────────────────────────────────────────────────────────────────
#  Sensor status helpers
# ─────────────────────────────────────────────────────────────────────────────

def _update_sensor(partial):
    with _sensor_lock:
        _sensor_status.update(partial)


def _set_sensor_component(name, ok, message):
    with _sensor_lock:
        _sensor_status[name] = {"ok": bool(ok), "message": message}


def _get_sensor_snapshot():
    with _sensor_lock:
        return dict(_sensor_status)

# ─────────────────────────────────────────────────────────────────────────────
#  GPIO / Relay
# ─────────────────────────────────────────────────────────────────────────────

def _init_gpio():
    global _GPIO_BACKEND
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in RELAY_GPIO_MAP.values():
            # initial=HIGH keeps active-LOW relays OFF during setup.
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
        _GPIO_BACKEND = GPIO
        with _gpio_lock:
            _gpio_status.update({"available": True, "initialized": True,
                                  "message": "GPIO relay control active"})
        for zone_id in VALID_ZONES:
            _write_relay(zone_id, "OFF")
        # Sync DB to match hardware — all pins are now HIGH (relay OFF).
        conn = get_db()
        conn.execute(
            "UPDATE valve_status SET status='OFF', last_updated=CURRENT_TIMESTAMP"
        )
        conn.commit()
        conn.close()
        print("[GPIO] Ready.")
    except Exception as exc:
        with _gpio_lock:
            _gpio_status.update({"available": False, "initialized": False,
                                  "message": f"GPIO unavailable: {exc}"})


def _write_relay(zone_id, state):
    """Drive the GPIO pin for zone_id. No-op if GPIO is unavailable."""
    if _GPIO_BACKEND is None or zone_id not in RELAY_GPIO_MAP:
        return
    with _gpio_lock:
        if not _gpio_status["initialized"]:
            return
    pin     = RELAY_GPIO_MAP[zone_id]
    want_on = state == "ON"
    level   = (_GPIO_BACKEND.LOW if want_on else _GPIO_BACKEND.HIGH) if RELAY_ACTIVE_LOW \
              else (_GPIO_BACKEND.HIGH if want_on else _GPIO_BACKEND.LOW)
    _GPIO_BACKEND.output(pin, level)

# ─────────────────────────────────────────────────────────────────────────────
#  Valve state management
# ─────────────────────────────────────────────────────────────────────────────

def _log_event(zone_id, event_type, source, detail=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO control_events (zone_id, event_type, source, detail) VALUES (?,?,?,?)",
        (zone_id, event_type, source, detail),
    )
    conn.commit()
    conn.close()


def _failsafe_close(zone_id):
    """Called by threading.Timer when the failsafe expires."""
    _log_event(zone_id, "valve_off", "failsafe",
               f"Auto-close after {AUTO_FAILSAFE_MINUTES} min")
    try:
        _write_relay(zone_id, "OFF")
    except Exception as exc:
        print(f"[FAILSAFE] GPIO write failed for zone {zone_id}: {exc}")
    conn = get_db()
    conn.execute(
        "UPDATE valve_status SET status='OFF', last_updated=CURRENT_TIMESTAMP WHERE valve_id=?",
        (zone_id,),
    )
    conn.commit()
    conn.close()
    with _valve_lock:
        _valve_timers.pop(zone_id, None)
        _valve_manual_until.pop(zone_id, None)


def set_valve(zone_id, state, auto_close_minutes=None, source="manual"):
    """
    Activate or deactivate a zone valve.

    state              : "ON" | "OFF"
    auto_close_minutes : float | None — starts failsafe timer when ON
    source             : label stored in control_events
    """
    try:
        _write_relay(zone_id, state)
    except Exception as exc:
        print(f"[RELAY] Write failed zone {zone_id}: {exc}")

    conn = get_db()
    conn.execute(
        "UPDATE valve_status SET status=?, last_updated=CURRENT_TIMESTAMP WHERE valve_id=?",
        (state, zone_id),
    )
    conn.commit()
    conn.close()

    with _valve_lock:
        old = _valve_timers.pop(zone_id, None)
        if old:
            old.cancel()
        if state == "ON":
            if auto_close_minutes and auto_close_minutes > 0:
                t = threading.Timer(auto_close_minutes * 60, _failsafe_close, args=(zone_id,))
                t.daemon = True
                t.start()
                _valve_timers[zone_id]        = t
                _valve_manual_until[zone_id]  = datetime.now() + timedelta(minutes=auto_close_minutes)
            else:
                # No timer — protect from auto_control for 24 h until OFF.
                _valve_manual_until[zone_id] = datetime.now() + timedelta(hours=24)
        else:
            _valve_manual_until.pop(zone_id, None)

    detail = f"Valve {zone_id} → {state}"
    if state == "ON" and auto_close_minutes and auto_close_minutes > 0:
        detail += f" (failsafe {auto_close_minutes} min)"
    _log_event(zone_id, f"valve_{state.lower()}", source, detail)

# ─────────────────────────────────────────────────────────────────────────────
#  Hardware reading
# ─────────────────────────────────────────────────────────────────────────────

def _read_smoothed_channel(ads, pin):
    from adafruit_ads1x15.analog_in import AnalogIn
    ch = AnalogIn(ads, pin)
    samples = [float(ch.voltage) for _ in range(ADS_SAMPLES) if not time.sleep(ADS_SAMPLE_DELAY)]
    return sum(samples) / len(samples)


def read_hardware():
    """Full sensor snapshot with temperature, humidity, and 4 zone voltages/moisture."""
    result = {
        "temperature": None, "humidity": None,
        **{f"soil_probe_{z}": None         for z in VALID_ZONES},
        **{f"soil_probe_{z}_voltage": None  for z in VALID_ZONES},
    }
    missing = []

    try:
        import board, busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_bme280 import basic as adafruit_bme280
        from adafruit_ads1x15.analog_in import AnalogIn

        i2c = busio.I2C(board.SCL, board.SDA)

        # BME280
        bme_ok = False
        for addr in BME280_ADDRESSES:
            try:
                bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
                result["temperature"] = round(float(bme.temperature), 1)
                result["humidity"]    = round(float(bme.humidity),    1)
                _set_sensor_component("bme280", True, f"BME280 @ {hex(addr)}")
                bme_ok = True
                break
            except Exception:
                pass
        if not bme_ok:
            missing.append("BME280@0x76/0x77")
            _set_sensor_component("bme280", False, "BME280 not found at 0x76 or 0x77")

        # ADS1115 — one zone per channel (0=Zone1 … 3=Zone4)
        try:
            ads = ADS.ADS1115(i2c, address=0x48)
            for zone_id in VALID_ZONES:
                ch = zone_id - 1
                # Discard first read — ADS MUX settling artefact.
                _ = AnalogIn(ads, ch).voltage
                time.sleep(0.02)
                v = _read_smoothed_channel(ads, ch)
                result[f"soil_probe_{zone_id}_voltage"] = round(v, 4)
                # Uncalibrated raw % — replaced by voltage_to_pct in poll loop when baseline exists.
                result[f"soil_probe_{zone_id}"] = round(clamp((1.0 - v / 3.3) * 100.0, 0.0, 100.0), 1)
            _set_sensor_component("ads1115_0x48", True, "ADS1115 @ 0x48")
        except Exception as exc:
            missing.append("ADS1115@0x48")
            _set_sensor_component("ads1115_0x48", False, f"ADS1115 @ 0x48: {exc}")

    except Exception as exc:
        _set_sensor_component("bme280",       False, "I2C bus unavailable")
        _set_sensor_component("ads1115_0x48", False, "I2C bus unavailable")
        missing = ["I2C bus unavailable"]
        _update_sensor({"last_error": f"I2C init: {exc}"})

    with _sensor_lock:
        _sensor_status["missing_inputs"] = missing
    return result

# ─────────────────────────────────────────────────────────────────────────────
#  Background workers
# ─────────────────────────────────────────────────────────────────────────────

def _sensor_poll_loop():
    while True:
        now = datetime.now().isoformat()
        try:
            snapshot = read_hardware()
            conn     = get_db()

            zones     = conn.execute(
                "SELECT zone_id, disabled, soil_baseline_id FROM zone_profile ORDER BY zone_id"
            ).fetchall()
            baselines = {r["id"]: r for r in conn.execute(
                "SELECT id, dry_voltage, wet_voltage FROM soil_baseline"
            ).fetchall()}

            moisture = {}
            for z in zones:
                zid = z["zone_id"]
                if z["disabled"]:
                    moisture[zid] = None
                    continue
                vol = snapshot.get(f"soil_probe_{zid}_voltage")
                bl  = baselines.get(z["soil_baseline_id"])
                moisture[zid] = (
                    voltage_to_pct(vol, bl["dry_voltage"], bl["wet_voltage"]) if bl
                    else snapshot.get(f"soil_probe_{zid}")
                )

            conn.execute(
                """INSERT INTO sensor_data
                   (temperature, humidity,
                    soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4)
                   VALUES (?,?,?,?,?,?)""",
                (snapshot["temperature"], snapshot["humidity"],
                 moisture.get(1), moisture.get(2), moisture.get(3), moisture.get(4)),
            )
            conn.commit()
            conn.close()

            missing = _get_sensor_snapshot().get("missing_inputs", [])
            _update_sensor({"last_poll": now,
                             **({"last_success": now, "last_error": None} if not missing
                                else {"last_error": f"Missing: {', '.join(missing)}"})})
        except Exception as exc:
            _update_sensor({"last_poll": now, "last_error": f"Poll error: {exc}"})
            print(f"[SENSOR] {exc}")

        time.sleep(SENSOR_POLL_SECONDS)


def _predict_moisture(rows, zone_id, minutes_ahead):
    """Linear extrapolation over recent readings. Returns None if not enough data."""
    key    = f"soil_moisture_{zone_id}"
    points = [(parse_ts(r["timestamp"]), float(r[key]))
              for r in rows if r[key] is not None and parse_ts(r["timestamp"])]
    if len(points) < 4:
        return None
    t0   = points[0][0]
    xs   = [(ts - t0).total_seconds() / 60 for ts, _ in points]
    ys   = [m for _, m in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx   = sum((x - mx) ** 2 for x in xs)
    if vx == 0:
        return ys[-1]
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return clamp(slope * (xs[-1] + minutes_ahead) + (my - slope * mx), 0.0, 100.0)


def _auto_control_loop():
    while True:
        time.sleep(CONTROL_LOOP_SECONDS)
        if not AUTO_CONTROL_ENABLED:
            continue
        try:
            conn   = get_db()
            rows   = conn.execute(
                """SELECT timestamp, soil_moisture_1, soil_moisture_2,
                          soil_moisture_3, soil_moisture_4
                   FROM sensor_data ORDER BY timestamp DESC LIMIT 48"""
            ).fetchall()
            zones  = conn.execute(
                "SELECT zone_id, target_moisture, disabled FROM zone_profile ORDER BY zone_id"
            ).fetchall()
            valves = {r["valve_id"]: r["status"]
                      for r in conn.execute("SELECT valve_id, status FROM valve_status").fetchall()}
            conn.close()

            if not rows:
                continue
            latest  = rows[0]
            history = list(reversed(rows))

            # Fetch any active testing locks (set by thesis_dashboard stress tests).
            locks = {r["zone_id"] for r in conn.execute(
                "SELECT zone_id FROM testing_lock WHERE locked_until > datetime('now')"
            ).fetchall()}
            conn.close()

            for z in zones:
                zid     = z["zone_id"]
                target  = z["target_moisture"]
                current = latest[f"soil_moisture_{zid}"]
                if z["disabled"] or target is None or current is None:
                    continue
                if zid in locks:
                    continue   # Defer to thesis_dashboard stress test
                with _valve_lock:
                    guard = _valve_manual_until.get(zid)
                if guard and guard > datetime.now():
                    continue
                predicted = _predict_moisture(history, zid, AUTO_PREDICT_MINUTES) or float(current)
                status    = valves.get(zid, "OFF")
                if predicted < target - AUTO_HYSTERESIS and status == "OFF":
                    set_valve(zid, "ON", auto_close_minutes=AUTO_FAILSAFE_MINUTES, source="auto")
                elif float(current) >= target + AUTO_HYSTERESIS and status == "ON":
                    set_valve(zid, "OFF", source="auto")
        except Exception as exc:
            print(f"[AUTO] {exc}")


def _run_logs_server():
    logs_app.run(host="0.0.0.0", port=LOG_VIEWER_PORT, debug=False, use_reloader=False)


def start_workers():
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        _init_gpio()
        threading.Thread(target=_sensor_poll_loop, daemon=True, name="sensor-loop").start()
        if AUTO_CONTROL_ENABLED:
            threading.Thread(target=_auto_control_loop, daemon=True, name="auto-control").start()
        threading.Thread(target=_run_logs_server, daemon=True, name="logs-server").start()
        _workers_started = True

# ─────────────────────────────────────────────────────────────────────────────
#  Payload builders
# ─────────────────────────────────────────────────────────────────────────────

def _baseline_dict(row):
    return {"id": row["id"], "name": row["name"],
            "dry_voltage": row["dry_voltage"], "wet_voltage": row["wet_voltage"],
            "created_at": row["created_at"]}


def _crop_target_dict(row):
    return {"id": row["id"], "name": row["name"],
            "target_voltage": row["target_voltage"], "created_at": row["created_at"]}


def _zone_dict(row):
    return {k: row[k] for k in row.keys()}


def _dashboard_payload():
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
        with _valve_lock:
            until = _valve_manual_until.get(zid)
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
            "valve_status":        valve_map.get(zid, "OFF"),
            "manual_until":        until.isoformat() if until else None,
        })

    valve_payload = []
    with _valve_lock:
        for v in valves:
            until = _valve_manual_until.get(v["valve_id"])
            valve_payload.append({
                "valve_id":     v["valve_id"],
                "status":       v["status"],
                "water_flowing": v["status"] == "ON",
                "manual_until": until.isoformat() if until else None,
            })

    uptime = datetime.now() - APP_START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    sensor_snap = _get_sensor_snapshot()
    with _gpio_lock:
        gpio_snap = dict(_gpio_status)

    return {
        "environment": {
            "temperature": latest["temperature"] if latest else None,
            "humidity":    latest["humidity"]    if latest else None,
        },
        "system_health": {
            "hotspot_status": _hotspot_status(),
            "db_uptime":      f"{h}h {rem // 60}m",
            "sensor_status":  sensor_snap,
            "relay_status":   gpio_snap,
        },
        "zones":          zone_payload,
        "valves":         valve_payload,
        "soil_baselines": [_baseline_dict(r) for r in baselines],
        "crop_targets":   [_crop_target_dict(r) for r in crop_targets],
        "runtime": {
            "sensor_poll_seconds":  SENSOR_POLL_SECONDS,
            "auto_control_enabled": AUTO_CONTROL_ENABLED,
            "predict_minutes":      AUTO_PREDICT_MINUTES,
            "hysteresis":           AUTO_HYSTERESIS,
        },
    }


def _hotspot_status():
    try:
        r = subprocess.run(["systemctl", "is-active", "hostapd"],
                           capture_output=True, text=True, timeout=2, check=False)
        return "UP" if r.stdout.strip() == "active" else "DOWN"
    except Exception:
        return "UNKNOWN"


def _capture_live_voltage(channel):
    import board, busio
    import adafruit_ads1x15.ads1115 as ADS
    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, address=0x48)
    return round(_read_smoothed_channel(ads, channel), 4)

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — pages
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("home_page"))

@app.route("/home")
def home_page():
    return render_template("home.html", active_page="home")

@app.route("/zone/<int:zone_id>")
def zone_detail_page(zone_id):
    if zone_id not in VALID_ZONES:
        return redirect(url_for("home_page"))
    return render_template("zone_detail.html", active_page="home", zone_id=zone_id)

@app.route("/zones")
def zones_page():
    return redirect(url_for("home_page"))

@app.route("/testing")
def testing_page():
    return render_template("testing.html", active_page="testing")

@app.route("/analytics")
def analytics_page():
    return render_template("analytics.html", active_page="analytics")

@app.route("/hardware")
def hardware_page():
    return render_template("hardware.html", active_page="hardware")

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — API: data
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(_dashboard_payload())


@app.route("/api/trends")
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


@app.route("/api/zone/<int:zone_id>/history")
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
        "zone_id":        zone_id,
        "history":        [dict(r) for r in rows],
        "control_history":[dict(e) for e in events],
    })


@app.route("/api/system/status")
def api_system_status():
    with _valve_lock:
        overrides = {zid: until.isoformat()
                     for zid, until in _valve_manual_until.items()
                     if until > datetime.now()}
    with _gpio_lock:
        gpio = dict(_gpio_status)
    return jsonify({
        "sensor_mode":          "hardware-only",
        "sensor_status":        _get_sensor_snapshot(),
        "relay_status":         gpio,
        "auto_control_enabled": AUTO_CONTROL_ENABLED,
        "manual_overrides":     overrides,
        "control": {
            "predict_minutes":      AUTO_PREDICT_MINUTES,
            "hysteresis":           AUTO_HYSTERESIS,
            "failsafe_minutes":     AUTO_FAILSAFE_MINUTES,
            "control_loop_seconds": CONTROL_LOOP_SECONDS,
        },
    })


@app.route("/export/csv")
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
    w.writerow(["timestamp","temperature","humidity",
                "soil_moisture_1","soil_moisture_2","soil_moisture_3","soil_moisture_4"])
    w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=irrigation_records.csv"})

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — API: valve control
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/toggle_valve/<int:valve_id>", methods=["POST"])
def toggle_valve(valve_id):
    conn = get_db()
    row  = conn.execute("SELECT status FROM valve_status WHERE valve_id=?", (valve_id,)).fetchone()
    conn.close()
    if not row:
        return redirect(url_for("home_page"))
    new_state  = "ON" if row["status"] == "OFF" else "OFF"
    auto_close = request.form.get("auto_close_minutes", type=int)
    set_valve(valve_id, new_state, auto_close if new_state == "ON" else None)
    return redirect(url_for("home_page"))


@app.route("/api/valve/<int:valve_id>", methods=["POST"])
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
            acm = int(acm)
        except (TypeError, ValueError):
            return jsonify({"error": "auto_close_minutes must be an integer"}), 400
        if acm < 0:
            return jsonify({"error": "auto_close_minutes cannot be negative"}), 400
    set_valve(valve_id, state, acm if state == "ON" else None, source="manual")
    return jsonify({"success": True, "valve_id": valve_id, "state": state})

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — API: calibration
# ─────────────────────────────────────────────────────────────────────────────

_CH_MAP = {"A0": 0, "A1": 1, "A2": 2, "A3": 3}


@app.route("/api/calibration/capture-live", methods=["POST"])
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


@app.route("/api/calibration/baseline", methods=["POST"])
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


@app.route("/api/calibration/baseline/<int:baseline_id>", methods=["DELETE"])
def api_delete_baseline(baseline_id):
    conn = get_db()
    if not conn.execute("SELECT id FROM soil_baseline WHERE id=?", (baseline_id,)).fetchone():
        conn.close()
        return jsonify({"error": "Baseline not found"}), 404
    conn.execute("UPDATE zone_profile SET soil_baseline_id=NULL WHERE soil_baseline_id=?", (baseline_id,))
    conn.execute("DELETE FROM soil_baseline WHERE id=?", (baseline_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted_id": baseline_id})


@app.route("/api/calibration/crop-target", methods=["POST"])
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


@app.route("/api/calibration/crop-target/<int:target_id>", methods=["DELETE"])
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

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — API: zone profiles
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/zone/<int:zone_id>/mapping", methods=["POST"])
def api_zone_mapping(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    p  = request.get_json(silent=True) or {}
    bl = int(p["soil_baseline_id"]) if p.get("soil_baseline_id") not in (None, "") else None
    ct = int(p["crop_target_id"])   if p.get("crop_target_id")   not in (None, "") else None

    conn = get_db()
    bls  = {r["id"]: r for r in conn.execute("SELECT id,dry_voltage,wet_voltage FROM soil_baseline").fetchall()}
    cts  = {r["id"]: r for r in conn.execute("SELECT id,target_voltage FROM crop_target").fetchall()}

    if bl is not None and bl not in bls:
        conn.close(); return jsonify({"error": "Soil baseline not found"}), 400
    if ct is not None and ct not in cts:
        conn.close(); return jsonify({"error": "Crop target not found"}), 400

    target_moisture = voltage_to_pct(
        cts[ct]["target_voltage"], bls[bl]["dry_voltage"], bls[bl]["wet_voltage"]
    ) if bl and ct else None

    conn.execute(
        "UPDATE zone_profile SET soil_baseline_id=?,crop_target_id=?,target_moisture=? WHERE zone_id=?",
        (bl, ct, target_moisture, zone_id),
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


@app.route("/api/zone/<int:zone_id>/profile", methods=["POST"])
def api_zone_profile(zone_id):
    if zone_id not in VALID_ZONES:
        return jsonify({"error": "Invalid zone_id"}), 400
    p    = request.get_json(silent=True) or {}
    crop = p.get("crop", "Corn")
    if crop not in CROP_TARGETS:
        return jsonify({"error": "Invalid crop"}), 400
    if crop == "Custom":
        tm = p.get("target_moisture")
        if tm is None:
            return jsonify({"error": "Custom crop requires target_moisture"}), 400
    else:
        tm = CROP_TARGETS[crop]
    conn = get_db()
    conn.execute("UPDATE zone_profile SET crop=?,target_moisture=? WHERE zone_id=?",
                 (crop, tm, zone_id))
    conn.commit()
    z = conn.execute("SELECT * FROM zone_profile WHERE zone_id=?", (zone_id,)).fetchone()
    conn.close()
    return jsonify({"zone": _zone_dict(z)})


@app.route("/api/zone/<int:zone_id>/disable", methods=["POST"])
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
    _log_event(zone_id,
               "zone_disabled_manual" if disabled else "zone_enabled_manual",
               "manual-config")
    return jsonify({"zone": _zone_dict(z)})

# ─────────────────────────────────────────────────────────────────────────────
#  Routes — API: diagnostics
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/diagnostics/i2c-scan", methods=["POST"])
def api_i2c_scan():
    addresses, error = [], None
    try:
        import board, busio
        i2c = busio.I2C(board.SCL, board.SDA)
        while not i2c.try_lock():
            pass
        try:
            addresses = [hex(a) for a in i2c.scan()]
        finally:
            i2c.unlock()
    except Exception as exc:
        error = str(exc)
    required = [hex(a) for a in sorted(REQUIRED_ADS_ADDRESSES)]
    missing  = [a for a in required if a not in addresses]
    if "0x76" not in addresses and "0x77" not in addresses:
        missing.append("0x76/0x77")
    if missing and not error:
        error = f"Missing: {', '.join(missing)}"
    return jsonify({"addresses": addresses, "missing": missing, "error": error})


@app.route("/api/diagnostics/relay-test", methods=["POST"])
def api_relay_test():
    for zid in sorted(VALID_ZONES):
        set_valve(zid, "ON",  source="diagnostic")
        threading.Event().wait(0.2)
        set_valve(zid, "OFF", source="diagnostic")
    with _gpio_lock:
        ready = _gpio_status["initialized"]
        msg   = _gpio_status["message"]
    return jsonify({"success": True,
                    "message": "Sequential relay test complete." + ("" if ready else f" (No GPIO: {msg})")})


@app.route("/api/system/shutdown", methods=["POST"])
def api_shutdown():
    for cmd in [["sudo","shutdown","-h","now"], ["shutdown","-h","now"]]:
        try:
            subprocess.Popen(cmd)
            return jsonify({"success": True, "message": "Shutdown started."})
        except Exception:
            pass
    return jsonify({"error": "Shutdown command failed"}), 500

# ─────────────────────────────────────────────────────────────────────────────
#  Logs viewer app (port 5001)
# ─────────────────────────────────────────────────────────────────────────────

@logs_app.route("/")
def logs_page():
    limit = clamp(request.args.get("limit", default=200, type=int) or 200, 20, 500)
    conn  = get_db()
    sensor_rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  temperature, humidity,
                  soil_moisture_1, soil_moisture_2, soil_moisture_3, soil_moisture_4
           FROM sensor_data ORDER BY timestamp DESC LIMIT ?""", (limit,)
    ).fetchall()
    event_rows = conn.execute(
        """SELECT datetime(timestamp,'localtime') AS timestamp,
                  zone_id, event_type, source, detail
           FROM control_events ORDER BY timestamp DESC LIMIT ?""", (limit,)
    ).fetchall()
    conn.close()
    return render_template("logs_viewer.html",
                           sensor_rows=sensor_rows, event_rows=event_rows,
                           limit=limit, main_port=MAIN_APP_PORT,
                           log_port=LOG_VIEWER_PORT,
                           cleared=request.args.get("cleared"))


@logs_app.route("/delete-all", methods=["POST"])
def logs_delete_all():
    conn = get_db()
    conn.execute("DELETE FROM sensor_data")
    conn.execute("DELETE FROM control_events")
    conn.commit()
    conn.close()
    return redirect(url_for("logs_page", cleared="1"))

# ─────────────────────────────────────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────────────────────────────────────

initialize_db()
start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=MAIN_APP_PORT, debug=True, use_reloader=False)
