# logic.py - Pure decision functions: should the light and fan be ON?
# should_be_on = class scheduled now (from LEAD_MIN before start until end)
#                AND room occupied (PIR motion within HOLD_MIN minutes).
# No database or network code here, so it is easy to unit-test.
#
# A "slot" is anything with keys day, start_min, end_min, subject
# (a dict, or a sqlite3.Row from db.get_timetable()).

from datetime import timedelta

# Switch on this many minutes BEFORE a class starts, so the room is ready.
LEAD_MIN = 5

# PIR sees motion, not presence: people sitting still trigger nothing.
# So we count the room as occupied for HOLD_MIN minutes after the last motion.
# Normal range 5-15. (The Stage 7 bench test temporarily sets it to 1.)
HOLD_MIN = 10


def minute_of_day(now):
    """datetime -> minutes since midnight (09:30 -> 570)."""
    return now.hour * 60 + now.minute


def find_current_class(slots, now, lead_min=LEAD_MIN):
    """Return the slot that is active at 'now', or None.
    Active = same weekday AND (start - lead_min) <= now < end.
    The end minute itself is NOT included: a 09:00-10:00 class is off at 10:00."""
    day = now.weekday()              # 0=Mon .. 6=Sun, same as the database
    minute = minute_of_day(now)
    for slot in slots:
        if slot["day"] == day and slot["start_min"] - lead_min <= minute < slot["end_min"]:
            return slot
    return None


def is_occupied(last_motion, now, hold_min=HOLD_MIN):
    """True if the last PIR motion (a datetime, or None if never) was
    less than hold_min minutes before 'now'."""
    if last_motion is None:
        return False
    return now - last_motion < timedelta(minutes=hold_min)


def decide(slots, now, last_motion, lead_min=LEAD_MIN, hold_min=HOLD_MIN):
    """The main rule. Returns {light, fan, reason, subject}.
    light/fan are True/False; subject is the current class name or None."""
    slot = find_current_class(slots, now, lead_min)
    if slot is None:
        return {"light": False, "fan": False,
                "reason": "no class scheduled", "subject": None}

    if not is_occupied(last_motion, now, hold_min):
        return {"light": False, "fan": False,
                "reason": "class scheduled but no motion", "subject": slot["subject"]}

    return {"light": True, "fan": True,
            "reason": "class scheduled and room occupied", "subject": slot["subject"]}


def find_next_class(slots, now):
    """The next class that has NOT started yet (looks up to 7 days ahead).
    Returns (slot, days_ahead) or (None, None). days_ahead 0 = later today."""
    today = now.weekday()
    minute = minute_of_day(now)
    for days_ahead in range(8):
        day = (today + days_ahead) % 7
        todays = sorted((s for s in slots if s["day"] == day), key=lambda s: s["start_min"])
        for slot in todays:
            if days_ahead > 0 or slot["start_min"] > minute:
                return slot, days_ahead
    return None, None


# Samples further apart than this are a gap (ESP32 was off), not energy use.
MAX_GAP_S = 120


def energy_kwh(samples):
    """samples = list of (ts_seconds, power_w), oldest first.
    Energy = sum(power x dt) / 3600 / 1000. Each sample's power is assumed to
    last until the next sample. Gaps longer than MAX_GAP_S are skipped."""
    watt_seconds = 0.0
    for (t1, p1), (t2, _) in zip(samples, samples[1:]):
        dt = t2 - t1
        if 0 < dt <= MAX_GAP_S:
            watt_seconds += (p1 or 0) * dt
    return watt_seconds / 3600 / 1000
