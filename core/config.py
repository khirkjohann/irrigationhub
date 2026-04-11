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

# ── Fan (GPIO14 / TXD pin — 3-wire PWM fan) ───────────────────────────────────
FAN_GPIO      = int(os.getenv("IRRIGATION_FAN_GPIO", "14"))
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

ML_MODEL_PATH = os.getenv("IRRIGATION_BRAIN_PKL", "/home/pi/irrigation_brain.pkl")

# ── WiFi profiles ─────────────────────────────────────────────────────────────
WIFI_PROFILE_PREFIX = "irr-wifi-"
