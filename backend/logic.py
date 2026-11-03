# logic.py - Pure decision functions: should the light and fan be ON?
# should_be_on = class time now (from LEAD_MIN before the start until TRAIL_MIN
#                after the end) AND room occupied (PIR motion within HOLD_MIN minutes).
# No database or network code here, so it is easy to unit-test.
#
# A "slot" is anything with keys day, start_min, end_min, subject
# (a dict, or a sqlite3.Row from db.get_timetable()).

from datetime import timedelta

# Switch on this many minutes BEFORE a class starts, so the room is ready.
LEAD_MIN = 10

# Keep the light on this many minutes AFTER a class ends (students leaving).
TRAIL_MIN = 10

# PIR sees motion, not presence: people sitting still trigger nothing.
# So we count the room as occupied for HOLD_MIN minutes after the last motion.
# Normal range 5-15. An empty room (class cancelled) switches off within HOLD_MIN.
HOLD_MIN = 10


def minute_of_day(now):
    """datetime -> minutes since midnight (09:30 -> 570)."""
    return now.hour * 60 + now.minute


def find_current_class(slots, now, lead_min=LEAD_MIN, trail_min=TRAIL_MIN):
    """Return the slot that is active at 'now', or None.
    Active = same weekday AND (start - lead_min) <= now < (end + trail_min).
    The last minute is NOT included: a 09:00-10:00 class with trail 10 is off at 10:10."""
    day = now.weekday()              # 0=Mon .. 6=Sun, same as the database
    minute = minute_of_day(now)
    for slot in slots:
        if slot["day"] == day and slot["start_min"] - lead_min <= minute < slot["end_min"] + trail_min:
            return slot
    return None


def is_occupied(last_motion, now, hold_min=HOLD_MIN):
    """True if the last PIR motion (a datetime, or None if never) was
    less than hold_min minutes before 'now'."""
    if last_motion is None:
        return False
    return now - last_motion < timedelta(minutes=hold_min)


def decide(slots, now, last_motion, lead_min=LEAD_MIN, hold_min=HOLD_MIN, trail_min=TRAIL_MIN):
    """The main rule. Returns {light, fan, reason, subject}.
    light/fan are True/False; subject is the current class name or None."""
    slot = find_current_class(slots, now, lead_min, trail_min)
    if slot is None:
        return {"light": False, "fan": False,
                "reason": "no class scheduled", "subject": None}

    if not is_occupied(last_motion, now, hold_min):
        return {"light": False, "fan": False,
                "reason": "class scheduled but no motion", "subject": slot["subject"]}

    if minute_of_day(now) >= slot["end_min"]:
        until = slot["end_min"] + trail_min
        return {"light": True, "fan": True, "subject": slot["subject"],
                "reason": "class over, room occupied - on until %02d:%02d (after-class time)" % (until // 60, until % 60)}
    return {"light": True, "fan": True,
            "reason": "class scheduled and room occupied", "subject": slot["subject"]}


# A watchman's manual ON/OFF from the dashboard lasts at most this long,
# then the device goes back to automatic (so nothing stays on by mistake).
OVERRIDE_MIN = 60


def apply_overrides(result, overrides, now):
    """Apply the watchman's manual choices on top of the automatic decision.
    overrides = {"light": ("on" or "off", until_datetime), "fan": (...)}
    Only overrides that have not expired count. Returns a new result dict."""
    final = dict(result)
    active = {}
    for device in ("light", "fan"):
        if device in overrides:
            mode, until = overrides[device]
            if mode in ("on", "off") and now < until:
                final[device] = (mode == "on")
                active[device] = (mode, until)
    if len(active) == 2 and active["light"] == active["fan"]:
        # The dashboard's single switch sets both together: one short note
        mode, until = active["light"]
        notes = ["%s until %s" % (mode.upper(), until.strftime("%H:%M"))]
    else:
        notes = ["%s %s until %s" % (d, m.upper(), u.strftime("%H:%M")) for d, (m, u) in active.items()]
    if notes:
        final["reason"] = "watchman override: %s (auto: %s)" % (", ".join(notes), result["reason"])
    # Separate light/fan reasons (only present in the result of decide_full)
    for device, (mode, until) in active.items():
        if device + "_reason" in final:
            final[device + "_reason"] = "%s %s: watchman override until %s" % (
                device, mode, until.strftime("%H:%M"))
    return final


# ---------- temperature-controlled fan (hysteresis) ----------
# The fan turns ON at FAN_ON_C and only turns OFF again below FAN_OFF_C, so the
# relay does not chatter when the temperature hovers around one value.
FAN_ON_C = 28.0
FAN_OFF_C = 27.0
TEMP_RANGE_C = (0.0, 50.0)   # DHT11 measuring range; anything outside is a bad reading
TEMP_FRESH_S = 15            # a reading older than this counts as "missing"
TEMP_GRACE_S = 60            # missing for longer than this -> fan OFF


def temp_is_valid(temp):
    return temp is not None and TEMP_RANGE_C[0] <= temp <= TEMP_RANGE_C[1]


def fan_heat(temp, age_s, was_hot, on_c=FAN_ON_C, off_c=FAN_OFF_C,
             fresh_s=TEMP_FRESH_S, grace_s=TEMP_GRACE_S):
    """Is it warm enough for the fan? Hysteresis on the newest VALID temperature.
    temp, age_s = that reading and its age in seconds (None if there is none).
    was_hot = the previous answer (stored in device_state).
    Returns (hot, note) where note explains the decision."""
    if temp is None or age_s is None or age_s > grace_s:
        return False, "fan off: no valid temperature reading for over %d s" % grace_s
    if age_s > fresh_s:
        return was_hot, "fan %s: temperature missing for %d s, keeping previous state" % (
            "on" if was_hot else "off", age_s)
    if was_hot:
        if temp < off_c:
            return False, "fan off: %.1f C dropped below %g C" % (temp, off_c)
        if temp >= on_c:
            return True, "fan on: %.1f C >= %g C" % (temp, on_c)
        return True, "fan on: %.1f C, stays on until below %g C" % (temp, off_c)
    if temp >= on_c:
        return True, "fan on: %.1f C >= %g C" % (temp, on_c)
    return False, "fan off: %.1f C is below %g C" % (temp, on_c)


# ---------- daylight saving (light off when it is bright enough) ----------
# Brightness = the LDR's raw ADC value (0-4095), turned round if your wiring
# makes the value FALL when it gets brighter. See README "LDR calibration".
LDR_HIGHER_IS_BRIGHTER = True   # 3.3 V -> LDR -> GPIO32 -> resistor -> GND: brighter = higher
DAY_OFF = 1800                  # brightness above this: enough daylight -> light OFF
DAY_ON = 1400                   # light ON again only when brightness falls below this
DAY_MIN_INTERVAL_S = 60         # at least this long between daylight-driven changes


def brightness(ldr_raw, higher_is_brighter=LDR_HIGHER_IS_BRIGHTER):
    """Raw LDR value -> brightness where higher always means brighter (None stays None)."""
    if ldr_raw is None:
        return None
    return ldr_raw if higher_is_brighter else 4095 - ldr_raw


def daylight_update(level, was_day, last_change_ts, now_ts,
                    off_at=DAY_OFF, on_at=DAY_ON, min_interval_s=DAY_MIN_INTERVAL_S):
    """Hysteresis + minimum interval. Returns (is_day, last_change_ts).
    Day starts above off_at, ends below on_at; in between nothing changes.
    A change is ignored if the last one was less than min_interval_s ago."""
    if level is None:
        return was_day, last_change_ts           # no reading: keep what we had
    want = was_day
    if not was_day and level > off_at:
        want = True
    elif was_day and level < on_at:
        want = False
    if want == was_day:
        return was_day, last_change_ts
    if last_change_ts is not None and now_ts - last_change_ts < min_interval_s:
        return was_day, last_change_ts           # too soon after the last change
    return want, now_ts


def decide_full(base, hot, fan_note, is_day, level=None):
    """Combine the class/occupancy rule (decide()) with the fan temperature rule
    and daylight saving. Returns {light, fan, reason, light_reason, fan_reason, subject}."""
    if base["light"]:
        if is_day:
            light = False
            light_reason = "light off: enough daylight" + (
                " (brightness %d)" % level if level is not None else "")
        else:
            light, light_reason = True, "light on: " + base["reason"]
        fan, fan_reason = hot, fan_note
    else:
        light, light_reason = False, "light off: " + base["reason"]
        fan, fan_reason = False, "fan off: " + base["reason"]
    return {"light": light, "fan": fan, "subject": base["subject"],
            "light_reason": light_reason, "fan_reason": fan_reason,
            "reason": light_reason + "; " + fan_reason}


# ---------- SMS alert: light on with no class ----------
ALERT_AFTER_MIN = 20


def alert_step(light_on, has_class, on_since, armed, now_ts, after_min=ALERT_AFTER_MIN):
    """One step of the alert state machine. Returns (fire, on_since, armed).
    on_since = when the light was first seen ON with no class (None = not counting).
    Fires once when that lasts after_min minutes; re-arms only when the light is OFF."""
    if not light_on:
        return False, None, True
    if has_class:
        return False, None, armed                # light is expected: stop counting
    if on_since is None:
        on_since = now_ts
    if armed and now_ts - on_since >= after_min * 60:
        return True, on_since, False
    return False, on_since, armed


# The SMS the watchman receives. {room} and {minutes} are filled in.
ALERT_TEXT = ("{room}: the class is cancelled but the lights have been on for {minutes} min. "
              "Please switch off the lights and fans.")


def alert_text(room, minutes=ALERT_AFTER_MIN):
    return ALERT_TEXT.format(room=room, minutes=minutes)


def toggle_mode(auto_on, current_on):
    """The dashboard's single ON/OFF button flips the current state.
    If the new state is what the automatic rule wants anyway, go back to
    automatic ("auto"); otherwise return the manual mode ("on" / "off")."""
    target_on = not current_on
    if target_on == auto_on:
        return "auto"
    return "on" if target_on else "off"


# ---------- fast demo clock (tools/demo.py) ----------
# During a demo, time runs faster (default 120x: 1 hour in 30 s) so a whole
# class can be shown in a minute. Lead and hold are then in DEMO minutes:
DEMO_LEAD_MIN = 10      # switch on 10 demo-minutes before class (5 s real at 120x; the ESP32
                        # asks every 2 s = 4 demo-minutes, so 1 demo-minute would be invisible)
DEMO_HOLD_MIN = 20      # empty room switches off 20 demo-minutes after the last motion (10 s real)
DEMO_TRAIL_MIN = 10     # light stays on 10 demo-minutes after the class (5 s real)


def demo_clock(real_ts, demo):
    """Real Unix time -> simulated datetime.
    demo = (start_real_ts, start_sim_datetime, speed)."""
    start_real, start_sim, speed = demo
    return start_sim + timedelta(seconds=(real_ts - start_real) * speed)


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
