# test_analytics.py - Tests for analytics.py and GET /api/analytics, on a temporary
# database filled with known telemetry, so the expected results are exact.
#
# Run from the backend folder:
#   ..\venv\Scripts\python.exe -m unittest discover -s tests -v

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import alerts     # noqa: E402
import analytics  # noqa: E402
import db         # noqa: E402

MON = datetime(2026, 10, 26)          # a Monday
NOON = MON.replace(hour=12)


def ts(h, m=0, s=0):
    return MON.replace(hour=h, minute=m, second=s).timestamp()


class AnalyticsDB(unittest.TestCase):
    """Fresh database: one class Mon 09:00-10:00, telemetry every 2 s from 08:30 to 10:30."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "a.db")
        db.init_db()
        self.conn = db.get_conn()
        self.conn.execute("DELETE FROM timetable")
        self.conn.execute("INSERT INTO timetable (room_id, day, start_min, end_min, subject) VALUES (1, 0, 540, 600, 'Maths')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db.DB_PATH = self.old

    def fill(self, start, end, pir=lambda t: 1, power=lambda t: 9.0 if ts(9) <= t < ts(10) else 0.0,
             temp=lambda t: 26.0, skip=()):
        t = start
        rows = []
        while t < end:
            if not any(a <= t < b for a, b in skip):
                rows.append((t, pir(t), 1000, temp(t), 50.0, power(t), None))
            t += 2
        self.conn.executemany("INSERT INTO telemetry (ts, pir, ldr, temp, hum, power_w, current_a) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def run_today(self):
        return analytics.compute(self.conn, "today", light_w=9, fan_w=0, now=NOON)


class TestEnergyAndSavings(AnalyticsDB):

    def test_energy_one_hour_at_9w(self):
        self.fill(ts(8, 30), ts(10, 30))
        a = self.run_today()
        self.assertAlmostEqual(a["energy"]["kwh_total"], 0.009, places=4)
        self.assertAlmostEqual(a["energy"]["cost_total"], round(0.009 * analytics.PRICE_PER_KWH, 2), places=2)

    def test_baselines(self):
        self.fill(ts(8, 30), ts(10, 30))
        s = self.run_today()["savings"]
        self.assertAlmostEqual(s["baseline_scheduled_kwh"], 60 * 9 / 60 / 1000, places=4)   # 1 class hour
        self.assertAlmostEqual(s["baseline_college_kwh"], 180 * 9 / 60 / 1000, places=4)    # 09:00-12:00 so far
        self.assertAlmostEqual(s["saved_vs_college_kwh"], 0.027 - 0.009, places=4)

    def test_gap_is_not_counted_as_energy(self):
        self.fill(ts(8, 30), ts(10, 30), skip=[(ts(9, 10), ts(9, 20))])         # 10 min without data
        self.assertAlmostEqual(self.run_today()["energy"]["kwh_total"], 0.009 * 50 / 60, places=4)


class TestLightingAndOccupancy(AnalyticsDB):

    def test_light_minutes_inside_and_outside_class(self):
        self.fill(ts(8, 30), ts(10, 30), power=lambda t: 9.0 if ts(8, 50) <= t < ts(10) else 0.0)
        light = self.run_today()["lighting"]
        self.assertEqual(light["per_day"][-1]["in_class"], 60)
        self.assertEqual(light["per_day"][-1]["outside"], 10)

    def test_occupied_class(self):
        self.fill(ts(8, 30), ts(10, 30))
        occ = self.run_today()["occupancy"]
        self.assertEqual(occ["occupied_pct"], 100.0)
        self.assertEqual(occ["classes"]["held"], 1)
        self.assertEqual(occ["classes"]["no_motion"], [])

    def test_class_with_nobody_is_listed(self):
        self.fill(ts(8, 30), ts(10, 30), pir=lambda t: 0)
        occ = self.run_today()["occupancy"]
        self.assertEqual(occ["occupied_pct"], 0.0)
        self.assertEqual(occ["classes"]["no_motion"], [{"date": "Mon 26 Oct", "time": "09:00-10:00"}])

    def test_class_without_data_is_not_called_cancelled(self):
        self.fill(ts(11, 0), ts(11, 30))
        classes = self.run_today()["occupancy"]["classes"]
        self.assertEqual((classes["no_data"], classes["no_motion"]), (1, []))

    def test_heatmap(self):
        self.fill(ts(8, 30), ts(9, 0))
        heat = self.run_today()["occupancy"]["heatmap"]
        self.assertEqual(heat[0][8], 30)                     # Monday, 08:00 hour, 30 motion minutes
        self.assertEqual(sum(map(sum, heat)), 30)


class TestEnvironmentReliabilitySms(AnalyticsDB):

    def test_environment_and_minutes_above_28(self):
        self.fill(ts(8, 30), ts(10, 30), temp=lambda t: 29.0 if t >= ts(10) else 26.0)
        env = self.run_today()["environment"]
        self.assertEqual(env["minutes_above_fan_on"], 30)
        hour10 = [b for b in env["buckets"] if b["label"] == "10:00"][0]
        self.assertEqual(hour10["temp"]["max"], 29.0)

    def test_invalid_temperatures_ignored(self):
        self.fill(ts(9), ts(9, 10), temp=lambda t: 85.0)     # outside the DHT11 range
        bucket = self.run_today()["environment"]["buckets"][0]
        self.assertIsNone(bucket["temp"])

    def test_uptime_and_offline_periods(self):
        self.fill(ts(8, 30), ts(10, 30), skip=[(ts(9, 30), ts(9, 31))])        # one 60 s gap
        rel = self.run_today()["reliability"]
        self.assertEqual(rel["offline_periods"], 1)
        self.assertGreaterEqual(rel["longest_gap_s"], 60)
        self.assertAlmostEqual(rel["uptime_pct"], round(100 * 119 / 210, 1), delta=0.6)

    def test_sms_counts(self):
        for kind, status in (("alert", "delivered"), ("test", "failed"), ("test", "delivered")):
            self.conn.execute("INSERT INTO sms_log (ts, kind, status) VALUES (?, ?, ?)", (ts(9), kind, status))
        self.conn.commit()
        sms = self.run_today()["sms"]
        self.assertEqual((sms["total"], sms["alerts"], sms["tests"]), (3, 1, 2))
        self.assertEqual(sms["by_status"], {"delivered": 2, "failed": 1})

    def test_empty_database(self):
        a = self.run_today()
        self.assertFalse(a["has_data"])
        self.assertEqual(a["energy"]["kwh_total"], 0)
        self.assertIsNone(a["occupancy"]["occupied_pct"])
        self.assertIsNone(a["reliability"]["uptime_pct"])

    def test_decision_summary(self):
        rows = [(ts(9), 0, 0, "light off: enough daylight", 0), (ts(9, 30), 1, 1, "light on: x", 1),
                (ts(10), 0, 0, "light off: no class scheduled", 0)]
        self.conn.executemany("INSERT INTO decision_log (ts, light, fan, light_reason, override) "
                              "VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()
        d = self.run_today()["decisions"]
        self.assertEqual((d["daylight_off_min"], d["override_min"], d["light_on_min"]), (30.0, 30.0, 30.0))

    def test_7d_range_has_seven_days(self):
        a = analytics.compute(self.conn, "7d", 9, 0, now=NOON)
        self.assertEqual(len(a["energy"]["per_day"]), 7)
        self.assertEqual(a["energy"]["per_day"][-1]["label"], "Mon 26 Oct")


class TestAnalyticsEndpoint(AnalyticsDB):

    def setUp(self):
        super().setUp()
        import app as app_module
        self.app = app_module
        self.client = app_module.app.test_client()
        analytics._cache.clear()
        self.patches = [mock.patch.object(alerts, "CONFIG_PATH", os.path.join(self.tmp, "none.json")),
                        mock.patch.object(alerts, "_client", side_effect=AssertionError("no real Twilio call"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_ranges(self):
        for r in ("today", "7d", "30d"):
            resp = self.client.get("/api/analytics?range=" + r)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["range"], r)

    def test_invalid_range(self):
        self.assertEqual(self.client.get("/api/analytics?range=1y").status_code, 400)

    def test_cached(self):
        self.client.get("/api/analytics?range=today")
        with mock.patch.object(analytics, "compute") as compute:
            self.client.get("/api/analytics?range=today")
        compute.assert_not_called()

    def test_decision_log_only_on_change(self):
        for _ in range(5):
            self.client.post("/telemetry", json={"pir": 0, "ldr": 900, "temp": 25})
            self.client.get("/command")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0], 1)
        self.client.post("/api/toggle")                       # watchman ON -> decision changes
        self.client.get("/command")
        rows = self.conn.execute("SELECT light, override FROM decision_log ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(0, 0), (1, 1)])

    def test_status_still_has_old_fields(self):
        s = self.client.get("/api/status").get_json()
        for key in ("time", "esp_online", "latest", "command", "current_class", "next_class", "overrides", "alert"):
            self.assertIn(key, s)


if __name__ == "__main__":
    unittest.main()
