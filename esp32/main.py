# main.py - ESP32 MicroPython firmware (upload with Thonny).
# Reads sensors, reports them to the backend, and switches the relays as the
# backend commands. No decision logic here, except the fail-safe:
# if the backend is unreachable for FALLBACK_AFTER_S, it runs PIR-only
# (light on for FALLBACK_HOLD_S after the last motion, fan off) until the
# backend answers again.
#
# Needs MicroPython 1.20 or newer (for the "timeout" option of requests).
# Settings (Wi-Fi, laptop IP, relay polarity) are in config.py.

import time

import dht
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
# GPIO34 (ACS712 current) and GPIO35 (ZMPT101B voltage) are NOT used
# until mains is wired by a qualified person.

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


def read_dht():
    """Update temp/hum. On a failed read, keep the last good values."""
    global temp, hum
    try:
        dht_sensor.measure()
        temp = dht_sensor.temperature()
        hum = dht_sensor.humidity()
    except OSError:
        print("DHT11 read failed - keeping last value")


def fake_power_w():
    # *** FAKE POWER ***  Not measured! Just the sum of constants for the
    # relays that are ON. Real measurement needs ACS712/ZMPT101B on mains.
    return (config.LIGHT_W if light_on else 0) + (config.FAN_W if fan_on else 0)


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
    last_backend_ok = time.ticks_ms()   # start counting from boot
    last_motion = None                  # ticks of the last PIR=1 (for the fail-safe)
    in_failsafe = False

    while True:
        cycle_start = time.ticks_ms()

        # Wi-Fi lost? Start a reconnect in the background every 15 s
        # (do not wait here, so the loop keeps running).
        if not wlan.isconnected() and \
                time.ticks_diff(cycle_start, last_wifi_try) > config.WIFI_TIMEOUT_S * 1000:
            last_wifi_try = cycle_start
            print("Wi-Fi lost - reconnecting in background")
            try:
                wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
            except OSError:
                pass

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

        # Report and ask the backend
        cmd = None
        problem = "Wi-Fi not connected"
        if wlan.isconnected():
            try:
                post_telemetry({"pir": pir, "ldr": ldr, "temp": temp, "hum": hum,
                                "power_w": fake_power_w()})
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
        print("%s IST | PIR=%d LDR=%4d T=%s H=%s | light=%s fan=%s | %s" % (
            ist_clock(), pir, ldr,
            "--" if temp is None else "%dC" % temp,
            "--" if hum is None else "%d%%" % hum,
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
