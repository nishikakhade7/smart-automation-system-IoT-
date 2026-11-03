# demo.py - Fast-clock demo: 1 hour passes in 30 seconds (speed 120), with a
# ready-made timetable, so the light switches on and off by itself.
#
#   Class A 09:00-10:00  (people present)  -> ON at 08:50 (lead), OFF at 10:10 (10 min after the class)
#   Class B 10:30-12:30  (cancelled)       -> ON at 10:20 (people were just there),
#                                             OFF at 10:35 (nobody for 20 demo-min)
#
# Occupancy is SIMULATED by default (mock input): people are present until 10:15
# demo time, then the room is empty. The real PIR is ignored during the demo, so
# every case happens at the same moment every time. Use --real-pir to use the
# sensor instead (then wave until 10:00 and cover the PIR).
#
# During the demo the backend ignores the real timetable and uses the demo lead
# (10 min), after-class time (10 min) and hold (20 min) from logic.py. Flask must be running.
#
#   ..\venv\Scripts\python.exe tools\demo.py              start (simulated occupancy)
#   ..\venv\Scripts\python.exe tools\demo.py --real-pir   start, use the real PIR
#   ..\venv\Scripts\python.exe tools\demo.py --stop       stop now (back to real time)
#
# The demo stops by itself at 12:45 demo time (about 2 min), or with Ctrl+C.

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db  # noqa: E402

SIM_START = (8, 40)                       # demo clock starts at 08:40
SIM_END = (12, 45)                        # ... and stops at 12:45
PEOPLE_UNTIL = (10, 15)                   # simulated occupancy: people present until 10:15
SLOTS = [((9, 0), (10, 0), "DEMO Class A"),
         ((10, 30), (12, 30), "DEMO Class B (cancelled)")]
URL = "http://127.0.0.1:5000/api/status"


def stop(conn):
    conn.execute("DELETE FROM demo")
    conn.execute("DELETE FROM timetable WHERE subject LIKE 'DEMO%'")
    conn.commit()
    print("\nDemo stopped - back to real time and the real timetable.")


def start(conn, speed, real_pir):
    today = datetime.now()
    at = lambda hm: today.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    conn.execute("DELETE FROM timetable WHERE subject LIKE 'DEMO%'")
    for (h1, m1), (h2, m2), name in SLOTS:
        conn.execute("INSERT INTO timetable (room_id, day, start_min, end_min, subject) "
                     "VALUES (?, ?, ?, ?, ?)", (db.ROOM_ID, today.weekday(), h1 * 60 + m1, h2 * 60 + m2, name))
    conn.execute("DELETE FROM overrides")          # watchman switch back to automatic
    motion_until = None if real_pir else at(PEOPLE_UNTIL).timestamp()
    conn.execute("INSERT OR REPLACE INTO demo (id, start_real, start_sim, speed, motion_until) "
                 "VALUES (1, ?, ?, ?, ?)", (time.time(), at(SIM_START).timestamp(), speed, motion_until))
    conn.commit()

    real = lambda h, m: ((h * 60 + m) - (SIM_START[0] * 60 + SIM_START[1])) * 60 / speed
    print("DEMO started: speed %gx (1 hour = %.0f s), occupancy: %s\n" % (
        speed, 3600 / speed, "REAL PIR" if real_pir else "SIMULATED (people until 10:15)"))
    print("  Real time  Demo time  What should happen")
    print("  +%4.1f s    08:50      Case 2: class A in 10 min, people present -> light ON" % real(8, 50))
    print("  +%4.1f s    10:10      Case 3: 10 min after class A -> light OFF" % real(10, 10))
    print("  +%4.1f s    10:20      Case 1: class B in 10 min, people just left -> light ON" % real(10, 20))
    if real_pir:
        print("  ~+65 s     ~10:50     Case 1: nobody came -> light OFF (10 s after the PIR shows 0)")
        print("\n  YOU: keep waving at the PIR until 10:00, then cover it and step away.\n")
    else:
        print("  +%4.1f s    10:35      Case 1: nobody came -> light OFF" % real(10, 35))
        print("\n  Nothing to do - just watch the bulb and the dashboard.\n")


def watch(conn, speed, real_pir):
    """Print the demo clock and the light until SIM_END."""
    end_real = time.time() + ((SIM_END[0] * 60 + SIM_END[1]) - (SIM_START[0] * 60 + SIM_START[1])) * 60 / speed
    last = None
    told_to_cover = False
    if real_pir:
        print(">>> WAVE AT THE PIR NOW (keep waving until 10:00) <<<\n", flush=True)
    while time.time() < end_real:
        try:
            with urllib.request.urlopen(URL, timeout=3) as resp:
                s = json.loads(resp.read())
            hhmm = s["time"][11:16]
            if real_pir and hhmm >= "10:00" and not told_to_cover:
                print("\n>>> 10:00 - COVER THE PIR NOW AND STEP AWAY <<<\n", flush=True)
                told_to_cover = True
            cls = s["current_class"]["subject"] if s["current_class"] else "no class"
            pir = (s["latest"] or {}).get("pir") if real_pir else "sim"
            line = "demo %s | PIR=%-3s | light %-3s | room %-8s | %-24s | %s" % (
                hhmm, pir, "ON" if s["command"]["light"] else "OFF",
                "occupied" if s["occupied"] else "empty", cls, s["command"]["reason"])
            # print only when something changes, plus every 10 demo-minutes
            key = (s["command"]["light"], s["occupied"], cls, pir)
            if key != last or s["time"][15] == "0":
                print(line, flush=True)
                last = key
        except OSError as e:
            print("Flask not reachable:", e, flush=True)
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Fast-clock demo of the smart classroom")
    parser.add_argument("--speed", type=float, default=120, help="120 = 1 hour in 30 s")
    parser.add_argument("--real-pir", action="store_true", help="use the real PIR instead of simulated occupancy")
    parser.add_argument("--stop", action="store_true", help="stop a running demo")
    args = parser.parse_args()

    db.init_db()
    conn = db.get_conn()
    if args.stop:
        stop(conn)
        return
    start(conn, args.speed, args.real_pir)
    try:
        watch(conn, args.speed, args.real_pir)
    except KeyboardInterrupt:
        pass
    finally:
        stop(conn)
        conn.close()


if __name__ == "__main__":
    main()
