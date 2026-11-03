# main.py - ESP32 MicroPython firmware (upload with Thonny).
# Reads sensors, reports them to the backend, and switches the relays as the
# backend commands. No decision logic here, except the fail-safe:
# if the backend is unreachable for FALLBACK_AFTER_S, it runs PIR-only
# (light on for FALLBACK_HOLD_S after the last motion, fan off) until the
# backend answers again.
#
# Needs MicroPython 1.20 or newer (for the "timeout" option of requests).
# Settings (Wi-Fi, laptop IP, relay polarity) are in config.py.

import math
import time

import dht
import machine
import network
import ntptime
from machine import ADC, Pin

try:
    import urequests as requests
except ImportError:
    import requests

import config

# ---------- pins (see docs/wiring.md) ----------
PIN_PIR = 27
PIN_DHT = 26
PIN_LDR = 32            # ADC1 - ADC2 pins do not work while Wi-Fi is on
PIN_RELAY_LIGHT = 25    # relay IN1
PIN_RELAY_FAN = 33      # relay IN2
PIN_RELAY_SPARE = (18, 19)  # relay IN3 / IN4, unused, kept OFF
PIN_ACS712 = 34         # ACS712 OUT through the 2.2k / 4.4k divider (ADC1, input only)
PIN_ZMPT = 35           # ZMPT101B OUT (ADC1, input only) - mains voltage

IST_OFFSET_S = 19800    # India = UTC + 5:30


# ---------- relays (set up FIRST so they are OFF from the very start) ----------

def relay_level(on):
    """Pin level that switches a relay ON or OFF, depending on polarity."""
    if config.RELAY_ACTIVE_LOW:
        return 0 if on else 1
    return 1 if on else 0


relay_light = Pin(PIN_RELAY_LIGHT, Pin.OUT, value=relay_level(False))
relay_fan = Pin(PIN_RELAY_FAN, Pin.OUT, value=relay_level(False))
relay_spares = [Pin(p, Pin.OUT, value=relay_level(False)) for p in PIN_RELAY_SPARE]

light_on = False
fan_on = False


def set_relays(light, fan):
    global light_on, fan_on
    relay_light.value(relay_level(light))
    relay_fan.value(relay_level(fan))
    light_on, fan_on = light, fan


# ---------- sensors ----------

# PIR: the interrupt remembers any motion between two loop cycles,
# so a short pulse is not missed while we are busy with Wi-Fi.
pir_pin = Pin(PIN_PIR, Pin.IN)
motion_seen = False


def on_motion(pin):
    global motion_seen
    motion_seen = True


pir_pin.irq(trigger=Pin.IRQ_RISING, handler=on_motion)


def read_pir():
    """1 if there was motion since the last call (or right now), else 0."""
    global motion_seen
    value = 1 if (motion_seen or pir_pin.value()) else 0
    motion_seen = False
    return value


ldr_adc = ADC(Pin(PIN_LDR))
ldr_adc.atten(ADC.ATTN_11DB)    # full 0-3.3 V range


def read_ldr():
    """Average of 5 ADC readings (0-4095) to smooth out noise."""
    total = 0
    for _ in range(5):
        total += ldr_adc.read()
        time.sleep_ms(2)
    return total // 5


dht_sensor = dht.DHT11(Pin(PIN_DHT))   # DHT11, NOT DHT22
temp = None
hum = None
dht_ok_at = None        # ticks of the last good DHT11 reading


def read_dht():
    """Update temp/hum. On a failed read, keep the last good values."""
    global temp, hum, dht_ok_at
    try:
        dht_sensor.measure()
        temp = dht_sensor.temperature()
        hum = dht_sensor.humidity()
        dht_ok_at = time.ticks_ms()
    except OSError:
        print("DHT11 read failed - keeping last value")


def dht_fresh():
    """True if the last good DHT11 reading is recent. Old values are not sent,
    so the server can see that the sensor is failing (its fan rule needs that)."""
    return dht_ok_at is not None and \
        time.ticks_diff(time.ticks_ms(), dht_ok_at) < getattr(config, "DHT_STALE_S", 15) * 1000


def fake_power_w():
    # *** ESTIMATED POWER *** (used when MEASURE_CURRENT is False): the sum of
    # the rated watts of the relays that are ON.
    return (config.LIGHT_W if light_on else 0) + (config.FAN_W if fan_on else 0)


# ACS712 current sensor (only when MEASURE_CURRENT = True in config.py)
MEASURE_CURRENT = getattr(config, "MEASURE_CURRENT", False)
if MEASURE_CURRENT:
    acs_adc = ADC(Pin(PIN_ACS712))
    acs_adc.atten(ADC.ATTN_11DB)    # 0-3.3 V range
acs_offset_uv = 1667000             # first guess of the zero-current level (2.5 V x 0.667)


def read_current_a():
    """RMS current through the ACS712, measured over 100 ms (= 5 mains cycles at 50 Hz).
    The zero-current level is learned from the samples themselves, so the ACS712's
    drift does not matter. Readings below CURRENT_NOISE_A count as 0."""
    global acs_offset_uv
    n = 0
    s = 0       # sum of (sample - offset)
    s2 = 0      # sum of (sample - offset)^2 ; integers, so no rounding errors
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < 100:
        d = acs_adc.read_uv() - acs_offset_uv
        n += 1
        s += d
        s2 += d * d
    mean_d = s / n
    acs_offset_uv += int(mean_d)                       # track the zero level for next time
    var_uv2 = s2 / n - mean_d * mean_d
    rms_v = math.sqrt(var_uv2) / 1e6 if var_uv2 > 0 else 0.0     # volts at GPIO34
    amps = rms_v / config.ACS712_DIVIDER / (config.ACS712_MV_PER_A / 1000) * config.CURRENT_CAL
    return 0.0 if amps < config.CURRENT_NOISE_A else amps


# ZMPT101B voltage sensor (only when MEASURE_VOLTAGE = True in config.py)
MEASURE_VOLTAGE = getattr(config, "MEASURE_VOLTAGE", False)
if MEASURE_VOLTAGE:
    zmpt_adc = ADC(Pin(PIN_ZMPT))
    zmpt_adc.atten(ADC.ATTN_11DB)
zmpt_offset_uv = 1650000             # first guess of the zero level (half of 3.3 V)


def read_voltage():
    """RMS mains voltage from the ZMPT101B over 100 ms (5 cycles at 50 Hz).
    Returns (signal_mv, volts): signal_mv = RMS signal at GPIO35 in millivolts
    (always sent, used for calibration); volts = signal x VOLTAGE_CAL, or None
    while VOLTAGE_CAL is 0 (not calibrated yet)."""
    global zmpt_offset_uv
    n = s = s2 = 0
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < 100:
        d = zmpt_adc.read_uv() - zmpt_offset_uv
        n += 1
        s += d
        s2 += d * d
    mean_d = s / n
    zmpt_offset_uv += int(mean_d)
    var = s2 / n - mean_d * mean_d
    signal_mv = math.sqrt(var) / 1000 if var > 0 else 0.0
    if signal_mv < getattr(config, "VOLTAGE_NOISE_MV", 20):
        signal_mv = 0.0                                   # no mains on the sensor
    cal = getattr(config, "VOLTAGE_CAL", 0)
    return signal_mv, (signal_mv / 1000 * cal if cal else None)


# ---------- Wi-Fi ----------

wlan = network.WLAN(network.STA_IF)


def connect_wifi():
    """One connection attempt, waiting up to WIFI_TIMEOUT_S. Returns True/False."""
    wlan.active(True)
    if wlan.isconnected():
        return True
    print("Connecting to Wi-Fi '%s' ..." % config.WIFI_SSID)
    try:
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    except OSError as e:
        print("Wi-Fi connect error:", e)
    start = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), start) > config.WIFI_TIMEOUT_S * 1000:
            print("Wi-Fi timeout after %d s" % config.WIFI_TIMEOUT_S)
            try:
                wlan.disconnect()
            except OSError:
                pass
            return False
        time.sleep_ms(250)
    print("Wi-Fi OK, ESP32 IP =", wlan.ifconfig()[0])
    return True


# ---------- time ----------

time_synced = False


def sync_ntp():
    """Set the ESP32 clock from the internet. ntptime sets UTC."""
    global time_synced
    try:
        ntptime.settime()
        time_synced = True
        print("NTP time synced")
    except Exception as e:
        print("NTP sync failed:", e)
    return time_synced


def ist_now():
    """The ONLY place where IST is calculated: UTC + 5:30."""
    return time.localtime(time.time() + IST_OFFSET_S)


def ist_clock():
    if not time_synced:
        return "--:--:--"
    t = ist_now()
    return "%02d:%02d:%02d" % (t[3], t[4], t[5])


# ---------- backend calls (always close the response) ----------

def post_telemetry(data):
    r = requests.post(config.SERVER_URL + "/telemetry", json=data,
                      timeout=config.HTTP_TIMEOUT_S)
    try:
        if r.status_code != 200:
            raise OSError("telemetry HTTP %d" % r.status_code)
    finally:
        r.close()


def get_command():
    r = requests.get(config.SERVER_URL + "/command", timeout=config.HTTP_TIMEOUT_S)
    try:
        if r.status_code != 200:
            raise OSError("command HTTP %d" % r.status_code)
        return r.json()
    finally:
        r.close()


# ---------- fail-safe (used only while the backend is unreachable) ----------

def failsafe_light(last_motion, now):
    """PIR-only rule: light ON if motion was seen in the last FALLBACK_HOLD_S."""
    if last_motion is None:
        return False
    return time.ticks_diff(now, last_motion) < config.FALLBACK_HOLD_S * 1000


# ---------- main loop ----------

def main():
    print("\n=== Smart classroom ESP32 starting ===")
    set_relays(False, False)

    # At boot: try Wi-Fi twice, then carry on (the loop keeps retrying).
    for _ in range(2):
        if connect_wifi():
            break
    if wlan.isconnected():
        sync_ntp()

    last_dht = None
    last_ntp = time.ticks_ms()
    last_wifi_try = time.ticks_ms()
    last_wifi_ok = time.ticks_ms()      # when Wi-Fi was last connected
    last_backend_ok = time.ticks_ms()   # start counting from boot
    last_motion = None                  # ticks of the last PIR=1 (for the fail-safe)
    in_failsafe = False

    while True:
        cycle_start = time.ticks_ms()

        # Wi-Fi lost? Start a reconnect in the background every 15 s
        # (do not wait here, so the loop keeps running). Drop the old attempt
        # first: after a long outage the Wi-Fi chip can get stuck "connecting".
        if wlan.isconnected():
            last_wifi_ok = cycle_start
        elif time.ticks_diff(cycle_start, last_wifi_try) > config.WIFI_TIMEOUT_S * 1000:
            last_wifi_try = cycle_start
            print("Wi-Fi lost - reconnecting in background")
            try:
                wlan.disconnect()
            except OSError:
                pass
            try:
                wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
            except OSError:
                pass

        # Still no Wi-Fi after WIFI_RESET_S: restart the board for a clean
        # reconnect (relays start OFF at boot, the fail-safe takes over after).
        if time.ticks_diff(cycle_start, last_wifi_ok) > getattr(config, "WIFI_RESET_S", 300) * 1000:
            print(">>> No Wi-Fi for %d s - restarting the ESP32" % getattr(config, "WIFI_RESET_S", 300))
            set_relays(False, False)
            time.sleep_ms(200)
            machine.reset()

        # Clock: re-sync every 3 h (or every minute until the first success)
        resync_ms = (config.NTP_RESYNC_S if time_synced else 60) * 1000
        if wlan.isconnected() and time.ticks_diff(cycle_start, last_ntp) > resync_ms:
            last_ntp = cycle_start
            sync_ntp()

        # Sensors
        pir = read_pir()
        if pir:
            last_motion = cycle_start
        ldr = read_ldr()
        if last_dht is None or time.ticks_diff(cycle_start, last_dht) >= config.DHT_EVERY_S * 1000:
            last_dht = cycle_start
            read_dht()

        # Power: measured with the ACS712, or estimated from the relay state
        voltage_mv, voltage_v = read_voltage() if MEASURE_VOLTAGE else (None, None)
        if MEASURE_CURRENT:
            current_a = read_current_a()
            # apparent power: measured voltage if calibrated, else the nominal mains voltage
            power_w = (voltage_v if voltage_v else config.MAINS_V) * current_a
        else:
            current_a = None
            power_w = fake_power_w()

        # Report and ask the backend
        cmd = None
        problem = "Wi-Fi not connected"
        if wlan.isconnected():
            try:
                fresh = dht_fresh()
                post_telemetry({"pir": pir, "ldr": ldr,
                                "temp": temp if fresh else None, "hum": hum if fresh else None,
                                "power_w": power_w, "current_a": current_a,
                                "voltage_v": voltage_v, "voltage_mv": voltage_mv})
                cmd = get_command()
            except Exception as e:
                problem = "backend unreachable (%s)" % e

        if cmd is not None:
            # Normal mode: obey the backend
            if in_failsafe:
                print(">>> Backend is back - leaving FAIL-SAFE mode")
                in_failsafe = False
            last_backend_ok = cycle_start
            set_relays(bool(cmd["light"]), bool(cmd["fan"]))
            reason = cmd.get("reason", "")
        else:
            down_s = time.ticks_diff(cycle_start, last_backend_ok) // 1000
            if down_s >= config.FALLBACK_AFTER_S:
                # Fail-safe mode: PIR only, fan always off
                if not in_failsafe:
                    print(">>> No backend for %d s - entering FAIL-SAFE mode" % down_s)
                    in_failsafe = True
                set_relays(failsafe_light(last_motion, cycle_start), False)
                reason = "FAIL-SAFE (PIR only, %s for %d s)" % (problem, down_s)
            else:
                reason = "%s - keeping last state, fail-safe in %d s" % (
                    problem, config.FALLBACK_AFTER_S - down_s)

        # One clear status line per cycle
        print("%s IST | PIR=%d LDR=%4d T=%s H=%s V=%s I=%s P=%dW | light=%s fan=%s | %s" % (
            ist_clock(), pir, ldr,
            "--" if temp is None else "%dC" % temp,
            "--" if hum is None else "%d%%" % hum,
            "--" if voltage_mv is None else ("%dV" % voltage_v if voltage_v is not None else "%dmV(uncal)" % voltage_mv),
            "--" if current_a is None else "%.2fA" % current_a, int(power_w),
            "ON " if light_on else "OFF", "ON " if fan_on else "OFF", reason))

        # Wait for the rest of the 2 s cycle
        used = time.ticks_diff(time.ticks_ms(), cycle_start)
        if used < config.LOOP_S * 1000:
            time.sleep_ms(config.LOOP_S * 1000 - used)


try:
    main()
except KeyboardInterrupt:
    # Thonny's Stop button lands here: leave everything switched off.
    set_relays(False, False)
    print("Stopped - relays OFF")
