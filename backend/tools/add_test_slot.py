# add_test_slot.py - Adds a "TEST" class that covers the current time,
# or removes all TEST slots. Used for demos and bench tests.
# Flask reads the timetable on every request, so no restart is needed.
#
# Examples (from the backend folder):
#   ..\venv\Scripts\python.exe tools\add_test_slot.py                  TEST class now, for 30 min
#   ..\venv\Scripts\python.exe tools\add_test_slot.py --minutes 10     TEST class now, for 10 min
#   ..\venv\Scripts\python.exe tools\add_test_slot.py --start-in 8     starts in 8 min (lead-time test)
#   ..\venv\Scripts\python.exe tools\add_test_slot.py --remove         delete all TEST slots

import argparse
import os
import sys
from datetime import datetime

# Let this script import db.py from the folder above
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Add or remove TEST timetable slots")
    parser.add_argument("--minutes", type=int, default=30, help="length of the TEST class")
    parser.add_argument("--start-in", type=int, default=0, help="start this many minutes from now")
    parser.add_argument("--remove", action="store_true", help="delete all TEST slots")
    args = parser.parse_args()

    db.init_db()
    conn = db.get_conn()

    if args.remove:
        n = conn.execute("DELETE FROM timetable WHERE subject LIKE 'TEST%'").rowcount
        conn.commit()
        conn.close()
        print("Removed %d TEST slot(s)." % n)
        return

    now = datetime.now()
    start = now.hour * 60 + now.minute + args.start_in
    end = min(start + args.minutes, 24 * 60)     # do not run past midnight
    if start >= 24 * 60:
        sys.exit("That would start after midnight - pick a smaller --start-in.")

    conn.execute(
        "INSERT INTO timetable (room_id, day, start_min, end_min, subject) VALUES (?, ?, ?, ?, ?)",
        (db.ROOM_ID, now.weekday(), start, end, "TEST"),
    )
    conn.commit()
    conn.close()
    print("Added TEST class: %s %s-%s (now is %s)" % (
        db.DAY_NAMES[now.weekday()], db.min_to_hhmm(start), db.min_to_hhmm(end),
        now.strftime("%H:%M")))


if __name__ == "__main__":
    main()
