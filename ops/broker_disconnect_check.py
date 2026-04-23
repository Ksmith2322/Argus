"""ops/broker_disconnect_check.py — Detect prolonged IBKR disconnects.

When TWS dies or the API port closes, the argus_flow runner logs
"Reconnect attempt N/200 in 120s..." and enters a retry loop. The runner
stays alive (process-liveness check passes), but no trades can happen.
Standard silent-block doesn't catch this because during non-session hours
it's OK for signals to be zero.

This check parses the argus_flow runner log for recent reconnect patterns.
If we've been in "reconnecting" state for > N minutes during market hours,
alert.

Run:
    python -m ops.broker_disconnect_check

Writes: argus_flow/logs/broker_disconnect_alert.json
Discord-alerted via shared _alert_helper with 30-min cooldown.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"
RUNNER_LOG = LOGS_DIR / "runner_unified.log"
ALERT_THRESHOLD_MIN = 5  # alert if disconnected > 5 min during market hours
COOLDOWN_MIN = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] broker_disconnect | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("broker_disconnect_check")

# Regex patterns for log lines indicating disconnect / reconnect attempts
RECONNECT_PAT = re.compile(r"Reconnect attempt \d+/\d+")
CONN_LOST_PAT = re.compile(r"Connection lost|API connection failed|Peer closed connection")
CONN_OK_PAT = re.compile(r"New session after reconnect|\[INFO\] unified \| Starting main loop")
TS_PAT = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")


def _is_market_hours(now: datetime) -> bool:
    """Loose check: weekdays 13:00-21:00 UTC (covers both DST and STD for US
    cash session). Weekends: FX is technically open Sun 21Z - Fri 21Z but
    treating weekends as 'off' avoids false alerts during known low-activity
    windows."""
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return 13 <= now.hour < 22


def analyze_recent_log(tail_lines: int = 200) -> dict:
    """Parse the last N lines of runner_unified.log, return disconnect status."""
    result = {
        "is_disconnected": False,
        "last_disconnect_ts": None,
        "last_reconnect_ok_ts": None,
        "recent_reconnect_attempts": 0,
        "minutes_since_disconnect": None,
        "reason": "",
    }
    if not RUNNER_LOG.exists():
        result["reason"] = "runner_unified.log missing"
        return result
    try:
        with RUNNER_LOG.open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-tail_lines:]
    except Exception as e:
        result["reason"] = f"read failed: {e}"
        return result

    last_disconnect_ts: datetime | None = None
    last_reconnect_ok_ts: datetime | None = None
    reconnect_attempts = 0

    for line in lines:
        m = TS_PAT.search(line)
        if not m:
            continue
        try:
            # Log timestamps use local time mislabeled as Z (known quirk).
            # Treat as naive UTC for recency checks — still OK because all
            # operations compare ages, not absolute times.
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if CONN_LOST_PAT.search(line):
            last_disconnect_ts = ts
        elif RECONNECT_PAT.search(line):
            reconnect_attempts += 1
        elif CONN_OK_PAT.search(line):
            last_reconnect_ok_ts = ts

    result["last_disconnect_ts"] = last_disconnect_ts.isoformat() if last_disconnect_ts else None
    result["last_reconnect_ok_ts"] = last_reconnect_ok_ts.isoformat() if last_reconnect_ok_ts else None
    result["recent_reconnect_attempts"] = reconnect_attempts

    # Disconnected iff: we saw a disconnect AND we haven't seen a successful reconnect since
    if last_disconnect_ts is None:
        result["reason"] = "no recent disconnect events"
        return result

    if last_reconnect_ok_ts and last_reconnect_ok_ts > last_disconnect_ts:
        result["reason"] = "reconnect observed after last disconnect"
        return result

    # Still disconnected — compute duration
    now = datetime.now(timezone.utc)
    # Since log timestamps are mislabeled local-as-UTC, compare naive values
    disco_naive = last_disconnect_ts.replace(tzinfo=None)
    now_naive = datetime.now()
    minutes = (now_naive - disco_naive).total_seconds() / 60
    result["minutes_since_disconnect"] = round(minutes, 1)
    result["is_disconnected"] = minutes > ALERT_THRESHOLD_MIN
    result["reason"] = (f"disconnected for {minutes:.1f} min "
                        f"({reconnect_attempts} reconnect attempts in tail)")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    status = analyze_recent_log()
    status["checked_at"] = datetime.now(timezone.utc).isoformat()
    status["market_hours"] = _is_market_hours(datetime.now(timezone.utc))

    out_path = LOGS_DIR / "broker_disconnect_alert.json"
    out_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    if status["is_disconnected"] and status["market_hours"]:
        log.warning("BROKER DISCONNECTED: %s", status["reason"])
        # Discord with cooldown
        try:
            sys.path.insert(0, str(REPO / "ops"))
            from _alert_helper import post_discord, load_cooldown_state, save_cooldown_state, should_alert, mark_alerted
            cooldown = load_cooldown_state("broker_disconnect_alert_state.json")
            if should_alert(cooldown, "broker_disconnect", COOLDOWN_MIN):
                body = (f"IBKR connection lost {status['minutes_since_disconnect']} min ago. "
                        f"{status['recent_reconnect_attempts']} reconnect attempts since. "
                        f"Check TWS API port 7497.")
                post_discord("BROKER DISCONNECT", body, color=16711680)
                mark_alerted(cooldown, "broker_disconnect", status["reason"])
                save_cooldown_state("broker_disconnect_alert_state.json", cooldown)
        except Exception as e:
            log.warning("alert path failed: %s", e)
    elif status["is_disconnected"]:
        log.info("disconnected but outside market hours — no alert")
    else:
        log.info("broker connection OK (%s)", status["reason"])
        # Clear cooldown on recovery
        try:
            sys.path.insert(0, str(REPO / "ops"))
            from _alert_helper import load_cooldown_state, save_cooldown_state, post_discord
            cooldown = load_cooldown_state("broker_disconnect_alert_state.json")
            if cooldown:
                post_discord("BROKER RECONNECTED", "IBKR connection restored.", color=65280)
                save_cooldown_state("broker_disconnect_alert_state.json", {})
        except Exception:
            pass

    if args.verbose:
        log.info("full status: %s", json.dumps(status, indent=2))

    return 1 if status["is_disconnected"] and status["market_hours"] else 0


if __name__ == "__main__":
    sys.exit(main())
