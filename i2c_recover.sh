#!/bin/bash
# I2C bus recovery for Pi 4B (BCM2711 / i2c_bcm2835 driver).
#
# Usage:
#   i2c_recover.sh           — only recovers if SDA is stuck LOW (for ExecStartPre)
#   i2c_recover.sh --force   — always bit-bangs 9 recovery pulses (for in-app use)
#
# Must run as root (modprobe requires root).

# Always run forced recovery on boot — ensures a clean bus state regardless
# of whether SDA appears stuck. The BME280 can glitch even when SDA looks HIGH.
# Pass --check-only to skip recovery when SDA is actually HIGH (legacy mode).
FORCE=1
[[ "${1:-}" == "--check-only" ]] && FORCE=0

# Unload I2C driver so pins 2/3 are free for bit-bang.
modprobe -r i2c_bcm2835 2>/dev/null || true
sleep 0.1

# Run the bit-bang recovery.
FORCE_RECOVERY="$FORCE" python3 - << 'PYEOF'
import RPi.GPIO as GPIO, time, os

SDA, SCL = 2, 3
force = os.environ.get("FORCE_RECOVERY", "0") == "1"

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(SCL, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(SDA, GPIO.IN)

if not force and GPIO.input(SDA) == GPIO.HIGH:
    print("[I2C-RECOVER] SDA HIGH — bus clean, skipping recovery.", flush=True)
else:
    if force:
        print("[I2C-RECOVER] Forced recovery — sending 9 SCL pulses.", flush=True)
    else:
        print("[I2C-RECOVER] SDA stuck LOW — sending 9 recovery pulses.", flush=True)
    for i in range(9):
        GPIO.output(SCL, GPIO.LOW);  time.sleep(0.0001)
        GPIO.output(SCL, GPIO.HIGH); time.sleep(0.0001)
        if GPIO.input(SDA) == GPIO.HIGH:
            print(f"[I2C-RECOVER] SDA released after {i+1} pulses.", flush=True)
            break
    else:
        print("[I2C-RECOVER] SDA still stuck after 9 pulses — check wiring.", flush=True)
    # Issue STOP condition: SCL high, SDA low→high
    GPIO.setup(SDA, GPIO.OUT, initial=GPIO.LOW)
    time.sleep(0.0001)
    GPIO.output(SDA, GPIO.HIGH)
    time.sleep(0.001)

GPIO.cleanup()
print("[I2C-RECOVER] Done.", flush=True)
PYEOF

# Reload I2C driver.
modprobe i2c_bcm2835 2>/dev/null || true
sleep 0.2
