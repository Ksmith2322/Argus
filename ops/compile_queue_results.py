#!/usr/bin/env python3
"""
compile_queue_results.py — Parse all bt_summary_*.json files and produce a
ranked leaderboard CSV + console summary for overnight queue review.

Usage:
    python ops/compile_queue_results.py              # full leaderboard
    python ops/compile_queue_results.py --top 20     # top 20 only
    python ops/compile_queue_results.py --coin ETH   # filter by coin
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

LOGS_DIR = Path("C:/Argus/repo/ops/logs")
OUT_CSV = LOGS_DIR / "queue_leaderboard.csv"


def load_all_summaries() -> list[dict]:
    """Load every bt_summary_bt_*.json in the logs dir."""
    results = []
    for f in LOGS_DIR.glob("bt_summary_bt_*.json"):
        if "latest" in f.name:
            continue
        try:
            data = json.loads(f.read_text())
            data["_file"] = f.name
            results.append(data)
        except Exception:
            pass
    return results


def extract_label(run_id: str) -> str:
    """Try to match run_id back to a queue label via queue_results.log."""
    log_path = LOGS_DIR / "queue_results.log"
    if not log_path.exists():
        return ""
    text = log_path.read_text()
    # Look for: DONE: <label> | run_id=<run_id>
    for line in text.splitlines():
        if run_id in line and "DONE:" in line:
            m = re.search(r"DONE:\s*(\S+)", line)
            if m:
                return m.group(1)
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=0, help="Show only top N results")
    parser.add_argument("--coin", type=str, default="", help="Filter by coin symbol")
    parser.add_argument("--min-trades", type=int, default=5, help="Min trades to include")
    args = parser.parse_args()

    summaries = load_all_summaries()
    if not summaries:
        print("No summaries found in", LOGS_DIR)
        return

    # Enrich with label
    for s in summaries:
        run_id = s.get("run_id", "")
        s["_label"] = extract_label(run_id)
        s["_coin"] = s.get("symbol", "").replace("-USD", "")

    # Filter
    if args.coin:
        summaries = [s for s in summaries if s["_coin"].upper() == args.coin.upper()]

    if args.min_trades:
        summaries = [s for s in summaries if s.get("trades_closed", 0) >= args.min_trades]

    # Sort by profit_factor descending
    summaries.sort(key=lambda s: float(s.get("profit_factor", 0)), reverse=True)

    if args.top:
        summaries = summaries[:args.top]

    # Print leaderboard
    print(f"\n{'='*100}")
    print(f"  BACKTEST LEADERBOARD — {len(summaries)} runs")
    print(f"{'='*100}")
    print(f"{'Rank':>4} {'Label':<40} {'Coin':<6} {'PF':>6} {'WR%':>6} {'Trades':>7} {'PnL':>10} {'DD%':>6} {'AvgWin':>8} {'AvgLoss':>8}")
    print(f"{'-'*100}")

    for i, s in enumerate(summaries, 1):
        label = s.get("_label", "") or s.get("run_id", "")[:30]
        coin = s.get("_coin", "?")
        pf = float(s.get("profit_factor", 0))
        wr = float(s.get("win_rate_pct", 0))
        trades = int(s.get("trades_closed", 0))
        pnl = float(s.get("pnl_usd", 0))
        dd = float(s.get("max_drawdown_pct", 0))
        avg_win = float(s.get("avg_win_usd", 0))
        avg_loss = float(s.get("avg_loss_usd", 0))

        # Color coding via markers
        pf_mark = "**" if pf >= 1.0 else "  "
        print(f"{i:>4} {label:<40} {coin:<6} {pf_mark}{pf:>4.3f} {wr:>5.1f}% {trades:>7} ${pnl:>8.2f} {dd:>5.2f}% ${avg_win:>6.3f} ${avg_loss:>7.3f}")

    # Summary stats
    profitable = [s for s in summaries if float(s.get("profit_factor", 0)) >= 1.0]
    print(f"\n{'='*100}")
    print(f"  PROFITABLE: {len(profitable)}/{len(summaries)} runs (PF >= 1.0)")

    # Per-coin summary
    coins = {}
    for s in summaries:
        coin = s.get("_coin", "?")
        if coin not in coins:
            coins[coin] = {"count": 0, "profitable": 0, "best_pf": 0, "best_label": ""}
        coins[coin]["count"] += 1
        pf = float(s.get("profit_factor", 0))
        if pf >= 1.0:
            coins[coin]["profitable"] += 1
        if pf > coins[coin]["best_pf"]:
            coins[coin]["best_pf"] = pf
            coins[coin]["best_label"] = s.get("_label", "") or s.get("run_id", "")[:20]

    print(f"\n  PER-COIN SUMMARY:")
    print(f"  {'Coin':<8} {'Runs':>5} {'Profitable':>10} {'Best PF':>8} {'Best Config':<30}")
    for coin in sorted(coins.keys()):
        c = coins[coin]
        print(f"  {coin:<8} {c['count']:>5} {c['profitable']:>10} {c['best_pf']:>8.3f} {c['best_label']:<30}")

    # Tier classification
    print(f"\n  COIN TIERS:")
    print(f"  {'TRADE (PF>1.0):':<20}", end="")
    trade_coins = [c for c, d in coins.items() if d["best_pf"] >= 1.0]
    print(", ".join(trade_coins) if trade_coins else "none")

    print(f"  {'WATCH (PF 0.85-1.0):':<20}", end="")
    watch_coins = [c for c, d in coins.items() if 0.85 <= d["best_pf"] < 1.0]
    print(", ".join(watch_coins) if watch_coins else "none")

    print(f"  {'SKIP (PF<0.85):':<20}", end="")
    skip_coins = [c for c, d in coins.items() if d["best_pf"] < 0.85]
    print(", ".join(skip_coins) if skip_coins else "none")

    # Write CSV
    fields = ["rank", "label", "coin", "profit_factor", "win_rate_pct", "trades_closed",
              "pnl_usd", "max_drawdown_pct", "avg_win_usd", "avg_loss_usd",
              "avg_trade_duration_s", "exposure_pct", "run_id"]
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i, s in enumerate(summaries, 1):
            writer.writerow({
                "rank": i,
                "label": s.get("_label", "") or s.get("run_id", ""),
                "coin": s.get("_coin", ""),
                "profit_factor": s.get("profit_factor", ""),
                "win_rate_pct": s.get("win_rate_pct", ""),
                "trades_closed": s.get("trades_closed", ""),
                "pnl_usd": s.get("pnl_usd", ""),
                "max_drawdown_pct": s.get("max_drawdown_pct", ""),
                "avg_win_usd": s.get("avg_win_usd", ""),
                "avg_loss_usd": s.get("avg_loss_usd", ""),
                "avg_trade_duration_s": s.get("avg_trade_duration_s", ""),
                "exposure_pct": s.get("exposure_pct", ""),
                "run_id": s.get("run_id", ""),
            })

    print(f"\n  Leaderboard CSV saved: {OUT_CSV}")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    main()