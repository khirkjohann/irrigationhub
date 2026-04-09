import board
import busio
import time
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_bme280 import basic as adafruit_bme280

# Initialize I2C bus
i2c = board.I2C()

print("--- Initializing Sensor Test ---")

# Test BME280 (Temperature/Humidity)
try:
    bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x76)
    print(f"[SUCCESS] BME280 - Temp: {bme280.temperature:.1f} °C | Humidity: {bme280.humidity:.1f} %")
except Exception as e:
    print(f"[FAIL] BME280: {e}")

def read_ads_module(i2c_bus, address):
    try:
        ads = ADS.ADS1115(i2c_bus, address=address)
        channels = [0, 1, 2, 3]
        readings = []
        for idx, pin in enumerate(channels):
            # Discard the first read after mux switch to reduce carry-over between channels.
            _ = AnalogIn(ads, pin).value
            time.sleep(0.02)

            raw_samples = []
            volt_samples = []
            for _ in range(5):
                analog = AnalogIn(ads, pin)
                raw_samples.append(analog.value)
                volt_samples.append(analog.voltage)
                time.sleep(0.01)

            raw_avg = int(sum(raw_samples) / len(raw_samples))
            volt_avg = sum(volt_samples) / len(volt_samples)
            readings.append(
                (
                    f"A{idx}: raw_avg={raw_avg} "
                    f"volt_avg={volt_avg:.3f}V "
                    f"raw_min={min(raw_samples)} raw_max={max(raw_samples)}"
                )
            )
        print(f"[SUCCESS] ADS1115@{hex(address)} -> " + " | ".join(readings))
    except Exception as e:
        print(f"[FAIL] ADS1115@{hex(address)}: {e}")


# Single ADS1115 layout: Zone 1=A0, Zone 2=A1, Zone 3=A2, Zone 4=A3.
read_ads_module(i2c, 0x48)

print("--------------------------------")