#!/usr/bin/env python3
"""Compare two or more backtest runs side-by-side.

Usage:
    python ops/compare_runs.py <run_id_1> <run_id_2> [run_id_3 ...]
    python ops/compare_runs.py --latest 3          # compare 3 most recent runs
    python ops/compare_runs.py --dir ops/logs/pc2  # use a different logs dir

Reads bt_summary_*.json files and prints a comparison table.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO / "ops" / "logs"

METRICS = [
    ("trades_closed",       "Trades",        "{:>6}",   None),
    ("win_rate_pct",        "Win Rate %",    "{:>8}",   float),
    ("profit_factor",       "Profit Factor", "{:>8}",   float),
    ("pnl_usd",            "PnL $",         "{:>+9}",  float),
    ("expectancy_usd",     "Expect $/trade", "{:>9}",  float),
    ("max_drawdown_pct",   "Max DD %",      "{:>8}",   float),
    ("max_drawdown_usd",   "Max DD $",      "{:>8}",   float),
    ("avg_win_usd",        "Avg Win $",     "{:>9}",   float),
    ("avg_loss_usd",       "Avg Loss $",    "{:>9}",   float),
    ("avg_trade_duration_s","Avg Dur (min)", "{:>8}",   lambda x: round(float(x)/60, 1)),
    ("exposure_pct",       "Exposure %",    "{:>8}",   float),
    ("entry_filled",       "Entries",       "{:>7}",    None),
    ("entry_attempts",     "Attempts",      "{:>8}",    None),
    ("entry_block_other",  "Block:Regime",  "{:>8}",    None),
    ("entry_block_size",   "Block:Size",    "{:>8}",    None),
    ("total_return_pct",   "Return %",      "{:>8}",    float),
]


def load_summary(log_dir: Path, run_id: str) -> dict:
    """Load a bt_summary JSON by run_id."""
    path = log_dir / f"bt_summary_{run_id}.json"
    if not path.exists():
        # Try without bt_ prefix
        path = log_dir / f"bt_summary_bt_{run_id}.json"
    if not path.exists():
        # Try partial match
        candidates = sorted(log_dir.glob(f"bt_summary_*{run_id}*.json"))
        if candidates:
            path = candidates[-1]
    if not path.exists():
        print(f"ERROR: No summary found for '{run_id}' in {log_dir}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def find_latest(log_dir: Path, n: int) -> list:
    """Find the N most recent summary files."""
    summaries = sorted(log_dir.glob("bt_summary_bt_*.json"), key=os.path.getmtime)
    # Exclude bt_summary_latest.json symlink
    summaries = [s for s in summaries if "latest" not in s.name]
    return [s.stem.replace("bt_summary_", "") for s in summaries[-n:]]


def get_run_header(log_dir: Path, run_id: str) -> dict:
    """Get run header fields."""
    for pattern in [f"run_header_{run_id}.json", f"run_header_bt_{run_id}.json"]:
        path = log_dir / pattern
        if path.exists():
            with open(path) as f:
                return json.load(f)
    # Partial match
    for p in log_dir.glob(f"run_header_*{run_id}*.json"):
        with open(p) as f:
            return json.load(f)
    return {}


def get_config_hash(log_dir: Path, run_id: str) -> str:
    """Get config hash from run header."""
    return get_run_header(log_dir, run_id).get("config_hash", "?")


def format_val(raw, converter, fmt):
    if raw is None or raw == "":
        return fmt.format(0)
    if converter:
        try:
            v = converter(raw)
        except (ValueError, TypeError):
            return str(raw)[:10]
        return fmt.format(v)
    return fmt.format(raw)


def delta_str(vals, idx, converter):
    """Show delta vs first run (the baseline)."""
    if idx == 0 or converter is None:
        return ""
    try:
        base = converter(vals[0]) if vals[0] not in (None, "") else 0
        curr = converter(vals[idx]) if vals[idx] not in (None, "") else 0
        d = curr - base
        if d == 0:
            return ""
        return f" ({d:+.2f})"
    except (ValueError, TypeError):
        return ""


def main():
    parser = argparse.ArgumentParser(description="Compare backtest runs")
    parser.add_argument("run_ids", nargs="*", help="Run IDs to compare")
    parser.add_argument("--latest", type=int, default=0, help="Compare N most recent runs")
    parser.add_argument("--dir", type=str, default=str(DEFAULT_LOG_DIR), help="Logs directory")
    args = parser.parse_args()

    log_dir = Path(args.dir)
    if not log_dir.exists():
        print(f"ERROR: {log_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    if args.latest > 0:
        run_ids = find_latest(log_dir, args.latest)
    elif args.run_ids:
        run_ids = args.run_ids
    else:
        run_ids = find_latest(log_dir, 2)

    if len(run_ids) < 2:
        print("Need at least 2 runs to compare. Found:", run_ids, file=sys.stderr)
        sys.exit(1)

    summaries = [load_summary(log_dir, rid) for rid in run_ids]
    headers = [get_run_header(log_dir, rid) for rid in run_ids]

    # Print header
    short_ids = [rid[-12:] for rid in run_ids]  # last 12 chars for readability
    labels = [h.get("label", "") for h in headers]
    label_w = 16
    col_w = 20
    print()
    print(f"{'':>{label_w}}", end="")
    for sid in short_ids:
        print(f"  {sid:>{col_w}}", end="")
    print()
    if any(labels):
        print(f"{'label':>{label_w}}", end="")
        for lbl in labels:
            print(f"  {(lbl or '-')[:col_w]:>{col_w}}", end="")
        print()
    print(f"{'config_hash':>{label_w}}", end="")
    for h in headers:
        print(f"  {h.get('config_hash', '?'):>{col_w}}", end="")
    print()
    print("-" * (label_w + (col_w + 2) * len(run_ids)))

    # Print each metric row
    for key, label, fmt, converter in METRICS:
        raw_vals = [s.get(key) for s in summaries]
        print(f"{label:>{label_w}}", end="")
        for i, (raw, s) in enumerate(zip(raw_vals, summaries)):
            val_str = format_val(raw, converter, fmt)
            d = delta_str(raw_vals, i, converter) if converter else ""
            cell = f"{val_str}{d}"
            print(f"  {cell:>{col_w}}", end="")
        print()

    # Event count comparison
    print()
    print(f"{'--- Events ---':>{label_w}}", end="")
    print()
    all_events = set()
    for s in summaries:
        all_events.update(s.get("event_counts", {}).keys())
    for ev in sorted(all_events):
        if ev == "TF_SNAPSHOT_VERIFY":
            continue  # noise
        print(f"{ev[-16:]:>{label_w}}", end="")
        for s in summaries:
            v = s.get("event_counts", {}).get(ev, 0)
            print(f"  {v:>{col_w}}", end="")
        print()

    print()
    print(f"First run is baseline for deltas.")
    print()


if __name__ == "__main__":
    main()