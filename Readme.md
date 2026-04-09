# Causwagan Norte Automated Irrigation System

A Raspberry Pi-based smart irrigation system built as a thesis project. It monitors soil moisture and ambient climate across four crop zones, automatically opens and closes solenoid valves to hit per-crop moisture targets, and provides a web dashboard for monitoring, manual control, analytics, and thesis data collection.

---

## Architecture

Three Flask services run concurrently on the Raspberry Pi:

| Service | File | Default Port | Purpose |
|---|---|---|---|
| Main dashboard | `app.py` | **5000** | Live control, zone management, analytics, calibration |
| Logs viewer | `app.py` (sub-app) | **5001** | Structured event log browser |
| Thesis dashboard | `thesis_dashboard.py` | **5002** | Thesis data collection panels (calibration tables, stress tests, ML volumetric output) |

All three share the same SQLite database at `/home/pi/irrigation_data.db` and the same physical I2C bus and GPIO pins. **Do not run the thesis dashboard and the main app simultaneously while doing GPIO-intensive tests.**

---

## Quick Start

```bash
# 1. Activate the virtual environment
source /home/pi/irrigation_env/bin/activate

# 2. (First run only) initialise the database
python setup_db.py

# 3. Start the main app
python app.py

# 4. In a second terminal, start the thesis dashboard (optional)
source /home/pi/irrigation_env/bin/activate
python thesis_dashboard.py
```

The systemd services `irrigation_network_bootstrap.service` (Wi-Fi / hotspot fallback) and `irrigation_web.service` (starts `app.py` on boot) handle this automatically on a production boot. On startup the Pi tries to connect to the Wi-Fi profile `g30`; if that is unavailable it brings up a `Hotspot` on `wlan0`.

---

## Main App Pages (`app.py`)

| URL | Description |
|---|---|
| `/` or `/home` | Live overview — all zones, climate, valve states |
| `/zones` | Zone list with auto-control status |
| `/zone/<id>` | Per-zone detail: moisture history, profile settings, calibration |
| `/analytics` | Trend charts, CSV export |
| `/testing` | Manual valve pulse control with testing lock |
| `/hardware` | I2C scan, relay self-test, system shutdown |

### Key REST endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/dashboard` | GET | Full live state (zones, climate, runtime config) |
| `/api/trends` | GET | Historical sensor data for charts |
| `/api/zone/<id>/history` | GET | Per-zone moisture history |
| `/api/system/status` | GET | Sensor health, GPIO state, zone sensor states |
| `/api/valve/<id>` | POST | Open/close a valve (manual or auto source) |
| `/api/calibration/capture-live` | POST | Snapshot live ADC voltage as a dry or wet calibration point |
| `/api/calibration/baseline` | POST/DELETE | Save or remove a soil baseline (dry/wet voltage pair) |
| `/api/calibration/crop-target` | POST/DELETE | Save or remove a crop-specific moisture target voltage |
| `/api/zone/<id>/mapping` | POST | Assign a soil baseline and crop target to a zone |
| `/api/zone/<id>/profile` | POST | Update crop assignment and target moisture % |
| `/api/zone/<id>/disable` | POST | Enable or disable a zone |
| `/api/diagnostics/i2c-scan` | POST | Live I2C bus scan |
| `/api/diagnostics/relay-test` | POST | Brief relay pulse test |
| `/export/csv` | GET | Download all sensor data as CSV |

---

## Auto-Control Logic

The background control loop runs every 10 seconds (configurable). For each enabled zone it:

1. Reads the latest soil moisture percentage (averaged from the last N ADC samples).
2. Compares it against the zone's crop-specific target with a ±3% hysteresis band to avoid rapid cycling.
3. Uses a 20-minute predictive lookahead — if moisture is trending down and is projected to breach the target before the next check, irrigation starts early.
4. Applies a 10-minute hardware failsafe: any valve that has been open longer than the failsafe limit is closed automatically, regardless of sensor state.
5. Skips a zone silently if its sensor is producing a floating/unstable signal (rapid unexplained swings), logging the event as `zone_disabled_floating`.

Manual valve commands block auto-control for the duration of a user-specified hold period.

### Crop moisture targets (default)

| Crop | Target moisture |
|---|---|
| Corn | 30 % |
| Cassava | 35 % |
| Peanuts | 25 % |
| Custom | User-defined |

---

## Thesis Dashboard Panels (`thesis_dashboard.py`)

| Panel | Thesis table | Purpose |
|---|---|---|
| Calibration Panel | Table 3 | Capture dry/wet voltage baselines, derive raw-to-% conversion |
| Hardware Stress Test | Table 5 | Step-response timing, ADC jitter, sensor drift |
| Relay & Queue Override | Table 1 | Verify sequential irrigation logic and relay switching |
| ML Volumetric Test | Table 2 | Run the regression model and log predicted volume/duration |

Calibration data is written to `/home/pi/thesis_calibration.json` and optionally synced back to the main SQLite database so zone profiles pick up the new baselines automatically.

An optional trained scikit-learn model can be placed at `/home/pi/irrigation_model.joblib`. If not present, the built-in linear regression formula is used.

---

## Database Schema

SQLite file: `/home/pi/irrigation_data.db`

| Table | Contents |
|---|---|
| `sensor_data` | Timestamped temperature, humidity, and four soil moisture readings |
| `valve_status` | Current ON/OFF state per zone |
| `zone_profile` | Per-zone crop assignment, target moisture %, disabled flag, and linked baseline/target IDs |
| `control_events` | Audit log of every valve open/close with source (auto/manual) and detail |
| `soil_baseline` | Named dry/wet voltage pairs for different soil types |
| `crop_target` | Named target voltages for crop-specific irrigation thresholds |
| `testing_lock` | Prevents auto-control from firing during active manual test sessions |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IRRIGATION_MAIN_PORT` | `5000` | Port for the main dashboard |
| `IRRIGATION_LOG_VIEWER_PORT` | `5001` | Port for the logs viewer |
| `THESIS_PORT` | `5002` | Port for the thesis dashboard |
| `IRRIGATION_RELAY_ACTIVE_LOW` | `1` | Set to `0` if your relay board is active-HIGH |
| `IRRIGATION_ENABLE_AUTO_CONTROL` | `1` | Set to `0` to disable the auto-control loop |
| `IRRIGATION_CONTROL_LOOP_SECONDS` | `10` | How often the control loop evaluates each zone |
| `IRRIGATION_AUTO_HYSTERESIS` | `3` | ±% band around the moisture target |
| `IRRIGATION_AUTO_PREDICT_MINUTES` | `20` | Predictive lookahead window |
| `IRRIGATION_AUTO_FAILSAFE_MINUTES` | `10` | Maximum valve-open time before forced close |
| `IRRIGATION_FLOATING_WINDOW_SAMPLES` | *(default)* | Samples used for floating-sensor detection |
| `IRRIGATION_FLOATING_RANGE_THRESHOLD` | *(default)* | Voltage span threshold for float detection |
| `IRRIGATION_FLOATING_AVG_DELTA_THRESHOLD` | *(default)* | Average step threshold for float detection |
| `THESIS_ML_MODEL` | `/home/pi/irrigation_model.joblib` | Path to optional trained regression model |

---

## Hardware Wiring

**⚠️ CRITICAL SAFETY WARNING:** This system uses two completely separate power supplies: a 5V USB-C supply for the Raspberry Pi, and a 12V DC supply for the water valves. **Never connect the 12V power supply or the valves directly to any pin on the Raspberry Pi.** Doing so will instantly destroy the computer.

---

### 1. The I2C Sensor Bus

The BME280 (climate) and the ADS1115 (ADC) share the same 4 I2C pins on the Raspberry Pi. Use a breadboard to branch these connections.

**Raspberry Pi to Breadboard:**
* **Pin 1 (3.3V)** ➔ Breadboard Positive (+) Rail
* **Pin 6 (GND)** ➔ Breadboard Negative (-) Rail
* **Pin 3 (GPIO 2 / SDA)** ➔ Breadboard Data Line
* **Pin 5 (GPIO 3 / SCL)** ➔ Breadboard Clock Line

Connect the `VIN`/`VCC`, `GND`, `SDA`, and `SCL` pins of the **BME280** and the **ADS1115** to those respective shared lines on the breadboard.

**ADS1115 I2C Address:** `0x48` — tie its `ADDR` pin to **GND**.  
**BME280 I2C Address:** `0x76` (auto-fallback to `0x77`).

---

### 2. Capacitive Soil Moisture Sensors

One sensor per zone, powered from the 3.3V rail. All sensors must use 3.3V — do not use 5V; the ADS1115 input cannot exceed the supply voltage.

**Power (all 4 sensors):**
* **VCC** ➔ 3.3V Breadboard Rail
* **GND** ➔ GND Breadboard Rail

**Analog output (AOUT pin):**
* **Zone 1 — SEN0308 #1** ➔ ADS1115 **A0**
* **Zone 2 — SEN0308 #2** ➔ ADS1115 **A1**
* **Zone 3 — SEN0193** ➔ ADS1115 **A2**
* **Zone 4 — Generic v1.2** ➔ ADS1115 **A3**

Use shielded or twisted-pair cable for long runs to the sensors.

---

### 3. The 4-Channel Relay Module

**Raspberry Pi to Relay Control Pins:**
* **Pin 2 or 4 (5V)** ➔ Relay `VCC` (relays need 5V — do not use 3.3V)
* **Pin 14 (GND)** ➔ Relay `GND`
* **Pin 11 (BCM 17)** ➔ Relay `IN1` — Zone 1
* **Pin 13 (BCM 27)** ➔ Relay `IN2` — Zone 2
* **Pin 15 (BCM 22)** ➔ Relay `IN3` — Zone 3
* **Pin 16 (BCM 23)** ➔ Relay `IN4` — Zone 4

The relay board used here is **active-LOW** (opto-isolated). Setting `IRRIGATION_RELAY_ACTIVE_LOW=1` (the default) is correct for this board.

---

### 4. The 12V Solenoid Valves and Pump

This circuit is completely physically isolated from the Raspberry Pi — the relay simply closes the 12V loop.

**Wiring the 12V supply and valves:**
1. Connect the **Positive (+)** wire from the 12V supply directly to the **COM (Common)** screw terminal on all four relays — daisy-chain the COM ports with jumper wire.
2. Connect each relay's **NO (Normally Open)** terminal to the positive wire of its corresponding valve.
3. Connect all valve **Negative (-)** wires together and run them back to the **Negative (-)** of the 12V supply.

The water pump is fired automatically via a **hardware diode interlock** — it turns on whenever any valve opens and turns off when all valves close. No additional GPIO pin is required for the pump.

*(Most 12V solenoid valves have no strict polarity. If both wires are the same colour, either terminal can be positive.)*