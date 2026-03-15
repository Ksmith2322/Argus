#!/usr/bin/env python3
"""Check candle CSV files for time gaps and optionally alert via Discord.

Usage:
    python ops/check_candle_gaps.py                       # check all, alert if gaps > 5min
    python ops/check_candle_gaps.py --no-discord           # print only
    python ops/check_candle_gaps.py --max-gap-minutes 10   # alert threshold = 10min
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA_DIR = REPO / "data"
INTERVAL_S = 60  # 1-minute candles


def _ts_fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def check_file(path: Path, max_gap_minutes: int):
    """Return dict with gap analysis for one CSV file."""
    times = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            times.append(int(row["time"]))

    if len(times) < 2:
        return {"file": path.name, "rows": len(times), "gaps": [], "largest_gap_min": 0}

    gaps = []
    largest = 0
    for i in range(1, len(times)):
        delta = times[i] - times[i - 1]
        if delta > INTERVAL_S:
            gap_min = delta / 60
            gaps.append((times[i - 1], times[i], gap_min))
            if gap_min > largest:
                largest = gap_min

    return {
        "file": path.name,
        "rows": len(times),
        "gaps": gaps,
        "largest_gap_min": largest,
    }


def format_report(results, max_gap_minutes):
    lines = []
    any_alert = False
    for r in results:
        name = r["file"]
        n_gaps = len(r["gaps"])
        largest = r["largest_gap_min"]
        alert_gaps = [g for g in r["gaps"] if g[2] > max_gap_minutes]
        if alert_gaps:
            any_alert = True

        lines.append(f"--- {name} ({r['rows']:,} rows) ---")
        if n_gaps == 0:
            lines.append("  No gaps found.")
        else:
            lines.append(f"  Total gaps: {n_gaps}  |  Largest: {largest:.1f} min")
            if alert_gaps:
                lines.append(f"  Gaps > {max_gap_minutes} min ({len(alert_gaps)}):")
                for start, end, mins in alert_gaps[:20]:
                    lines.append(f"    {_ts_fmt(start)} -> {_ts_fmt(end)}  ({mins:.1f} min)")
                if len(alert_gaps) > 20:
                    lines.append(f"    ... and {len(alert_gaps) - 20} more")
        lines.append("")

    return "\n".join(lines), any_alert


def main():
    args = sys.argv[1:]
    no_discord = "--no-discord" in args
    max_gap_minutes = 5
    if "--max-gap-minutes" in args:
        idx = args.index("--max-gap-minutes")
        max_gap_minutes = int(args[idx + 1])

    csv_files = sorted(DATA_DIR.glob("*_usd_1m.csv"))
    if not csv_files:
        print("No candle files found in", DATA_DIR)
        return

    results = [check_file(p, max_gap_minutes) for p in csv_files]
    report, any_alert = format_report(results, max_gap_minutes)
    print(report)

    if any_alert and not no_discord:
        from ops.notify import send_discord

        header = f"**Candle Gap Alert** (threshold: {max_gap_minutes} min)\n"
        body = report
        if len(header) + len(body) > 1900:
            body = body[:1900 - len(header)] + "\n... (truncated)"
        send_discord(header + "```\n" + body + "```")
        print("[discord alert sent]")
    elif any_alert:
        print("[--no-discord: skipped alert]")
    else:
        print(f"[no gaps > {max_gap_minutes} min -- no alert]")


if __name__ == "__main__":
    main()