# app.py - Flask web server for the smart classroom.
# Endpoints: POST /telemetry, GET /command, GET /api/status,
# GET /api/consumption, GET /api/timetable, GET / (dashboard).
# Decision logic itself lives in logic.py.
#
# Start it from the backend folder:   ..\venv\Scripts\python.exe app.py
#
# All times use the LAPTOP's clock (Windows is set to IST), so the ESP32's
# own clock never affects decisions.

import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import db
import logic

app = Flask(__name__)

# If no telemetry arrived for this long, the dashboard shows the ESP32 as offline.
ONLINE_TIMEOUT_S = 10

# How many recent power samples the chart gets (150 x 2 s = about 5 minutes).
CHART_SAMPLES = 150


# ---------- helpers ----------

def to_float(value):
    """Turn a JSON value into a float, or None if missing/invalid."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def last_motion_time(conn):
    """When did the PIR last see motion? datetime, or None if never."""
    row = conn.execute("SELECT MAX(ts) FROM telemetry WHERE pir = 1").fetchone()
    return datetime.fromtimestamp(row[0]) if row[0] else None


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
    """ESP32 sends {pir, ldr, temp, hum, power_w}. Missing values are stored as NULL."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "send JSON like {\"pir\": 1, ...}"}), 400

    pir = 1 if data.get("pir") in (1, True, "1") else 0
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO telemetry (ts, pir, ldr, temp, hum, power_w) VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), pir, to_float(data.get("ldr")), to_float(data.get("temp")),
         to_float(data.get("hum")), to_float(data.get("power_w"))),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/command")
def command():
    """ESP32 asks what to do. Runs the decision logic and remembers the answer."""
    conn = db.get_conn()
    result = logic.decide(db.get_timetable(conn), datetime.now(), last_motion_time(conn))
    conn.execute(
        "UPDATE device_state SET light = ?, fan = ?, reason = ?, updated_ts = ? WHERE room_id = ?",
        (int(result["light"]), int(result["fan"]), result["reason"], time.time(), db.ROOM_ID),
    )
    conn.commit()
    conn.close()
    return jsonify({"light": result["light"], "fan": result["fan"], "reason": result["reason"]})


# ---------- endpoints for the dashboard ----------

@app.route("/api/status")
def api_status():
    """Everything the dashboard needs for the status cards."""
    conn = db.get_conn()
    now = datetime.now()
    slots = db.get_timetable(conn)

    latest = conn.execute("SELECT * FROM telemetry ORDER BY ts DESC LIMIT 1").fetchone()
    state = conn.execute("SELECT * FROM device_state WHERE room_id = ?", (db.ROOM_ID,)).fetchone()
    last_motion = last_motion_time(conn)
    conn.close()

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
            "hum": latest["hum"], "power_w": latest["power_w"],
        },
        "occupied": logic.is_occupied(last_motion, now),
        "last_motion": last_motion.strftime("%H:%M:%S") if last_motion else None,
        # the last command actually sent to the ESP32
        "command": {"light": bool(state["light"]), "fan": bool(state["fan"]),
                    "reason": state["reason"]},
        "current_class": slot_to_dict(logic.find_current_class(slots, now)),
        "next_class": slot_to_dict(next_slot),
        "next_class_days_ahead": days_ahead,
        "lead_min": logic.LEAD_MIN,
        "hold_min": logic.HOLD_MIN,
    })


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
