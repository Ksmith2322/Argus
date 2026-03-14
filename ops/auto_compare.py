#!/usr/bin/env python3
"""ops/auto_compare.py -- Auto-detect completed backtest runs and generate comparison table.

Scans ops/logs for all bt_summary_bt_*.json files, extracts key metrics,
and prints a ranked comparison table sorted by profit factor.

Usage:
    python ops/auto_compare.py              # compare all runs
    python ops/auto_compare.py --top 5      # show top 5 only
    python ops/auto_compare.py --csv        # output as CSV
"""
import json
import os
import sys
from pathlib import Path

LOGS = Path(__file__).resolve().parent / "logs"

METRICS = [
    ("trades_closed", "Trades", "{:>6}"),
    ("win_rate_pct", "WR%", "{:>6}"),
    ("pnl_usd", "PnL$", "{:>9}"),
    ("profit_factor", "PF", "{:>7}"),
    ("expectancy_usd", "E[$/t]", "{:>8}"),
    ("max_drawdown_pct", "MaxDD%", "{:>7}"),
    ("entry_filled", "Fills", "{:>6}"),
    ("avg_win_usd", "AvgW$", "{:>7}"),
    ("avg_loss_usd", "AvgL$", "{:>7}"),
]


def load_summaries() -> list[dict]:
    rows = []
    for p in sorted(LOGS.glob("bt_summary_bt_*.json")):
        if "latest" in p.name:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            # Try to get label from run_header
            rh_path = LOGS / f"run_header_{d.get('run_id', '')}.json"
            label = ""
            if rh_path.exists():
                with open(rh_path) as f:
                    hdr = json.load(f)
                label = hdr.get("label", "")
            d["_label"] = label
            d["_mtime"] = os.path.getmtime(p)
            rows.append(d)
        except Exception:
            continue
    return rows


def print_table(rows: list[dict], csv_mode: bool = False):
    if not rows:
        print("No completed runs found.")
        return

    # Sort by profit_factor descending (best first)
    rows.sort(key=lambda r: float(r.get("profit_factor", "0") or "0"), reverse=True)

    if csv_mode:
        cols = ["run_id", "label"] + [m[0] for m in METRICS]
        print(",".join(cols))
        for r in rows:
            vals = [r.get("run_id", ""), r.get("_label", "")]
            for key, _, _ in METRICS:
                vals.append(str(r.get(key, "")))
            print(",".join(vals))
        return

    # Table header
    id_w = 22
    lbl_w = 30
    hdr = f"{'Run ID':>{id_w}}  {'Label':<{lbl_w}}"
    for _, name, fmt in METRICS:
        hdr += f"  {name:>{len(fmt.format('0'))}}"
    print(hdr)
    print("-" * len(hdr))

    for r in rows:
        rid = (r.get("run_id", "") or "")[-id_w:]
        label = (r.get("_label", "") or "")[:lbl_w]
        line = f"{rid:>{id_w}}  {label:<{lbl_w}}"
        for key, _, fmt in METRICS:
            val = r.get(key, "")
            if val == "" or val is None:
                line += f"  {'-':>{len(fmt.format('0'))}}"
            else:
                line += f"  {fmt.format(val)}"
        print(line)

    # Best run summary
    print()
    best = rows[0]
    print(f"BEST: {best.get('run_id', '')}  PF={best.get('profit_factor', '?')}  WR={best.get('win_rate_pct', '?')}%  PnL=${best.get('pnl_usd', '?')}")


def main():
    top = 0
    min_trades = 0
    csv_mode = "--csv" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--top" and i + 1 < len(sys.argv):
            top = int(sys.argv[i + 1])
        if a == "--min-trades" and i + 1 < len(sys.argv):
            min_trades = int(sys.argv[i + 1])

    rows = load_summaries()
    if min_trades > 0:
        rows = [r for r in rows if int(r.get("trades_closed", 0) or 0) >= min_trades]
    if top > 0:
        rows.sort(key=lambda r: float(r.get("profit_factor", "0") or "0"), reverse=True)
        rows = rows[:top]
    print_table(rows, csv_mode)


if __name__ == "__main__":
    main()