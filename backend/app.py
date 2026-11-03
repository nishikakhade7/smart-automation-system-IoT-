# app.py - Flask web server for the smart classroom.
# Endpoints: POST /telemetry, GET /command, GET /api/status,
# GET /api/consumption, GET /api/timetable, POST /api/override, POST /api/toggle,
# POST /api/test-alert (also /test-alert), GET /api/sms-log, POST /api/sms-refresh,
# GET /api/analytics?range=today|7d|30d,
# GET / (dashboard).
# Decision logic itself lives in logic.py.
#
# Start it from the backend folder:   ..\venv\Scripts\python.exe app.py
#
# All times use the LAPTOP's clock (Windows is set to IST), so the ESP32's
# own clock never affects decisions.

import logging
import re
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import alerts
import analytics
import db
import logic

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("classroom")

# Readings older than this are not used for the LDR (daylight) decision.
LDR_FRESH_S = 15

# If no telemetry arrived for this long, the dashboard shows the ESP32 as offline.
ONLINE_TIMEOUT_S = 10

# How many recent power samples the chart gets (150 x 2 s = about 5 minutes).
CHART_SAMPLES = 150

# Power shown on the dashboard:
#   "measured"  = the ESP32's power_w (ACS712 current x 230 V)
#   "estimated" = rated watts of what the relays have switched ON (below).
# The ACS712 cannot see small (LED) loads, so we estimate for now.
POWER_SOURCE = "estimated"
LIGHT_W = 9         # watts printed on the bulb (change to your bulb's value)
FAN_W = 0           # no fan connected yet


# ---------- helpers ----------

def to_float(value):
    """Turn a JSON value into a float, or None if missing/invalid."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def clock(conn):
    """Which time do decisions use? Normally the real time. While tools/demo.py
    runs, a fast demo clock with only the DEMO timetable slots.
    Returns a dict: now, to_dt (Unix time -> datetime), slots, lead_min, hold_min, demo."""
    row = conn.execute("SELECT * FROM demo WHERE id = 1").fetchone()
    slots = db.get_timetable(conn)
    if row is None:
        return {"now": datetime.now(), "to_dt": datetime.fromtimestamp, "slots": slots,
                "lead_min": logic.LEAD_MIN, "hold_min": logic.HOLD_MIN, "trail_min": logic.TRAIL_MIN, "demo": None}
    demo = (row["start_real"], datetime.fromtimestamp(row["start_sim"]), row["speed"])
    to_dt = lambda ts: logic.demo_clock(ts, demo)
    now = to_dt(time.time())
    # Simulated occupancy: people are "present" until motion_until, then the room is empty
    mock = row["motion_until"] is not None
    mock_motion = min(now, datetime.fromtimestamp(row["motion_until"])) if mock else None
    return {"now": now, "to_dt": to_dt,
            "slots": [s for s in slots if s["subject"].startswith("DEMO")],
            "lead_min": logic.DEMO_LEAD_MIN, "hold_min": logic.DEMO_HOLD_MIN, "trail_min": logic.DEMO_TRAIL_MIN,
            "mock_motion": mock_motion,
            "demo": {"speed": row["speed"], "mock_pir": mock}}


def last_motion_time(conn, to_dt=datetime.fromtimestamp):
    """When did the PIR last see motion? datetime (on the given clock), or None if never."""
    row = conn.execute("SELECT MAX(ts) FROM telemetry WHERE pir = 1").fetchone()
    return to_dt(row[0]) if row[0] else None


def motion_for(conn, c):
    """Last motion on clock c: the simulated one during a mock demo, else the real PIR."""
    if c.get("mock_motion") is not None:
        return c["mock_motion"]
    return last_motion_time(conn, c["to_dt"])


def auto_decision(conn, c):
    """The automatic rule on clock c (real or demo)."""
    return logic.decide(c["slots"], c["now"], motion_for(conn, c), c["lead_min"], c["hold_min"], c["trail_min"])


def latest_valid_temp(conn):
    """Newest plausible DHT11 temperature and its age: (temp, age_s) or (None, None)."""
    lo, hi = logic.TEMP_RANGE_C
    row = conn.execute("SELECT ts, temp FROM telemetry WHERE temp BETWEEN ? AND ? "
                       "ORDER BY ts DESC LIMIT 1", (lo, hi)).fetchone()
    return (row["temp"], time.time() - row["ts"]) if row else (None, None)


def latest_brightness(conn):
    """Brightness from the newest LDR reading (higher = brighter), None if old/missing."""
    row = conn.execute("SELECT ts, ldr FROM telemetry ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None or row["ldr"] is None or time.time() - row["ts"] > LDR_FRESH_S:
        return None
    return logic.brightness(row["ldr"])


def full_decision(conn, c, state):
    """Class/occupancy rule + fan temperature hysteresis + daylight saving.
    Returns (result, new_state) where new_state holds the updated hysteresis values."""
    base = auto_decision(conn, c)
    temp, age = latest_valid_temp(conn)
    hot, fan_note = logic.fan_heat(temp, age, bool(state["fan_hot"]))
    level = latest_brightness(conn)
    is_day, day_ts = logic.daylight_update(level, bool(state["daylight"]), state["daylight_ts"], time.time())
    result = logic.decide_full(base, hot, fan_note, is_day, level)
    return result, {"fan_hot": int(hot), "daylight": int(is_day), "daylight_ts": day_ts}


TEST_SMS_MIN_GAP_S = 30      # the "Send test SMS" button can be used once per 30 s


def log_sms(conn, kind, text, episode=None):
    """Claim a row in sms_log BEFORE sending. Returns its id, or None if this alert
    episode already has a row (= the SMS was already sent: no duplicate)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO sms_log (ts, kind, episode, text, status, info, updated_ts) "
        "VALUES (?, ?, ?, ?, 'sending', 'submitting to Twilio', ?)",
        (time.time(), kind, episode, text, time.time()))
    conn.commit()
    return cur.lastrowid if cur.rowcount == 1 else None


def update_sms(row_id, result):
    """Store Twilio's answer / a later delivery status (called from the sending thread)."""
    conn = db.get_conn()
    conn.execute("UPDATE sms_log SET sid = COALESCE(?, sid), status = COALESCE(?, status), "
                 "error_code = ?, info = ?, updated_ts = ? WHERE id = ?",
                 (result["sid"], result["status"] or ("queued" if result["ok"] else "failed"),
                  result["error_code"], result["info"], time.time(), row_id))
    conn.commit()
    conn.close()


def send_logged(conn, kind, text, episode=None):
    """Log first, then send in the background. Returns the log id (None = duplicate)."""
    row_id = log_sms(conn, kind, text, episode)
    if row_id is not None:
        alerts.send_async(text, lambda result: update_sms(row_id, result))
    return row_id


def check_alert(conn, state, light_on, has_class):
    """Light ON for ALERT_AFTER_MIN with no class -> one SMS per episode.
    Any error here is logged and ignored: alerts must never affect control."""
    try:
        fire, on_since, armed = logic.alert_step(
            light_on, has_class, state["alert_on_since"], bool(state["alert_armed"]), time.time())
        conn.execute("UPDATE device_state SET alert_on_since = ?, alert_armed = ? WHERE room_id = ?",
                     (on_since, int(armed), db.ROOM_ID))
        if fire:
            text = logic.alert_text(db.ROOM_NAME)
            if send_logged(conn, "alert", text, episode="%.3f" % on_since) is None:
                log.info("alert for this episode was already sent - no duplicate")
            else:
                log.info("alert condition reached - SMS queued in the background")
    except Exception as e:
        log.warning("alert check failed (%s) - control continues", type(e).__name__)


def log_decision(conn, result):
    """decision_log for the analytics: a row only when the decision changes.
    Numbers are removed from the light reason so a changing brightness value does
    not count as a change. Errors never affect control."""
    try:
        reason = re.sub(r"\s*\(brightness \d+\)", "", result["light_reason"] or "")
        override = int("watchman override" in (result["light_reason"] or "") + (result["fan_reason"] or ""))
        key = (int(result["light"]), int(result["fan"]), reason, override)
        last = conn.execute("SELECT light, fan, light_reason, override FROM decision_log "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if last is None or tuple(last) != key:
            conn.execute("INSERT INTO decision_log (ts, light, fan, light_reason, fan_reason, override) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (time.time(), key[0], key[1], reason,
                                                       result["fan_reason"], override))
    except Exception as e:
        log.warning("decision log failed (%s) - control continues", type(e).__name__)


def sms_row_to_dict(r):
    if r is None:
        return None
    return {"id": r["id"], "kind": r["kind"], "text": r["text"], "status": r["status"],
            "info": r["info"], "error_code": r["error_code"],
            "sid": (r["sid"][:4] + "..." + r["sid"][-6:]) if r["sid"] else None,
            "time": datetime.fromtimestamp(r["ts"]).strftime("%d-%m %H:%M:%S")}


def active_overrides(conn):
    """Watchman overrides that have not expired: {"light": ("on", until_datetime), ...}.
    Expired rows are deleted, so the device is automatic again."""
    conn.execute("DELETE FROM overrides WHERE until_ts <= ?", (time.time(),))
    conn.commit()
    return {r["device"]: (r["mode"], datetime.fromtimestamp(r["until_ts"]))
            for r in conn.execute("SELECT * FROM overrides")}


def slot_to_dict(slot):
    """Database slot -> JSON-friendly dict."""
    if slot is None:
        return None
    return {
        "day": slot["day"],
        "day_name": db.DAY_NAMES[slot["day"]],
        "start": db.min_to_hhmm(slot["start_min"]),
        "end": db.min_to_hhmm(slot["end_min"]),
        "subject": slot["subject"],
    }


# ---------- endpoints for the ESP32 ----------

@app.route("/telemetry", methods=["POST"])
def telemetry():
    """ESP32 sends {pir, ldr, temp, hum, power_w, current_a, voltage_v, voltage_mv}.
    Missing values are stored as NULL."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "send JSON like {\"pir\": 1, ...}"}), 400

    pir = 1 if data.get("pir") in (1, True, "1") else 0
    conn = db.get_conn()
    power_w = to_float(data.get("power_w"))
    if POWER_SOURCE == "estimated":
        # what the ESP32 is switching right now = the last command we sent it
        state = conn.execute("SELECT light, fan FROM device_state WHERE room_id = ?",
                             (db.ROOM_ID,)).fetchone()
        power_w = float(state["light"] * LIGHT_W + state["fan"] * FAN_W)
    conn.execute(
        "INSERT INTO telemetry (ts, pir, ldr, temp, hum, power_w, current_a, voltage_v, voltage_mv) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), pir, to_float(data.get("ldr")), to_float(data.get("temp")),
         to_float(data.get("hum")), power_w, to_float(data.get("current_a")),
         to_float(data.get("voltage_v")), to_float(data.get("voltage_mv"))),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/command")
def command():
    """ESP32 asks what to do. Runs the decision logic (class + occupancy, fan
    temperature, daylight, watchman override), remembers the answer and the
    hysteresis state, and checks the SMS alert."""
    conn = db.get_conn()
    state = conn.execute("SELECT * FROM device_state WHERE room_id = ?", (db.ROOM_ID,)).fetchone()
    result, new_state = full_decision(conn, clock(conn), state)
    result = logic.apply_overrides(result, active_overrides(conn), datetime.now())
    conn.execute(
        "UPDATE device_state SET light = ?, fan = ?, reason = ?, light_reason = ?, fan_reason = ?, "
        "fan_hot = ?, daylight = ?, daylight_ts = ?, updated_ts = ? WHERE room_id = ?",
        (int(result["light"]), int(result["fan"]), result["reason"], result["light_reason"],
         result["fan_reason"], new_state["fan_hot"], new_state["daylight"], new_state["daylight_ts"],
         time.time(), db.ROOM_ID),
    )
    log_decision(conn, result)
    check_alert(conn, state, result["light"], result["subject"] is not None)
    conn.commit()
    conn.close()
    return jsonify({"light": result["light"], "fan": result["fan"], "reason": result["reason"],
                    "light_reason": result["light_reason"], "fan_reason": result["fan_reason"]})


# ---------- endpoints for the dashboard ----------

@app.route("/api/status")
def api_status():
    """Everything the dashboard needs for the status cards."""
    conn = db.get_conn()
    c = clock(conn)
    now, slots = c["now"], c["slots"]

    latest = conn.execute("SELECT * FROM telemetry ORDER BY ts DESC LIMIT 1").fetchone()
    state = conn.execute("SELECT * FROM device_state WHERE room_id = ?", (db.ROOM_ID,)).fetchone()
    last_motion = motion_for(conn, c)
    overrides = active_overrides(conn)
    last_sms = conn.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    sms_cfg = alerts.load_config()
    sms_problems = alerts.config_problems(sms_cfg)

    seconds_since_data = round(time.time() - latest["ts"], 1) if latest else None
    next_slot, days_ahead = logic.find_next_class(slots, now)

    return jsonify({
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "day_name": db.DAY_NAMES[now.weekday()],
        "esp_online": seconds_since_data is not None and seconds_since_data < ONLINE_TIMEOUT_S,
        "seconds_since_data": seconds_since_data,
        "latest": None if latest is None else {
            "time": datetime.fromtimestamp(latest["ts"]).strftime("%H:%M:%S"),
            "pir": latest["pir"], "ldr": latest["ldr"], "temp": latest["temp"],
            "hum": latest["hum"], "power_w": latest["power_w"], "current_a": latest["current_a"],
            "voltage_v": latest["voltage_v"], "voltage_mv": latest["voltage_mv"],
        },
        "occupied": logic.is_occupied(last_motion, now, c["hold_min"]),
        "last_motion": last_motion.strftime("%H:%M:%S") if last_motion else None,
        # the last command actually sent to the ESP32
        "command": {"light": bool(state["light"]), "fan": bool(state["fan"]),
                    "reason": state["reason"], "light_reason": state["light_reason"],
                    "fan_reason": state["fan_reason"]},
        "fan_rule": {"on_c": logic.FAN_ON_C, "off_c": logic.FAN_OFF_C, "hot": bool(state["fan_hot"])},
        "daylight": {"level": None if latest is None or latest["ldr"] is None else logic.brightness(latest["ldr"]),
                     "off_at": logic.DAY_OFF, "on_at": logic.DAY_ON, "is_day": bool(state["daylight"]),
                     "higher_is_brighter": logic.LDR_HIGHER_IS_BRIGHTER},
        "alert": {"configured": not sms_problems, "problems": sms_problems,
                  "to": alerts.mask_number(sms_cfg.get("SMS_TO")) if not sms_problems else None,
                  "after_min": logic.ALERT_AFTER_MIN, "last": sms_row_to_dict(last_sms),
                  "trial_template": alerts.trial_template(sms_cfg)},
        "current_class": slot_to_dict(logic.find_current_class(slots, now, c["lead_min"], c["trail_min"])),
        "next_class": slot_to_dict(next_slot),
        "next_class_days_ahead": days_ahead,
        "lead_min": c["lead_min"],
        "trail_min": c["trail_min"],
        "hold_min": c["hold_min"],
        "demo": c["demo"],          # {"speed": 120} while tools/demo.py runs, else null
        "power_source": POWER_SOURCE,
        "command_time": datetime.fromtimestamp(state["updated_ts"]).strftime("%H:%M:%S")
        if state["updated_ts"] else None,
        "noise_a": 0.10,
        "price_per_kwh": analytics.PRICE_PER_KWH,
        "light_w": LIGHT_W,
        # watchman overrides: {"light": {"mode": "on", "until": "13:45"}} (missing = auto)
        "overrides": {d: {"mode": m, "until": u.strftime("%H:%M")} for d, (m, u) in overrides.items()},
        "override_min": logic.OVERRIDE_MIN,
    })


@app.route("/api/override", methods=["POST"])
def api_override():
    """Watchman button: {"device": "light" or "fan", "mode": "on", "off" or "auto"}.
    on/off lasts OVERRIDE_MIN minutes; auto removes the override at once.
    The ESP32 picks up the change at its next /command (within about 2 s)."""
    data = request.get_json(silent=True) or {}
    device, mode = data.get("device"), data.get("mode")
    if device not in ("light", "fan") or mode not in ("on", "off", "auto"):
        return jsonify({"ok": False, "error": "device must be light/fan, mode on/off/auto"}), 400

    conn = db.get_conn()
    if mode == "auto":
        conn.execute("DELETE FROM overrides WHERE device = ?", (device,))
    else:
        until = time.time() + logic.OVERRIDE_MIN * 60
        conn.execute("INSERT OR REPLACE INTO overrides (device, mode, until_ts) VALUES (?, ?, ?)",
                     (device, mode, until))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "device": device, "mode": mode})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    """The dashboard's single ON/OFF button. Flips the output (both relays
    together). If the new state equals the automatic decision, the override
    is removed, so a second press returns to automatic."""
    conn = db.get_conn()
    state = conn.execute("SELECT * FROM device_state WHERE room_id = ?", (db.ROOM_ID,)).fetchone()
    auto, _ = full_decision(conn, clock(conn), state)
    current = logic.apply_overrides(auto, active_overrides(conn), datetime.now())
    mode = logic.toggle_mode(auto["light"], current["light"])

    if mode == "auto":
        conn.execute("DELETE FROM overrides")
    else:
        until = time.time() + logic.OVERRIDE_MIN * 60
        for device in ("light", "fan"):
            conn.execute("INSERT OR REPLACE INTO overrides (device, mode, until_ts) VALUES (?, ?, ?)",
                         (device, mode, until))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/test-alert", methods=["POST"])
@app.route("/test-alert", methods=["POST"])
def api_test_alert():
    """Dashboard button: send a test SMS in the background (never blocks).
    Limited to one per TEST_SMS_MIN_GAP_S so repeated clicks cannot spam."""
    problems = alerts.config_problems()
    if problems:
        return jsonify({"ok": False, "error": "SMS not configured: " + "; ".join(problems)}), 400
    conn = db.get_conn()
    last = conn.execute("SELECT ts FROM sms_log WHERE kind = 'test' ORDER BY id DESC LIMIT 1").fetchone()
    if last and time.time() - last["ts"] < TEST_SMS_MIN_GAP_S:
        conn.close()
        return jsonify({"ok": False, "error": "wait %d s between test SMS" % TEST_SMS_MIN_GAP_S}), 429
    text = "Test alert from %s smart classroom (%s)" % (db.ROOM_NAME, datetime.now().strftime("%H:%M"))
    row_id = send_logged(conn, "test", text)
    conn.close()
    return jsonify({"ok": True, "queued": True, "log_id": row_id})


@app.route("/api/sms-log")
def api_sms_log():
    """The last 10 SMS attempts with their Twilio status (no secrets, SID shortened)."""
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    return jsonify([sms_row_to_dict(r) for r in rows])


@app.route("/api/sms-refresh", methods=["POST"])
def api_sms_refresh():
    """Ask Twilio again for the status of recent messages that are not final yet
    (e.g. still 'sent' after the automatic checks). Runs in the background."""
    conn = db.get_conn()
    rows = conn.execute("SELECT id, sid FROM sms_log WHERE sid IS NOT NULL AND ts > ? AND status NOT IN "
                        "('delivered', 'undelivered', 'failed', 'canceled', 'read')",
                        (time.time() - 24 * 3600,)).fetchall()
    conn.close()

    def run(items):
        for row_id, sid in items:
            status, code, info = alerts.fetch_status(sid)
            if status:
                update_sms(row_id, {"ok": status not in ("undelivered", "failed"), "sid": sid,
                                    "status": status, "error_code": code, "info": info})
    threading.Thread(target=run, args=([(r["id"], r["sid"]) for r in rows],), daemon=True).start()
    return jsonify({"ok": True, "checking": len(rows)})


@app.route("/api/analytics")
def api_analytics():
    """Aggregated analytics for range=today|7d|30d (read-only, cached 30 s)."""
    name = request.args.get("range", "today")
    if name not in analytics.RANGES:
        return jsonify({"ok": False, "error": "range must be one of: " + ", ".join(analytics.RANGES)}), 400
    return jsonify(analytics.get(name, LIGHT_W, FAN_W))


@app.route("/api/consumption")
def api_consumption():
    """Today's energy in kWh, plus recent power samples for the chart."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT ts, power_w FROM telemetry WHERE ts >= ? ORDER BY ts", (midnight,)
    ).fetchall()
    conn.close()

    samples = [(r["ts"], r["power_w"]) for r in rows]
    recent = samples[-CHART_SAMPLES:]
    return jsonify({
        "kwh_today": round(logic.energy_kwh(samples), 4),
        "samples": [{"time": datetime.fromtimestamp(t).strftime("%H:%M:%S"), "power_w": p}
                    for t, p in recent],
    })


@app.route("/api/timetable")
def api_timetable():
    conn = db.get_conn()
    slots = [slot_to_dict(s) for s in db.get_timetable(conn)]
    conn.close()
    return jsonify(slots)


@app.route("/")
def dashboard():
    """The watchman dashboard. It fetches its data from the /api/... endpoints."""
    return render_template("dashboard.html")


if __name__ == "__main__":
    db.init_db()
    # host 0.0.0.0 = reachable from other devices on the hotspot (the ESP32)
    app.run(host="0.0.0.0", port=5000, debug=False)
