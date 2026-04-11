#!/usr/bin/env python3
"""
I2C bus recovery script: sends 9 SCL clock pulses to unstick any device
holding SDA LOW after an incomplete transaction (e.g. power cut mid-read).

Uses lgpio (Pi 5 compatible). Runs as ExecStartPre in irrigation_web.service,
BEFORE the I2C kernel driver reclaims the pins.

BCM pin 2 = SDA, BCM pin 3 = SCL.
"""
import time
import sys

SDA = 2
SCL = 3

try:
    import lgpio

    # Use gpiochip4 on Pi 5, fall back to gpiochip0 on older Pi.
    for chip_id in (4, 0):
        try:
            h = lgpio.gpiochip_open(chip_id)
            break
        except Exception:
            h = None

    if h is None:
        print("[I2C-RECOVER] Could not open any gpiochip — skipping.", flush=True)
        sys.exit(0)

    # Check if SDA is being held LOW.
    lgpio.gpio_claim_input(h, SDA)
    sda_state = lgpio.gpio_read(h, SDA)

    if sda_state != 0:
        print("[I2C-RECOVER] SDA is HIGH — bus is clean, skipping recovery.", flush=True)
        lgpio.gpiochip_close(h)
        sys.exit(0)

    print("[I2C-RECOVER] SDA stuck LOW — sending 9 recovery clock pulses.", flush=True)

    lgpio.gpio_claim_output(h, SCL, 1)   # SCL high
    lgpio.gpio_claim_input(h, SDA)        # float SDA

    for i in range(9):
        lgpio.gpio_write(h, SCL, 0)
        time.sleep(0.00005)
        lgpio.gpio_write(h, SCL, 1)
        time.sleep(0.00005)
        if lgpio.gpio_read(h, SDA) == 1:
            print(f"[I2C-RECOVER] SDA released after {i+1} pulses.", flush=True)
            break

    # Send STOP condition: SCL high, SDA low → high
    lgpio.gpio_claim_output(h, SDA, 0)
    time.sleep(0.00005)
    lgpio.gpio_write(h, SDA, 1)
    time.sleep(0.001)

    # Release pins so the I2C kernel driver can reclaim them
    lgpio.gpio_free(h, SCL)
    lgpio.gpio_free(h, SDA)
    lgpio.gpiochip_close(h)

    print("[I2C-RECOVER] Bus recovery complete.", flush=True)

except Exception as exc:
    print(f"[I2C-RECOVER] Failed (non-fatal): {exc}", flush=True)

sys.exit(0)
