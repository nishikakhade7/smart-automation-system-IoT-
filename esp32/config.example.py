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

# --- FAKE power numbers (watts) reported while a relay is ON.
#     Real power measurement comes only after mains wiring. ---
LIGHT_W = 40
FAN_W = 60

# --- Timing (seconds) ---
LOOP_S = 2              # one sensor + backend cycle every 2 s
DHT_EVERY_S = 5         # DHT11 is slow; read it every 5 s
WIFI_TIMEOUT_S = 15     # give up one Wi-Fi attempt after 15 s
HTTP_TIMEOUT_S = 5      # give up one backend request after 5 s
NTP_RESYNC_S = 3 * 3600 # re-sync the clock every 3 hours

# --- Fail-safe: if the backend cannot be reached for FALLBACK_AFTER_S,
#     switch to PIR-only mode: light ON for FALLBACK_HOLD_S after the last
#     motion, fan OFF. Normal mode returns as soon as the backend answers. ---
FALLBACK_AFTER_S = 20
FALLBACK_HOLD_S = 5 * 60
