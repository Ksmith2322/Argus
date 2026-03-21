#!/usr/bin/env python3
"""
Penalty attribution report for MISSED_BUY_WATCH_GATED events.

Reads live_events.csv for ETH and BTC, parses penalty components from
the detail field, and produces a formatted summary + top-20 closest-to-trigger list.

Usage:
    C:\\Argus\\.venv\\Scripts\\python.exe ops\\analyze_missed_signals.py [--hours 72]
"""

import argparse
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

# ── paths ──────────────────────────────────────────────────────────────
REPO = Path(r"C:\Argus\repo")
LOG_ROOT = REPO / "ops" / "logs"
COINS = {
    "ETH": LOG_ROOT / "eth" / "live_events.csv",
    "BTC": LOG_ROOT / "btc" / "live_events.csv",
}
REPORT_PATH = LOG_ROOT / "penalty_attribution_report.txt"

EVENT_TYPE = "MISSED_BUY_WATCH_GATED"


# ── regex helpers for the pipe-delimited detail field ──────────────────
def _search(pattern, text, default=None):
    m = re.search(pattern, text)
    return m.group(1) if m else default


def _float(pattern, text, default=0.0):
    v = _search(pattern, text)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _int(pattern, text, default=0):
    v = _search(pattern, text)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


# ── parse one detail string ────────────────────────────────────────────
def parse_detail(detail: str) -> dict:
    """Extract all penalty components from the detail text."""
    d = {}

    # Prices / scores
    d["px"] = _float(r"px=([\d.]+)", detail)
    d["base_score"] = _int(r"base_score=(\d+)", detail)
    d["adj_score"] = _int(r"adj_score=(-?\d+)", detail)
    d["conf_score"] = _int(r"conf_score=(-?\d+)", detail)
    d["min_score"] = _int(r"min=(\d+)", detail)
    d["gate"] = _search(r"gate=(\w+)", detail, "UNKNOWN")

    # Regime
    d["regime"] = _search(r"regime=(\w+)", detail, "UNKNOWN")

    # Delta (total adjustment applied to base_score)
    d["delta"] = _int(r"delta=(-?\d+)", detail)

    # Trend strength / vol
    d["trend_strength"] = _float(r"trend_strength=([\d.]+)", detail)
    d["vol"] = _float(r"\bvol=([\d.]+)", detail)

    # Session bonus — e.g. session_bonus=+5 (NY:...)
    session_bonus_str = _search(r"session_bonus=([+-]?\d+)", detail)
    d["session_bonus"] = int(session_bonus_str) if session_bonus_str else 0
    d["session_name"] = _search(r"session_bonus=[+-]?\d+\s*\((\w+):", detail, "NONE")

    # Conf reasons breakdown
    d["conf_reasons"] = _search(r"conf_reasons=([^|]+)", detail, "").strip()

    # Regime penalty — appears as penalty_range=-15 in conf_reasons
    d["range_penalty"] = _int(r"penalty_range=(-?\d+)", detail)
    # Trend bonus — appears as bonus_trend_up=+5 or bonus_trend_down=...
    d["trend_bonus"] = _int(r"bonus_trend_(?:up|down)=([+-]?\d+)", detail)

    # OB imbalance — e.g. ob_imbalance=-0.298(-12)
    ob_match = re.search(r"ob_imbalance=([-\d.]+)\(([-+]?\d+)\)", detail)
    if ob_match:
        d["ob_value"] = float(ob_match.group(1))
        d["ob_score"] = int(ob_match.group(2))
    else:
        d["ob_value"] = None
        d["ob_score"] = 0

    # Liquidity penalty — e.g. liq_penalty=-10
    d["liq_penalty"] = _int(r"liq_penalty=(-?\d+)", detail)

    # Liq details
    d["liq_ok"] = _search(r"liq_ok=(\w+)", detail, "True")
    d["liq_mode"] = _search(r"liq_mode=(\w+)", detail, "UNKNOWN")

    # Structure
    d["struct"] = _search(r"struct=STRUCT:([^|]*)", detail, "").strip()

    # Gap to threshold (positive = below threshold)
    d["gap"] = d["min_score"] - d["conf_score"]

    return d


# ── read events for one coin ──────────────────────────────────────────
def read_events(csv_path: Path, cutoff_dt: datetime) -> list[dict]:
    """Read and parse MISSED_BUY_WATCH_GATED events after cutoff_dt."""
    events = []
    if not csv_path.exists():
        return events

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return events

        # Build column index map
        col = {name: idx for idx, name in enumerate(header)}
        ts_idx = col.get("ts", 0)
        event_idx = col.get("event", 4)
        detail_idx = col.get("detail", 5)

        for row in reader:
            if len(row) <= max(ts_idx, event_idx, detail_idx):
                continue
            if row[event_idx] != EVENT_TYPE:
                continue

            # Parse timestamp
            ts_str = row[ts_idx]
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue

            if ts < cutoff_dt:
                continue

            detail = row[detail_idx]
            parsed = parse_detail(detail)
            parsed["ts"] = ts
            parsed["ts_str"] = ts_str
            events.append(parsed)

    return events


# ── summary for one coin ──────────────────────────────────────────────
def coin_summary(coin: str, events: list[dict]) -> str:
    lines = []
    n = len(events)
    lines.append(f"{'=' * 72}")
    lines.append(f"  {coin}  --  MISSED_BUY_WATCH_GATED Penalty Attribution")
    lines.append(f"{'=' * 72}")

    if n == 0:
        lines.append("  No events found in the selected time window.\n")
        return "\n".join(lines)

    base_scores = [e["base_score"] for e in events]
    conf_scores = [e["conf_score"] for e in events]
    gaps = [e["gap"] for e in events]

    lines.append(f"  Total events:          {n:,}")
    lines.append(f"  Avg  base_score:       {mean(base_scores):.1f}")
    lines.append(f"  Med  base_score:       {median(base_scores):.1f}")
    lines.append(f"  Avg  conf_score:       {mean(conf_scores):.1f}")
    lines.append(f"  Med  conf_score:       {median(conf_scores):.1f}")
    lines.append(f"  Avg  gap to threshold: {mean(gaps):.1f}")
    lines.append(f"  Med  gap to threshold: {median(gaps):.1f}")
    lines.append("")

    # Penalty breakdowns
    range_ct = sum(1 for e in events if e["range_penalty"] != 0)
    ob_neg_ct = sum(1 for e in events if e["ob_score"] < 0)
    ob_any_ct = sum(1 for e in events if e["ob_value"] is not None)
    sess_ct = sum(1 for e in events if e["session_bonus"] != 0)
    liq_ct = sum(1 for e in events if e["liq_penalty"] != 0)

    lines.append("  Penalty Component          Count     %")
    lines.append("  " + "-" * 46)
    lines.append(f"  RANGE penalty applied       {range_ct:>6,}   {range_ct / n * 100:5.1f}%")
    lines.append(f"  OB imbalance (negative)     {ob_neg_ct:>6,}   {ob_neg_ct / n * 100:5.1f}%")
    lines.append(f"  OB imbalance (any data)     {ob_any_ct:>6,}   {ob_any_ct / n * 100:5.1f}%")
    lines.append(f"  Session bonus (non-zero)    {sess_ct:>6,}   {sess_ct / n * 100:5.1f}%")
    lines.append(f"  Liquidity penalty           {liq_ct:>6,}   {liq_ct / n * 100:5.1f}%")
    lines.append("")

    # Regime distribution
    regime_counts = defaultdict(int)
    for e in events:
        regime_counts[e["regime"]] += 1
    lines.append("  Regime Distribution")
    lines.append("  " + "-" * 46)
    for regime, ct in sorted(regime_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {regime:<24}  {ct:>6,}   {ct / n * 100:5.1f}%")
    lines.append("")

    # Session distribution
    sess_counts = defaultdict(int)
    for e in events:
        sess_counts[e["session_name"]] += 1
    lines.append("  Session Distribution")
    lines.append("  " + "-" * 46)
    for sess, ct in sorted(sess_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {sess:<24}  {ct:>6,}   {ct / n * 100:5.1f}%")
    lines.append("")

    # Gap distribution buckets
    lines.append("  Gap-to-Threshold Distribution")
    lines.append("  " + "-" * 46)
    buckets = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100), (100, 999)]
    for lo, hi in buckets:
        ct = sum(1 for g in gaps if lo <= g < hi)
        label = f"  [{lo:>3}, {hi:>3})" if hi < 999 else f"  [{lo:>3}, inf)"
        lines.append(f"{label:28s}  {ct:>6,}   {ct / n * 100:5.1f}%")
    lines.append("")

    # Avg penalty contributions for events where they are present
    if range_ct:
        avg_range = mean(e["range_penalty"] for e in events if e["range_penalty"] != 0)
        lines.append(f"  Avg RANGE penalty (when applied):   {avg_range:+.1f}")
    if ob_neg_ct:
        avg_ob = mean(e["ob_score"] for e in events if e["ob_score"] < 0)
        lines.append(f"  Avg OB penalty (when negative):     {avg_ob:+.1f}")
    if liq_ct:
        avg_liq = mean(e["liq_penalty"] for e in events if e["liq_penalty"] != 0)
        lines.append(f"  Avg LIQ penalty (when applied):     {avg_liq:+.1f}")
    if sess_ct:
        avg_sess = mean(e["session_bonus"] for e in events if e["session_bonus"] != 0)
        lines.append(f"  Avg session bonus (when non-zero):  {avg_sess:+.1f}")

    lines.append("")
    return "\n".join(lines)


# ── top N closest to trigger ──────────────────────────────────────────
def top_closest(coin: str, events: list[dict], n: int = 20) -> str:
    if not events:
        return ""

    sorted_events = sorted(events, key=lambda e: e["gap"])
    top = sorted_events[:n]

    lines = []
    lines.append(f"  TOP {n} CLOSEST TO TRIGGER --{coin}")
    lines.append("  " + "-" * 110)
    lines.append(
        f"  {'Timestamp':<34s} {'Price':>10s} {'Base':>5s} {'Adj':>5s} "
        f"{'Conf':>5s} {'Min':>5s} {'Gap':>5s} {'Regime':<11s} "
        f"{'Range':>6s} {'OB':>6s} {'Liq':>6s} {'Sess':>6s} {'Session':<8s}"
    )
    lines.append("  " + "-" * 110)

    for e in top:
        ob_str = f"{e['ob_score']:+d}" if e["ob_value"] is not None else "  --"
        lines.append(
            f"  {e['ts_str']:<34s} {e['px']:>10.3f} {e['base_score']:>5d} {e['adj_score']:>5d} "
            f"{e['conf_score']:>5d} {e['min_score']:>5d} {e['gap']:>5d} {e['regime']:<11s} "
            f"{e['range_penalty']:>+5d} {ob_str:>6s} {e['liq_penalty']:>+5d} {e['session_bonus']:>+5d} {e['session_name']:<8s}"
        )

    lines.append("")
    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Penalty attribution report for MISSED_BUY_WATCH_GATED events"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=72,
        help="How many hours back to look (default: 72)",
    )
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    report_lines = []
    report_lines.append("")
    report_lines.append(f"  ARGUS -- Penalty Attribution Report")
    report_lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report_lines.append(f"  Window:    last {args.hours} hours (cutoff {cutoff.strftime('%Y-%m-%d %H:%M UTC')})")
    report_lines.append("")

    total_events = 0

    for coin, csv_path in COINS.items():
        events = read_events(csv_path, cutoff)
        total_events += len(events)
        report_lines.append(coin_summary(coin, events))
        report_lines.append(top_closest(coin, events, n=20))

    if total_events == 0:
        report_lines.append("  No MISSED_BUY_WATCH_GATED events found in the time window.")
        report_lines.append(f"  Try increasing --hours (current: {args.hours}).")
        report_lines.append("")

    report = "\n".join(report_lines)
    print(report)

    # Save to file
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  Report saved to: {REPORT_PATH}")


if __name__ == "__main__":
    main()