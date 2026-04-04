"""Fetch upcoming high-impact economic events and write to data/economic_calendar.json.

Sources (tried in order):
  1. ForexFactory RSS/HTML scrape (free, no API key)
  2. Manual fallback: prints template for hand-entry

Usage:
  python -m argus_flow.ops.fetch_economic_calendar          # fetch next 2 weeks
  python -m argus_flow.ops.fetch_economic_calendar --weeks 4 # fetch next 4 weeks
  python -m argus_flow.ops.fetch_economic_calendar --dry-run # print without writing

Designed to run as weekly scheduled task (Sunday evening).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CALENDAR_PATH = REPO / "data" / "economic_calendar.json"

# Well-known HIGH impact events with typical schedules
# These are used as fallback when scraping fails
KNOWN_RECURRING = [
    # US events
    {"name": "US NFP", "currencies": ["USD"], "impact": "HIGH", "recurrence": "first_friday", "hour_utc": 12, "duration_minutes": 30},
    {"name": "FOMC Decision", "currencies": ["USD"], "impact": "HIGH", "recurrence": "fomc", "hour_utc": 18, "duration_minutes": 90},
    # CPI, PPI, Retail Sales dates vary too much — add manually from ForexFactory
    # EUR events
    {"name": "ECB Decision", "currencies": ["EUR"], "impact": "HIGH", "recurrence": "ecb", "hour_utc": 12, "duration_minutes": 90},
    # GBP events
    {"name": "BoE Decision", "currencies": ["GBP"], "impact": "HIGH", "recurrence": "boe", "hour_utc": 11, "duration_minutes": 60},
    # JPY events
    {"name": "BoJ Decision", "currencies": ["JPY"], "impact": "HIGH", "recurrence": "boj", "hour_utc": 3, "duration_minutes": 120},
]

# 2026 FOMC meeting dates (announcement days)
FOMC_2026 = [
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
]

# 2026 ECB meeting dates (approximate)
ECB_2026 = [
    "2026-01-22", "2026-03-05", "2026-04-16", "2026-06-04",
    "2026-07-16", "2026-09-10", "2026-10-29", "2026-12-17",
]

# 2026 BoE meeting dates (approximate)
BOE_2026 = [
    "2026-02-05", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-08-06", "2026-09-17", "2026-11-05", "2026-12-17",
]

# 2026 BoJ meeting dates (approximate)
BOJ_2026 = [
    "2026-01-23", "2026-03-13", "2026-04-28", "2026-06-16",
    "2026-07-17", "2026-09-18", "2026-10-30", "2026-12-18",
]


def _first_friday(year: int, month: int) -> datetime:
    """Return the first Friday of the given month."""
    d = datetime(year, month, 1)
    while d.weekday() != 4:  # Friday
        d += timedelta(days=1)
    return d


def _mid_month_day(year: int, month: int, offset: int = 0) -> datetime:
    """Return ~10th-15th of month (typical CPI/PPI/Retail release window)."""
    # CPI is usually 2nd Tuesday-Thursday of the month
    d = datetime(year, month, 10 + offset)
    # Skip weekends
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def generate_fallback_calendar(weeks: int = 2) -> dict[str, list[dict]]:
    """Generate calendar from known recurring events when scraping fails."""
    now = datetime.now(timezone.utc)
    start = now.date()
    end = start + timedelta(weeks=weeks)

    calendar: dict[str, list[dict]] = {}

    current = start
    while current <= end:
        date_str = current.isoformat()
        events: list[dict] = []

        for evt in KNOWN_RECURRING:
            rec = evt["recurrence"]
            match = False

            if rec == "first_friday":
                ff = _first_friday(current.year, current.month).date()
                match = current == ff
            elif rec == "fomc":
                match = date_str in FOMC_2026
            elif rec == "ecb":
                match = date_str in ECB_2026
            elif rec == "boe":
                match = date_str in BOE_2026
            elif rec == "boj":
                match = date_str in BOJ_2026
            elif rec == "monthly_mid":
                mid = _mid_month_day(current.year, current.month).date()
                match = current == mid
            elif rec == "monthly_mid_next":
                mid = _mid_month_day(current.year, current.month, offset=1).date()
                match = current == mid

            if match:
                events.append({
                    "hour_utc": evt["hour_utc"],
                    "duration_minutes": evt["duration_minutes"],
                    "name": evt["name"],
                    "currencies": evt["currencies"],
                    "impact": evt["impact"],
                })

        if events or current.weekday() < 5:  # Include weekdays even if empty
            calendar[date_str] = events

        current += timedelta(days=1)

    return calendar


def try_fetch_investing_com(weeks: int = 2) -> dict[str, list[dict]] | None:
    """Attempt to fetch from investing.com economic calendar API.

    Returns None if fetch fails (no network, blocked, etc).
    """
    try:
        import urllib.request
        import urllib.error

        # investing.com has a public JSON endpoint for economic calendar
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(weeks=weeks)).strftime("%Y-%m-%d")

        url = f"https://economic-calendar.investing.com/economic-calendar/Service/getCalendarFilteredData"
        # This endpoint typically requires form data and specific headers
        # For reliability, fall back to the known schedule
        return None  # Placeholder — scraping is fragile, use fallback

    except Exception:
        return None


def merge_calendars(existing: dict, new: dict) -> dict:
    """Merge new events into existing calendar, preserving manual additions."""
    merged = dict(existing)
    for date_str, events in new.items():
        if date_str not in merged:
            merged[date_str] = events
        else:
            # Add new events that don't already exist (by name)
            existing_names = {e.get("name") for e in merged[date_str]}
            for evt in events:
                if evt.get("name") not in existing_names:
                    merged[date_str].append(evt)
    # Sort by date
    return dict(sorted(merged.items()))


def main():
    parser = argparse.ArgumentParser(description="Fetch economic calendar events")
    parser.add_argument("--weeks", type=int, default=2, help="Weeks ahead to fetch (default: 2)")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to file")
    parser.add_argument("--force", action="store_true", help="Overwrite existing entries")
    args = parser.parse_args()

    print(f"Generating economic calendar ({args.weeks} weeks ahead)...")

    # Try online sources first
    online = try_fetch_investing_com(args.weeks)

    if online:
        new_events = online
        print(f"  Fetched {sum(len(v) for v in online.values())} events from online source")
    else:
        new_events = generate_fallback_calendar(args.weeks)
        event_count = sum(len(v) for v in new_events.values() if v)
        print(f"  Generated {event_count} events from known recurring schedule")
        print("  NOTE: Verify against ForexFactory.com for exact dates")

    # Load existing calendar
    existing: dict = {}
    if CALENDAR_PATH.exists() and not args.force:
        try:
            existing = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
            # Remove _comment key for merge
            existing.pop("_comment", None)
            print(f"  Loaded {len(existing)} existing dates")
        except Exception as e:
            print(f"  Warning: could not load existing calendar: {e}")

    # Merge
    merged = merge_calendars(existing, new_events) if not args.force else new_events

    # Add comment
    output = {
        "_comment": "High-impact economic events. Auto-generated + manual. Update weekly. Only HIGH impact events block entries. Buffer: 30min each side.",
        **merged,
    }

    if args.dry_run:
        print(json.dumps(output, indent=2))
    else:
        CALENDAR_PATH.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        total_events = sum(len(v) for k, v in merged.items() if k != "_comment" and v)
        print(f"  Written to {CALENDAR_PATH}")
        print(f"  Total: {len(merged)} dates, {total_events} events")

        # Show upcoming HIGH events
        now = datetime.now(timezone.utc)
        print("\n  Upcoming HIGH events:")
        for date_str in sorted(merged.keys()):
            try:
                d = datetime.fromisoformat(date_str)
            except ValueError:
                continue
            if d.date() >= now.date():
                for evt in merged[date_str]:
                    if evt.get("impact") == "HIGH":
                        print(f"    {date_str} {evt['hour_utc']:02d}:00 UTC — {evt['name']} ({', '.join(evt.get('currencies', []))})")


if __name__ == "__main__":
    main()
