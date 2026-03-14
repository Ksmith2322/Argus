#!/usr/bin/env python3
"""ops/export_trades.py -- Export trade-by-trade details from backtest runs.

Usage:
    python ops/export_trades.py <run_id>
    python ops/export_trades.py --latest
    python ops/export_trades.py <run_id> --losers-only
    python ops/export_trades.py <run_id> --winners-only
    python ops/export_trades.py <run_id> --csv
"""
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "ops" / "logs"


def find_trades_file(run_id: str) -> Path | None:
    for pattern in [f"trades_{run_id}.csv", f"trades_bt_{run_id}.csv"]:
        p = LOGS / pattern
        if p.exists():
            return p
    for p in sorted(LOGS.glob(f"trades_*{run_id}*.csv")):
        return p
    return None


def find_latest_run_id() -> str | None:
    summaries = sorted(LOGS.glob("bt_summary_bt_*.json"), key=os.path.getmtime)
    summaries = [s for s in summaries if "latest" not in s.name]
    if not summaries:
        return None
    return summaries[-1].stem.replace("bt_summary_", "")


def epoch_to_str(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(epoch)


def duration_str(seconds) -> str:
    try:
        s = int(float(seconds))
    except (ValueError, TypeError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _f(val, default=0.0) -> float:
    try:
        return float(val) if val != "" else default
    except (ValueError, TypeError):
        return default


def load_trades(path: Path) -> list[dict]:
    trades = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades


def print_table(trades: list[dict]):
    if not trades:
        print("No trades.")
        return

    # Header
    fmt = "{:>5} {:>11} {:>11} {:>7} {:>10} {:>10} {:>9} {:>8} {:>7} {:>7}"
    print(fmt.format("#", "Entry", "Exit", "Dur", "EntryPx", "ExitPx", "PnL$", "Ret%", "MFE%", "MAE%"))
    print("-" * 105)

    for i, t in enumerate(trades):
        entry = epoch_to_str(t.get("entry_epoch", ""))
        exit_ = epoch_to_str(t.get("exit_epoch", ""))
        dur = duration_str(t.get("duration_s", 0))
        entry_px = f"${_f(t.get('entry_px', 0)):.2f}"
        exit_px = f"${_f(t.get('exit_px', 0)):.2f}"
        pnl = _f(t.get("realized_usd", 0))
        ret = _f(t.get("realized_return_pct", 0))
        mfe = _f(t.get("mfe_pct_points", 0))
        mae = _f(t.get("mae_pct_points", 0))

        pnl_str = f"${pnl:+.4f}"
        ret_str = f"{ret:+.3f}"
        mfe_str = f"{mfe:.3f}"
        mae_str = f"{mae:.3f}"

        print(fmt.format(i + 1, entry, exit_, dur, entry_px, exit_px, pnl_str, ret_str, mfe_str, mae_str))


def print_csv(trades: list[dict]):
    if not trades:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=trades[0].keys())
    writer.writeheader()
    writer.writerows(trades)


def print_summary(trades: list[dict]):
    if not trades:
        return
    pnls = [_f(t.get("realized_usd", 0)) for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    durations = [int(_f(t.get("duration_s", 0))) for t in trades]

    print()
    print(f"  Total trades:  {len(trades)}")
    print(f"  Winners:       {len(winners)}  ({100*len(winners)/len(trades):.1f}%)")
    print(f"  Losers:        {len(losers)}")
    print(f"  Total PnL:     ${sum(pnls):+.4f}")
    if winners:
        print(f"  Avg winner:    ${sum(winners)/len(winners):+.4f}")
        print(f"  Best trade:    ${max(pnls):+.4f}")
    if losers:
        print(f"  Avg loser:     ${sum(losers)/len(losers):+.4f}")
        print(f"  Worst trade:   ${min(pnls):+.4f}")
    print(f"  Avg duration:  {duration_str(sum(durations)/len(durations))}")
    if durations:
        print(f"  Min duration:  {duration_str(min(durations))}")
        print(f"  Max duration:  {duration_str(max(durations))}")


def main():
    args = sys.argv[1:]
    run_id = None
    losers_only = "--losers-only" in args
    winners_only = "--winners-only" in args
    csv_mode = "--csv" in args
    latest = "--latest" in args

    # Get run_id
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        run_id = positional[0]
    elif latest:
        run_id = find_latest_run_id()
    else:
        run_id = find_latest_run_id()

    if not run_id:
        print("No runs found.", file=sys.stderr)
        sys.exit(1)

    trades_file = find_trades_file(run_id)
    if not trades_file:
        print(f"No trades file for {run_id}", file=sys.stderr)
        sys.exit(1)

    print(f"Run: {run_id}")
    print(f"File: {trades_file.name}")
    print()

    trades = load_trades(trades_file)

    # Filter
    if losers_only:
        trades = [t for t in trades if _f(t.get("realized_usd", 0)) <= 0]
        print(f"(showing {len(trades)} losing trades)")
    elif winners_only:
        trades = [t for t in trades if _f(t.get("realized_usd", 0)) > 0]
        print(f"(showing {len(trades)} winning trades)")

    if csv_mode:
        print_csv(trades)
    else:
        print_table(trades)
        print_summary(trades)


if __name__ == "__main__":
    main()