"""
sensors.py — I2C bus management, BME280, ADS1115 reading, and sensor state.

This is the file to look at first when debugging sensor/I2C issues.

Bus: Hardware I2C — GPIO2 (SDA) / GPIO3 (SCL)
  BME280  @ 0x76 or 0x77  → temperature, humidity
  ADS1115 @ 0x48          → A0-A3 soil moisture probes (Zones 1-4)
"""
import mmap
import struct
import threading
import time
from contextlib import contextmanager

from core.config import (
    ADS_SAMPLE_DELAY,
    ADS_SAMPLES,
    BME280_ADDRESSES,
    VALID_ZONES,
    ZONE_ADS_CHANNEL,
)
from core.utils import clamp

# ── Shared sensor state ───────────────────────────────────────────────────────
# Access via the helper functions below — never write directly from other modules.

_sensor_lock   = threading.Lock()
_sensor_status: dict = {
    "last_poll":      None,
    "last_success":   None,
    "last_error":     None,
    "bme280":         {"ok": False, "message": "Not read yet"},
    "ads1115_0x48":   {"ok": False, "message": "Not read yet"},
    "missing_inputs": [],
}

# Prevents concurrent I2C access from the sensor poll thread, API scan, and calibration.
_i2c_bus_lock = threading.Lock()


# ── Sensor state accessors ────────────────────────────────────────────────────

def update_sensor(partial: dict) -> None:
    with _sensor_lock:
        _sensor_status.update(partial)


def set_sensor_component(name: str, ok: bool, message: str) -> None:
    with _sensor_lock:
        _sensor_status[name] = {"ok": bool(ok), "message": message}


def get_sensor_snapshot() -> dict:
    with _sensor_lock:
        return dict(_sensor_status)


# ── I2C pin helpers ───────────────────────────────────────────────────────────

def gpio_restore_i2c_alt0() -> None:
    """
    Write ALT0 (0b100) into GPFSEL0 for BCM2 (SDA) and BCM3 (SCL) via /dev/gpiomem.

    WHY THIS EXISTS:
    Any call to GPIO.setup(pin, GPIO.IN) or GPIO.cleanup() writes 000 (INPUT)
    into GPFSEL0, overwriting the 100 (ALT0) that the i2c_bcm2835 kernel driver
    needs. The driver only sets ALT0 during modprobe — NOT on each open() of
    /dev/i2c-1. So after any bit-bang recovery, the pins get corrupted and every
    subsequent I2C transaction silently hangs until this is called.

    /dev/gpiomem is writable by the 'gpio' group — no sudo required.
    """
    try:
        with open('/dev/gpiomem', 'r+b') as f:
            mem = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
            gpfsel0 = struct.unpack_from('<I', mem, 0)[0]
            # BCM2 bits [8:6], BCM3 bits [11:9]; ALT0 = 0b100 = 4
            gpfsel0 = (gpfsel0 & ~(0b111 << 6)) | (0b100 << 6)
            gpfsel0 = (gpfsel0 & ~(0b111 << 9)) | (0b100 << 9)
            struct.pack_into('<I', mem, 0, gpfsel0)
            mem.close()
    except Exception as exc:
        print(f"[I2C] Warning: could not restore ALT0 pin function: {exc}")


def i2c_sda_stuck() -> bool:
    """
    Return True if SDA (BCM2) is being held LOW (bus locked).

    Reads GPLEV0 bit 2 via /dev/gpiomem — does NOT modify any pin function
    register, so it is completely safe to call while the i2c_bcm2835 driver
    is active. (Using GPIO.setup() here would corrupt ALT0 and lock the bus.)
    """
    try:
        with open('/dev/gpiomem', 'r+b') as f:
            mem = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ)
            gplev0 = struct.unpack_from('<I', mem, 0x34)[0]   # GPLEV0 register
            mem.close()
        return not bool(gplev0 & (1 << 2))   # BCM2 = bit 2; LOW → stuck
    except Exception:
        return False


def i2c_bus_recover() -> None:
    """
    Send 9 SCL pulses via bit-bang to release a device holding SDA low.

    BCM pin 2 = SDA, BCM pin 3 = SCL.
    Called ONLY when a bus fault is actually detected — not on every read.
    After bit-bang, gpio_restore_i2c_alt0() is called to restore ALT0.
    """
    try:
        import RPi.GPIO as GPIO
        SDA, SCL = 2, 3
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(SCL, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(SDA, GPIO.IN)   # float SDA — let the stuck device release it
        for _ in range(9):
            GPIO.output(SCL, GPIO.LOW);  time.sleep(0.00005)
            GPIO.output(SCL, GPIO.HIGH); time.sleep(0.00005)
            if GPIO.input(SDA):          # SDA released — bus is free
                break
        # Issue a STOP condition: SCL high, SDA low → high
        GPIO.setup(SDA, GPIO.OUT, initial=GPIO.LOW)
        time.sleep(0.00005)
        GPIO.output(SDA, GPIO.HIGH)
        GPIO.cleanup([SCL, SDA])   # release GPIO lib's hold on the pins
    except Exception:
        pass
    # CRITICAL: restore BCM2/BCM3 to ALT0 after bit-bang.
    # GPIO.cleanup() leaves pins as INPUT (000) but i2c_bcm2835 needs ALT0 (100).
    gpio_restore_i2c_alt0()
    time.sleep(0.010)   # let bus settle (≥10 ms) before busio reopens it
    print("[I2C] Bus recovery complete.")


@contextmanager
def i2c_open():
    """
    Context manager: acquires the global I2C bus lock, opens busio.I2C,
    yields it, and always deinits + releases the lock.

    Use this for one-shot I2C operations (calibration, diagnostics).
    The sensor poll loop manages its own handle for performance.
    """
    import board
    import busio
    _i2c_bus_lock.acquire()
    i2c = None
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        yield i2c
    finally:
        if i2c is not None:
            try:
                i2c.deinit()
            except Exception:
                pass
        _i2c_bus_lock.release()


# ── ADS1115 helper ────────────────────────────────────────────────────────────

def read_smoothed_channel(ads, pin) -> float:
    """Average ADS_SAMPLES readings from one ADS1115 channel, discarding the highest outlier."""
    from adafruit_ads1x15.analog_in import AnalogIn
    ch = AnalogIn(ads, pin)
    samples = [float(ch.voltage) for _ in range(ADS_SAMPLES)
               if not time.sleep(ADS_SAMPLE_DELAY)]
    if len(samples) > 2:
        samples.remove(max(samples))   # drop worst spike
    return sum(samples) / len(samples)


# ── Full hardware read ────────────────────────────────────────────────────────

def read_hardware() -> dict:
    """
    Read BME280 (temp/humidity) and all 4 ADS1115 channels (soil moisture).
    Returns a flat dict with all values; None for any channel that failed.

    I2C fault handling:
      - SDA stuck LOW → bit-bang recovery before opening the bus
      - BME280 init error or all-zeros → bus recovery + retry at 0x77
      - ADS1115 OSError → one recovery attempt before giving up
    """
    result: dict[str, float | None] = {
        "temperature": None, "humidity": None,
        **{f"soil_probe_{z}": None         for z in VALID_ZONES},
        **{f"soil_probe_{z}_voltage": None  for z in VALID_ZONES},
    }
    missing: list[str] = []

    try:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        from adafruit_bme280 import basic as adafruit_bme280
    except ImportError as exc:
        set_sensor_component("bme280",       False, "Driver unavailable")
        set_sensor_component("ads1115_0x48", False, "Driver unavailable")
        update_sensor({"last_error": f"Import error: {exc}"})
        with _sensor_lock:
            _sensor_status["missing_inputs"] = ["Driver unavailable"]
        return result

    def _reopen(old_i2c):
        try:
            old_i2c.deinit()
        except Exception:
            pass
        i2c_bus_recover()
        return busio.I2C(board.SCL, board.SDA)

    with _i2c_bus_lock:
        if i2c_sda_stuck():
            print("[I2C] SDA stuck low — running bus recovery before read.")
            i2c_bus_recover()

        i2c = None
        try:
            i2c = busio.I2C(board.SCL, board.SDA)

            # ── BME280 ──────────────────────────────────────────────
            bme_ok = False
            for addr in BME280_ADDRESSES:
                try:
                    bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
                    time.sleep(0.05)   # BME280 internal reset time
                    temp = float(bme.temperature)
                    hum  = float(bme.humidity)
                    if temp == 0.0 and hum == 0.0:
                        print(f"[I2C] BME280 @ {hex(addr)} all-zeros — recovering bus.")
                        i2c = _reopen(i2c)
                        continue
                    result["temperature"] = round(temp, 1)
                    result["humidity"]    = round(hum, 1)
                    set_sensor_component("bme280", True, f"BME280 @ {hex(addr)}")
                    bme_ok = True
                    break
                except Exception as bme_exc:
                    print(f"[I2C] BME280 @ {hex(addr)} error ({bme_exc}) — recovering bus.")
                    i2c = _reopen(i2c)
            if not bme_ok:
                missing.append("BME280@0x76/0x77")
                set_sensor_component("bme280", False, "BME280 not found at 0x76 or 0x77")

            # ── ADS1115 ─────────────────────────────────────────────
            for _attempt in range(2):
                try:
                    ads = ADS.ADS1115(i2c, address=0x48)  # type: ignore[attr-defined]
                    for zone_id in VALID_ZONES:
                        ch = ZONE_ADS_CHANNEL[zone_id]
                        _ = AnalogIn(ads, ch).voltage   # discard — MUX settling
                        time.sleep(0.02)
                        v = read_smoothed_channel(ads, ch)
                        result[f"soil_probe_{zone_id}_voltage"] = round(v, 4)
                        result[f"soil_probe_{zone_id}"] = round(
                            clamp((1.0 - v / 3.3) * 100.0, 0.0, 100.0), 1
                        )
                    set_sensor_component("ads1115_0x48", True, "ADS1115 @ 0x48")
                    break
                except OSError as exc:
                    if _attempt == 0:
                        print(f"[I2C] ADS1115 OSError ({exc}) — recovering bus.")
                        i2c = _reopen(i2c)
                    else:
                        missing.append("ADS1115@0x48")
                        set_sensor_component("ads1115_0x48", False, f"ADS1115 @ 0x48: {exc}")
                except Exception as exc:
                    missing.append("ADS1115@0x48")
                    set_sensor_component("ads1115_0x48", False, f"ADS1115 @ 0x48: {exc}")
                    break

        except Exception as exc:
            set_sensor_component("bme280",       False, "I2C bus unavailable")
            set_sensor_component("ads1115_0x48", False, "I2C bus unavailable")
            missing = ["I2C bus unavailable"]
            update_sensor({"last_error": f"I2C read: {exc}"})
        finally:
            if i2c is not None:
                try:
                    i2c.deinit()
                except Exception:
                    pass

    with _sensor_lock:
        _sensor_status["missing_inputs"] = missing
    return result
