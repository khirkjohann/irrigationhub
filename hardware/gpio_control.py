"""
gpio_control.py — Relay GPIO initialisation, valve driving, and fan PWM control.
"""
import threading
import time

from core.config import (
    FAN_CURVE,
    FAN_GPIO,
    FAN_POLL_SECS,
    FAN_PWM_HZ,
    RELAY_ACTIVE_LOW,
    RELAY_GPIO_MAP,
    VALID_ZONES,
)
from core.db import get_db

# ── Shared GPIO state ─────────────────────────────────────────────────────────
_gpio_lock    = threading.Lock()
_gpio_status  = {"available": False, "initialized": False, "message": "Not initialized"}
_GPIO_BACKEND = None   # RPi.GPIO module once loaded

_fan_lock   = threading.Lock()
_fan_pwm    = None    # RPi.GPIO.PWM instance set after _init_gpio
_fan_status = {"duty": 0, "temp_c": None, "message": "Not initialized"}


def get_gpio_status() -> dict:
    with _gpio_lock:
        return dict(_gpio_status)


def get_fan_status() -> dict:
    with _fan_lock:
        return dict(_fan_status)


# ── Initialisation ────────────────────────────────────────────────────────────

def init_gpio() -> None:
    """Set up relay GPIO pins and start fan PWM. Called once at startup."""
    global _GPIO_BACKEND, _fan_pwm
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in RELAY_GPIO_MAP.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)   # HIGH = relay OFF (active-low)
        _GPIO_BACKEND = GPIO
        with _gpio_lock:
            _gpio_status.update({"available": True, "initialized": True,
                                  "message": "GPIO relay control active"})
        for zone_id in VALID_ZONES:
            write_relay(zone_id, "OFF")
        # Sync DB — all pins are now HIGH → relay OFF.
        conn = get_db()
        conn.execute("UPDATE valve_status SET status='OFF', last_updated=CURRENT_TIMESTAMP")
        conn.commit()
        conn.close()

        # Fan PWM on GPIO12 (hardware PWM0 pin). Starts at 0 % duty.
        try:
            GPIO.setup(FAN_GPIO, GPIO.OUT, initial=GPIO.LOW)
            pwm = GPIO.PWM(FAN_GPIO, FAN_PWM_HZ)
            pwm.start(0)
            with _fan_lock:
                _fan_pwm = pwm
                _fan_status["message"] = "Fan initialized — awaiting first temp read"
            print(f"[FAN] PWM ready on GPIO{FAN_GPIO} @ {FAN_PWM_HZ} Hz.")
        except Exception as exc:
            with _fan_lock:
                _fan_status["message"] = f"Fan init failed: {exc}"
            print(f"[FAN] Init error: {exc}")

        print("[GPIO] Ready.")
    except Exception as exc:
        with _gpio_lock:
            _gpio_status.update({"available": False, "initialized": False,
                                  "message": f"GPIO unavailable: {exc}"})


# ── Relay control ─────────────────────────────────────────────────────────────

def write_relay(zone_id: int, state: str) -> None:
    """Drive the relay GPIO pin for zone_id. No-op if GPIO is unavailable."""
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


# ── Fan control ───────────────────────────────────────────────────────────────

def fan_duty_for_temp(temp_c: float) -> int:
    """Return PWM duty cycle (0–100) for the given CPU temperature."""
    duty = 0
    for threshold, d in FAN_CURVE:
        if temp_c < threshold:
            return d
        duty = d
    return duty


def fan_control_loop() -> None:
    """Background thread: read CPU temp every FAN_POLL_SECS and adjust fan PWM."""
    while True:
        time.sleep(FAN_POLL_SECS)
        with _fan_lock:
            pwm = _fan_pwm
        if pwm is None:
            continue
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_c = round(int(f.read().strip()) / 1000.0, 1)
            duty = fan_duty_for_temp(temp_c)
            pwm.ChangeDutyCycle(duty)
            with _fan_lock:
                _fan_status.update({"duty": duty, "temp_c": temp_c,
                                    "message": f"{duty}% @ {temp_c} °C"})
        except Exception as exc:
            with _fan_lock:
                _fan_status["message"] = f"Fan control error: {exc}"
