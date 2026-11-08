"""Delayed NQ POC alerts, using native Yahoo 15-minute/hourly OHLCV and Gmail SMTP.

Run once from a scheduler. The state directory must survive across runs.
No trading/account endpoints are used. Yahoo's public chart endpoint is not
a guaranteed service; bar confirmation uses a configurable delay heuristic.
"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from volume_profile import Bar, profile, crossing

INTERVAL = 900
TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600}
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("nq")


def interval_seconds(cfg):
    timeframe = cfg.get("timeframe", "15m")
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported candle timeframe")
    return TIMEFRAME_SECONDS[timeframe]


def timeframe_label(cfg):
    return "1H" if cfg.get("timeframe", "15m") == "1h" else "15 min"


def nq_session_open(stamp):
    """Regular weekly CME equity-index hours; not a holiday calendar."""
    local = datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    weekday, hour = local.weekday(), local.hour
    if weekday == 5:  # Saturday.
        return False
    if weekday == 6:  # Sunday evening reopening.
        return hour >= 18
    if weekday == 4:  # Friday weekly shutdown.
        return hour < 17
    return hour < 17 or hour >= 18  # Daily 17:00-18:00 maintenance.


def valid_bar_timestamp(stamp, timeframe="15m", symbol="NQ=F"):
    """Accept complete grid intervals, applying NQ's regular weekly session."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported candle timeframe")
    duration = TIMEFRAME_SECONDS[timeframe]
    if type(stamp) is not int or stamp % duration:
        return False
    if symbol != "NQ=F":
        return True
    # The close itself may be exactly 17:00; every instant in [open, close)
    # must belong to the regular session. Supported bars are at most one hour.
    return nq_session_open(stamp) and nq_session_open(stamp + duration - 1)


def notification_count(cfg):
    count = cfg.get("notification_repeats", 1)
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("config.json: notification_repeats must be an integer from 1 to 3")
    return count


def read_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(cfg.get("symbol"), str) or not cfg["symbol"]:
        raise ValueError("config.json: symbol is required")
    for key, lo, hi in (("bars", 1, 500), ("rows", 5, 100),
                        ("assumed_delay_seconds", 0, 3600),
                        ("finalization_buffer_seconds", 0, 3600),
                        ("maximum_alert_age_seconds", 1800, 86400)):
        value = cfg.get(key)
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"config.json: invalid {key}")
    percent = cfg.get("value_area_percent")
    if type(percent) not in (int, float) or not 0 <= percent <= 100:
        raise ValueError("config.json: invalid value_area_percent")
    for key in ("alert_up", "alert_down"):
        if type(cfg.get(key)) is not bool:
            raise ValueError(f"config.json: invalid {key}")
    timeframes = cfg.get("timeframes", ["15m"])
    if (not isinstance(timeframes, list) or not timeframes or
            any(not isinstance(frame, str) or frame not in TIMEFRAME_SECONDS for frame in timeframes) or
            len(set(timeframes)) != len(timeframes)):
        raise ValueError("config.json: timeframes must contain distinct 15m and/or 1h values")
    cfg["timeframes"] = timeframes
    cfg["notification_repeats"] = notification_count(cfg)
    ZoneInfo(cfg["display_timezone"])
    return cfg


def identity(cfg):
    relevant = {k: cfg[k] for k in ("symbol", "bars", "rows", "value_area_percent", "alert_up", "alert_down")}
    relevant["calculation_version"] = 1
    if cfg.get("timeframe", "15m") != "15m":
        relevant["timeframe"] = cfg["timeframe"]
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


def fetch_chart(symbol, timeframe="15m"):
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported candle timeframe")
    query = urlencode({"range": "1mo", "interval": timeframe, "includePrePost": "true"})
    last_error = None
    # The second hostname is a fallback for service outages, not rate limits.
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}?{query}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.load(response)
            chart = payload.get("chart", {})
            if chart.get("error") or not chart.get("result"):
                raise ValueError("Data provider did not return a chart")
            return chart["result"][0]
        except HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("Yahoo rate limit (429); retry on next scheduled run") from exc
            last_error = exc
        except (URLError, TimeoutError, ValueError) as exc:
            last_error = exc
    raise RuntimeError("Yahoo chart request failed") from last_error


def parse_candles(chart, timeframe="15m", symbol="NQ=F"):
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported candle timeframe")
    expected = ("1h", "60m") if timeframe == "1h" else ("15m",)
    if chart.get("meta", {}).get("dataGranularity") not in expected:
        raise ValueError("Provider returned a different candle interval")
    timestamps = chart.get("timestamp", [])
    quotes = chart.get("indicators", {}).get("quote", [])
    if not timestamps or not quotes:
        raise ValueError("Provider returned no OHLCV candles")
    fields = ("open", "high", "low", "close", "volume")
    q = quotes[0]
    if any(len(q.get(field, [])) != len(timestamps) for field in fields):
        raise ValueError("Provider returned mismatched OHLCV arrays")
    candles = []
    seen = set()
    for i, stamp in enumerate(timestamps):
        if type(stamp) is not int or stamp in seen:
            raise ValueError("Invalid or duplicate candle timestamp")
        seen.add(stamp)
        if not valid_bar_timestamp(stamp, timeframe, symbol):
            continue  # Quote snapshots and closed-session filler are not candles.
        values = [q[field][i] for field in fields]
        if all(value is None for value in values):
            continue  # Provider's explicit no-trading row.
        # Keep invalid partial bars in sequence so no profile bridges past them.
        bar = None
        if all(value is not None for value in values):
            try:
                candidate = Bar(*(float(value) for value in values))
                candidate.validate()
                bar = candidate
            except (ValueError, TypeError):
                pass
        candles.append((stamp, bar))
    return sorted(candles)


def finalized(candles, cfg, now):
    cutoff = now - cfg["assumed_delay_seconds"] - cfg["finalization_buffer_seconds"]
    duration = interval_seconds(cfg)
    return [(stamp, bar) for stamp, bar in candles
            if valid_bar_timestamp(stamp, cfg.get("timeframe", "15m"), cfg.get("symbol", "NQ=F"))
            and stamp + duration <= cutoff]


def poc_at(candles, index, cfg):
    count = cfg["bars"]
    if index < count - 1:
        return None
    bars = [bar for _, bar in candles[index - count + 1:index + 1]]
    if any(bar is None for bar in bars):
        return None
    return profile(bars, cfg["rows"], cfg["value_area_percent"])


def new_state():
    return {"version": 1, "last_bar": None, "pending": [], "contract_hint": None}


def load_state(path):
    if not path.exists():
        return new_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("pending"), list):
        raise ValueError("Invalid state file; preserve it and inspect before resetting")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def repair_invalid_checkpoint(candles, cfg, state):
    """Rebaseline only a proven invalid NQ checkpoint, preserving real outbox events."""
    saved = state["last_bar"]
    if cfg.get("symbol", "NQ=F") != "NQ=F" or saved is None:
        return False
    if type(saved) is not int:
        raise ValueError("Invalid state checkpoint timestamp")
    timeframe = cfg.get("timeframe", "15m")
    if valid_bar_timestamp(saved, timeframe, "NQ=F"):
        return False
    if (len(candles) < cfg["bars"] + 1 or
            any(not valid_bar_timestamp(stamp, timeframe, "NQ=F") for stamp, _ in candles) or
            poc_at(candles, len(candles) - 1, cfg) is None or
            poc_at(candles, len(candles) - 2, cfg) is None):
        raise ValueError("Cannot repair invalid checkpoint without valid finalized profile history")
    original_pending = state["pending"]
    genuine_pending = [event for event in original_pending
                       if valid_bar_timestamp(event.get("bar_open"), timeframe, "NQ=F")]
    state["last_bar"] = candles[-1][0]
    state["pending"] = genuine_pending
    LOG.warning("%s: Repaired invalid NQ checkpoint %s to %s; historical alerts suppressed; "
                "retained %d genuine pending signals; discarded %d invalid-timestamp signals",
                timeframe, saved, state["last_bar"], len(genuine_pending),
                len(original_pending) - len(genuine_pending))
    return True


def process(candles, cfg, now, state, contract_hint=None):
    duration = interval_seconds(cfg)
    if len(candles) < cfg["bars"] + 1:
        raise ValueError("Not enough finalized candles for current and previous POC")
    if repair_invalid_checkpoint(candles, cfg, state):
        if contract_hint:
            state["contract_hint"] = contract_hint
        return
    # Names are merely a best-effort contract-change hint, not a rollover guarantee.
    if contract_hint and state.get("contract_hint") and contract_hint != state["contract_hint"]:
        LOG.warning("Contract description changed; resetting signal baseline")
        state.update(last_bar=None, pending=[])
    if contract_hint:
        state["contract_hint"] = contract_hint
    latest = candles[-1][0]
    if state["last_bar"] is None:
        state["last_bar"] = latest
        LOG.info("First run: baseline recorded; historical alerts suppressed")
        return
    if latest < state["last_bar"]:
        raise ValueError("Provider data is older than the saved checkpoint")
    unseen = [(i, stamp) for i, (stamp, _) in enumerate(candles) if stamp > state["last_bar"]]
    if unseen and unseen[0][0] < cfg["bars"]:
        LOG.warning("Data gap exceeds available history; resetting baseline")
        state["last_bar"] = latest
        return
    for index, stamp in unseen:
        current = poc_at(candles, index, cfg)
        previous = poc_at(candles, index - 1, cfg)
        if current is None or previous is None:
            LOG.warning("Skipping candle %s: undefined profile or invalid OHLCV", stamp)
        else:
            direction = crossing(candles[index - 1][1].close, previous["poc"], candles[index][1].close, current["poc"])
            enabled = cfg["alert_up"] if direction == "up" else cfg["alert_down"]
            if direction and enabled:
                if now - (stamp + duration) <= cfg["maximum_alert_age_seconds"]:
                    event_id = f"{identity(cfg)}-{stamp}-{direction}"
                    if not any(item["id"] == event_id for item in state["pending"]):
                        state["pending"].append({"id": event_id, "bar_open": stamp,
                                                 "direction": direction,
                                                 "close": candles[index][1].close,
                                                 "poc": current["poc"],
                                                 "previous_close": candles[index - 1][1].close,
                                                 "previous_poc": previous["poc"],
                                                 "notifications_total": notification_count(cfg),
                                                 "notifications_sent": 0})
                        LOG.info("Queued delayed POC signal: %s", direction)
                else:
                    LOG.info("Old recovery signal skipped")
        state["last_bar"] = stamp


def mail_settings():
    username = os.getenv("GMAIL_USER", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    recipient = os.getenv("ALERT_EMAIL", "").strip() or username
    if not username or not password or "@" not in recipient or "@" not in username:
        raise ValueError("Set GMAIL_USER, GMAIL_APP_PASSWORD and optionally ALERT_EMAIL")
    if any(ch in username + recipient for ch in "\r\n"):
        raise ValueError("Invalid email address")
    return username, password, recipient


def send_mail(subject, body, event_id):
    username, password, recipient = mail_settings()
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = f"<nq-{event_id}@nq-monitor.local>"
    msg.set_content(body)
    for use_ssl in (False, True):
        port = 465 if use_ssl else 587
        submission_started = False
        try:
            LOG.info("Connecting to Gmail SMTP %d using %s", port, "TLS" if use_ssl else "STARTTLS")
            connection = (smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30,
                                          context=ssl.create_default_context()) if use_ssl else
                          smtplib.SMTP("smtp.gmail.com", port, timeout=30))
            with connection as smtp:
                smtp.ehlo()
                if not use_ssl:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                LOG.info("Gmail SMTP %d TLS established; authenticating", port)
                smtp.login(username, password)
                LOG.info("Gmail SMTP %d authenticated; submitting email", port)
                # A disconnect during submission can leave delivery uncertain.
                # Only switch transports before any email has been submitted.
                submission_started = True
                refused = smtp.send_message(msg)
                if refused:
                    raise RuntimeError("SMTP recipient rejected")
            return
        except smtplib.SMTPServerDisconnected:
            if use_ssl or submission_started:
                raise
            LOG.warning("Gmail SMTP 587 disconnected before email submission; trying encrypted SMTP 465 once")


def describe_event(event, cfg, now):
    zone = ZoneInfo(cfg["display_timezone"])
    duration = interval_seconds(cfg)
    label = timeframe_label(cfg)
    closed = datetime.fromtimestamp(event["bar_open"] + duration, timezone.utc).astimezone(zone)
    direction = "Kaina virš POC" if event["direction"] == "up" else "Kaina žemiau POC"
    age = max(0, int((now - event["bar_open"] - duration) / 60))
    subject = f"[VĖLUOJANTYS DUOMENYS] {cfg['symbol']} {label} — {direction}"
    body = (f"{direction}\n\nSimbolis: {cfg['symbol']} (Yahoo Finance)\n"
            f"Žvakės intervalas: {label}\n"
            f"Žvakės užsidarymas: {closed:%Y-%m-%d %H:%M %Z} ({cfg['display_timezone']})\n"
            f"Užsidarymo kaina: {event['close']:.2f}\nPOC: {event['poc']:.4f}\n"
            f"Ankstesnis užsidarymas / POC: {event['previous_close']:.2f} / {event['previous_poc']:.4f}\n"
            f"Praėjo nuo žvakės pabaigos: apie {age} min.\n"
            f"Profilis: {cfg['bars']} žvakių, {cfg['rows']} eilučių, VA {cfg['value_area_percent']} %.\n\n"
            "Tai vėluojančių duomenų signalas. POC perskaičiuojamas, todėl pusės pasikeitimą "
            "gali sukelti ir linijos judėjimas. Yahoo NQ=F duomenys gali skirtis nuo "
            "TradingView pasirinkto kontrakto ir sesijos.\n")
    return subject, body


def notification_payload(subject, body, event_id, number, total):
    """Give each repeated copy stable identity while retaining legacy single emails."""
    if total == 1:
        return subject, body, event_id
    return (f"[{number}/{total}] {subject}",
            f"Pranešimas {number}/{total} apie tą patį signalą.\n\n{body}",
            f"{event_id}-notification-{number}")


def deliver_pending(state, path, cfg, now, sender=send_mail):
    duration = interval_seconds(cfg)
    while state["pending"]:
        event = state["pending"][0]
        if now - event["bar_open"] - duration > cfg["maximum_alert_age_seconds"]:
            LOG.warning("Expired pending signal discarded")
        else:
            # Pending events written before repeats existed must remain one email,
            # regardless of the current configuration. New events snapshot the count.
            total = event.get("notifications_total", 1)
            sent = event.get("notifications_sent", 0)
            if (type(total) is not int or not 1 <= total <= 3 or
                    type(sent) is not int or not 0 <= sent <= total):
                raise ValueError("Invalid pending notification progress")
            subject, body = describe_event(event, cfg, now)
            while sent < total:
                number = sent + 1
                sender(*notification_payload(subject, body, event["id"], number, total))
                event["notifications_total"] = total
                event["notifications_sent"] = number
                save_state(path, state)  # Each accepted copy is durable before the next.
                sent = number
                LOG.info("Signal accepted by SMTP server (%d/%d)", sent, total)
        state["pending"].pop(0)
        save_state(path, state)


def run_timeframe(cfg, args):
    timeframe = cfg.get("timeframe", "15m")
    LOG.info("Starting %s timeframe monitoring (%s)", timeframe, timeframe_label(cfg))
    if not args.dry_run:
        mail_settings()  # Do not start monitoring with incomplete email setup.
    path = Path(args.state_dir) / f"{identity(cfg)}.json"
    state = new_state() if args.reset_state else load_state(path)
    chart = fetch_chart(cfg["symbol"], timeframe)
    now = time.time()
    candles = finalized(parse_candles(chart, timeframe, cfg["symbol"]), cfg, now)
    process(candles, cfg, now, state, chart.get("meta", {}).get("shortName"))
    current = poc_at(candles, len(candles) - 1, cfg)
    latest_time = datetime.fromtimestamp(candles[-1][0] + interval_seconds(cfg), timezone.utc).isoformat()
    LOG.info("%s: Finalized candles: %d; latest close time UTC: %s", timeframe, len(candles), latest_time)
    if current:
        LOG.info("%s: Close: %.2f; POC: %.4f; VA: %.4f .. %.4f", timeframe, candles[-1][1].close,
                 current["poc"], current["val"], current["vah"])
    if args.dry_run:
        LOG.info("%s: Dry run: %d pending signals; nothing sent or saved", timeframe, len(state["pending"]))
        return
    save_state(path, state)  # Durable per-timeframe outbox before attempting SMTP.
    deliver_pending(state, path, cfg, now)


def log_failure(context, exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        LOG.error("%s: Gmail rejected login; check app password and 2-Step Verification", context)
    elif isinstance(exc, smtplib.SMTPException):
        # Record only the exception type and numeric codes, never server text or credentials.
        code = getattr(exc, "smtp_code", None)
        codes = {code} if isinstance(code, int) else set()
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            codes.update(reply[0] for reply in exc.recipients.values() if isinstance(reply[0], int))
        LOG.error("%s: SMTP failed (%s; response codes: %s); pending signal retained for a later retry",
                  context, type(exc).__name__, ",".join(str(code) for code in sorted(codes)) or "not provided")
    elif isinstance(exc, (ValueError, KeyError, IndexError)):
        LOG.error("%s: Setup or data error: %s", context, exc)
    else:
        LOG.error("%s: Service error (%s): %s", context, type(exc).__name__, exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    parser.add_argument("--once", action="store_true", help="One scheduled check (default)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect data; no mail and no state changes")
    parser.add_argument("--test-email", action="store_true", help="Send an explicit test email only")
    parser.add_argument("--reset-state", action="store_true", help="Reset this configuration's baseline; discard pending alerts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        cfg = read_config(args.config)
        if args.test_email:
            if args.dry_run:
                raise ValueError("Do not combine --test-email with --dry-run")
            total = notification_count(cfg)
            event_id = f"test-{time.time_ns()}"
            subject = "[TESTAS] NQ stebėtojas — bandomasis laiškas"
            body = "Laiškų siuntimas veikia. TESTAS: tai bandymas, ne rinkos signalas."
            for number in range(1, total + 1):
                send_mail(*notification_payload(subject, body, event_id, number, total))
                LOG.info("Test email accepted by SMTP server (%d/%d)", number, total)
            return 0
    except (ValueError, KeyError, IndexError, smtplib.SMTPException, OSError, RuntimeError) as exc:
        log_failure("Setup/test email", exc)
        return 1
    failed = False
    for timeframe in cfg["timeframes"]:
        frame_cfg = dict(cfg, timeframe=timeframe)
        try:
            run_timeframe(frame_cfg, args)
        except (ValueError, KeyError, IndexError, smtplib.SMTPException, OSError, RuntimeError) as exc:
            log_failure(timeframe, exc)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
