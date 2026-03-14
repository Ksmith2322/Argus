#!/usr/bin/env python3
"""Analyze confluence score distribution of filled entries in a backtest run.

Usage:
    python ops/analyze_scores.py <run_id>
    python ops/analyze_scores.py --latest
    python ops/analyze_scores.py --dir ops/logs/pc2 --latest

Reads bt_events_*.csv and trades_*.csv, cross-references WOULD_BUY events
with trade outcomes, and prints score-bucketed performance.
"""
import argparse
import csv
import datetime
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO / "ops" / "logs"


def find_file(log_dir: Path, pattern: str, run_id: str) -> Path:
    """Find a file by pattern and run_id (supports partial match)."""
    exact = log_dir / f"{pattern}_{run_id}.csv"
    if exact.exists():
        return exact
    exact2 = log_dir / f"{pattern}_bt_{run_id}.csv"
    if exact2.exists():
        return exact2
    candidates = sorted(log_dir.glob(f"{pattern}*{run_id}*.csv"))
    if candidates:
        return candidates[-1]
    return exact  # will fail on open


def find_latest_run(log_dir: Path) -> str:
    summaries = sorted(log_dir.glob("bt_summary_bt_*.json"), key=os.path.getmtime)
    summaries = [s for s in summaries if "latest" not in s.name]
    if not summaries:
        print("No summaries found", file=sys.stderr)
        sys.exit(1)
    return summaries[-1].stem.replace("bt_summary_", "")


def get_session(utc_hour: int) -> str:
    if 0 <= utc_hour < 8:
        return "ASIA"
    elif 8 <= utc_hour < 13:
        return "LONDON"
    elif 13 <= utc_hour < 17:
        return "OVERLAP"
    elif 17 <= utc_hour < 22:
        return "NY"
    return "OFF"


def extract_score(detail_str: str) -> int:
    """Extract adj_score from the detail/notes column."""
    m = re.search(r'adj_score=(-?\d+)', detail_str)
    if m:
        return int(m.group(1))
    # Fallback: look for base_score
    m = re.search(r'base_score=(\d+)', detail_str)
    if m:
        return int(m.group(1))
    return -1


def extract_regime(detail_str: str) -> str:
    m = re.search(r'regime=(\w+)', detail_str)
    return m.group(1) if m else "?"


def main():
    parser = argparse.ArgumentParser(description="Analyze entry score distribution")
    parser.add_argument("run_id", nargs="?", default="", help="Run ID")
    parser.add_argument("--latest", action="store_true", help="Use latest run")
    parser.add_argument("--dir", type=str, default=str(DEFAULT_LOG_DIR))
    args = parser.parse_args()

    log_dir = Path(args.dir)
    run_id = args.run_id if args.run_id else find_latest_run(log_dir)
    print(f"Run: {run_id}")
    print(f"Dir: {log_dir}")
    print()

    # Load trades (entry_epoch -> outcome)
    trades_file = find_file(log_dir, "trades", run_id)
    trades = {}
    try:
        with open(trades_file) as f:
            for r in csv.DictReader(f):
                ep = r.get("entry_epoch", "")
                if ep:
                    pnl = float(r["realized_usd"]) if r.get("realized_usd") else None
                    dur = int(r["duration_s"]) if r.get("duration_s") else None
                    trades[int(ep)] = {"pnl": pnl, "duration": dur}
    except FileNotFoundError:
        print(f"WARNING: {trades_file} not found — PnL data unavailable")

    # Parse WOULD_BUY events from events file
    events_file = find_file(log_dir, "bt_events", run_id)
    entries = []
    seen_epochs = set()
    try:
        with open(events_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Only count ENTRY_ATTEMPT with action=WOULD_BUY (one per entry)
                if row.get("event") != "ENTRY_ATTEMPT":
                    continue
                if row.get("action") != "WOULD_BUY":
                    continue

                epoch = int(row.get("epoch", 0))
                if epoch in seen_epochs or epoch == 0:
                    continue
                seen_epochs.add(epoch)

                score_raw = row.get("confluence_score", "")
                score = int(score_raw) if score_raw else -1
                regime = row.get("regime", "?")
                session_col = row.get("session", "")
                dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
                session = session_col if session_col else get_session(dt.hour)

                # Match to trade outcome
                trade = trades.get(epoch)
                if not trade:
                    # Try nearby epochs (within 60s)
                    for offset in range(-60, 61):
                        trade = trades.get(epoch + offset)
                        if trade:
                            break

                entries.append({
                    "epoch": epoch,
                    "score": score,
                    "regime": regime,
                    "session": session,
                    "hour": dt.hour,
                    "pnl": trade["pnl"] if trade else None,
                    "duration": trade["duration"] if trade else None,
                })
    except FileNotFoundError:
        print(f"ERROR: {events_file} not found", file=sys.stderr)
        sys.exit(1)

    if not entries:
        print("No WOULD_BUY entries found.")
        return

    # --- Score bucket analysis ---
    buckets = [(0, 59, "<60"), (60, 69, "60-69"), (70, 79, "70-79"),
               (80, 84, "80-84"), (85, 89, "85-89"), (90, 100, "90+")]

    print(f"{'Score':>8} {'N':>4} {'Wins':>4} {'WR%':>6} {'PnL':>8} {'AvgPnL':>8} {'AvgDur':>7}")
    print("-" * 55)
    for lo, hi, label in buckets:
        matched = [e for e in entries if lo <= e["score"] <= hi and e["pnl"] is not None]
        if not matched:
            continue
        n = len(matched)
        wins = sum(1 for e in matched if e["pnl"] > 0)
        pnl = sum(e["pnl"] for e in matched)
        avg_pnl = pnl / n
        durs = [e["duration"] for e in matched if e["duration"] and e["duration"] > 0]
        avg_dur = (sum(durs) / len(durs) / 60) if durs else 0
        wr = wins / n * 100
        print(f"{label:>8} {n:>4} {wins:>4} {wr:>5.1f}% {pnl:>+7.2f} {avg_pnl:>+7.3f} {avg_dur:>5.0f}m")

    # Unmatched entries (no trade outcome found)
    unmatched = [e for e in entries if e["pnl"] is None]
    if unmatched:
        print(f"{'(open)':>8} {len(unmatched):>4}   --     --       --      --")

    # --- Regime breakdown ---
    print()
    print(f"{'Regime':>12} {'N':>4} {'Wins':>4} {'WR%':>6} {'PnL':>8}")
    print("-" * 40)
    regimes = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for e in entries:
        if e["pnl"] is None:
            continue
        r = regimes[e["regime"]]
        r["n"] += 1
        r["wins"] += 1 if e["pnl"] > 0 else 0
        r["pnl"] += e["pnl"]
    for regime in sorted(regimes.keys()):
        r = regimes[regime]
        wr = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
        print(f"{regime:>12} {r['n']:>4} {r['wins']:>4} {wr:>5.1f}% {r['pnl']:>+7.2f}")

    # --- Session breakdown ---
    print()
    print(f"{'Session':>12} {'N':>4} {'Wins':>4} {'WR%':>6} {'PnL':>8}")
    print("-" * 40)
    sessions = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for e in entries:
        if e["pnl"] is None:
            continue
        s = sessions[e["session"]]
        s["n"] += 1
        s["wins"] += 1 if e["pnl"] > 0 else 0
        s["pnl"] += e["pnl"]
    for sess in ["ASIA", "LONDON", "OVERLAP", "NY", "OFF"]:
        if sess not in sessions:
            continue
        s = sessions[sess]
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"{sess:>12} {s['n']:>4} {s['wins']:>4} {wr:>5.1f}% {s['pnl']:>+7.2f}")

    # --- Duration breakdown ---
    print()
    dur_buckets = [(0, 1800, "<30m"), (1800, 3600, "30-60m"), (3600, 5400, "1-1.5h"),
                   (5400, 10800, "1.5-3h"), (10800, 999999, ">3h")]
    print(f"{'Duration':>12} {'N':>4} {'Wins':>4} {'WR%':>6} {'PnL':>8}")
    print("-" * 40)
    for lo, hi, label in dur_buckets:
        matched = [e for e in entries if e["duration"] is not None and lo <= e["duration"] < hi]
        if not matched:
            continue
        n = len(matched)
        wins = sum(1 for e in matched if e["pnl"] and e["pnl"] > 0)
        pnl = sum(e["pnl"] for e in matched if e["pnl"])
        wr = wins / n * 100
        print(f"{label:>12} {n:>4} {wins:>4} {wr:>5.1f}% {pnl:>+7.2f}")

    print()


if __name__ == "__main__":
    main()