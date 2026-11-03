# db.py - SQLite connection, table schema and timetable seeding
# from docs/timetable.csv.
#
# Run it directly to create the database and print what is inside:
#   python db.py            -> create tables (if missing), seed if empty, print
#   python db.py --reseed   -> throw away the timetable and reload it from the CSV

import csv
import os
import sqlite3
import sys

# File locations (relative to this file, so it works from any folder)
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BACKEND_DIR, "classroom.db")
CSV_PATH = os.path.join(BACKEND_DIR, "..", "docs", "timetable.csv")

# We automate exactly ONE classroom, so everything uses this room id.
ROOM_ID = 1
ROOM_NAME = "Classroom 1"

# CSV uses day names; the database stores 0=Mon .. 6=Sun
# (same numbering as Python's datetime.weekday()).
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- One row per class. Times are minutes since midnight (09:30 -> 570).
CREATE TABLE IF NOT EXISTS timetable (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id   INTEGER NOT NULL REFERENCES rooms(id),
    day       INTEGER NOT NULL,          -- 0=Mon .. 6=Sun
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    subject   TEXT NOT NULL
);

-- One row per ESP32 report. ts = Unix time (seconds) on the laptop.
CREATE TABLE IF NOT EXISTS telemetry (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    pir     INTEGER,                     -- 1 = motion seen, 0 = none
    ldr     INTEGER,                     -- raw ADC value 0-4095
    temp    REAL,                        -- deg C (DHT11)
    hum     REAL,                        -- % (DHT11)
    power_w REAL,                        -- watts (measured, or estimated from relay state)
    current_a REAL,                      -- amps RMS from the ACS712 (NULL if not measured)
    voltage_v REAL,                      -- mains volts RMS from the ZMPT101B (NULL if not calibrated)
    voltage_mv REAL                      -- ZMPT101B signal in mV RMS (for calibration)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);

-- The last command sent to the room, so the dashboard can show it.
CREATE TABLE IF NOT EXISTS device_state (
    room_id    INTEGER PRIMARY KEY REFERENCES rooms(id),
    light      INTEGER NOT NULL DEFAULT 0,
    fan        INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    updated_ts REAL
);

-- Fast demo clock (tools/demo.py). One row while a demo runs, none otherwise.
CREATE TABLE IF NOT EXISTS demo (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    start_real REAL NOT NULL,            -- real Unix time when the demo started
    start_sim  REAL NOT NULL,            -- simulated time at that moment (Unix time)
    speed      REAL NOT NULL,            -- 120 = 1 hour in 30 s
    motion_until REAL                    -- simulated occupancy: people present until this
                                         -- simulated time (Unix time); NULL = use the real PIR
);

-- Every SMS attempt (Twilio). kind = 'alert' or 'test'. For alerts, episode is the
-- start time of the "light on with no class" episode; the unique index below makes
-- it impossible to send two alerts for the same episode.
CREATE TABLE IF NOT EXISTS sms_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,            -- when it was queued
    kind       TEXT NOT NULL,
    episode    TEXT,
    text       TEXT,
    sid        TEXT,                     -- Twilio message SID (SM...)
    status     TEXT,                     -- sending / queued / sent / delivered / undelivered / failed
    error_code INTEGER,
    info       TEXT,                     -- readable result, never contains secrets
    updated_ts REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_episode ON sms_log(kind, episode) WHERE episode IS NOT NULL;

-- History of decisions for the analytics. A row is written only when the decision
-- CHANGES (light, fan, light reason without numbers, override), not every 2 s.
CREATE TABLE IF NOT EXISTS decision_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    light        INTEGER NOT NULL,
    fan          INTEGER NOT NULL,
    light_reason TEXT,
    fan_reason   TEXT,
    override     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts);

-- Manual ON/OFF chosen by the watchman on the dashboard.
-- No row = automatic. A row stops counting after until_ts.
CREATE TABLE IF NOT EXISTS overrides (
    device   TEXT PRIMARY KEY,           -- 'light' or 'fan'
    mode     TEXT NOT NULL,              -- 'on' or 'off'
    until_ts REAL NOT NULL
);
"""


# Extra device_state columns, added to old and new databases by init_db()
DEVICE_STATE_EXTRA = [
    ("light_reason", "TEXT"),            # why the light is on/off
    ("fan_reason", "TEXT"),              # why the fan is on/off
    ("fan_hot", "INTEGER DEFAULT 0"),    # fan temperature hysteresis: 1 = warm enough
    ("daylight", "INTEGER DEFAULT 0"),   # daylight hysteresis: 1 = enough daylight
    ("daylight_ts", "REAL"),             # when the daylight state last changed
    ("alert_on_since", "REAL"),          # light ON with no class since (NULL = not counting)
    ("alert_armed", "INTEGER DEFAULT 1"),  # 1 = an alert may be sent
    ("last_alert_ts", "REAL"),           # last SMS attempt
    ("last_alert_text", "TEXT"),
    ("last_alert_ok", "INTEGER"),
    ("last_alert_info", "TEXT"),         # (old, unused: SMS results are now in sms_log)
]


def get_conn():
    """Open the database. Rows behave like dicts: row["subject"]."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hhmm_to_min(text):
    """'09:30' -> 570"""
    hours, minutes = text.strip().split(":")
    return int(hours) * 60 + int(minutes)


def min_to_hhmm(minutes):
    """570 -> '09:30'"""
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def read_timetable_csv(path=CSV_PATH):
    """Read the CSV and return a list of (day, start_min, end_min, subject).
    Stops with a clear message if a row is wrong."""
    slots = []
    with open(path, newline="", encoding="utf-8") as f:
        # line 1 is the header, so data starts at line 2
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                day = DAY_NAMES.index(row["day"].strip().title())
                start = hhmm_to_min(row["start"])
                end = hhmm_to_min(row["end"])
                subject = row["subject"].strip()
            except (ValueError, KeyError, AttributeError):
                sys.exit("timetable.csv line %d is invalid: %s\n"
                         "Expected format: Mon,09:00,10:00,Subject" % (line_no, row))
            if start >= end:
                sys.exit("timetable.csv line %d: start must be before end" % line_no)
            slots.append((day, start, end, subject))
    return slots


def seed_timetable(conn, path=CSV_PATH):
    """Replace the room's timetable with the contents of the CSV."""
    slots = read_timetable_csv(path)
    conn.execute("DELETE FROM timetable WHERE room_id = ?", (ROOM_ID,))
    conn.executemany(
        "INSERT INTO timetable (room_id, day, start_min, end_min, subject) "
        "VALUES (?, ?, ?, ?, ?)",
        [(ROOM_ID,) + slot for slot in slots],
    )
    conn.commit()
    return len(slots)


def init_db():
    """Create tables if missing, add the room, and seed the timetable
    the first time (when it is empty). Safe to call on every start."""
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Databases made before the ACS712 was added have no current_a column yet
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(telemetry)")]
    for col in ("current_a", "voltage_v", "voltage_mv"):
        if col not in columns:
            conn.execute("ALTER TABLE telemetry ADD COLUMN %s REAL" % col)
    if "motion_until" not in [r["name"] for r in conn.execute("PRAGMA table_info(demo)")]:
        conn.execute("ALTER TABLE demo ADD COLUMN motion_until REAL")
    # State kept between /command requests (fan + daylight hysteresis, SMS alert)
    have = [r["name"] for r in conn.execute("PRAGMA table_info(device_state)")]
    for name, kind in DEVICE_STATE_EXTRA:
        if name not in have:
            conn.execute("ALTER TABLE device_state ADD COLUMN %s %s" % (name, kind))
    conn.execute("INSERT OR IGNORE INTO rooms (id, name) VALUES (?, ?)",
                 (ROOM_ID, ROOM_NAME))
    conn.execute("INSERT OR IGNORE INTO device_state (room_id, light, fan, reason) "
                 "VALUES (?, 0, 0, 'not started yet')", (ROOM_ID,))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM timetable").fetchone()[0]
    if count == 0:
        print("Timetable empty - seeding %d slots from timetable.csv" % seed_timetable(conn))
    conn.close()


def get_timetable(conn, room_id=ROOM_ID):
    """All slots for the room, sorted by day and start time."""
    return conn.execute(
        "SELECT day, start_min, end_min, subject FROM timetable "
        "WHERE room_id = ? ORDER BY day, start_min", (room_id,)
    ).fetchall()


def print_contents():
    """Print every table so we can see what is stored."""
    conn = get_conn()
    print("\n--- rooms ---")
    for r in conn.execute("SELECT * FROM rooms"):
        print(" ", r["id"], r["name"])

    print("\n--- timetable ---")
    for r in get_timetable(conn):
        print("  %s  %s-%s  %s" % (DAY_NAMES[r["day"]], min_to_hhmm(r["start_min"]),
                                   min_to_hhmm(r["end_min"]), r["subject"]))

    n = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    print("\n--- telemetry --- %d rows" % n)

    print("\n--- device_state ---")
    for r in conn.execute("SELECT * FROM device_state"):
        print("  room %d: light=%d fan=%d reason=%r" %
              (r["room_id"], r["light"], r["fan"], r["reason"]))
    conn.close()


if __name__ == "__main__":
    init_db()
    if "--reseed" in sys.argv:
        conn = get_conn()
        print("Reseeded %d slots from timetable.csv" % seed_timetable(conn))
        conn.close()
    print_contents()
