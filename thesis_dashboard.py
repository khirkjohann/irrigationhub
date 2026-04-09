#!/usr/bin/env python3
"""
=============================================================================
  Thesis Testing & Diagnostics Dashboard  —  thesis_dashboard.py
  Standalone Flask server on Port 5002
  (Override with env var: THESIS_PORT=5001 python thesis_dashboard.py)
=============================================================================

  PORT NOTE:
    The main irrigation app (app.py) already binds a log-viewer sub-server
    on port 5001 by default.  To avoid a conflict run either:
      a) Redirect the log-viewer:  IRRIGATION_LOG_VIEWER_PORT=5003 python app.py
      b) Change this port:         THESIS_PORT=5001 python thesis_dashboard.py
    Both apps share the same GPIO pins and I2C bus.  Do not run both at the
    same time while performing GPIO-intensive tests.

  ACTIVATE THE VIRTUALENV FIRST:
    source /home/pi/irrigation_env/bin/activate
    python thesis_dashboard.py

  HARDWARE MAP (matches main app):
    ADS1115 @ I2C 0x48:
      A0 → SEN0308 #1    (Zone 1 — Heavy-Duty comparative sensor A)
      A1 → SEN0308 #2    (Zone 2 — Heavy-Duty comparative sensor B)
      A2 → SEN0193       (Zone 3 — Premium capacitive sensor)
      A3 → Generic v1.2  (Zone 4 — Low-cost control sensor)

    BME280  @ I2C 0x76 (or 0x77 auto-fallback)
      → Temperature (°C) and Relative Humidity (%)

    GPIO Relay Module (BCM numbering, active-LOW opto-isolated board):
      BCM 17 → Solenoid Valve 1 — Zone 1 (12V)
      BCM 27 → Solenoid Valve 2 — Zone 2 (12V)
      BCM 22 → Solenoid Valve 3 — Zone 3 (12V)
      BCM 23 → Solenoid Valve 4 — Zone 4 (12V)
      Pump SSR fires automatically via hardware diode interlock.

  FOUR THESIS PANELS:
    1. Calibration Panel     — For Table 3 (Raw-to-% conversion baselines)
    2. Hardware Stress Test  — For Table 5 (Step-response, jitter, drift)
    3. Relay & Queue Override — For Table 1 (Sequential irrigation logic)
    4. ML Volumetric Test    — For Table 2 (Regression volume/duration output)
=============================================================================
"""

import json
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, render_template, request, stream_with_context


# ─────────────────────────────────────────────────────────────────────────────
#  App Configuration
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")

# Port for this standalone dashboard (default 5002 to avoid conflicts).
DASHBOARD_PORT = int(os.getenv("THESIS_PORT", "5002"))

# JSON file that persists the dry/wet voltage calibration baselines.
CALIBRATION_FILE = "/home/pi/thesis_calibration.json"

# Path to the main irrigation app's SQLite database.
# When both dry and wet voltages are captured for a channel, the baseline is
# automatically upserted here so it appears in Zone Settings without any
# manual re-entry in the main app.
MAIN_DB_PATH = "/home/pi/irrigation_data.db"

# Optional: path to a trained scikit-learn / joblib model.
# If the file exists it will be loaded; otherwise the built-in linear
# regression formula is used.
ML_MODEL_PATH = os.getenv("THESIS_ML_MODEL", "/home/pi/irrigation_model.joblib")


# ─────────────────────────────────────────────────────────────────────────────
#  Sensor Constants
# ─────────────────────────────────────────────────────────────────────────────

# Human-readable name for each ADS1115 channel — used in tables & chart legend.
CHANNEL_LABELS = {
    0: "SEN0308 #1",    # Heavy-duty comparative sensor A
    1: "SEN0308 #2",    # Heavy-duty comparative sensor B
    2: "SEN0193",       # Premium capacitive sensor
    3: "Generic v1.2",  # Low-cost control / baseline sensor
}

ADS_I2C_ADDRESS   = 0x48          # Single ADS1115 on the I2C bus
BME280_ADDRESSES  = (0x76, 0x77)  # Try both common BME280 addresses

# Number of voltage samples to average per channel read (reduces ADC noise).
ADS_SMOOTHING_SAMPLES = 8
# Pause between each individual sample (seconds).
ADS_SMOOTHING_DELAY   = 0.025


# ─────────────────────────────────────────────────────────────────────────────
#  Relay / GPIO Constants
# ─────────────────────────────────────────────────────────────────────────────

# BCM GPIO pin for each relay channel.  Matches the main app's RELAY_GPIO_MAP
# so that both applications refer to the same physical pins.
# Board pins 11,13,15,16 → BCM 17,27,22,23  (4 zone solenoid valves).
# The pump is wired in series via a diode and fires automatically with any valve.
RELAY_GPIO_MAP = {
    "valve1": 17,   # BCM 17 → Zone 1 solenoid valve
    "valve2": 27,   # BCM 27 → Zone 2 solenoid valve
    "valve3": 22,   # BCM 22 → Zone 3 solenoid valve
    "valve4": 23,   # BCM 23 → Zone 4 solenoid valve
}

# Display labels for each relay key (shown in the UI dashboard).
RELAY_LABELS = {
    "valve1": "Valve 1 (Zone 1)",
    "valve2": "Valve 2 (Zone 2)",
    "valve3": "Valve 3 (Zone 3)",
    "valve4": "Valve 4 (Zone 4)",
}

# Most opto-isolated relay boards are active-LOW (pin LOW = relay ON).
# Set env var IRRIGATION_RELAY_ACTIVE_LOW=0 if your board is active-HIGH.
RELAY_ACTIVE_LOW = os.getenv("IRRIGATION_RELAY_ACTIVE_LOW", "1") == "1"

# One sensor per zone — each ADS1115 channel maps to its own valve.
CHANNEL_VALVE_MAP = {
    0: "valve1",   # SEN0308 #1  → Zone 1 (BCM 17)
    1: "valve2",   # SEN0308 #2  → Zone 2 (BCM 27)
    2: "valve3",   # SEN0193     → Zone 3 (BCM 22)
    3: "valve4",   # Generic     → Zone 4 (BCM 23)
}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging-mode Intervals
# ─────────────────────────────────────────────────────────────────────────────

# The stress-test panel allows switching between:
#   "production" — 10-minute poll (normal service cadence)
#   "test"       — 5-second poll  (captures step-response / jitter in real time)
LOGGING_INTERVALS = {
    "production": 600,   # seconds
    "test":         5,   # seconds
}

# ML hardcoded flow rate (Litres / minute).  Adjust to your pump's measured Q.
ML_FLOW_RATE_LPM = float(os.getenv("THESIS_FLOW_RATE", "3.0"))

# ─── Built-in linear regression coefficients ────────────────────────────────
# Volume(L) = β₀ + β₁·T + β₂·H + β₃·(100 − M)
# Based on a simplified single-square-metre water-balance model.
# IMPORTANT: Replace these with your thesis model's fitted coefficients,
# OR place a trained joblib file at ML_MODEL_PATH to override them entirely.
ML_B0 = float(os.getenv("THESIS_ML_B0", "-2.0"))    # intercept
ML_B1 = float(os.getenv("THESIS_ML_B1",  "0.15"))   # temperature coefficient
ML_B2 = float(os.getenv("THESIS_ML_B2", "-0.04"))   # humidity coefficient
ML_B3 = float(os.getenv("THESIS_ML_B3",  "0.08"))   # moisture deficit coeff


# ─────────────────────────────────────────────────────────────────────────────
#  Shared Runtime State  (each variable protected by its own Lock)
# ─────────────────────────────────────────────────────────────────────────────

# Current polling/logging mode ("production" or "test").
_logging_mode      = "production"
_logging_mode_lock = threading.Lock()

# Most recent full sensor snapshot (dict, updated by the poll thread).
_latest_reading      = {}
_latest_reading_lock = threading.Lock()

# SSE: each connected browser client gets its own Queue.
# The poll thread puts JSON payloads into every queue simultaneously.
_sse_clients      = []
_sse_clients_lock = threading.Lock()

# Event used to wake the poll thread immediately when the mode changes.
_poll_wake = threading.Event()

# ─── Relay state ─────────────────────────────────────────────────────────────
# Possible values per key: "OFF" | "ON" | "QUEUED"
_relay_states = {k: "OFF" for k in RELAY_GPIO_MAP}

# FIFO queue of valve keys waiting to activate (prevents pump pressure drops).
# All four zone valves participate in the sequential queue.
_valve_queue   = deque()
_active_valve  = None           # currently active valve key or None

_relay_lock    = threading.Lock()

# threading.Timer handles for active failsafe countdowns.
_failsafe_timers    = {}        # valve_key → threading.Timer
# Failsafe end times (monotonic clock) for computing time-remaining in the UI.
_failsafe_end_times = {}        # valve_key → float  (time.time() deadline)
# Pending failsafe minutes for valves that are currently QUEUED.
# The timer is not started until the valve actually activates.
_queued_failsafe    = {}        # valve_key → float (minutes)

# ─── Stress-test session ─────────────────────────────────────────────────────
# Tracks a single running sensor-reliability test (5-min protocol).
_stress_test = {
    "running":        False,
    "valve":          None,     # 'valve1' | 'valve2'
    "start_time":     None,     # float — time.time() when test started
    "phase":          "idle",   # 'idle' | 'watering' | 'monitoring' | 'done'
    "on_duration":    120,      # seconds valve is open
    "total_duration": 300,      # total test length in seconds
    "data":           {str(ch): [] for ch in range(4)},
}
_stress_test_lock        = threading.Lock()
_stress_test_stop_event  = threading.Event()   # set() to abort running test

# ─── GPIO backend ─────────────────────────────────────────────────────────────
_GPIO      = None
_gpio_ok   = False              # True once GPIO is initialised successfully

# ─── ML mode ──────────────────────────────────────────────────────────────────
# Runtime toggle — set via POST /api/ml/mode.
# When False, /api/ml/predict returns 503 so Panel 4 inputs are clearly disabled.
_ml_model       = None
_ml_loaded      = False
_ml_enabled     = True
_ml_enabled_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  Calibration — Load / Save
# ─────────────────────────────────────────────────────────────────────────────

# In-memory calibration store.  Keyed "A0".."A3", each holds dry_v / wet_v.
_calibration      = {f"A{ch}": {"dry_v": None, "wet_v": None} for ch in range(4)}
_calibration_lock = threading.Lock()


def _load_calibration():
    """Read the JSON calibration file from disk into the in-memory store."""
    global _calibration
    if not os.path.exists(CALIBRATION_FILE):
        return
    try:
        with open(CALIBRATION_FILE) as fh:
            data = json.load(fh)
        with _calibration_lock:
            for key, vals in data.items():
                if key in _calibration:
                    _calibration[key] = vals
        print(f"[CALIB] Loaded baselines from {CALIBRATION_FILE}")
    except Exception as exc:
        print(f"[CALIB] Load failed: {exc}")


def _save_calibration():
    """Persist the current in-memory calibration to the JSON file."""
    try:
        with _calibration_lock:
            data = dict(_calibration)
        with open(CALIBRATION_FILE, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        print(f"[CALIB] Save failed: {exc}")


def _sync_channel_to_main_db(ch):
    """
    Upsert the captured dry/wet baseline for channel `ch` into the main
    app's irrigation_data.db soil_baseline table.

    Called automatically after either the dry or wet voltage is captured,
    but only writes to the DB once BOTH values are present.

    The baseline name is  "<label> (A<ch>)"  e.g. "SEN0308 #1 (A0)".
    Any existing row with that name is updated in-place (ON CONFLICT).

    Returns a dict: {"synced": bool, "name": str | None, "reason": str | None}
    """
    key = f"A{ch}"
    with _calibration_lock:
        baseline = dict(_calibration.get(key, {}))
    dry_v = baseline.get("dry_v")
    wet_v = baseline.get("wet_v")

    if dry_v is None or wet_v is None:
        return {"synced": False, "reason": "Waiting for both dry and wet to be captured"}

    name = f"{CHANNEL_LABELS[ch]} ({key})"
    try:
        conn = sqlite3.connect(MAIN_DB_PATH, timeout=10)
        conn.execute(
            """
            INSERT INTO soil_baseline (name, dry_voltage, wet_voltage)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                dry_voltage = excluded.dry_voltage,
                wet_voltage = excluded.wet_voltage
            """,
            (name, dry_v, wet_v),
        )
        conn.commit()
        conn.close()
        print(f"[SYNC] Baseline '{name}' → {MAIN_DB_PATH}")
        return {"synced": True, "name": name, "dry_voltage": dry_v, "wet_voltage": wet_v}
    except Exception as exc:
        print(f"[SYNC] Failed for channel {ch}: {exc}")
        return {"synced": False, "reason": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware Reading
# ─────────────────────────────────────────────────────────────────────────────

def _read_hardware():
    """
    Perform a single full hardware snapshot:
      - Reads the BME280 for temperature and humidity.
      - Reads all four ADS1115 channels with noise-reduction averaging.
    Returns a dict.  All values are None on I2C or hardware failure.
    """
    result = {
        "temperature": None,
        "humidity":    None,
        "channels": [
            {
                "ch":    ch,
                "label": CHANNEL_LABELS[ch],
                "voltage": None,         # Raw measured voltage (V)
                "calibrated_pct": None,  # Mapped 0–100 % (requires baselines)
            }
            for ch in range(4)
        ],
        "timestamp": datetime.now().isoformat(),
        "hw_error":  None,
    }

    try:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        from adafruit_bme280 import basic as adafruit_bme280

        i2c = busio.I2C(board.SCL, board.SDA)

        # ── BME280 ───────────────────────────────────────────────────────────
        for addr in BME280_ADDRESSES:
            try:
                bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
                result["temperature"] = round(float(bme.temperature), 1)
                result["humidity"]    = round(float(bme.humidity), 1)
                break
            except Exception:
                pass

        # ── ADS1115 — all four channels ──────────────────────────────────────
        ads = ADS.ADS1115(i2c, address=ADS_I2C_ADDRESS)

        for ch in range(4):
            # Throw away the first read after multiplexer switch; the ADS1115
            # input capacitor retains charge from the previous channel for ~1 ms.
            _ = AnalogIn(ads, ch).voltage
            time.sleep(0.02)

            # Collect multiple samples and average them to reduce ADC noise.
            samples = []
            for _ in range(ADS_SMOOTHING_SAMPLES):
                samples.append(float(AnalogIn(ads, ch).voltage))
                time.sleep(ADS_SMOOTHING_DELAY)

            result["channels"][ch]["voltage"] = round(sum(samples) / len(samples), 4)

    except Exception as exc:
        result["hw_error"] = str(exc)

    # Apply saved calibration baselines to compute moisture percentages.
    _apply_calibration(result)
    return result


def _apply_calibration(reading):
    """
    Enrich each channel entry with 'calibrated_pct' based on the saved
    dry / wet voltage baselines.

    Capacitive soil sensors output HIGHER voltage when DRY, LOWER when WET.
    Formula:  pct = (dry_v − measured_v) / (dry_v − wet_v) × 100
    Clamped to 0–100 %.
    """
    with _calibration_lock:
        cal = dict(_calibration)

    for ch_data in reading["channels"]:
        ch       = ch_data["ch"]
        key      = f"A{ch}"
        voltage  = ch_data.get("voltage")
        baseline = cal.get(key, {})
        dry_v    = baseline.get("dry_v")
        wet_v    = baseline.get("wet_v")

        if voltage is not None and dry_v is not None and wet_v is not None:
            span = float(dry_v) - float(wet_v)
            if abs(span) > 1e-9:
                pct = ((float(dry_v) - float(voltage)) / span) * 100.0
                ch_data["calibrated_pct"] = round(max(0.0, min(100.0, pct)), 1)
            else:
                ch_data["calibrated_pct"] = None
        else:
            ch_data["calibrated_pct"] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Background Sensor Poll Thread
# ─────────────────────────────────────────────────────────────────────────────

def _sensor_poll_worker():
    """
    Runs as a daemon thread.  Polls hardware at the configured interval and:
      1. Updates _latest_reading for one-shot API calls.
      2. Pushes the JSON payload to every connected SSE client queue.

    The _poll_wake event allows the mode-change endpoint to trigger an
    immediate poll so the chart does not stall waiting for a 10-minute timeout.
    """
    while True:
        _poll_wake.clear()

        with _logging_mode_lock:
            interval = LOGGING_INTERVALS.get(_logging_mode, 600)

        snapshot = _read_hardware()

        with _latest_reading_lock:
            _latest_reading.clear()
            _latest_reading.update(snapshot)

        # Broadcast to all SSE subscribers.
        payload = json.dumps(snapshot, default=str)
        with _sse_clients_lock:
            stale = []
            for q in _sse_clients:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    stale.append(q)    # Client is not consuming; drop it.
            for q in stale:
                _sse_clients.remove(q)

        # Sleep for the configured interval OR until woken by a mode change.
        _poll_wake.wait(timeout=interval)


# ─────────────────────────────────────────────────────────────────────────────
#  GPIO / Relay Control
# ─────────────────────────────────────────────────────────────────────────────

def _init_gpio():
    """
    Initialise the RPi.GPIO library and configure each relay pin as output.
    Called once in a background thread at startup.
    Sets all relays to OFF as a safety default before the user takes control.
    """
    global _GPIO, _gpio_ok
    try:
        import RPi.GPIO as GPIO

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in RELAY_GPIO_MAP.values():
            # initial=GPIO.HIGH keeps the pin HIGH at setup time.
            # For active-LOW boards HIGH = relay OFF, so coils never
            # energise during initialisation.
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

        _GPIO   = GPIO
        _gpio_ok = True

        # Belt-and-braces: explicitly write OFF to every relay.
        for key in RELAY_GPIO_MAP:
            _write_gpio(key, "OFF")

        # Sync the shared DB so valve_status rows reflect real hardware
        # state — clears any stale ON rows left over from a previous session.
        try:
            import sqlite3 as _sqlite3
            _db = _sqlite3.connect(MAIN_DB_PATH, timeout=5)
            _db.execute(
                "UPDATE valve_status SET status='OFF', last_updated=CURRENT_TIMESTAMP"
            )
            _db.commit()
            _db.close()
        except Exception as _dbe:
            print(f"[GPIO] DB sync warning: {_dbe}")

        print("[GPIO] Relay control ready.")
    except Exception as exc:
        _gpio_ok = False
        print(f"[GPIO] Unavailable (non-Pi environment?): {exc}")


def _write_gpio(valve_key, state):
    """
    Write the physical GPIO pin for a relay.
    Handles active-LOW / active-HIGH board polarity via RELAY_ACTIVE_LOW.
    Safe to call when GPIO is unavailable (no-op with a log message).
    """
    if _GPIO is None:
        return
    pin     = RELAY_GPIO_MAP[valve_key]
    want_on = state == "ON"
    if RELAY_ACTIVE_LOW:
        level = _GPIO.LOW if want_on else _GPIO.HIGH
    else:
        level = _GPIO.HIGH if want_on else _GPIO.LOW
    _GPIO.output(pin, level)


def _force_set_relay(valve_key, state):
    """
    Bypass the sequential valve queue and directly drive the GPIO pin.
    Used exclusively by _run_stress_test to guarantee immediate activation
    without being blocked by a concurrently active valve from Panel 3.
    """
    global _active_valve
    with _relay_lock:
        # Cancel any pending failsafe for this key.
        old = _failsafe_timers.pop(valve_key, None)
        if old and isinstance(old, threading.Timer):
            old.cancel()
        _failsafe_end_times.pop(valve_key, None)
        _queued_failsafe.pop(valve_key, None)
        try:
            _valve_queue.remove(valve_key)
        except ValueError:
            pass
        _relay_states[valve_key] = state
        _write_gpio(valve_key, state)
        if state == "ON":
            _active_valve = valve_key
        elif _active_valve == valve_key:
            _active_valve = None


def _start_failsafe_timer(valve_key, minutes):
    """
    Start a countdown timer that automatically turns off valve_key after
    the specified number of minutes.  Cancels any existing timer first.
    MUST be called with _relay_lock already held.
    """
    # Cancel and clear any previous timer for this key.
    old = _failsafe_timers.pop(valve_key, None)
    if old and isinstance(old, threading.Timer):
        old.cancel()

    t = threading.Timer(
        float(minutes) * 60,
        _auto_close_valve,
        args=(valve_key,),
    )
    t.daemon = True
    t.start()
    _failsafe_timers[valve_key]    = t
    _failsafe_end_times[valve_key] = time.time() + float(minutes) * 60


def _auto_close_valve(valve_key):
    """
    Failsafe callback — fires when the auto-close timer expires.
    Turns the relay OFF, clears state, and activates the next queued valve.
    """
    print(f"[FAILSAFE] Auto-closing {valve_key}")
    with _relay_lock:
        global _active_valve
        _failsafe_timers.pop(valve_key, None)
        _failsafe_end_times.pop(valve_key, None)
        _relay_states[valve_key] = "OFF"
        _write_gpio(valve_key, "OFF")
        if _active_valve == valve_key:
            _active_valve = None
        _discharge_queue()


def _discharge_queue():
    """
    If no valve is currently active, activate the next one in the FIFO queue.
    MUST be called with _relay_lock already held.
    This implements the sequential irrigation logic that prevents simultaneous
    valve operation and the pump pressure drop it would cause.
    """
    global _active_valve
    if _active_valve is not None:
        return                        # Another valve is still running.
    if not _valve_queue:
        return                        # Nothing waiting.

    next_valve = _valve_queue.popleft()
    _active_valve = next_valve
    _relay_states[next_valve] = "ON"
    _write_gpio(next_valve, "ON")
    print(f"[QUEUE] Dequeued and activated {next_valve}")

    # Start the failsafe timer that was stored when this valve was queued.
    fs_mins = _queued_failsafe.pop(next_valve, None)
    if fs_mins and float(fs_mins) > 0:
        _start_failsafe_timer(next_valve, float(fs_mins))


def set_relay(valve_key, desired_state, failsafe_minutes=None):
    """
    Public interface for relay control — thread-safe.

    Parameters
    ----------
    valve_key       : "valve1" | "valve2" | "valve3" | "valve4"
    desired_state   : "ON" | "OFF"
    failsafe_minutes: float | None — auto-close after N minutes (0 = no timer)

    Queue logic:
      • If no valve is active   → activate immediately.
      • If another valve is ON  → set status to QUEUED; it will activate
                                   automatically when the current valve closes.

    Returns the current full relay snapshot dict.
    """
    global _active_valve

    with _relay_lock:

        # ── OFF ───────────────────────────────────────────────────────────────
        if desired_state == "OFF":
            # Cancel any running failsafe timer.
            timer = _failsafe_timers.pop(valve_key, None)
            if timer and isinstance(timer, threading.Timer):
                timer.cancel()
            _failsafe_end_times.pop(valve_key, None)

            # Remove from queue if it was waiting there.
            _queued_failsafe.pop(valve_key, None)
            try:
                _valve_queue.remove(valve_key)
            except ValueError:
                pass

            _relay_states[valve_key] = "OFF"
            _write_gpio(valve_key, "OFF")

            # If this was the active valve, clear and activate next in queue.
            if _active_valve == valve_key:
                _active_valve = None
                _discharge_queue()

        # ── ON (Valve — goes through sequential queue) ────────────────────────
        else:
            # Cancel any old failsafe in case this is a re-issue while active.
            old = _failsafe_timers.pop(valve_key, None)
            if old and isinstance(old, threading.Timer):
                old.cancel()
            _failsafe_end_times.pop(valve_key, None)

            if _active_valve is None or _active_valve == valve_key:
                # ── Activate immediately ──────────────────────────────────────
                _active_valve = valve_key
                _relay_states[valve_key] = "ON"
                _write_gpio(valve_key, "ON")
                if failsafe_minutes and float(failsafe_minutes) > 0:
                    _start_failsafe_timer(valve_key, float(failsafe_minutes))
            else:
                # ── Queue the request ─────────────────────────────────────────
                if valve_key not in _valve_queue:
                    _valve_queue.append(valve_key)
                _relay_states[valve_key] = "QUEUED"
                # Store the failsafe so it starts when dequeued.
                if failsafe_minutes and float(failsafe_minutes) > 0:
                    _queued_failsafe[valve_key] = float(failsafe_minutes)
                else:
                    _queued_failsafe.pop(valve_key, None)

    return _relay_snapshot()


def _relay_snapshot():
    """
    Build a serialisable dict of the current relay states and queue.
    Includes seconds remaining on any active failsafe timer for display.
    """
    with _relay_lock:
        snap = {}
        for key in RELAY_GPIO_MAP:
            remaining = None
            end_time  = _failsafe_end_times.get(key)
            if end_time:
                remaining = round(max(0.0, end_time - time.time()), 1)
            snap[key] = {
                "state":               _relay_states[key],
                "label":               RELAY_LABELS[key],
                "failsafe_remaining_s": remaining,
            }
        snap["queue"]        = list(_valve_queue)
        snap["gpio_ready"]   = _gpio_ok
        return snap


# ─────────────────────────────────────────────────────────────────────────────
#  Machine Learning Prediction
# ─────────────────────────────────────────────────────────────────────────────

def _load_ml_model():
    """
    Attempt to load a trained scikit-learn model from ML_MODEL_PATH using
    joblib.  Called once at startup.  Falls back to the built-in linear
    regression if the file is absent or incompatible.
    """
    global _ml_model, _ml_loaded
    if _ml_loaded:
        return
    _ml_loaded = True
    try:
        import joblib
        _ml_model = joblib.load(ML_MODEL_PATH)
        print(f"[ML] Loaded trained model from {ML_MODEL_PATH}")
    except Exception as exc:
        _ml_model = None
        print(f"[ML] No trained model found — using built-in regression: {exc}")


def run_ml_prediction(temperature, humidity, current_moisture, flow_rate_override=None, target_moisture=100.0):
    """
    Predict irrigation Volume (L) and Duration (min) given environmental inputs.

    If a joblib model was loaded it receives a (1, 3) feature array:
        [temperature, humidity, current_moisture]

    Otherwise the built-in multivariate linear regression is evaluated:
        Volume(L) = β₀ + β₁·T + β₂·H + β₃·(target_moisture − current_moisture)

    Duration(min) = Volume / Q    where Q = ML_FLOW_RATE_LPM

    Returns a result dict with volume, duration, inputs, and model info.
    """
    temp     = float(temperature)
    humidity = float(humidity)
    moisture = float(current_moisture)
    target   = float(target_moisture)
    deficit  = max(0.0, target - moisture)

    if _ml_model is not None:
        # ── Trained model path ───────────────────────────────────────────────
        try:
            import numpy as np
            features     = np.array([[temp, humidity, moisture]])
            volume       = float(_ml_model.predict(features)[0])
            model_source = f"Trained model: {os.path.basename(ML_MODEL_PATH)}"
        except Exception as exc:
            volume       = 0.0
            model_source = f"Model predict() failed: {exc}"
    else:
        # ── Built-in linear regression fallback ───────────────────────────────
        volume = (
            ML_B0
            + ML_B1 * temp
            + ML_B2 * humidity
            + ML_B3 * deficit
        )
        model_source = (
            "Built-in regression — "
            f"β₀={ML_B0}, β₁={ML_B1}, β₂={ML_B2}, β₃={ML_B3}"
        )

    volume   = max(0.0, round(volume, 3))
    q        = float(flow_rate_override) if flow_rate_override is not None else ML_FLOW_RATE_LPM
    duration = round(volume / q, 2) if q > 0 else 0.0

    return {
        "volume_liters":    volume,
        "duration_minutes": duration,
        "flow_rate_lpm":    q,
        "model_source":     model_source,
        "inputs": {
            "temperature":      temp,
            "humidity":         humidity,
            "current_moisture": moisture,
            "target_moisture":  target,
            "moisture_deficit": deficit,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Sensor Reliability Test (5-minute step-response protocol)
# ─────────────────────────────────────────────────────────────────────────────

def _run_stress_test(valve_key, on_duration=120, total_duration=300, collect_interval=5):
    """
    Background thread for the 5-minute sensor reliability test.

    Protocol (default):
      0:00 – 2:00   Valve ON  (watering phase — sensors should rise)
      2:00 – 5:00   Valve OFF (monitoring phase — sensors should flatten/decay)

    Works with any of the four zone valves (valve1–valve4).
    Uses _force_set_relay to bypass the valve queue and guarantee immediate
    activation. Also writes a testing_lock to the shared DB so that app.py's
    auto-control loop defers to this test and does not override the valve.

    Readings are appended to _stress_test['data'] every *collect_interval*
    seconds and served by GET /api/stress-test/status for the frontend to plot.
    """
    global _stress_test
    _stress_test_stop_event.clear()
    start          = time.time()
    valve_off_done = False

    # Derive zone_id from valve_key (valve1 → 1, valve4 → 4).
    zone_id = int(valve_key.replace("valve", ""))

    # Write a testing_lock to the shared DB so app.py will not auto-control
    # this zone for the duration of the test plus a 60-second safety margin.
    try:
        conn = sqlite3.connect(MAIN_DB_PATH, timeout=10)
        locked_until_dt = datetime.now() + timedelta(seconds=total_duration + 60)
        conn.execute(
            "INSERT OR REPLACE INTO testing_lock (zone_id, locked_until) VALUES (?, ?)",
            (zone_id, locked_until_dt.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[TEST] Failed to write testing_lock for zone {zone_id}: {exc}")

    # Force-activate the valve directly, bypassing the sequential queue.
    _force_set_relay(valve_key, "ON")
    try:
        while not _stress_test_stop_event.is_set():
            elapsed = time.time() - start
            if elapsed >= total_duration:
                break

            # Close valve at the on_duration mark.
            if not valve_off_done and elapsed >= on_duration:
                _force_set_relay(valve_key, "OFF")
                valve_off_done = True

            with _stress_test_lock:
                _stress_test["phase"] = "watering" if elapsed < on_duration else "monitoring"

            reading = _read_hardware()

            with _stress_test_lock:
                for ch in range(4):
                    c = reading["channels"][ch]
                    _stress_test["data"][str(ch)].append({
                        "t":    round(elapsed, 1),
                        "pct":  c["calibrated_pct"],
                        "volt": c["voltage"],
                    })

            # Interruptible sleep — wakes immediately when stop event fires.
            _stress_test_stop_event.wait(timeout=collect_interval)

    finally:
        if not valve_off_done:
            _force_set_relay(valve_key, "OFF")
        with _stress_test_lock:
            _stress_test["running"] = False
            _stress_test["phase"]   = "done"
        # Release the testing lock in the shared DB.
        try:
            conn = sqlite3.connect(MAIN_DB_PATH, timeout=10)
            conn.execute("DELETE FROM testing_lock WHERE zone_id=?", (zone_id,))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"[TEST] Failed to clear testing_lock for zone {zone_id}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  Flask Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the single-page Thesis Dashboard HTML."""
    return render_template("thesis_dashboard.html")


# ── Sensor live read ──────────────────────────────────────────────────────────

@app.route("/api/sensors/live")
def api_sensors_live():
    """
    Perform an immediate hardware read and return the full snapshot.
    Used by the Calibration Panel's 'Refresh' button and the voltage
    capture buttons to ensure the reading is fresh.
    """
    snapshot = _read_hardware()
    with _latest_reading_lock:
        _latest_reading.clear()
        _latest_reading.update(snapshot)
    return jsonify(snapshot)


# ── Calibration endpoints ─────────────────────────────────────────────────────

@app.route("/api/calibration", methods=["GET"])
def api_calibration_get():
    """Return the current dry / wet voltage baselines for all four channels."""
    with _calibration_lock:
        return jsonify(dict(_calibration))


@app.route("/api/calibration/capture", methods=["POST"])
def api_calibration_capture():
    """
    Read the current live voltage for one channel and save it as the 'dry'
    or 'wet' baseline.

    Request body (JSON):
        { "channel": 0,  "point": "dry" | "wet" }

    This is the core action for filling in Table 3 of the thesis.
    """
    data = request.get_json(force=True) or {}

    try:
        ch = int(data["channel"])
        pt = str(data["point"]).lower()
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Provide 'channel' (0–3) and 'point' ('dry'|'wet')"}), 400

    if ch not in range(4):
        return jsonify({"error": "channel must be 0–3"}), 400
    if pt not in ("dry", "wet"):
        return jsonify({"error": "point must be 'dry' or 'wet'"}), 400

    # Perform a fresh hardware read so the voltage is current.
    snapshot = _read_hardware()
    voltage  = snapshot["channels"][ch]["voltage"]

    if voltage is None:
        return jsonify({"error": "Hardware read failed — check I2C connection"}), 503

    key = f"A{ch}"
    with _calibration_lock:
        _calibration[key][f"{pt}_v"] = voltage

    _save_calibration()
    sync_result = _sync_channel_to_main_db(ch)
    return jsonify({"channel": ch, "key": key, "point": pt, "voltage": voltage, "sync": sync_result})


@app.route("/api/calibration/reset", methods=["POST"])
def api_calibration_reset():
    """
    Wipe the saved baselines for a single channel or all channels.
    Request body: { "channel": 0 }  — omit 'channel' to reset everything.
    """
    data = request.get_json(force=True) or {}
    ch   = data.get("channel")   # None = reset all

    with _calibration_lock:
        if ch is None:
            for k in _calibration:
                _calibration[k] = {"dry_v": None, "wet_v": None}
        else:
            key = f"A{int(ch)}"
            if key in _calibration:
                _calibration[key] = {"dry_v": None, "wet_v": None}

    _save_calibration()
    return jsonify({"ok": True})


@app.route("/api/calibration/sync", methods=["POST"])
def api_calibration_sync():
    """
    Manually push all channels that have both dry and wet voltages captured
    into the main app's irrigation_data.db as soil baselines.
    Returns per-channel sync results.
    """
    results = {}
    for ch in range(4):
        results[f"A{ch}"] = _sync_channel_to_main_db(ch)
    return jsonify(results)


# ── Logging mode ──────────────────────────────────────────────────────────────

@app.route("/api/logging-mode", methods=["GET"])
def api_logging_mode_get():
    """Return the current logging mode and its poll interval in seconds."""
    with _logging_mode_lock:
        mode = _logging_mode
    return jsonify({"mode": mode, "interval_s": LOGGING_INTERVALS[mode]})


@app.route("/api/logging-mode", methods=["POST"])
def api_logging_mode_set():
    """
    Switch the hardware poll interval.
    Request body: { "mode": "production" | "test" }
    Wakes the poll thread immediately via _poll_wake so the chart reacts
    without waiting for the old interval to expire.
    """
    data = request.get_json(force=True) or {}
    mode = str(data.get("mode", ""))

    if mode not in LOGGING_INTERVALS:
        return jsonify({"error": "mode must be 'production' or 'test'"}), 400

    global _logging_mode
    with _logging_mode_lock:
        _logging_mode = mode

    _poll_wake.set()   # Wake the poll thread immediately.
    return jsonify({"mode": mode, "interval_s": LOGGING_INTERVALS[mode]})


# ── Sensor Reliability Test ───────────────────────────────────────────────────

@app.route("/api/stress-test/start", methods=["POST"])
def api_stress_test_start():
    """
    Start the 5-minute sensor reliability test for a given valve zone.

    Request body:
        {
            "valve":          "valve1" | "valve2",
            "on_duration":    120,   // optional — seconds valve stays open (default 120)
            "total_duration": 300    // optional — total test length in seconds (default 300)
        }

    Returns 409 if a test is already running.
    """
    body       = request.get_json(force=True, silent=True) or {}
    valve_key  = body.get("valve")
    if valve_key not in RELAY_GPIO_MAP:
        return jsonify({"error": f"valve must be one of: {list(RELAY_GPIO_MAP.keys())}"}), 400

    on_dur    = max(10,  int(body.get("on_duration",    120)))
    total_dur = max(on_dur + 10, int(body.get("total_duration", 300)))

    with _stress_test_lock:
        if _stress_test["running"]:
            return jsonify({"error": "A test is already running. Stop it first."}), 409
        _stress_test["running"]        = True
        _stress_test["valve"]          = valve_key
        _stress_test["start_time"]     = time.time()
        _stress_test["phase"]          = "watering"
        _stress_test["on_duration"]    = on_dur
        _stress_test["total_duration"] = total_dur
        _stress_test["data"]           = {str(ch): [] for ch in range(4)}

    t = threading.Thread(
        target=_run_stress_test,
        args=(valve_key,),
        kwargs={"on_duration": on_dur, "total_duration": total_dur, "collect_interval": 5},
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started", "valve": valve_key,
                    "on_duration": on_dur, "total_duration": total_dur})


@app.route("/api/stress-test/status")
def api_stress_test_status():
    """
    Return the current test state plus all collected data points.

    Response shape:
        {
            "running":        bool,
            "valve":          "valve1" | null,
            "phase":          "idle" | "watering" | "monitoring" | "done",
            "elapsed_s":      float,
            "on_duration":    int,
            "total_duration": int,
            "data": {
                "0": [{"t": 12.3, "pct": 45.1, "volt": 1.823}, ...],
                "1": [...],
                "2": [...],
                "3": [...]
            }
        }
    """
    with _stress_test_lock:
        snap = dict(_stress_test)
        snap["data"]      = {k: list(v) for k, v in _stress_test["data"].items()}
        snap["elapsed_s"] = (
            round(time.time() - snap["start_time"], 1)
            if snap["start_time"] else 0
        )
        snap["start_time"] = None   # don't expose raw epoch to client
    return jsonify(snap)


@app.route("/api/stress-test/stop", methods=["POST"])
def api_stress_test_stop():
    """Abort the running test. The background thread will shut down within one poll cycle."""
    _stress_test_stop_event.set()
    with _stress_test_lock:
        _stress_test["running"] = False
        _stress_test["phase"]   = "done"
    return jsonify({"status": "stopped"})


# ── Relay control ─────────────────────────────────────────────────────────────

@app.route("/api/relays", methods=["GET"])
def api_relays_get():
    """Return the current state of all relays and the valve queue."""
    return jsonify(_relay_snapshot())


@app.route("/api/relays/<valve_key>", methods=["POST"])
def api_relay_set(valve_key):
    """
    Control a relay.

    URL param  : valve_key → "valve1" | "valve2" | "pump"
    Request body:
        {
            "state":            "ON" | "OFF",
            "failsafe_minutes": 5          // optional; 0 or omit = no timer
        }

    For valves, sequential queue logic applies automatically.
    """
    if valve_key not in RELAY_GPIO_MAP:
        return jsonify({"error": f"Unknown relay: {valve_key}"}), 400

    data  = request.get_json(force=True) or {}
    state = str(data.get("state", "")).upper()

    if state not in ("ON", "OFF"):
        return jsonify({"error": "state must be ON or OFF"}), 400

    fs_mins = data.get("failsafe_minutes")
    if fs_mins is not None:
        try:
            fs_mins = float(fs_mins)
            if fs_mins <= 0:
                fs_mins = None
        except (ValueError, TypeError):
            fs_mins = None

    snapshot = set_relay(valve_key, state, failsafe_minutes=fs_mins)
    return jsonify(snapshot)


# ── Server-Sent Events stream ─────────────────────────────────────────────────

@app.route("/api/stream")
def api_stream():
    """
    Server-Sent Events endpoint for the live Chart.js stress-test graph.

    Each EventSource message carries a JSON payload identical to the
    /api/sensors/live response.  The poll thread pushes data at the
    currently configured interval (5 s in test mode, 10 min in production).

    The :heartbeat line prevents reverse-proxies from killing idle connections
    during long production-mode intervals.
    """
    client_q = queue.Queue(maxsize=30)
    with _sse_clients_lock:
        _sse_clients.append(client_q)

    # Push the most recent reading immediately so the chart isn't blank.
    with _latest_reading_lock:
        if _latest_reading:
            client_q.put_nowait(json.dumps(dict(_latest_reading), default=str))

    @stream_with_context
    def generate():
        try:
            while True:
                try:
                    payload = client_q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"   # Keep-alive comment line.
        except GeneratorExit:
            pass
        finally:
            with _sse_clients_lock:
                try:
                    _sse_clients.remove(client_q)
                except ValueError:
                    pass

    headers = {
        "Cache-Control":     "no-cache",
        "X-Accel-Buffering": "no",    # Disable nginx output buffering.
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


# ── ML mode toggle ────────────────────────────────────────────────────────────

@app.route("/api/ml/mode", methods=["GET"])
def api_ml_mode_get():
    """Return whether ML mode is currently enabled."""
    with _ml_enabled_lock:
        enabled = _ml_enabled
    return jsonify({"enabled": enabled})


@app.route("/api/ml/mode", methods=["POST"])
def api_ml_mode_set():
    """
    Enable or disable the ML prediction panel at runtime.
    Request body: { "enabled": true | false }
    When disabled /api/ml/predict returns 503 so Panel 4 inputs grey out.
    """
    global _ml_enabled
    data = request.get_json(force=True) or {}
    if "enabled" not in data:
        return jsonify({"error": "enabled is required"}), 400
    enabled = bool(data["enabled"])
    with _ml_enabled_lock:
        _ml_enabled = enabled
    print(f"[ML] Mode {'enabled' if enabled else 'disabled'}")
    return jsonify({"enabled": enabled})


@app.route("/api/config")
def api_config():
    """Return read-only dashboard constants for UI initialisation."""
    with _ml_enabled_lock:
        ml_enabled = _ml_enabled
    with _logging_mode_lock:
        mode = _logging_mode
    return jsonify({
        "flow_rate_lpm":      ML_FLOW_RATE_LPM,
        "ml_enabled":         ml_enabled,
        "logging_mode":       mode,
        "logging_interval_s": LOGGING_INTERVALS[mode],
    })


# ── Zone targets ─────────────────────────────────────────────────────────────

@app.route("/api/zones/targets")
def api_zones_targets():
    """Return target_moisture per zone from the main app DB."""
    try:
        conn = sqlite3.connect(MAIN_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT zone_id, target_moisture FROM zone_profile ORDER BY zone_id"
        ).fetchall()
        conn.close()
        return jsonify({str(r["zone_id"]): r["target_moisture"] for r in rows})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ── ML prediction ─────────────────────────────────────────────────────────────

@app.route("/api/ml/predict", methods=["POST"])
def api_ml_predict():
    """
    Run the irrigation-volume ML regression.

    Returns 503 if ML mode is disabled (toggle in Panel 4).

    Request body:
        { "temperature": 30.5, "humidity": 65.0, "moisture": 42.0 }
    """
    with _ml_enabled_lock:
        if not _ml_enabled:
            return jsonify(
                {"error": "ML mode is disabled. Use the toggle in Panel 4 to re-enable it."}
            ), 503

    data = request.get_json(force=True) or {}
    try:
        temp     = float(data["temperature"])
        humidity = float(data["humidity"])
        moisture = float(data["moisture"])
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid or missing input field: {exc}"}), 400

    # Optional per-zone flow rate override from the UI.
    # Falls back to the server-side ML_FLOW_RATE_LPM if not supplied.
    if "flow_rate" in data:
        try:
            flow_rate = float(data["flow_rate"])
            if flow_rate <= 0:
                raise ValueError("flow_rate must be positive")
        except (ValueError, TypeError) as exc:
            return jsonify({"error": f"Invalid flow_rate: {exc}"}), 400
    else:
        flow_rate = None

    # Optional target moisture — defaults to 100 % (full saturation) if not supplied.
    target_moisture = float(data.get("target_moisture", 100.0))
    if not (0.0 <= target_moisture <= 100.0):
        return jsonify({"error": "target_moisture must be 0–100 %"}), 400

    # Basic input sanity checks (system-boundary validation only).
    if not (-10.0 <= temp <= 60.0):
        return jsonify({"error": "temperature must be between -10 and 60 °C"}), 400
    if not (0.0 <= humidity <= 100.0):
        return jsonify({"error": "humidity must be 0–100 %"}), 400
    if not (0.0 <= moisture <= 100.0):
        return jsonify({"error": "moisture must be 0–100 %"}), 400

    result = run_ml_prediction(temp, humidity, moisture, flow_rate_override=flow_rate,
                               target_moisture=target_moisture)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print(f"  Thesis Diagnostics Dashboard")
    print(f"  http://irrigation-hub.local:{DASHBOARD_PORT}")
    print("=" * 60)

    # 1. Load saved calibration baselines from disk.
    _load_calibration()

    # 2. Initialise GPIO relay control synchronously before serving requests.
    #    Running it in a background thread caused a race: if a valve was turned
    #    on (via app.py) before the thread ran, _init_gpio() would later reset
    #    all GPIO pins to HIGH (relay OFF), producing the 2-3 s phantom turn-off.
    _init_gpio()

    # 3. Start the background sensor polling thread.
    threading.Thread(target=_sensor_poll_worker, daemon=True).start()

    # 4. Attempt to load a trained ML model (falls back silently if absent).
    _load_ml_model()

    # 5. Launch Flask.  threaded=True lets multiple SSE clients connect at once.
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, threaded=True)
