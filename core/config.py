"""
config.py — All configuration constants for the Irrigation System.
Import this module everywhere instead of using magic numbers.
"""
import os
from datetime import datetime

# ── Paths & ports ─────────────────────────────────────────────────────────────
DB_PATH         = "/home/pi/irrigation_data.db"
MAIN_APP_PORT   = int(os.getenv("IRRIGATION_MAIN_PORT", "5000"))
LOG_VIEWER_PORT = int(os.getenv("IRRIGATION_LOG_VIEWER_PORT", "5001"))
APP_START_TIME  = datetime.now()

# ── Zone / GPIO ───────────────────────────────────────────────────────────────
VALID_ZONES      = {1, 2, 3, 4}
RELAY_GPIO_MAP   = {1: 17, 2: 27, 3: 22, 4: 23}   # zone_id → BCM pin
RELAY_ACTIVE_LOW = os.getenv("IRRIGATION_RELAY_ACTIVE_LOW", "1") == "1"

# ── I2C / Sensors ─────────────────────────────────────────────────────────────
BME280_ADDRESSES       = (0x76, 0x77)
REQUIRED_ADS_ADDRESSES = {0x48}
ADS_SAMPLES            = 10      # averages per channel read
ADS_SAMPLE_DELAY       = 0.05    # seconds between samples

# Zone → ADS1115 channel (0=A0 … 3=A3)
ZONE_ADS_CHANNEL = {1: 0, 2: 1, 3: 2, 4: 3}

# ── Fan (GPIO12 — hardware PWM0 pin, no UART conflict) ────────────────────────
# GPIO14 (TXD) MUST NOT be used: enable_uart=1 keeps the UART hardware active
# on that pin, causing UART↔GPIO contention and SCHED_RR PWM-thread starvation
# of the I2C kernel driver (~1 s timeout per address in i2cdetect).
FAN_GPIO      = int(os.getenv("IRRIGATION_FAN_GPIO", "12"))
FAN_PWM_HZ    = 100
FAN_POLL_SECS = 5.0
# (temp_celsius_threshold, duty_percent)  — evaluated low→high
FAN_CURVE = [
    (40,   0),
    (50,  30),
    (60,  55),
    (70,  80),
    (999, 100),
]

# ── Auto-control ──────────────────────────────────────────────────────────────
SENSOR_POLL_SECONDS   = 300.0
# Seconds to suppress sensor DB writes after any valve switches state.
# Prevents pump inrush / relay switching noise from corrupting training data.
RELAY_BLACKOUT_SECONDS = 15.0

ML_MODEL_PATH = os.getenv("IRRIGATION_BRAIN_PKL", "/home/pi/irrigation_brain.pkl")

# ── WiFi profiles ─────────────────────────────────────────────────────────────
WIFI_PROFILE_PREFIX = "irr-wifi-"
