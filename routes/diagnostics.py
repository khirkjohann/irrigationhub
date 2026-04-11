"""
routes/diagnostics.py — Raw ADC read, I2C scan, relay test, CPU temp, and shutdown routes.
"""
import subprocess
import threading

from flask import Blueprint, jsonify

from core.config import REQUIRED_ADS_ADDRESSES, VALID_ZONES
from hardware.gpio_control import get_gpio_status
from hardware.irrigation import set_valve
from hardware.sensors import i2c_open

bp = Blueprint("diagnostics", __name__)


@bp.route("/api/diagnostics/raw")
def api_diagnostics_raw():
    """Single raw ADS1115 read for channel 0 (calibration reference)."""
    adc_value, voltage, error = None, None, None
    try:
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        with i2c_open() as i2c:
            ads  = ADS.ADS1115(i2c, address=0x48)
            chan = AnalogIn(ads, 0)
            adc_value = chan.value
            voltage   = round(float(chan.voltage), 4)
    except Exception as exc:
        error = str(exc)
    return jsonify({"adc_value": adc_value, "voltage": voltage, "error": error})


@bp.route("/api/diagnostics/i2c-scan", methods=["POST"])
def api_i2c_scan():
    addresses, error = [], None
    try:
        with i2c_open() as i2c:
            while not i2c.try_lock():
                pass
            try:
                addresses = [hex(a) for a in i2c.scan()]
            finally:
                i2c.unlock()
        if len(addresses) >= 30:
            error = (
                f"Bus fault detected: {len(addresses)} addresses returned — "
                "likely SDA held low or I2C conflict. Real devices not readable."
            )
            addresses = []
    except Exception as exc:
        error = str(exc)
    required = [hex(a) for a in sorted(REQUIRED_ADS_ADDRESSES)]
    missing  = [a for a in required if a not in addresses]
    if "0x76" not in addresses and "0x77" not in addresses:
        missing.append("0x76/0x77")
    if missing and not error:
        error = f"Missing: {', '.join(missing)}"
    return jsonify({"addresses": addresses, "missing": missing, "error": error})


@bp.route("/api/diagnostics/relay-test", methods=["POST"])
def api_relay_test():
    for zid in sorted(VALID_ZONES):
        set_valve(zid, "ON",  source="diagnostic")
        threading.Event().wait(0.2)
        set_valve(zid, "OFF", source="diagnostic")
    status = get_gpio_status()
    return jsonify({
        "success": True,
        "message": "Sequential relay test complete." + (
            "" if status["initialized"] else f" (No GPIO: {status['message']})"
        ),
    })


@bp.route("/api/diagnostics/cpu-temp")
def api_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000.0, 1)
        return jsonify({"temp_c": temp_c})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/system/shutdown", methods=["POST"])
def api_shutdown():
    for cmd in [["sudo", "shutdown", "-h", "now"], ["shutdown", "-h", "now"]]:
        try:
            subprocess.Popen(cmd)
            return jsonify({"success": True, "message": "Shutdown started."})
        except Exception:
            pass
    return jsonify({"error": "Shutdown command failed"}), 500
