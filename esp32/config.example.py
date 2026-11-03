# config.example.py - TEMPLATE for the ESP32 settings. Safe to share.
# Copy this file to config.py and put your real hotspot name, password and
# laptop IP there. config.py is git-ignored. Upload BOTH config.py and
# main.py to the ESP32 with Thonny.

# --- Wi-Fi: your PHONE HOTSPOT (must be 2.4 GHz; ESP32 cannot see 5 GHz) ---
WIFI_SSID = "YOUR_HOTSPOT_NAME"
WIFI_PASSWORD = "YOUR_HOTSPOT_PASSWORD"

# --- Laptop running Flask. Find the IP with "ipconfig" while the laptop is on
#     the hotspot (look for "IPv4 Address" under the Wi-Fi adapter). ---
SERVER_URL = "http://192.168.43.100:5000"

# --- Relay module polarity. Most cheap 4-channel boards switch ON when the
#     input is LOW -> True. Do the polarity test in the README to be sure. ---
RELAY_ACTIVE_LOW = True

# --- ESTIMATED power (watts) of what each relay switches, from the label on
#     the bulb / fan. Used only when MEASURE_CURRENT is False. ---
LIGHT_W = 40
FAN_W = 60

# --- Real current measurement with the ACS712 (mains wiring by the lab in-charge).
#     ACS712 OUT -> 2.2k -> GPIO34 -> 4.4k (two 2.2k) -> GND. ---
MEASURE_CURRENT = False   # True once the ACS712 is wired
ACS712_MV_PER_A = 185     # printed on the chip: 05B -> 185, 20A -> 100, 30A -> 66
ACS712_DIVIDER = 0.667    # 4.4k / (2.2k + 4.4k)
CURRENT_CAL = 1.0         # calibration factor: multimeter amps / dashboard amps
CURRENT_NOISE_A = 0.10    # readings below this count as 0 A (sensor noise; measured ~0.06 A at 0 A)
MAINS_V = 230             # nominal mains voltage for power = V x I

# --- Timing (seconds) ---
LOOP_S = 2              # one sensor + backend cycle every 2 s
DHT_EVERY_S = 5         # DHT11 is slow; read it every 5 s
DHT_STALE_S = 15        # no good DHT11 reading for 15 s -> send "no value" (server turns fan off)
WIFI_TIMEOUT_S = 15     # give up one Wi-Fi attempt after 15 s
WIFI_RESET_S = 300      # no Wi-Fi for 5 min -> restart the ESP32 (clean reconnect)
HTTP_TIMEOUT_S = 5      # give up one backend request after 5 s
NTP_RESYNC_S = 3 * 3600 # re-sync the clock every 3 hours

# --- Fail-safe: if the backend cannot be reached for FALLBACK_AFTER_S,
#     switch to PIR-only mode: light ON for FALLBACK_HOLD_S after the last
#     motion, fan OFF. Normal mode returns as soon as the backend answers. ---
FALLBACK_AFTER_S = 20
FALLBACK_HOLD_S = 5 * 60

# --- Real voltage measurement with the ZMPT101B (mains wiring by the lab in-charge).
#     ZMPT101B VCC -> 3.3 V, GND -> GND, OUT -> GPIO35. ---
MEASURE_VOLTAGE = False     # True once the ZMPT101B is wired
VOLTAGE_CAL = 0           # 0 = not calibrated. Calibrate: mains volts (multimeter) / (signal mV / 1000)
VOLTAGE_NOISE_MV = 20     # signals below this count as "no mains"
