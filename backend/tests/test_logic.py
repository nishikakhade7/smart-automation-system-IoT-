# test_logic.py - Unit tests for logic.py: no class, class + motion,
# hold timer expiry, 5-minute lead time, end of class, wrong weekday.
#
# Run from the backend folder:
#   ..\venv\Scripts\python.exe -m unittest discover -s tests -v

import os
import sys
import unittest
from datetime import datetime, timedelta

# Let the test import logic.py from the folder above
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import logic  # noqa: E402

# A small fixed timetable just for the tests (not the real CSV),
# so the tests never break when the timetable changes.
SLOTS = [
    {"day": 0, "start_min": 9 * 60, "end_min": 10 * 60, "subject": "Maths"},       # Mon 09:00-10:00
    {"day": 2, "start_min": 14 * 60, "end_min": 15 * 60, "subject": "Physics"},    # Wed 14:00-15:00
]


def mon(hour, minute):
    """A time on Monday 12 Oct 2026."""
    return datetime(2026, 10, 12, hour, minute)


class TestDecision(unittest.TestCase):

    def test_date_really_is_monday(self):
        self.assertEqual(mon(9, 0).weekday(), 0)

    # 1. No class -> off, even with motion
    def test_no_class_is_off(self):
        now = mon(13, 0)
        result = logic.decide(SLOTS, now, last_motion=now)
        self.assertFalse(result["light"])
        self.assertFalse(result["fan"])
        self.assertEqual(result["reason"], "no class scheduled")

    # 2. Class + recent motion -> on
    def test_class_and_motion_is_on(self):
        now = mon(9, 30)
        result = logic.decide(SLOTS, now, last_motion=now - timedelta(minutes=1), hold_min=10)
        self.assertTrue(result["light"])
        self.assertTrue(result["fan"])
        self.assertEqual(result["reason"], "class scheduled and room occupied")
        self.assertEqual(result["subject"], "Maths")

    # 3. Class but last motion older than HOLD_MIN -> off
    def test_class_but_hold_expired_is_off(self):
        now = mon(9, 30)
        result = logic.decide(SLOTS, now, last_motion=now - timedelta(minutes=11), hold_min=10)
        self.assertFalse(result["light"])
        self.assertEqual(result["reason"], "class scheduled but no motion")

    def test_hold_timer_boundary(self):
        now = mon(9, 30)
        # 9 min 59 s after motion: still occupied
        self.assertTrue(logic.is_occupied(now - timedelta(minutes=9, seconds=59), now, 10))
        # exactly 10 min after motion: hold has expired
        self.assertFalse(logic.is_occupied(now - timedelta(minutes=10), now, 10))

    def test_class_but_never_any_motion_is_off(self):
        result = logic.decide(SLOTS, mon(9, 30), last_motion=None)
        self.assertFalse(result["light"])
        self.assertEqual(result["reason"], "class scheduled but no motion")

    # 4. Lead time: on from 5 minutes before the start
    def test_lead_time_boundary(self):
        at_0855 = mon(8, 55)
        at_0854 = mon(8, 54)
        self.assertTrue(logic.decide(SLOTS, at_0855, last_motion=at_0855)["light"])
        self.assertFalse(logic.decide(SLOTS, at_0854, last_motion=at_0854)["light"])
        self.assertEqual(logic.decide(SLOTS, at_0854, last_motion=at_0854)["reason"],
                         "no class scheduled")

    # 5. End of class: on at 09:59, off at 10:00
    def test_end_of_class_boundary(self):
        at_0959 = mon(9, 59)
        at_1000 = mon(10, 0)
        self.assertTrue(logic.decide(SLOTS, at_0959, last_motion=at_0959)["light"])
        self.assertFalse(logic.decide(SLOTS, at_1000, last_motion=at_1000)["light"])

    # 6. Right time, wrong weekday -> off
    def test_wrong_day_is_off(self):
        tuesday_0930 = datetime(2026, 10, 13, 9, 30)
        self.assertEqual(tuesday_0930.weekday(), 1)
        result = logic.decide(SLOTS, tuesday_0930, last_motion=tuesday_0930)
        self.assertFalse(result["light"])
        self.assertEqual(result["reason"], "no class scheduled")

    def test_other_day_slot_works(self):
        wed_1430 = datetime(2026, 10, 14, 14, 30)
        self.assertEqual(logic.decide(SLOTS, wed_1430, last_motion=wed_1430)["subject"], "Physics")


class TestNextClass(unittest.TestCase):

    def test_next_class_later_today(self):
        slot, days = logic.find_next_class(SLOTS, mon(8, 0))
        self.assertEqual((slot["subject"], days), ("Maths", 0))

    def test_next_class_on_another_day(self):
        # Monday 09:30: Maths already started, so next is Wednesday Physics
        slot, days = logic.find_next_class(SLOTS, mon(9, 30))
        self.assertEqual((slot["subject"], days), ("Physics", 2))

    def test_no_slots(self):
        self.assertEqual(logic.find_next_class([], mon(9, 0)), (None, None))


class TestEnergy(unittest.TestCase):

    def test_one_hour_at_100w_is_0_1_kwh(self):
        # 100 W reported every 2 s for one hour
        samples = [(t, 100) for t in range(0, 3601, 2)]
        self.assertAlmostEqual(logic.energy_kwh(samples), 0.1)

    def test_long_gap_is_ignored(self):
        # 10 s at 1000 W, then a 300 s gap (ESP32 off), then 10 s more
        samples = [(0, 1000), (10, 1000), (310, 1000), (320, 1000)]
        # counted: 0->10 and 310->320 = 20 s x 1000 W
        self.assertAlmostEqual(logic.energy_kwh(samples), 20 * 1000 / 3600 / 1000)

    def test_empty(self):
        self.assertEqual(logic.energy_kwh([]), 0)


if __name__ == "__main__":
    unittest.main()
