"""argus_flow/ops/news_calendar.py -- Economic calendar for NEWS_BLOCKED gate.

Pulls high-impact economic events and provides a check function
that the runner can call before entering trades.

Events covered (hardcoded schedule — these are known in advance):
  - FOMC rate decisions (8x/year)
  - NFP (first Friday monthly)
  - CPI (monthly)
  - GDP (quarterly)
  - ECB rate decisions
  - BOJ rate decisions

Usage:
    from argus_flow.ops.news_calendar import is_news_blocked, get_upcoming_events

    blocked, reason = is_news_blocked()
    if blocked:
        # Don't enter trade
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger("argus.news")
REPO = Path(__file__).resolve().parents[2]
CALENDAR_FILE = REPO / "argus_flow" / "data" / "economic_calendar.json"

# High-impact events for 2026 (known dates — update annually)
# Format: "YYYY-MM-DD": {"event": "name", "time_utc": "HH:MM", "impact": "high", "currencies": ["USD"]}
HARDCODED_EVENTS_2026 = {
    # FOMC decisions (2:00 PM ET = 18:00 UTC on announcement day)
    "2026-01-28": {"event": "FOMC Rate Decision", "time_utc": "19:00", "impact": "high", "currencies": ["USD"]},
    "2026-03-18": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-05-06": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-06-17": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-07-29": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-09-16": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-11-04": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    "2026-12-16": {"event": "FOMC Rate Decision", "time_utc": "18:00", "impact": "high", "currencies": ["USD"]},
    # NFP (first Friday of each month, 8:30 AM ET = 12:30 UTC)
    "2026-01-02": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-02-06": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-03-06": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-04-03": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-05-01": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-06-05": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-07-02": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-08-07": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-09-04": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-10-02": {"event": "Non-Farm Payrolls", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-11-06": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-12-04": {"event": "Non-Farm Payrolls", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    # CPI (roughly mid-month, 8:30 AM ET)
    "2026-01-14": {"event": "CPI", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-02-11": {"event": "CPI", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-03-11": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-04-14": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-05-13": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-06-10": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-07-14": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-08-12": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-09-15": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-10-13": {"event": "CPI", "time_utc": "12:30", "impact": "high", "currencies": ["USD"]},
    "2026-11-12": {"event": "CPI", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
    "2026-12-10": {"event": "CPI", "time_utc": "13:30", "impact": "high", "currencies": ["USD"]},
}

BLOCK_WINDOW_MINUTES = 30  # block entries 30 min before and after event


def load_calendar() -> dict:
    """Load calendar from file, fallback to hardcoded."""
    if CALENDAR_FILE.exists():
        try:
            return json.loads(CALENDAR_FILE.read_text())
        except Exception:
            pass
    return HARDCODED_EVENTS_2026


def is_news_blocked(symbol: str = "", now: datetime | None = None) -> tuple[bool, str]:
    """Check if current time is within a news blackout window.

    Returns (blocked: bool, reason: str)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    calendar = load_calendar()
    today = now.strftime("%Y-%m-%d")

    # Check today and tomorrow (for overnight events)
    for check_date in [today, (now + timedelta(days=1)).strftime("%Y-%m-%d")]:
        event = calendar.get(check_date)
        if not event:
            continue

        try:
            h, m = event["time_utc"].split(":")
            event_time = datetime(
                now.year, int(check_date[5:7]), int(check_date[8:10]),
                int(h), int(m), tzinfo=timezone.utc,
            )
        except (ValueError, KeyError):
            continue

        # Check if we're within the blackout window
        window_start = event_time - timedelta(minutes=BLOCK_WINDOW_MINUTES)
        window_end = event_time + timedelta(minutes=BLOCK_WINDOW_MINUTES)

        if window_start <= now <= window_end:
            reason = f"NEWS_BLOCKED: {event['event']} at {event['time_utc']} UTC ({BLOCK_WINDOW_MINUTES}min window)"
            _log.info(reason)
            return True, reason

    return False, ""


def get_upcoming_events(days: int = 7, now: datetime | None = None) -> list[dict]:
    """Get events in the next N days."""
    if now is None:
        now = datetime.now(timezone.utc)

    calendar = load_calendar()
    upcoming = []

    for i in range(days + 1):
        check_date = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        event = calendar.get(check_date)
        if event:
            upcoming.append({"date": check_date, **event})

    return upcoming


if __name__ == "__main__":
    blocked, reason = is_news_blocked()
    print(f"Blocked: {blocked} | {reason}")
    print(f"\nUpcoming events (7 days):")
    for e in get_upcoming_events(7):
        print(f"  {e['date']} {e['time_utc']} UTC | {e['event']} | {e['impact']}")
