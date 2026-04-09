# Hardware Wiring Guide: Automated Irrigation System

**⚠️ CRITICAL SAFETY WARNING:** This system uses two completely separate power supplies: a 5V USB-C supply for the Raspberry Pi, and a 12V DC supply for the water valves. **Never connect the 12V power supply or the valves directly to any pin on the Raspberry Pi.** Doing so will instantly destroy the computer. 

---

## 1. The I2C Sensor Bus (The Brains)
The BME280 (Climate) and both ADS1115 modules (Analog-to-Digital Converters) all share the exact same 4 pins on the Raspberry Pi using the I2C protocol. Use a breadboard to branch these connections.



[Image of Raspberry Pi 4 GPIO pinout]


**Raspberry Pi to Breadboard (The Main Trunk):**
* **Pin 1 (3.3V)** ➔ Breadboard Positive (+) Rail
* **Pin 6 (GND)** ➔ Breadboard Negative (-) Rail
* **Pin 3 (GPIO 2 / SDA)** ➔ Breadboard Data Line
* **Pin 5 (GPIO 3 / SCL)** ➔ Breadboard Clock Line

**Sensors to Breadboard:**
Connect the `VIN`/`VCC`, `GND`, `SDA`, and `SCL` pins of the **BME280** and **single ADS1115** module to those respective shared lines on the breadboard.

### ADS1115 Address
This build uses a single ADS1115 at address **0x48**. Tie its `ADDR` pin to **Ground (GND)**.

---

## 2. The Capacitive Soil Moisture Sensors (The Inputs)
You have 8 analog soil moisture sensors (2 per zone). They must be powered by the same 3.3V rail as the ADS1115 to ensure their analog signals do not exceed the ADS1115's maximum voltage rating.

**Powering the Sensors (Use Ethernet/Shielded Cable for distance):**
* **VCC** (All 8 sensors) ➔ 3.3V Breadboard Rail
* **GND** (All 8 sensors) ➔ GND Breadboard Rail

**Routing the Analog Data (AOUT pin):**
* **Zone 1, Sensor A** ➔ ADS1115 #1, Pin A0
* **Zone 1, Sensor B** ➔ ADS1115 #1, Pin A1
* **Zone 2, Sensor A** ➔ ADS1115 #1, Pin A2
* **Zone 2, Sensor B** ➔ ADS1115 #1, Pin A3
* **Zone 3, Sensor A** ➔ ADS1115 #1, Pin A2
* **Zone 3, Sensor B** ➔ ADS1115 #1, Pin A3
* **Zone 4, Sensor A** ➔ (unused in single-ADS build)
* **Zone 4, Sensor B** ➔ (unused in single-ADS build)

---

## 3. The Relay Module (The Switch)
The relay acts as the protective middleman between the Pi's 3.3V brain and the 12V muscle. 

**Raspberry Pi to 4-Channel Relay Control Pins:**
* **Pin 2 or 4 (5V)** ➔ Relay `VCC` (Relays need 5V to switch, do not use 3.3V here!)
* **Pin 14 (GND)** ➔ Relay `GND`
* **Pin 11 (GPIO 17)** ➔ Relay `IN1` (Controls Zone 1)
* **Pin 13 (GPIO 27)** ➔ Relay `IN2` (Controls Zone 2)
* **Pin 15 (GPIO 22)** ➔ Relay `IN3` (Controls Zone 3)
* **Pin 16 (GPIO 23)** ➔ Relay `IN4` (Controls Zone 4)

---

## 4. The 12V Solenoid Valves (The Muscle)
This circuit is completely physically isolated from the Raspberry Pi. The relay simply closes the loop.



**Wiring the 12V Power Supply and Valves:**
1. Connect the **Positive (+)** wire from your 12V Power Supply directly to the **COM (Common)** screw terminal on *all four* relays. You can use a short jumper wire to daisy-chain the COM ports together.
2. Connect the **NO (Normally Open)** screw terminal of Relay 1 to the positive wire of Valve 1. Repeat for Relays 2, 3, and 4.
3. Connect all the **Negative (-)** wires from all four valves together, and run them straight back to the **Negative (-)** wire of your 12V Power Supply.

*(Note: Most standard 12V solenoid valves do not have a strict polarity. If the two wires on the valve are the exact same color, it doesn't matter which one is positive and which is negative).*