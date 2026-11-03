# Smart Classroom Light & Fan Automation

Automatically switches one classroom's **light and fan** on and off based on the
**class timetable** and **room occupancy** (PIR motion sensor). A web dashboard
shows the watchman the live status and the (estimated) power consumption.

> **Safety:** all development and demos use **LEDs** (or 12 V DC loads) on the relay
> outputs. **Do not connect anything to 230 V mains.** Real mains wiring comes last,
> only with the department's or an electrician's permission, using an
> **opto-isolated AC-rated relay module**, with the logic side and the mains side
> **physically separated**. See [docs/wiring.md](docs/wiring.md).

---

## How it works

```
 ESP32 (MicroPython)                         Laptop (Flask + SQLite)
 ------------------                          -----------------------
 every 2 s:                                  
   read PIR, LDR, DHT11                      
   POST /telemetry  ───────────────────────► store reading
   GET  /command    ───────────────────────► logic.decide(timetable, last motion)
   switch relays    ◄─────────────────────── {light, fan, reason}
                                             
                                             dashboard (browser) polls /api/* every 2.5 s
```

**The rule** (in `backend/logic.py`):

```
ON  =  class scheduled now (from 5 min before start until the end)
       AND  room occupied (PIR motion within the last 10 min)
```

- **Lead time** (`LEAD_MIN = 5`): lights may switch on 5 minutes before class.
- **Hold time** (`HOLD_MIN = 10`, sensible range 5-15): a PIR detects *motion*, not
  *presence* - students sitting still trigger nothing - so the room counts as
  occupied for 10 minutes after the last motion.
- Every command has a readable **reason**: `class scheduled and room occupied`,
  `class scheduled but no motion`, `no class scheduled`.
- **Watchman switch:** the dashboard has one **ON/OFF** button. It shows the current
  state; pressing it flips the output (both relays) manually. Pressing it again
  returns to automatic. A manual state also ends by itself after
  `OVERRIDE_MIN = 60` minutes, so nothing stays on by mistake. The reason then reads
  e.g. `watchman override: ON until 14:05 (auto: no class scheduled)`.
- The ESP32 makes **no decisions** - except the **fail-safe**: if the backend is
  unreachable for 20 s it switches to PIR-only mode (light on for 5 min after the
  last motion, fan off) and returns to normal automatically when the backend answers.
- Time: decisions use the **laptop's clock**. The ESP32 gets UTC from NTP and adds
  IST (+5:30) in one function, only for its printed status line.
- **Power is estimated** (40 W light + 60 W fan when the relay is on) until real
  sensors are wired. Energy = sum(power × time) / 3600 / 1000 kWh, gaps over 120 s ignored.

## Folder structure

```
classroom-automation/
├── README.md
├── backend/                  runs on the laptop
│   ├── app.py                Flask: endpoints + dashboard
│   ├── db.py                 SQLite schema + timetable seeding
│   ├── logic.py              decision rules (pure functions, unit-tested)
│   ├── requirements.txt
│   ├── classroom.db          created automatically (git-ignored)
│   ├── tools/fake_esp.py     pretends to be the ESP32
│   ├── tools/add_test_slot.py  adds/removes a TEST class at the current time
│   ├── alerts.py             SMS alerts (background thread, never blocks)
│   ├── alert_config.example.json  template for alert_config.json (git-ignored)
│   ├── tests/               91 unit tests (test_logic.py, test_features.py)
│   ├── templates/dashboard.html
│   └── static/dashboard.js
├── esp32/                    copied to the board with Thonny
│   ├── config.example.py     template (safe to share)
│   ├── config.py             real Wi-Fi + laptop IP (git-ignored)
│   └── main.py               firmware
└── docs/
    ├── timetable.csv         the classroom's slots
    ├── wiring.md             pin map, circuits, safety
    └── test_log.md           bench test checklist + results
```

## API

| Method | Path | Used by | Returns |
|--------|------|---------|---------|
| POST | `/telemetry` | ESP32 | stores `{pir, ldr, temp, hum, power_w}` |
| GET | `/command` | ESP32 | `{light, fan, reason}` |
| GET | `/api/status` | dashboard | latest reading, command, current/next class, ESP online |
| GET | `/api/consumption` | dashboard | today's kWh + recent power samples |
| GET | `/api/timetable` | dashboard | all slots |
| GET | `/api/sms-log` | dashboard | last 10 SMS attempts with Twilio status |
| POST | `/api/sms-refresh` | dashboard | re-check delivery status of recent SMS |
| POST | `/api/toggle` | dashboard ON/OFF button | flips the output; second press = automatic |
| POST | `/api/test-alert` (or `/test-alert`) | dashboard "Send test SMS" | sends a test SMS in the background |
| POST | `/api/override` | (advanced) | `{device: light/fan, mode: on/off/auto}` |
| GET | `/` | browser | the dashboard |

---

## 1. Install (laptop, once)

Needs **Python 3.10+** (tested with 3.11) and **Thonny**.

Open PowerShell:

```
cd D:\SEM-5\IoT\ISE_Project\Smart_classroom\classroom-automation
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

Check:

```
cd backend
..\venv\Scripts\python.exe -m unittest discover -s tests
```

Expected: `Ran 91 tests ... OK`.

> All commands below are run from `classroom-automation\backend` and always start
> with `..\venv\Scripts\python.exe`. Typing just `tools\add_test_slot.py` does **not** work.

## 2. Timetable

Edit [docs/timetable.csv](docs/timetable.csv) (one class per line):

```
day,start,end,subject
Mon,09:00,10:00,Maths
```

- Days: `Mon Tue Wed Thu Fri Sat Sun` (3 letters). Times: 24-hour `HH:MM`. No commas in the subject.
- The current file contains **PLACEHOLDER** slots - replace them with the real timetable.
- Load it into the database: `..\venv\Scripts\python.exe db.py --reseed`
  (this also removes any TEST slots). `..\venv\Scripts\python.exe db.py` shows what is stored.

## 3. Network

The ESP32 and the laptop must be on the **same 2.4 GHz Wi-Fi** (the ESP32 cannot use 5 GHz).

- Many college networks **block device-to-device traffic**. If the ESP32 cannot
  reach the laptop, use a **phone hotspot** set to **2.4 GHz**
  (Android: Hotspot → AP band / "Extend compatibility"; iPhone: "Maximise Compatibility").
  Connect the **laptop** to the same hotspot.
- Quick check: phone on the same Wi-Fi (mobile data off) → open `http://<laptop-IP>:5000`.
  If the page loads, the ESP32 can reach the laptop too.

### Laptop IP (check every session - it changes)

```
ipconfig
```

Under **Wireless LAN adapter Wi-Fi** copy the **IPv4 Address**, e.g. `10.98.9.4`.
Flask also prints it when it starts (`Running on http://10.98.9.4:5000`).

### Windows firewall

The first time Flask runs, Windows may ask whether Python may accept connections:
tick **Private networks** and click **Allow**. If the Wi-Fi is marked *Public*,
either allow Public too or set the network to Private
(Settings → Network & Internet → Wi-Fi → your network → Private).
Third-party antivirus firewalls (e.g. McAfee) can block it separately.

## 4. Configure and upload the ESP32 (Thonny)

1. Copy `esp32\config.example.py` to `esp32\config.py` (already done in this repo) and set:
   ```python
   WIFI_SSID = "your Wi-Fi name"        # exact, including spaces/full stops
   WIFI_PASSWORD = "your password"
   SERVER_URL = "http://10.98.9.4:5000" # your laptop IP from ipconfig
   ```
2. Plug in the ESP32 (USB data cable). In Thonny: **Tools → Options → Interpreter** →
   **MicroPython (ESP32)** → port **CP210x (COMx)**.
   No MicroPython yet? Same dialog → *Install or update MicroPython* → ESP32 → latest (1.20+ needed).
3. Click **Stop** until you see `>>>`.
4. **File → Open → This computer** → `esp32\config.py` → **File → Save as → MicroPython device** → `config.py`.
5. Same for `esp32\main.py` → `main.py`.
6. Click in the Shell, press **Ctrl+D**. Expected:
   ```
   === Smart classroom ESP32 starting ===
   Wi-Fi OK, ESP32 IP = 10.98.9.225
   NTP time synced
   16:20:01 IST | PIR=0 LDR=1234 T=28C H=60% | light=OFF fan=OFF | no class scheduled
   ```

`main.py` now runs automatically whenever the board gets power - Thonny is only
needed to upload files or watch the output.

> **When the laptop IP changes:** edit `config.py`, **close any old `config.py` tab in
> Thonny first**, open the file from the laptop again and save it to the device.
> Uploading a stale tab puts the old IP on the board.

### Relay polarity test

1. In Thonny press **Stop** (relays switch OFF).
2. In the Shell:
   ```python
   from machine import Pin
   r = Pin(25, Pin.OUT)
   r.value(0)
   ```
3. Relay clicks / LED on → **active-low** → keep `RELAY_ACTIVE_LOW = True`.
   Nothing → `r.value(1)`; if *that* switches it on → set `RELAY_ACTIVE_LOW = False` and upload `config.py`.
4. Repeat with `Pin(33, Pin.OUT)` for the fan relay. Press **Ctrl+D** to restart `main.py`.

### Settings (esp32/config.py)

| Setting | Default | Meaning |
|---------|---------|---------|
| `RELAY_ACTIVE_LOW` | `True` | relay polarity |
| `LIGHT_W`, `FAN_W` | 40, 60 | **estimated** watts reported while a relay is on |
| `LOOP_S` | 2 | seconds per cycle |
| `DHT_EVERY_S` | 5 | DHT11 read interval |
| `WIFI_TIMEOUT_S` | 15 | one Wi-Fi attempt |
| `HTTP_TIMEOUT_S` | 5 | one backend request |
| `NTP_RESYNC_S` | 10800 | clock re-sync (3 h) |
| `FALLBACK_AFTER_S` | 20 | backend silence before fail-safe |
| `FALLBACK_HOLD_S` | 300 | fail-safe: light on this long after motion |

Backend settings are in `backend/logic.py`: `LEAD_MIN = 5`, `HOLD_MIN = 10`, `OVERRIDE_MIN = 60`.
Restart Flask after changing them.

## 5. Run

**Window 1** - backend (leave it running):

```
cd D:\SEM-5\IoT\ISE_Project\Smart_classroom\classroom-automation\backend
..\venv\Scripts\python.exe app.py
```

You should see `"POST /telemetry HTTP/1.1" 200` and `"GET /command HTTP/1.1" 200`
from the ESP32 every 2 s.

**Dashboard:** open `http://127.0.0.1:5000` on the laptop, or `http://<laptop-IP>:5000`
on any phone on the same Wi-Fi. (Needs internet for the Tailwind and Chart.js CDNs.)

**Window 2** - tools:

```
..\venv\Scripts\python.exe tools\add_test_slot.py               TEST class now, 30 min
..\venv\Scripts\python.exe tools\add_test_slot.py --minutes 5   TEST class now, 5 min
..\venv\Scripts\python.exe tools\add_test_slot.py --start-in 8  starts in 8 min (lead-time test)
..\venv\Scripts\python.exe tools\add_test_slot.py --remove      delete all TEST slots
..\venv\Scripts\python.exe tools\fake_esp.py --pir 1 --loop     fake ESP32 (no hardware needed)
..\venv\Scripts\python.exe db.py                                show database contents
```

> Keep Flask running whenever the ESP32 is on. Without Flask, the ESP32 goes into
> fail-safe after 20 s and the light follows motion only.

---

## 6. Fan by temperature, daylight saving, SMS alerts

All three run on the laptop (`backend/logic.py`, `backend/app.py`); the ESP32 only
sends readings. Settings are constants at the top of the sections in `logic.py`
(restart Flask after changing them).

### Fan by temperature (DHT11)

The fan needs **class + occupied** (as before) **and** a warm room:

| Setting | Default | Meaning |
|---------|---------|---------|
| `FAN_ON_C` | 28 | fan turns ON at 28 °C or more |
| `FAN_OFF_C` | 27 | ...and stays ON until it is **below** 27 °C (hysteresis, no relay chatter) |
| `TEMP_FRESH_S` / `TEMP_GRACE_S` | 15 / 60 | no valid reading for 15 s: keep the previous fan state; for 60 s: fan OFF |

Readings outside 0-50 °C (the DHT11 range) count as invalid. The reason says what
happened, e.g. `fan on: 29.5 C >= 28 C`, `fan off: 24.0 C is below 28 C`,
`fan off: no valid temperature reading for over 60 s`.

### Daylight saving (LDR)

If it is bright enough, the **light** stays OFF (`light off: enough daylight`);
the fan keeps following its own rule. Two thresholds stop the bulb's own light
from making it flicker: daylight starts **above `DAY_OFF`** and ends only **below
`DAY_ON`**, and there are at least `DAY_MIN_INTERVAL_S` (60 s) between changes.

#### LDR calibration (5 minutes, with the dashboard open)

1. **Direction.** Watch "Light (LDR)" in the Sensors card. Cover the LDR with a
   finger, then shine a phone torch on it.
   - Number goes **up** with more light -> keep `LDR_HIGHER_IS_BRIGHTER = True`
     (this is the case for 3.3 V -> LDR -> GPIO32 -> resistor -> GND).
   - Number goes **down** -> set `LDR_HIGHER_IS_BRIGHTER = False`. The dashboard then
     shows the turned-round value (4095 - raw) and marks it "[inverted LDR]".
2. **Two readings** (use the brightness number the dashboard shows next to "Daylight: ... now"):
   - **A** = room in normal daytime light with the bulb **OFF** (curtains open).
   - **B** = evening / curtains closed with the bulb **ON**.
3. **Thresholds.** Choose `DAY_OFF` a little **below A** and `DAY_ON` a little **above B**,
   with a clear gap: e.g. A = 2900, B = 1300 -> `DAY_OFF = 2600`, `DAY_ON = 1700`.
   If A and B are close together, move the LDR away from the bulb (towards the window).
4. Restart Flask and check the Light card: "light off: enough daylight" in daylight,
   "light on: ..." when it is dark.

### SMS alerts to the watchman (Twilio)

The server sends an SMS to the watchman's mobile when the **light has been ON for 20
minutes with no class** (e.g. a manual switch-on that was forgotten):
`Classroom 1: the class is cancelled but the lights have been on for 20 min. Please switch off the lights and fans.`
(change the wording in `ALERT_TEXT` in `logic.py`) (`ALERT_AFTER_MIN = 20` in `logic.py`,
real time).

**How it works** (`backend/alerts.py`, official `twilio` Python SDK):

- **One SMS per episode.** An episode starts when the light is first seen ON with no
  class and ends when the light goes OFF. Each episode gets a row in the `sms_log`
  table *before* sending; a unique index on the episode makes a second SMS for the same
  episode impossible, however often the ESP32 or the dashboard polls.
- **Never blocks control.** Sending runs in a background thread with a 10 s timeout.
  No internet, wrong credentials or a Twilio refusal are logged and shown on the
  dashboard; the light/fan decisions carry on.
- **Accepted is not delivered.** Twilio first answers `queued` (accepted). The server
  then asks Twilio for the real status after 5, 15, 30 and 60 s using the message SID:
  `sent` -> `delivered`, or `undelivered` / `failed` with the error code and a readable
  explanation. "Check delivery status" on the dashboard asks again later.
- **Retries:** only after HTTP 429 (rate limit; Twilio did not accept the message, so
  no duplicate is possible), at most 2 times with 1 s / 2 s backoff.
- **Secrets** are never logged or sent to the browser; phone numbers are masked
  (`+91******1038`) and the message SID is shortened.
- **Delivery-status callbacks** (Twilio calling *us*) are not used: they need a public
  HTTPS URL, which a laptop on a hotspot does not have. If the server is ever deployed
  publicly, add a `StatusCallback` URL and validate each callback with
  `twilio.request_validator.RequestValidator` (X-Twilio-Signature) - never trust an
  unsigned status update.

**Setup:**

1. **Account:** sign up at twilio.com/try-twilio. Console home -> "Account Info" shows
   the **Account SID** (`AC...`) and the **Auth Token** (click "show").
2. **Sender:** the SMS-capable Twilio number shown as "From" in Console -> *Try out SMS*,
   or Console -> Phone Numbers -> Manage -> Active numbers. Use E.164 format, `+1...`.
3. **Recipient:** the watchman's number in E.164 format, e.g. `+91` + 10 digits. A trial
   account can only send to **verified** numbers (Console -> Phone Numbers -> Verified
   Caller IDs; your sign-up number is verified automatically; max 5).
4. **India allowed:** Console -> Messaging -> Settings -> **Geo permissions** -> tick India.
5. **Configure** without committing secrets: copy `backend/alert_config.example.json`
   to `backend/alert_config.json` (git-ignored) and fill in `SMS_PROVIDER` (`"twilio"`),
   `SMS_TO`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`. Or set environment
   variables with the same names (they win over the file). Placeholder values count as
   missing; the dashboard's SMS card lists exactly what is missing or invalid.
6. **Install** the SDK once: `..\venv\Scripts\python.exe -m pip install -r requirements.txt`
7. **Test:** restart Flask, press **Send test SMS** (max once per 30 s). The card shows
   ⏳ queued/sent, then ✔ delivered or ✖ with the reason.
8. **Real alert:** no class scheduled, press the Switch ON, wait 20 minutes -> exactly one
   SMS; switch OFF and ON again for the next episode.
9. **Diagnose:** Console -> Monitor -> Logs -> Messaging shows every message with its
   status and error code (Twilio's error dictionary: twilio.com/docs/api/errors).

**Trial account limits (important):**

- A **trial account can only send Twilio's predefined templates, not custom text.** Our
  alert text is refused with **error 572006** ("Invalid template name. Trial accounts can
  only use predefined SMS templates") - the dashboard says so. To send the real alerts,
  **upgrade the account** (add billing in the Console).
- Trial: verified recipients only (max 5), about 50 messages per day, and a
  "Sent from a Twilio trial account" prefix.
- India: messages from an international number arrive from a random shared sender and
  can still be filtered by Indian carriers (status `undelivered`, e.g. error 30007).
  Upgrading removes the trial limits but **does not guarantee delivery**; always check the
  status in the dashboard or the Twilio logs.

The laptop needs **internet** for SMS (phone hotspot with mobile data on).

---

## Demo script (about 8 minutes)

Before the demo: laptop + ESP32 on the same Wi-Fi, IP checked, Flask running,
dashboard open, `tools\add_test_slot.py --remove` run, PIR warmed up.

| # | Say | Do | Audience sees |
|---|-----|----|---------------|
| 1 | "No class now, so nothing switches on - even with people in the room." | Wave at the PIR | Dashboard: Room **Occupied**, Light/Fan **OFF**, reason `no class scheduled`; LED off |
| 2 | "Now a class starts." | `tools\add_test_slot.py --minutes 15`, wave | Within ~3 s: Light/Fan **ON**, Class now **TEST**, chart jumps to 100 W, LED on |
| 3 | "Students sit still - PIR sees nothing - but the light stays on for the hold time." | Stand still | PIR=0 but still ON, "Last motion hh:mm:ss (hold 10 min)" |
| 4 | "The laptop decides - the ESP32 only reports and obeys. Every decision has a reason." | Point at the reason bar and the Thonny status line | Same reason on both |
| 5 | "If the server fails, the room is not stuck." | Ctrl+C in window 1 | ~20 s later Thonny: `entering FAIL-SAFE`, fan OFF, light stays ON; dashboard banner **Server unreachable** |
| 6 | "When it comes back, it recovers by itself." | Start `app.py` again | Thonny: `Backend is back`, ON/ON again; banner disappears |
| 7 | "The class ends - everything switches off." | `tools\add_test_slot.py --remove` | Light/Fan **OFF**, `no class scheduled`; LED off |
| 8 | "The watchman sees today's energy and the timetable." | Scroll the dashboard | kWh number, power chart, timetable with today in bold |

Optional extras: show `docs/test_log.md` (all bench tests passed) and run the unit tests.

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|--------------|-----|
| Thonny: `backend unreachable ([Errno 116] ETIMEDOUT)` right after boot | Wrong `SERVER_URL` (laptop IP changed), or an old `config.py` tab was uploaded | `ipconfig`, fix config.py, close old tab, re-upload. Check on the board: `import config; print(config.SERVER_URL)` |
| `ECONNREFUSED` / `refused` | Flask not running | Start `app.py` in window 1 |
| ETIMEDOUT although the IP is right | Firewall, or the Wi-Fi blocks device-to-device traffic | Phone test (section 3); allow Python in the firewall; use a 2.4 GHz phone hotspot |
| `Wi-Fi timeout after 15 s` | Wrong SSID/password, or 5 GHz network | SSID must match exactly (spaces, full stops); use 2.4 GHz |
| Thonny shows "Device is busy or does not respond" | Thonny connected while `main.py` was running | Normal - wait, or click Stop |
| Nothing appears in Thonny | `main.py` stopped or Thonny lost the port | Click Stop, check the port (bottom-right), press Ctrl+D |
| `could not open port COM9` | Another program uses the port | Close the other program (only one can use COM9) |
| `AttributeError: ... FALLBACK_AFTER_S` | New `main.py` with an old `config.py` | Upload both files |
| Light turns on by itself, fan stays off | Flask is not running → fail-safe (light follows motion) | Start Flask |
| Stays ON long after people leave | Hold time is 10 min (by design) | Wait, or lower `HOLD_MIN` (5-15) and restart Flask |
| Stays OFF with a class and people | PIR not seeing motion, or no class at that time | Check `PIR=1` in Thonny; check the timetable / `db.py` |
| `PIR=1` all the time | PIR warming up, sensitivity too high, sees a moving fan | Wait 60 s; turn the sensitivity pot down; point it away |
| `DHT11 read failed` every cycle | Loose wire / wrong pin | Reseat wires; test in Thonny: `import dht, machine; d=dht.DHT11(machine.Pin(26)); d.measure(); print(d.temperature())` |
| LDR always ~0 | Divider resistor too small, or DO pin used | See [docs/wiring.md](docs/wiring.md#ldr-voltage-divider) |
| Relays do the opposite of the Shell line | Polarity | Run the polarity test, change `RELAY_ACTIVE_LOW` |
| `Address already in use` when starting Flask | Another `app.py` is running | Close it (Ctrl+C in that window) |
| Dashboard unstyled / no chart | No internet → CDNs cannot load | Connect the laptop to the internet |
| Dashboard red banner | Flask stopped | Start `app.py` |
| A test switches on/off unexpectedly | Leftover TEST slot | `tools\add_test_slot.py --remove` |

## Tests

- **Unit tests** (decision logic, next class, energy, fan/daylight hysteresis,
  SMS alerts with a mocked provider, the Flask server on a temporary database):
  `..\venv\Scripts\python.exe -m unittest discover -s tests`
- **Bench tests** with the real hardware: [docs/test_log.md](docs/test_log.md)
  (all 10 passed on 15-10-2026 and 21-10-2026).

## Not done yet (optional stages)

- Real power measurement (ACS712 + ZMPT101B) and mains installation - **only with
  permission and a qualified electrician**.
