# test_features.py - Tests for the temperature fan, daylight saving and SMS alerts.
# The SMS provider is never contacted: urllib is mocked.
#
# Run from the backend folder:
#   ..\venv\Scripts\python.exe -m unittest discover -s tests -v

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import alerts  # noqa: E402
import logic   # noqa: E402

BASE_ON = {"light": True, "fan": True, "reason": "class scheduled and room occupied", "subject": "Maths"}
BASE_NO_CLASS = {"light": False, "fan": False, "reason": "no class scheduled", "subject": None}


class TestFanHysteresis(unittest.TestCase):

    def test_turns_on_at_28(self):
        hot, note = logic.fan_heat(28.0, 2, was_hot=False)
        self.assertTrue(hot)
        self.assertEqual(note, "fan on: 28.0 C >= 28 C")

    def test_stays_off_below_28(self):
        hot, note = logic.fan_heat(27.5, 2, was_hot=False)
        self.assertFalse(hot)
        self.assertEqual(note, "fan off: 27.5 C is below 28 C")

    def test_stays_on_at_27_5(self):
        hot, note = logic.fan_heat(27.5, 2, was_hot=True)
        self.assertTrue(hot)
        self.assertIn("stays on until below 27 C", note)

    def test_stays_on_at_exactly_27(self):
        self.assertTrue(logic.fan_heat(27.0, 2, was_hot=True)[0])

    def test_turns_off_below_27(self):
        hot, note = logic.fan_heat(26.9, 2, was_hot=True)
        self.assertFalse(hot)
        self.assertEqual(note, "fan off: 26.9 C dropped below 27 C")

    def test_full_cycle_no_chatter(self):
        hot, states = False, []
        for t in [26, 27.5, 28, 27.8, 27.2, 27.0, 26.8, 27.5]:
            hot, _ = logic.fan_heat(t, 2, hot)
            states.append(hot)
        self.assertEqual(states, [False, False, True, True, True, True, False, False])


class TestInvalidTemperature(unittest.TestCase):

    def test_no_reading_at_all_is_off(self):
        hot, note = logic.fan_heat(None, None, was_hot=True)
        self.assertFalse(hot)
        self.assertIn("no valid temperature", note)

    def test_short_gap_keeps_previous_state(self):
        hot, note = logic.fan_heat(29.0, 30, was_hot=True)     # last good reading 30 s ago
        self.assertTrue(hot)
        self.assertIn("keeping previous state", note)
        self.assertFalse(logic.fan_heat(29.0, 30, was_hot=False)[0])

    def test_long_gap_turns_fan_off(self):
        hot, note = logic.fan_heat(29.0, 61, was_hot=True)
        self.assertFalse(hot)
        self.assertIn("for over 60 s", note)

    def test_out_of_range_values_are_invalid(self):
        self.assertFalse(logic.temp_is_valid(None))
        self.assertFalse(logic.temp_is_valid(-5))
        self.assertFalse(logic.temp_is_valid(85))
        self.assertTrue(logic.temp_is_valid(24))


class TestDaylight(unittest.TestCase):

    def test_hysteresis(self):
        self.assertEqual((logic.DAY_OFF, logic.DAY_ON), (1800, 1400))
        day, ts = logic.daylight_update(1790, False, None, 1000)        # below DAY_OFF: still dark
        self.assertFalse(day)
        day, ts = logic.daylight_update(1810, False, None, 1000)        # above 1800: day -> light off
        self.assertTrue(day)
        self.assertEqual(ts, 1000)
        day, ts = logic.daylight_update(1500, True, ts, 1100)           # between: stays day
        self.assertTrue(day)
        day, ts = logic.daylight_update(1300, True, ts, 1100)           # below DAY_ON: dark again
        self.assertFalse(day)
        self.assertEqual(ts, 1100)

    def test_minimum_interval(self):
        day, ts = logic.daylight_update(3000, False, None, 1000)        # day at t=1000
        day, ts = logic.daylight_update(1000, day, ts, 1030)            # 30 s later: too soon
        self.assertTrue(day)
        self.assertEqual(ts, 1000)
        day, ts = logic.daylight_update(1000, day, ts, 1060)            # 60 s later: allowed
        self.assertFalse(day)

    def test_missing_reading_keeps_state(self):
        self.assertEqual(logic.daylight_update(None, True, 5, 1000), (True, 5))

    def test_inverted_ldr(self):
        self.assertEqual(logic.brightness(500, higher_is_brighter=False), 3595)
        self.assertEqual(logic.brightness(500, higher_is_brighter=True), 500)
        self.assertIsNone(logic.brightness(None))


class TestDecideFull(unittest.TestCase):

    def test_daylight_turns_light_off_but_fan_follows_temperature(self):
        r = logic.decide_full(BASE_ON, True, "fan on: 29.5 C >= 28 C", is_day=True, level=3000)
        self.assertFalse(r["light"])
        self.assertTrue(r["fan"])
        self.assertTrue(r["light_reason"].startswith("light off: enough daylight"))
        self.assertEqual(r["fan_reason"], "fan on: 29.5 C >= 28 C")

    def test_cool_room_light_on_fan_off(self):
        r = logic.decide_full(BASE_ON, False, "fan off: 24.0 C is below 28 C", is_day=False)
        self.assertTrue(r["light"])
        self.assertFalse(r["fan"])
        self.assertEqual(r["light_reason"], "light on: class scheduled and room occupied")

    def test_no_class_everything_off_even_if_hot(self):
        r = logic.decide_full(BASE_NO_CLASS, True, "fan on: 30.0 C >= 28 C", is_day=False)
        self.assertFalse(r["light"])
        self.assertFalse(r["fan"])
        self.assertEqual(r["fan_reason"], "fan off: no class scheduled")


class TestAlertStep(unittest.TestCase):

    def run_steps(self, steps):
        """steps = [(minute, light_on, has_class)] -> list of minutes where an alert fired."""
        on_since, armed, fired = None, True, []
        for minute, light, has_class in steps:
            fire, on_since, armed = logic.alert_step(light, has_class, on_since, armed, minute * 60)
            if fire:
                fired.append(minute)
        return fired

    def test_fires_once_after_20_min(self):
        steps = [(m, True, False) for m in range(0, 60)]               # light on, no class, 1 h
        self.assertEqual(self.run_steps(steps), [20])

    def test_not_before_20_min(self):
        self.assertEqual(self.run_steps([(m, True, False) for m in range(0, 20)]), [])

    def test_rearms_after_light_off(self):
        steps = ([(m, True, False) for m in range(0, 25)] + [(25, False, False)] +
                 [(m, True, False) for m in range(26, 50)])
        self.assertEqual(self.run_steps(steps), [20, 46])

    def test_class_scheduled_means_no_alert(self):
        self.assertEqual(self.run_steps([(m, True, True) for m in range(0, 60)]), [])

    def test_text(self):
        self.assertEqual(logic.alert_text("Room 101"), "Room 101: the class is cancelled but the lights have been on for 20 min. Please switch off the lights and fans.")


SECRET = "0123456789abcdef0123456789abcdef"           # fake auth token, must never leak
CFG = {"SMS_PROVIDER": "twilio", "SMS_TO": "+91-98765 43210",
       "TWILIO_ACCOUNT_SID": "AC" + "1" * 32, "TWILIO_AUTH_TOKEN": SECRET, "TWILIO_FROM": "+15550001111"}


def twilio_error(status, code, text="error"):
    from twilio.base.exceptions import TwilioRestException
    return TwilioRestException(status, "https://api.twilio.com/x", msg=text, code=code)


def msg(status, code=None, message=None):
    return mock.Mock(sid="SM" + "a" * 32, status=status, error_code=code, error_message=message)


def fake_client(create=None, fetch=None):
    """A mocked Twilio Client: messages.create(...) and messages(sid).fetch()."""
    client = mock.MagicMock()
    if isinstance(create, (Exception, list)):
        client.messages.create.side_effect = create
    else:
        client.messages.create.return_value = create or msg("queued")
    if fetch is not None:
        client.messages.return_value.fetch.side_effect = fetch if isinstance(fetch, list) else [fetch]
    return client


class TestTwilioConfig(unittest.TestCase):

    def test_complete_config_is_valid(self):
        self.assertEqual(alerts.config_problems(CFG), [])

    def test_missing_values(self):
        problems = alerts.config_problems({"SMS_PROVIDER": "twilio"})
        for key in ("SMS_TO", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
            self.assertIn("%s is missing" % key, problems)

    def test_placeholders_count_as_missing(self):
        cfg = dict(CFG, TWILIO_AUTH_TOKEN="paste-your-twilio-auth-token-here", TWILIO_FROM="+1XXXXXXXXXX")
        self.assertIn("TWILIO_AUTH_TOKEN is missing", alerts.config_problems(cfg))
        self.assertIn("TWILIO_FROM is missing", alerts.config_problems(cfg))

    def test_wrong_provider(self):
        self.assertIn('SMS_PROVIDER must be "twilio"', alerts.config_problems(dict(CFG, SMS_PROVIDER="fast2sms")))

    def test_bad_account_sid(self):
        self.assertTrue(any("ACCOUNT_SID" in p for p in alerts.config_problems(dict(CFG, TWILIO_ACCOUNT_SID="AC12"))))

    def test_e164_normalisation(self):
        self.assertEqual(alerts.normalize_e164("+91-93261 61038"), "+919326161038")
        self.assertEqual(alerts.normalize_e164("0091 9326161038"), "+919326161038")
        self.assertEqual(alerts.normalize_e164("+1 (555) 000-1111"), "+15550001111")

    def test_invalid_numbers(self):
        for bad in ("9326161038", "+91 12345", "+91 5326161038", "+0123456789", "+91abc", ""):
            with self.assertRaises(ValueError):
                alerts.normalize_e164(bad)
        self.assertTrue(any(p.startswith("SMS_TO") for p in alerts.config_problems(dict(CFG, SMS_TO="9326161038"))))

    def test_env_overrides_file(self):
        with mock.patch.object(alerts, "CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "none.json")), \
             mock.patch.dict(os.environ, {"SMS_PROVIDER": "twilio", "SMS_TO": "+919326161038"}):
            cfg = alerts.load_config()
        self.assertEqual(cfg["SMS_TO"], "+919326161038")


class TestTwilioSend(unittest.TestCase):

    def test_success_uses_right_numbers_and_body(self):
        client = fake_client()
        with mock.patch.object(alerts, "_client", return_value=client):
            r = alerts.send_sms("Room 101: test", cfg=CFG)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "queued")
        self.assertTrue(r["sid"].startswith("SM"))
        self.assertIn("not yet delivered", r["info"])           # accepted is not delivered
        client.messages.create.assert_called_once_with(to="+919876543210", from_="+15550001111", body="Room 101: test")

    def test_not_configured_does_not_call_twilio(self):
        with mock.patch.object(alerts, "_client") as factory:
            r = alerts.send_sms("x", cfg={})
        self.assertFalse(r["ok"])
        self.assertIn("not configured", r["info"])
        factory.assert_not_called()

    def test_auth_error(self):
        with mock.patch.object(alerts, "_client", return_value=fake_client(twilio_error(401, 20003, "Authenticate"))):
            r = alerts.send_sms("x", cfg=CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], 20003)
        self.assertIn("Account SID / Auth Token", r["info"])

    def test_trial_unverified_recipient(self):
        err = twilio_error(400, 21608, "The number +919876543210 is unverified. Trial accounts ...")
        with mock.patch.object(alerts, "_client", return_value=fake_client(err)):
            r = alerts.send_sms("x", cfg=CFG)
        self.assertIn("not verified", r["info"])
        self.assertNotIn("9876543210", r["info"])

    def test_country_not_enabled(self):
        with mock.patch.object(alerts, "_client", return_value=fake_client(twilio_error(400, 21408))):
            self.assertIn("Geo permissions", alerts.send_sms("x", cfg=CFG)["info"])

    def test_trial_custom_text_not_allowed(self):
        err = twilio_error(400, 572006, "Unable to create record: Invalid template name. Trial accounts ...")
        with mock.patch.object(alerts, "_client", return_value=fake_client(err)):
            r = alerts.send_sms("x", cfg=CFG)
        self.assertFalse(r["ok"])
        self.assertIn("upgrade the Twilio account", r["info"])

    def test_trial_template_replaces_our_text(self):
        client = fake_client()
        with mock.patch.object(alerts, "_client", return_value=client):
            r = alerts.send_sms("Room 101: our text", cfg=dict(CFG, TWILIO_TRIAL_TEMPLATE="sms_internal_alerts"))
        self.assertTrue(r["ok"])
        self.assertEqual(client.messages.create.call_args[1]["body"], "sms_internal_alerts")
        self.assertIn("trial template 'sms_internal_alerts'", r["info"])

    def test_unknown_trial_template_is_a_config_problem(self):
        problems = alerts.config_problems(dict(CFG, TWILIO_TRIAL_TEMPLATE="sms_hello"))
        self.assertTrue(any("TWILIO_TRIAL_TEMPLATE" in p for p in problems))

    def test_no_template_sends_our_text(self):
        client = fake_client()
        with mock.patch.object(alerts, "_client", return_value=client):
            alerts.send_sms("Room 101: our text", cfg=CFG)
        self.assertEqual(client.messages.create.call_args[1]["body"], "Room 101: our text")

    def test_sender_not_capable(self):
        with mock.patch.object(alerts, "_client", return_value=fake_client(twilio_error(400, 21606))):
            self.assertIn("TWILIO_FROM", alerts.send_sms("x", cfg=CFG)["info"])

    def test_unknown_error_code_is_masked(self):
        err = twilio_error(400, 99999, "odd problem with +919876543210 and token " + SECRET)
        with mock.patch.object(alerts, "_client", return_value=fake_client(err)):
            info = alerts.send_sms("x", cfg=CFG)["info"]
        self.assertIn("99999", info)
        self.assertNotIn(SECRET, info)
        self.assertNotIn("9876543210", info)

    def test_rate_limit_is_retried_then_succeeds(self):
        client = fake_client([twilio_error(429, 20429), msg("queued")])
        with mock.patch.object(alerts, "_client", return_value=client), mock.patch("alerts.time.sleep"):
            r = alerts.send_sms("x", cfg=CFG)
        self.assertTrue(r["ok"])
        self.assertEqual(client.messages.create.call_count, 2)

    def test_other_errors_are_not_retried(self):
        client = fake_client(twilio_error(400, 21211))
        with mock.patch.object(alerts, "_client", return_value=client), mock.patch("alerts.time.sleep"):
            alerts.send_sms("x", cfg=CFG)
        self.assertEqual(client.messages.create.call_count, 1)

    def test_network_error_and_timeout(self):
        import requests
        for err, word in ((requests.exceptions.ConnectionError("down"), "no connection"),
                          (requests.exceptions.ReadTimeout("slow"), "timeout")):
            with mock.patch.object(alerts, "_client", return_value=fake_client(err)):
                r = alerts.send_sms("x", cfg=CFG)
            self.assertFalse(r["ok"])
            self.assertIn(word, r["info"])

    def test_secrets_never_in_logs_or_results(self):
        errors = [twilio_error(401, 20003, SECRET), twilio_error(400, 12345, SECRET), RuntimeError(SECRET)]
        with self.assertLogs("alerts", level="INFO") as logs:
            for err in errors:
                with mock.patch.object(alerts, "_client", return_value=fake_client(err)):
                    self.assertNotIn(SECRET, str(alerts.send_sms("x", cfg=CFG)))
            alerts.log.info("end")
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn("9876543210", "\n".join(logs.output))

    def test_client_has_a_timeout(self):
        client = alerts._client(CFG)                          # builds the client, no network call
        self.assertEqual(client.http_client.timeout, alerts.TIMEOUT_S)


class TestTwilioStatus(unittest.TestCase):

    def test_delivered(self):
        with mock.patch.object(alerts, "_client", return_value=fake_client(fetch=msg("delivered"))):
            status, code, info = alerts.fetch_status("SMx", cfg=CFG)
        self.assertEqual((status, info), ("delivered", "delivered to the phone"))

    def test_undelivered_carrier_filter(self):
        with mock.patch.object(alerts, "_client", return_value=fake_client(fetch=msg("undelivered", 30007))):
            status, code, info = alerts.fetch_status("SMx", cfg=CFG)
        self.assertEqual(code, 30007)
        self.assertIn("filtered by the carrier", info)

    def test_status_lookup_failure_does_not_raise(self):
        with mock.patch.object(alerts, "_client", side_effect=RuntimeError("boom")):
            self.assertIsNone(alerts.fetch_status("SMx", cfg=CFG)[0])

    def test_send_async_follows_status_until_final(self):
        updates = []
        client = fake_client(fetch=[msg("sent"), msg("delivered")])
        with mock.patch.object(alerts, "_client", return_value=client), \
             mock.patch.object(alerts, "STATUS_CHECKS_S", (0, 0, 0)), mock.patch("alerts.time.sleep"):
            alerts.send_async("hi", updates.append, cfg=CFG).join(5)
        self.assertEqual([u["status"] for u in updates], ["queued", "sent", "delivered"])
        self.assertEqual(client.messages.return_value.fetch.call_count, 2)    # stops at the final status

    def test_send_async_failure_reports_and_does_not_raise(self):
        updates = []
        with mock.patch.object(alerts, "_client", return_value=fake_client(twilio_error(400, 21608))):
            alerts.send_async("hi", updates.append, cfg=CFG).join(5)
        self.assertEqual(len(updates), 1)
        self.assertFalse(updates[0]["ok"])

    def test_callback_error_is_swallowed(self):
        def broken(result):
            raise RuntimeError("db down")
        with mock.patch.object(alerts, "_client", return_value=fake_client(twilio_error(400, 21211))):
            alerts.send_async("hi", broken, cfg=CFG).join(5)            # must not raise



class TestServer(unittest.TestCase):
    """The real Flask app on a temporary database."""

    def setUp(self):
        import db
        import app as app_module
        self.db, self.app = db, app_module
        self.tmp = tempfile.mkdtemp()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.init_db()
        conn = db.get_conn()
        conn.execute("DELETE FROM timetable")                  # no class unless a test adds one
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()
        # Hermetic: never read the real alert_config.json, never call the real Twilio API
        self.patches = [mock.patch.object(alerts, "CONFIG_PATH", os.path.join(self.tmp, "no_config.json")),
                        mock.patch.object(alerts, "_client", side_effect=AssertionError("real Twilio call in a test"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.DB_PATH = self.old_path

    def sms_rows(self):
        conn = self.db.get_conn()
        rows = conn.execute("SELECT kind, episode, status, sid, info FROM sms_log ORDER BY id").fetchall()
        conn.close()
        return rows

    def add_class_now(self):
        n = datetime.now()
        m = n.hour * 60 + n.minute
        conn = self.db.get_conn()
        conn.execute("INSERT INTO timetable (room_id, day, start_min, end_min, subject) "
                     "VALUES (1, ?, ?, ?, 'TEST')", (n.weekday(), max(m - 1, 0), min(m + 30, 1440)))
        conn.commit()
        conn.close()

    def test_class_motion_hot_room(self):
        self.add_class_now()
        self.client.post("/telemetry", json={"pir": 1, "ldr": 500, "temp": 29.5, "hum": 50})
        cmd = self.client.get("/command").get_json()
        self.assertTrue(cmd["light"])
        self.assertTrue(cmd["fan"])
        self.assertEqual(cmd["fan_reason"], "fan on: 29.5 C >= 28 C")

    def test_class_motion_cool_room_fan_off(self):
        self.add_class_now()
        self.client.post("/telemetry", json={"pir": 1, "ldr": 500, "temp": 24.0, "hum": 50})
        cmd = self.client.get("/command").get_json()
        self.assertTrue(cmd["light"])
        self.assertFalse(cmd["fan"])
        self.assertEqual(cmd["fan_reason"], "fan off: 24.0 C is below 28 C")

    def test_hysteresis_kept_between_requests(self):
        self.add_class_now()
        self.client.post("/telemetry", json={"pir": 1, "temp": 28.0})
        self.assertTrue(self.client.get("/command").get_json()["fan"])
        self.client.post("/telemetry", json={"pir": 1, "temp": 27.5})
        self.assertTrue(self.client.get("/command").get_json()["fan"])     # stays on
        self.client.post("/telemetry", json={"pir": 1, "temp": 26.5})
        self.assertFalse(self.client.get("/command").get_json()["fan"])

    def test_dht_missing_from_the_start(self):
        self.add_class_now()
        self.client.post("/telemetry", json={"pir": 1, "temp": None})
        cmd = self.client.get("/command").get_json()
        self.assertFalse(cmd["fan"])
        self.assertIn("no valid temperature", cmd["fan_reason"])

    def test_daylight_light_off_fan_on(self):
        self.add_class_now()
        self.client.post("/telemetry", json={"pir": 1, "ldr": 3500, "temp": 30})
        cmd = self.client.get("/command").get_json()
        self.assertFalse(cmd["light"])
        self.assertTrue(cmd["fan"])
        self.assertIn("enough daylight", cmd["light_reason"])

    def force_alert_condition(self):
        """Manual light ON (no class) that started 21 minutes ago."""
        self.client.post("/api/override", json={"device": "light", "mode": "on"})
        conn = self.db.get_conn()
        conn.execute("UPDATE device_state SET alert_on_since = ?, alert_armed = 1", (time.time() - 21 * 60,))
        conn.commit()
        conn.close()

    def test_alert_sent_once_per_episode(self):
        self.force_alert_condition()
        with mock.patch.object(self.app.alerts, "send_async") as send:
            for _ in range(3):
                self.assertTrue(self.client.get("/command").get_json()["light"])
        self.assertEqual(send.call_count, 1)
        self.assertIn("Please switch off the lights and fans", send.call_args[0][0])

    def test_alert_failure_does_not_affect_control(self):
        self.force_alert_condition()
        with mock.patch.object(self.app.alerts, "send_async", side_effect=RuntimeError("boom")):
            resp = self.client.get("/command")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["light"])

    def test_polling_never_duplicates(self):
        """The ESP32 polls /command every 2 s and the dashboard polls /api/status:
        neither may send a second SMS for the same episode."""
        self.force_alert_condition()
        with mock.patch.object(self.app.alerts, "send_async") as send:
            for _ in range(10):
                self.client.get("/command")
                self.client.get("/api/status")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.sms_rows()), 1)

    def test_same_episode_cannot_be_sent_twice(self):
        """Even if the alert state were re-armed by mistake, the sms_log unique
        index blocks a second SMS for the same episode."""
        self.force_alert_condition()
        with mock.patch.object(self.app.alerts, "send_async") as send:
            self.client.get("/command")
            conn = self.db.get_conn()
            ep = float(conn.execute("SELECT episode FROM sms_log").fetchone()[0])
            conn.execute("UPDATE device_state SET alert_on_since = ?, alert_armed = 1", (ep,))
            conn.commit()
            conn.close()
            self.client.get("/command")
        self.assertEqual(send.call_count, 1)

    def test_new_episode_after_light_off_sends_again(self):
        self.force_alert_condition()
        with mock.patch.object(self.app.alerts, "send_async") as send:
            self.client.get("/command")
            self.client.post("/api/override", json={"device": "light", "mode": "auto"})   # light OFF
            self.client.get("/command")
            self.force_alert_condition()                                                # new episode
            self.client.get("/command")
        self.assertEqual(send.call_count, 2)

    def test_alert_result_and_status_are_recorded(self):
        self.force_alert_condition()
        fake = fake_client(fetch=[msg("sent"), msg("delivered")])
        with mock.patch.object(alerts, "CONFIG_PATH", os.path.join(self.tmp, "cfg.json")), \
             mock.patch.object(alerts, "_client", return_value=fake), \
             mock.patch.object(alerts, "STATUS_CHECKS_S", (0, 0)), mock.patch("alerts.time.sleep"), \
             mock.patch("threading.Thread.start", lambda t: t.run()):           # run the thread inline
            with open(os.path.join(self.tmp, "cfg.json"), "w") as f:
                json.dump(CFG, f)
            self.client.get("/command")
        rows = self.sms_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "delivered")
        self.assertTrue(rows[0]["sid"].startswith("SM"))
        last = self.client.get("/api/status").get_json()["alert"]["last"]
        self.assertEqual(last["status"], "delivered")
        self.assertNotEqual(last["sid"], rows[0]["sid"])                    # shortened for the browser

    def test_test_alert_not_configured(self):
        resp = self.client.post("/api/test-alert")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("SMS_TO is missing", resp.get_json()["error"])

    def test_test_alert_queued_and_rate_limited(self):
        with mock.patch.object(self.app.alerts, "config_problems", return_value=[]), \
             mock.patch.object(self.app.alerts, "send_async") as send:
            first = self.client.post("/api/test-alert")
            second = self.client.post("/api/test-alert")
        self.assertTrue(first.get_json()["queued"])
        self.assertEqual(second.status_code, 429)
        self.assertEqual(send.call_count, 1)

    def test_no_secrets_in_api_responses(self):
        with mock.patch.dict(os.environ, {k: str(v) for k, v in CFG.items()}):
            body = self.client.get("/api/status").get_data(as_text=True)
            body += self.client.get("/api/sms-log").get_data(as_text=True)
        self.assertNotIn(SECRET, body)
        self.assertNotIn(CFG["TWILIO_ACCOUNT_SID"], body)
        self.assertNotIn("9876543210", body)
        self.assertIn("+91", body)                       # the masked number is shown

    def test_voltage_is_stored_and_shown(self):
        self.client.post("/telemetry", json={"pir": 0, "voltage_v": 231.4, "voltage_mv": 503.0, "current_a": 0.26})
        latest = self.client.get("/api/status").get_json()["latest"]
        self.assertEqual((latest["voltage_v"], latest["voltage_mv"], latest["current_a"]), (231.4, 503.0, 0.26))

    def test_old_firmware_without_voltage_still_works(self):
        self.assertEqual(self.client.post("/telemetry", json={"pir": 0}).status_code, 200)
        self.assertIsNone(self.client.get("/api/status").get_json()["latest"]["voltage_v"])

    def test_status_has_new_fields(self):
        self.client.post("/telemetry", json={"pir": 0, "ldr": 900, "temp": 25})
        self.client.get("/command")
        s = self.client.get("/api/status").get_json()
        for key in ("light_reason", "fan_reason"):
            self.assertIn(key, s["command"])
        self.assertEqual(s["fan_rule"]["on_c"], 28.0)
        self.assertEqual(s["daylight"]["level"], 900)
        self.assertIn("configured", s["alert"])


if __name__ == "__main__":
    unittest.main()
