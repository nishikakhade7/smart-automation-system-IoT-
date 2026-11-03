# alerts.py - SMS alerts to the watchman's mobile phone via Twilio (official SDK).
#
# Settings: SMS_PROVIDER ("twilio"), SMS_TO, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
# TWILIO_FROM. They come from backend/alert_config.json (git-ignored, see
# alert_config.example.json); environment variables with the same names win.
#
# Rules:
# - Secrets are never logged, printed or returned. Error texts are our own,
#   with phone numbers masked.
# - send_sms() never raises. It returns a result dict (ok, sid, status, info, error_code).
# - "ok" means Twilio ACCEPTED the message (status queued/accepted). Whether it was
#   really delivered is learned later with fetch_status() (sent/delivered/
#   undelivered/failed) - send_async() does that automatically for about a minute.
# - Only HTTP 429 (rate limit) is retried: then Twilio has certainly not accepted
#   the message, so a retry cannot create a duplicate SMS.

import json
import logging
import os
import re
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_config.json")
KEYS = ["SMS_PROVIDER", "SMS_TO", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
        "TWILIO_TRIAL_TEMPLATE"]

# Twilio TRIAL accounts cannot send our own text (error 572006); they may only send
# one of these predefined templates, by putting its name in the message body.
# Set TWILIO_TRIAL_TEMPLATE (optional) to one of them for a demo on a trial account;
# leave it out after upgrading so the real alert text is sent.
TRIAL_TEMPLATES = ["sms_2fa", "sms_appointment_reminders", "sms_order_confirmation",
                   "sms_delivery_updates", "sms_customer_support", "sms_marketing_promotions",
                   "sms_event_notifications", "sms_account_alerts", "sms_feedback_surveys",
                   "sms_internal_alerts"]
TIMEOUT_S = 10                        # per HTTP request to Twilio
RETRY_429 = 2                         # extra attempts after a rate-limit answer
STATUS_CHECKS_S = (5, 15, 30, 60)     # after sending: look up the delivery status at these times
FINAL_STATUSES = {"delivered", "undelivered", "failed", "canceled", "read"}

log = logging.getLogger("alerts")
# The Twilio SDK logs every HTTP request at INFO level; keep that out of our logs.
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)

# Twilio error codes -> what to do (https://www.twilio.com/docs/api/errors)
ERRORS = {
    20003: "Twilio rejected the Account SID / Auth Token - check them in the Twilio Console",
    20005: "the Twilio account is not active (suspended or closed)",
    20008: "this action is not available on a Twilio trial account",
    20404: "Twilio could not find the account or message (check TWILIO_ACCOUNT_SID)",
    20429: "too many requests - Twilio rate limit, try again later",
    21211: "SMS_TO is not a valid phone number",
    21212: "TWILIO_FROM is not a valid sender number",
    21219: "trial account: the recipient number is not verified",
    21408: "SMS to this country is not enabled - turn on India in Twilio Console > "
           "Messaging > Settings > Geo permissions",
    21606: "TWILIO_FROM is not an SMS-capable number of this Twilio account",
    21608: "trial account: the recipient number is not verified - verify it under "
           "Phone Numbers > Verified Caller IDs, or upgrade the account",
    21610: "the recipient replied STOP and has blocked messages from this sender",
    21612: "Twilio cannot send from this sender to this recipient",
    21614: "SMS_TO is not a mobile number",
    21659: "TWILIO_FROM is not a number of this Twilio account",
    30003: "undelivered: the phone is switched off or unreachable",
    30004: "undelivered: the message was blocked (recipient or carrier)",
    30005: "undelivered: unknown or inactive recipient number",
    30006: "undelivered: landline or the carrier cannot be reached",
    30007: "undelivered: filtered by the carrier (spam filter / content rules)",
    30008: "undelivered: unknown delivery error from the carrier",
    30044: "trial account: the message is too long for a trial account",
    572005: "trial account: the recipient number has not finished verification",
    572006: "trial account: only Twilio's predefined templates can be sent, not our own alert "
            "text - upgrade the Twilio account (add billing) to send custom messages",
}


# ---------- configuration ----------

def load_config():
    """alert_config.json, then environment variables on top."""
    cfg = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("alert_config.json could not be read (%s)", type(e).__name__)
    for key in KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def _filled(value):
    """A real value: not empty and not a template placeholder."""
    v = str(value or "").strip().lower()
    return bool(v) and "xxxx" not in v and not v.startswith(("paste-", "your_", "your-"))


def normalize_e164(number):
    """'+91-93261 61038' -> '+919326161038'. Raises ValueError if it is not a valid
    E.164 number. Indian (+91) numbers must have 10 digits starting with 6-9."""
    s = str(number or "").strip()
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        raise ValueError("must start with + and the country code, e.g. +91...")
    digits = re.sub(r"[\s\-().]", "", s[1:])
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise ValueError("is not a valid international (E.164) number")
    if digits.startswith("91") and not re.fullmatch(r"91[6-9]\d{9}", digits):
        raise ValueError("is not a valid Indian mobile number (+91 and 10 digits starting 6-9)")
    return "+" + digits


def config_problems(cfg=None):
    """List of what is missing or wrong (never contains secret values)."""
    cfg = load_config() if cfg is None else cfg
    problems = []
    if str(cfg.get("SMS_PROVIDER", "")).lower() != "twilio":
        problems.append('SMS_PROVIDER must be "twilio"')
    for key in ("SMS_TO", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        if not _filled(cfg.get(key)):
            problems.append("%s is missing" % key)
    template = trial_template(cfg)
    if template and template not in TRIAL_TEMPLATES:
        problems.append("TWILIO_TRIAL_TEMPLATE must be one of: " + ", ".join(TRIAL_TEMPLATES))
    sid = str(cfg.get("TWILIO_ACCOUNT_SID", ""))
    if _filled(sid) and not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
        problems.append("TWILIO_ACCOUNT_SID must start with AC and have 34 characters")
    for key in ("SMS_TO", "TWILIO_FROM"):
        if _filled(cfg.get(key)):
            try:
                normalize_e164(cfg[key])
            except ValueError as e:
                problems.append("%s %s" % (key, e))
    return problems


def is_configured(cfg=None):
    return not config_problems(cfg)


def trial_template(cfg=None):
    """The predefined Twilio trial template to send instead of our text, or None."""
    cfg = load_config() if cfg is None else cfg
    value = str(cfg.get("TWILIO_TRIAL_TEMPLATE") or "").strip()
    return value if _filled(value) else None


# ---------- helpers ----------

def mask_number(number):
    """+919326161038 -> +91******1038 (for logs and the dashboard)."""
    s = str(number or "")
    return s[:3] + "*" * max(len(s) - 7, 0) + s[-4:] if len(s) > 7 else "****"


def _clean(text, cfg):
    """Remove secrets and long digit runs (phone numbers) from a provider message."""
    text = str(text or "")
    for key in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID"):
        if cfg.get(key):
            text = text.replace(str(cfg[key]), "***")
    return re.sub(r"\+?\d{6,}", lambda m: mask_number(m.group(0)), text)[:200]


def explain(code, fallback="", cfg=None):
    if code in ERRORS:
        return "%s (Twilio error %s)" % (ERRORS[code], code)
    return "Twilio error %s%s" % (code, (": " + _clean(fallback, cfg or {})) if fallback else "")


def _client(cfg):
    """The Twilio REST client with a request timeout (tests replace this function)."""
    from twilio.http.http_client import TwilioHttpClient
    from twilio.rest import Client
    return Client(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"],
                  http_client=TwilioHttpClient(timeout=TIMEOUT_S))


def _result(ok, info, sid=None, status=None, error_code=None):
    return {"ok": ok, "info": info, "sid": sid, "status": status, "error_code": error_code}


def _network_error(e):
    """Requests' timeout / connection errors, without importing requests here."""
    name = type(e).__name__
    if "Timeout" in name:
        return "Twilio did not answer within %d s (timeout)" % TIMEOUT_S
    if "Connection" in name or isinstance(e, OSError):
        return "no connection to Twilio (is the laptop online?)"
    return None


# ---------- sending ----------

def send_sms(text, cfg=None):
    """Submit one SMS to Twilio. Never raises. ok=True means Twilio accepted it
    (status queued); it does NOT mean delivered - see fetch_status()."""
    cfg = load_config() if cfg is None else cfg
    problems = config_problems(cfg)
    if problems:
        return _result(False, "SMS not configured: " + "; ".join(problems))
    try:
        to, sender = normalize_e164(cfg["SMS_TO"]), normalize_e164(cfg["TWILIO_FROM"])
        from twilio.base.exceptions import TwilioRestException
    except Exception as e:
        return _result(False, "SMS setup problem (%s)" % type(e).__name__)

    template = trial_template(cfg)
    body = template or text                          # trial: the template name IS the body
    for attempt in range(RETRY_429 + 1):
        try:
            msg = _client(cfg).messages.create(to=to, from_=sender, body=body)
            info = "accepted by Twilio, status %s (not yet delivered)" % msg.status
            if template:
                info += " - sent as Twilio trial template '%s', not our own text" % template
            log.info("SMS to %s: %s", mask_number(to), info)
            return _result(True, info, sid=msg.sid, status=msg.status, error_code=msg.error_code)
        except TwilioRestException as e:
            if (e.status == 429 or e.code == 20429) and attempt < RETRY_429:
                time.sleep(2 ** attempt)                  # 1 s, 2 s - not accepted, safe to retry
                continue
            info = explain(e.code, getattr(e, "msg", ""), cfg) if e.code else \
                "Twilio refused the request (HTTP %s)" % e.status
            log.warning("SMS to %s refused: %s", mask_number(to), info)
            return _result(False, info, status="failed", error_code=e.code)
        except Exception as e:
            info = _network_error(e) or "SMS failed (%s)" % type(e).__name__
            log.warning("SMS to %s not sent: %s", mask_number(to), info)
            return _result(False, info, error_code=None)
    return _result(False, "Twilio rate limit - gave up after %d attempts" % (RETRY_429 + 1))


def fetch_status(sid, cfg=None):
    """Current delivery status of a sent message: (status, error_code, info). Never raises."""
    cfg = load_config() if cfg is None else cfg
    try:
        msg = _client(cfg).messages(sid).fetch()
    except Exception as e:
        return None, None, _network_error(e) or "could not read the status (%s)" % type(e).__name__
    if msg.status == "delivered":
        info = "delivered to the phone"
        if trial_template(cfg):
            info += " (Twilio trial template '%s')" % trial_template(cfg)
    elif msg.status in ("undelivered", "failed"):
        info = explain(msg.error_code, msg.error_message, cfg) if msg.error_code else msg.status
    else:
        info = "%s - waiting for the delivery report" % msg.status
    return msg.status, msg.error_code, info


def send_async(text, on_update=None, cfg=None):
    """Send in a background thread (control logic never waits), then follow the
    delivery status for about a minute. on_update(result) is called after the
    submission and after every status change; its errors are logged, not raised."""
    def report(result):
        if on_update is not None:
            try:
                on_update(result)
            except Exception as e:
                log.warning("could not record the SMS result (%s)", type(e).__name__)

    def run():
        result = send_sms(text, cfg)
        report(result)
        if not result["ok"] or not result["sid"]:
            return
        waited, last = 0, result["status"]
        for at in STATUS_CHECKS_S:
            time.sleep(at - waited)
            waited = at
            status, code, info = fetch_status(result["sid"], cfg)
            if status and status != last:
                last = status
                report(_result(status not in ("undelivered", "failed"), info,
                               sid=result["sid"], status=status, error_code=code))
            if status in FINAL_STATUSES:
                return

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
