# Bench Test Log

All tests use **LEDs on the relay outputs** (5 V → COM, NO → 330 Ω → LED → GND).
**No 230 V mains anywhere.**

Tick a box only after you have seen the result yourself. Write the date and
what you saw in the "Result / notes" column.

## Before every test session

- [ ] Laptop and ESP32 on the same Wi-Fi (2.4 GHz)
- [ ] `ipconfig` IPv4 address matches `SERVER_URL` in `esp32/config.py`
      (if not: edit config.py, close the old config.py tab in Thonny, upload again)
- [ ] Flask running in window 1: `..\venv\Scripts\python.exe app.py`
      (prints `Running on http://<laptop-IP>:5000`)
- [ ] No old TEST slots: `..\venv\Scripts\python.exe tools\add_test_slot.py --remove`
- [ ] Unit tests pass: `..\venv\Scripts\python.exe -m unittest discover -s tests` → `OK`
- [ ] PIR warmed up (60 s after power-on), jumper on **H**, time pot at minimum

All commands are run in PowerShell from `classroom-automation\backend`.
"Shell" means the Thonny Shell (or the serial monitor).

## Checklist

| #  | Test | Done | Date | Result / notes |
|----|------|------|------|----------------|
| 1  | Data reaches the backend | [x] | 15-10-2026 | Flask log shows `POST /telemetry 200` + `GET /command 200` every 2 s |
| 2  | Relays follow commands | [x] | 21-10-2026 | TEST added → ON/ON within 2 s, removed → OFF/OFF within 3 s; LED on relay 1 followed both |
| 3  | Schedule match (class + motion → ON) | [x] | 15-10-2026 | `class scheduled and room occupied` → ON/ON |
| 4  | Hold timer (HOLD_MIN = 1) | [x] | 15-10-2026, 21-10-2026 | 21-10: TEST slot kept, last `PIR=1` 16:00:21 → 28× `PIR=0` → OFF ~16:01:21 (`no motion`); motion 16:01:32 → ON. LED followed |
| 5  | 5-minute lead time | [x] | 21-10-2026 | class 16:09 → OFF (`no class scheduled`) until 16:03:59, ON from 16:04:00 with `PIR=1` throughout; LED followed |
| 6  | Class end | [x] | 21-10-2026 | class 16:07-16:09, `PIR=1` in all 66 readings → OFF at 16:09:00 (`no class scheduled`); LED followed |
| 7  | No class → OFF even with motion | [x] | 21-10-2026 | 30 s waving, 15/15 `PIR=1`, 15/15 `light=OFF fan=OFF` (`no class scheduled`) |
| 8  | Fail-safe when Flask is stopped | [x] | 21-10-2026 | countdown, `entering FAIL-SAFE` after 25 s, light ON / fan OFF |
| 9  | Recovery when Flask restarts | [x] | 21-10-2026 | `Backend is back` 2 s after restart, ON/ON again |
| 10 | Relays OFF at boot / on Stop | [x] | 21-10-2026 | Ctrl+C → `Stopped - relays OFF`; reset → OFF through boot + Wi-Fi (7 s) until first command; LED followed |

Notes from 21-10-2026:
- Only one external LED (on one relay channel); the other channel was checked via the ESP32 log only.
- ESP32 clock prints ~2 s ahead of the laptop; decisions use the laptop clock, so switch times are exact.
- HC-SR501 stays HIGH for a few seconds after motion stops (its own time pot) - expected.

---

## Test details

### 1. Data reaches the backend
1. Flask running, ESP32 powered.
2. **Expect** in window 1, every 2 s:
   `"POST /telemetry HTTP/1.1" 200` and `"GET /command HTTP/1.1" 200` from the ESP32's IP.
3. Open `http://127.0.0.1:5000/api/status` → `"esp_online": true`, `seconds_since_data` below 3.
- **Fail:** Shell says `backend unreachable` → wrong IP in config.py, Flask not running, or firewall.

### 2. Relays follow commands
1. `tools\add_test_slot.py --minutes 10`, wave at the PIR.
2. **Expect:** Shell `light=ON fan=ON` → both relays click, both LEDs ON.
3. `tools\add_test_slot.py --remove`.
4. **Expect:** Shell `light=OFF fan=OFF` → both relays click, LEDs OFF.
- **Fail:** LEDs do the opposite → set `RELAY_ACTIVE_LOW` the other way in config.py and upload it.
  No click at all → relay VCC/GND/IN wiring.

### 3. Schedule match
1. `tools\add_test_slot.py --minutes 10`, keep still / cover the PIR.
2. **Expect:** `light=OFF fan=OFF | class scheduled but no motion`.
3. Wave at the PIR.
4. **Expect within ~2 s:** `light=ON fan=ON | class scheduled and room occupied`.
5. Clean up: `tools\add_test_slot.py --remove`.

### 4. Hold timer (uses HOLD_MIN = 1)
1. In `backend\logic.py` set `HOLD_MIN = 1` and restart Flask.
   Check `/api/status` shows `"hold_min": 1`.
2. `tools\add_test_slot.py --minutes 10`, wave → ON/ON.
3. Cover the PIR / move away. Note the time of the **last `PIR=1`** line.
4. **Expect:** still ON for ~1 min (`PIR=0` but `room occupied`), then
   `light=OFF fan=OFF | class scheduled but no motion` about 60 s after the last `PIR=1`.
5. Wave again → ON immediately.
6. Clean up: `tools\add_test_slot.py --remove`.
   **Afterwards set `HOLD_MIN = 10` again and restart Flask.**

### 5. 5-minute lead time
A class that starts in 7 minutes should switch ON 5 minutes early, i.e. in about 2 minutes.
1. Note the current time (e.g. 15:50).
2. `tools\add_test_slot.py --start-in 7 --minutes 10` → prints e.g. `TEST class: 15:57-16:07`.
3. Keep waving at the PIR now and then.
4. **Expect:** until 15:51 → `light=OFF | no class scheduled`.
   From **15:52** (start − 5 min) → `light=ON fan=ON | class scheduled and room occupied`.
5. Clean up: `tools\add_test_slot.py --remove`.
- **Fail:** ON immediately → an old TEST slot still exists (run `--remove` first).

### 6. Class end
1. `tools\add_test_slot.py --minutes 2` → prints e.g. `TEST class: 16:10-16:12`.
2. Keep waving so the room stays occupied.
3. **Expect:** ON/ON until 16:11:59, then at **16:12** → `light=OFF fan=OFF | no class scheduled`
   even though `PIR=1`.
4. Clean up: `tools\add_test_slot.py --remove` (nothing left, but safe).

### 7. No class → OFF even with motion
1. Make sure no class is scheduled now (`--remove`, and not during a real slot in timetable.csv).
2. Wave at the PIR.
3. **Expect:** `PIR=1 ... light=OFF fan=OFF | no class scheduled`.

### 8. Fail-safe when Flask is stopped
1. `tools\add_test_slot.py --minutes 15`, wave → ON/ON.
2. Wave again, then press **Ctrl+C** in window 1 (Flask stops).
3. **Expect:** ~20 s of `keeping last state, fail-safe in N s` (relays unchanged), then
   `>>> No backend for ~20 s - entering FAIL-SAFE mode` and `light=ON fan=OFF`
   → **fan relay clicks OFF**, light stays ON.
4. Without motion for `FALLBACK_HOLD_S` (5 min) the light also goes OFF; motion → ON again.

### 9. Recovery when Flask restarts
1. Continue from test 8: start Flask again in window 1.
2. **Expect within a few seconds:** `>>> Backend is back - leaving FAIL-SAFE mode`,
   then normal reason; wave if it says `no motion` → ON/ON, **fan relay clicks ON**.
3. Clean up: `tools\add_test_slot.py --remove`.

### 10. Relays OFF at boot / on Stop
1. With a TEST slot and motion (ON/ON), press **Stop** in Thonny.
2. **Expect:** `Stopped - relays OFF`, both LEDs OFF.
3. Press the **EN/RST** button on the ESP32 (or unplug and replug USB).
4. **Expect:** both LEDs stay OFF from power-up until the first command arrives.
5. Clean up: `tools\add_test_slot.py --remove`.

---

## Problems found

| Date | Problem | Fix |
|------|---------|-----|
| 15-10-2026 | DHT11 always `read failed` in main.py | loose wire; works after reseating (24 °C / 61 %) |
| 21-10-2026 | `ETIMEDOUT` to backend after laptop IP changed | updated SERVER_URL; Thonny had uploaded an old config.py tab |
| 21-10-2026 | LDR reads ~0 most of the time | open - check wiring (AO not DO, divider to GND) |
