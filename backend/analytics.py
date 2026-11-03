# analytics.py - Read-only analytics for the dashboard (GET /api/analytics).
#
# Everything is computed from the database: telemetry (one row every ~2 s),
# timetable, sms_log and decision_log. Results are aggregates only - no raw rows,
# no secrets. Cost and savings are ESTIMATES (rated watts, assumed price).

import time
from datetime import datetime, timedelta

import db
import logic

PRICE_PER_KWH = 8.0             # Rs per kWh (assumption - change to your tariff)
COLLEGE_HOURS = (9 * 60, 17 * 60)   # "lights on all college hours" baseline, Mon-Fri
COLLEGE_DAYS = range(0, 5)
OFFLINE_GAP_S = 10              # no telemetry for longer than this = ESP32 offline
FAILSAFE_GAP_S = 20             # gaps this long: the ESP32 may have been in fail-safe
RANGES = {"today": 1, "7d": 7, "30d": 30}
CACHE_S = 30

_cache = {}


def range_bounds(name, now=None):
    """(start_datetime, now, list of dates) for 'today' | '7d' | '30d'."""
    now = now or datetime.now()
    days = RANGES[name]
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [(start + timedelta(days=i)).date() for i in range(days)]
    return start, now, dates


def class_minutes_by_day(slots):
    """weekday -> list of (start_min, end_min)."""
    out = {d: [] for d in range(7)}
    for s in slots:
        out[s["day"]].append((s["start_min"], s["end_min"]))
    return out


def in_class(dt, by_day):
    m = dt.hour * 60 + dt.minute
    return any(a <= m < b for a, b in by_day[dt.weekday()])


def elapsed_minutes(day_start, a, b, now):
    """Minutes of the window [a, b) on that day that are already in the past."""
    start = day_start + timedelta(minutes=a)
    end = min(day_start + timedelta(minutes=b), now)
    return max((end - start).total_seconds() / 60, 0)


def energy_per_day(conn, start_ts, end_ts):
    """kWh per local date, the same rule as logic.energy_kwh (previous power x dt,
    gaps over MAX_GAP_S ignored), computed in SQL."""
    rows = conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') AS d,
               SUM(CASE WHEN dt > 0 AND dt <= ? THEN COALESCE(p, 0) * dt ELSE 0 END) AS ws
        FROM (SELECT ts, ts - LAG(ts) OVER (ORDER BY ts) AS dt,
                     LAG(power_w) OVER (ORDER BY ts) AS p
              FROM telemetry WHERE ts >= ? AND ts <= ?)
        GROUP BY d""", (logic.MAX_GAP_S, start_ts, end_ts)).fetchall()
    return {r["d"]: r["ws"] / 3600 / 1000 for r in rows}


def minute_rows(conn, start_ts, end_ts):
    """One row per minute that has data: minute start, readings, motion, on, temp, hum, ldr."""
    lo, hi = logic.TEMP_RANGE_C
    return conn.execute("""
        SELECT CAST(ts / 60 AS INTEGER) * 60 AS m, COUNT(*) AS n, MAX(pir) AS motion,
               MAX(CASE WHEN power_w > 0 THEN 1 ELSE 0 END) AS is_on,
               AVG(CASE WHEN temp BETWEEN ? AND ? THEN temp END) AS temp,
               AVG(CASE WHEN hum BETWEEN 0 AND 100 THEN hum END) AS hum,
               AVG(ldr) AS ldr
        FROM telemetry WHERE ts >= ? AND ts <= ? GROUP BY m ORDER BY m""",
        (lo, hi, start_ts, end_ts)).fetchall()


def gaps(conn, start_ts, end_ts):
    row = conn.execute("""
        SELECT SUM(CASE WHEN gap > ? THEN 1 ELSE 0 END) AS n, MAX(gap) AS longest
        FROM (SELECT ts - LAG(ts) OVER (ORDER BY ts) AS gap
              FROM telemetry WHERE ts >= ? AND ts <= ?)""",
        (FAILSAFE_GAP_S, start_ts, end_ts)).fetchone()
    return (row["n"] or 0), (row["longest"] or 0)


def decision_summary(conn, start_ts, end_ts):
    """Minutes per light reason and in override, from decision_log (only rows whose
    period overlaps the range). Returns (summary, since) - since = first log entry."""
    first = conn.execute("SELECT MIN(ts) FROM decision_log").fetchone()[0]
    rows = conn.execute("SELECT ts, light, light_reason, override FROM decision_log "
                        "WHERE ts <= ? ORDER BY ts", (end_ts,)).fetchall()
    summary = {"daylight_off_min": 0.0, "override_min": 0.0, "light_on_min": 0.0}
    for cur, nxt in zip(rows, list(rows[1:]) + [None]):
        a = max(cur["ts"], start_ts)
        b = min(nxt["ts"] if nxt else end_ts, end_ts)
        if b <= a:
            continue
        minutes = (b - a) / 60
        if "daylight" in (cur["light_reason"] or ""):
            summary["daylight_off_min"] += minutes
        if cur["override"]:
            summary["override_min"] += minutes
        if cur["light"]:
            summary["light_on_min"] += minutes
    return {k: round(v, 1) for k, v in summary.items()}, first


def compute(conn, range_name, light_w, fan_w, now=None):
    start, now, dates = range_bounds(range_name, now)
    start_ts, end_ts = start.timestamp(), now.timestamp()
    by_day = class_minutes_by_day(db.get_timetable(conn))
    rated_w = light_w + fan_w
    hold_min = logic.HOLD_MIN

    # --- energy, cost, baselines ---
    kwh_day = energy_per_day(conn, start_ts, end_ts)
    energy = [{"date": d.isoformat(), "label": d.strftime("%a %d %b"),
               "kwh": round(kwh_day.get(d.isoformat(), 0.0), 4)} for d in dates]
    kwh_total = sum(e["kwh"] for e in energy)
    sched_min = college_min = 0.0
    for d in dates:
        day_start = datetime(d.year, d.month, d.day)
        sched_min += sum(elapsed_minutes(day_start, a, b, now) for a, b in by_day[d.weekday()])
        if d.weekday() in COLLEGE_DAYS:
            college_min += elapsed_minutes(day_start, COLLEGE_HOURS[0], COLLEGE_HOURS[1], now)
    base_sched = sched_min * rated_w / 60 / 1000
    base_college = college_min * rated_w / 60 / 1000

    # --- per-minute analysis ---
    minutes = minute_rows(conn, start_ts, end_ts)
    motion_minutes = [r["m"] for r in minutes if r["motion"]]
    on_by_day = {d.isoformat(): {"in_class": 0, "outside": 0} for d in dates}
    heat = [[0] * 24 for _ in range(7)]                 # weekday x hour motion minutes
    class_min_with_data = class_min_occupied = 0
    above_on = 0
    motion_idx = 0
    for r in minutes:
        dt = datetime.fromtimestamp(r["m"])
        cls = in_class(dt, by_day)
        if r["is_on"]:
            on_by_day[dt.date().isoformat()]["in_class" if cls else "outside"] += 1
        if r["motion"]:
            heat[dt.weekday()][dt.hour] += 1
        if r["temp"] is not None and r["temp"] >= logic.FAN_ON_C:
            above_on += 1
        if cls:
            class_min_with_data += 1
            # occupied = motion in this minute or the HOLD_MIN minutes before
            while motion_idx < len(motion_minutes) and motion_minutes[motion_idx] < r["m"] - hold_min * 60:
                motion_idx += 1
            if motion_idx < len(motion_minutes) and motion_minutes[motion_idx] <= r["m"]:
                class_min_occupied += 1

    # --- classes with nobody present (scheduled, enough data, no motion at all) ---
    minute_set = {r["m"]: r for r in minutes}
    classes = {"held": 0, "no_motion": [], "no_data": 0}
    for d in dates:
        day_start = datetime(d.year, d.month, d.day)
        for a, b in sorted(by_day[d.weekday()]):
            end = day_start + timedelta(minutes=b)
            if end > now:
                continue                                 # not finished yet
            ms = [int((day_start + timedelta(minutes=x)).timestamp()) // 60 * 60 for x in range(a, b)]
            have = [minute_set[m] for m in ms if m in minute_set]
            if len(have) < 0.5 * len(ms):
                classes["no_data"] += 1
            elif any(h["motion"] for h in have):
                classes["held"] += 1
            else:
                classes["no_motion"].append({"date": d.strftime("%a %d %b"),
                                             "time": "%s-%s" % (db.min_to_hhmm(a), db.min_to_hhmm(b))})

    # --- environment trend: per hour for today, per day otherwise ---
    buckets = {}
    for r in minutes:
        dt = datetime.fromtimestamp(r["m"])
        key = dt.strftime("%H:00") if range_name == "today" else dt.strftime("%a %d %b")
        b = buckets.setdefault(key, {"temp": [], "hum": [], "ldr": []})
        for k in ("temp", "hum", "ldr"):
            if r[k] is not None:
                b[k].append(r[k])

    def stat(values):
        return None if not values else {"min": round(min(values), 1), "avg": round(sum(values) / len(values), 1),
                                        "max": round(max(values), 1)}
    environment = [{"label": k, "temp": stat(v["temp"]), "hum": stat(v["hum"]), "ldr": stat(v["ldr"])}
                   for k, v in buckets.items()]

    # --- reliability ---
    first_ts = conn.execute("SELECT MIN(ts) FROM telemetry WHERE ts >= ?", (start_ts,)).fetchone()[0]
    span_min = (end_ts - max(start_ts, first_ts)) / 60 if first_ts else 0
    offline_periods, longest_gap = gaps(conn, start_ts, end_ts)
    last_ts = conn.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0]

    # --- SMS ---
    sms_rows = conn.execute("SELECT kind, status FROM sms_log WHERE ts >= ?", (start_ts,)).fetchall()
    sms = {"total": len(sms_rows), "alerts": sum(1 for r in sms_rows if r["kind"] == "alert"),
           "tests": sum(1 for r in sms_rows if r["kind"] == "test"), "by_status": {}}
    for r in sms_rows:
        sms["by_status"][r["status"] or "unknown"] = sms["by_status"].get(r["status"] or "unknown", 0) + 1

    decisions, decisions_since = decision_summary(conn, start_ts, end_ts)

    return {
        "range": range_name, "from": start.strftime("%d %b %Y"), "to": now.strftime("%d %b %Y %H:%M"),
        "has_data": bool(minutes),
        "assumptions": {"price_per_kwh": PRICE_PER_KWH, "rated_w": rated_w,
                        "college_hours": "%s-%s Mon-Fri" % (db.min_to_hhmm(COLLEGE_HOURS[0]), db.min_to_hhmm(COLLEGE_HOURS[1])),
                        "hold_min": hold_min},
        "energy": {"per_day": energy, "kwh_total": round(kwh_total, 4),
                   "cost_total": round(kwh_total * PRICE_PER_KWH, 2),
                   "kwh_today": energy[-1]["kwh"], "kwh_yesterday": round(kwh_day.get(
                       (dates[-1] - timedelta(days=1)).isoformat(), 0.0), 4)},
        "savings": {"baseline_scheduled_kwh": round(base_sched, 4),
                    "baseline_college_kwh": round(base_college, 4),
                    "saved_vs_scheduled_kwh": round(base_sched - kwh_total, 4),
                    "saved_vs_college_kwh": round(base_college - kwh_total, 4),
                    "saved_vs_college_cost": round((base_college - kwh_total) * PRICE_PER_KWH, 2)},
        "lighting": {"per_day": [{"label": e["label"], **on_by_day[e["date"]]} for e in energy],
                     "on_minutes": sum(v["in_class"] + v["outside"] for v in on_by_day.values()),
                     "outside_minutes": sum(v["outside"] for v in on_by_day.values())},
        "occupancy": {"class_minutes_with_data": class_min_with_data,
                      "occupied_minutes": class_min_occupied,
                      "occupied_pct": round(100 * class_min_occupied / class_min_with_data, 1)
                      if class_min_with_data else None,
                      "heatmap": heat, "classes": classes},
        "environment": {"buckets": environment, "minutes_above_fan_on": above_on,
                        "fan_on_c": logic.FAN_ON_C},
        "reliability": {"uptime_pct": round(100 * len(minutes) / span_min, 1) if span_min > 0 else None,
                        "minutes_with_data": len(minutes), "offline_periods": offline_periods,
                        "longest_gap_s": round(longest_gap), "failsafe_gap_s": FAILSAFE_GAP_S,
                        "last_reading": datetime.fromtimestamp(last_ts).strftime("%d %b %H:%M:%S") if last_ts else None},
        "sms": sms,
        "decisions": dict(decisions, since=datetime.fromtimestamp(decisions_since).strftime("%d %b %H:%M")
                          if decisions_since else None),
    }


def get(range_name, light_w, fan_w):
    """Cached for CACHE_S seconds per range."""
    hit = _cache.get(range_name)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    conn = db.get_conn()
    try:
        result = compute(conn, range_name, light_w, fan_w)
    finally:
        conn.close()
    _cache[range_name] = (time.time(), result)
    return result
