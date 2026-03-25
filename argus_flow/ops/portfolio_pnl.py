"""Portfolio P&L Aggregation — cross-strategy performance tracking.

Usage:
    python -m argus_flow.ops.portfolio_pnl
    python -m argus_flow.ops.portfolio_pnl --period weekly
    python -m argus_flow.ops.portfolio_pnl --period monthly
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"

# Class A active cohort
RUNNERS = [
    {"name": "EUR/USD", "symbol": "eurusd", "log_dir": LOGS / "eurusd", "unit": "pips"},
    {"name": "GBP/USD", "symbol": "gbpusd", "log_dir": LOGS / "gbpusd", "unit": "pips"},
    {"name": "EUR/JPY", "symbol": "eurjpy", "log_dir": LOGS / "eurjpy", "unit": "pips"},
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r") as f:
        rows = list(csv.DictReader(f))
    # Normalize pnl field
    for row in rows:
        for key in ("pnl_pips", "pnl", "realized_pnl"):
            if key in row:
                try:
                    row["_pnl"] = float(row[key])
                except (ValueError, TypeError):
                    row["_pnl"] = 0.0
                break
        else:
            row["_pnl"] = 0.0
        # Parse close timestamp
        for key in ("close_ts", "exit_ts", "close_time", "timestamp"):
            if key in row and row[key]:
                try:
                    row["_close_dt"] = datetime.fromisoformat(row[key].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
                break
    return [r for r in rows if "_close_dt" in r]


def _date_key(dt: datetime, period: str) -> str:
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    elif period == "weekly":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    elif period == "monthly":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(period: str = "daily") -> dict:
    """Aggregate P&L across all pairs by time period."""
    now = datetime.now(timezone.utc).isoformat()

    # Collect all trades with pair labels
    all_trades = []
    per_pair_pnl = {}

    for r in RUNNERS:
        trades = _load_trades(r["log_dir"])
        pair_total = sum(t["_pnl"] for t in trades)
        per_pair_pnl[r["symbol"]] = {
            "name": r["name"],
            "total_pnl": round(pair_total, 4),
            "trade_count": len(trades),
            "unit": r["unit"],
        }
        for t in trades:
            t["_pair"] = r["symbol"]
            t["_pair_name"] = r["name"]
            all_trades.append(t)

    if not all_trades:
        return {
            "timestamp": now,
            "period": period,
            "total_pnl": 0.0,
            "trade_count": 0,
            "periods": [],
            "per_pair": per_pair_pnl,
            "streaks": {"current": 0, "longest_win": 0, "longest_loss": 0},
            "best_period": None,
            "worst_period": None,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
        }

    # Sort by close time
    all_trades.sort(key=lambda t: t["_close_dt"])

    # Group by period
    period_buckets: dict[str, list[float]] = defaultdict(list)
    for t in all_trades:
        key = _date_key(t["_close_dt"], period)
        period_buckets[key].append(t["_pnl"])

    # Build period summaries
    period_summaries = []
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for key in sorted(period_buckets.keys()):
        pnls = period_buckets[key]
        period_pnl = sum(pnls)
        cumulative += period_pnl
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_drawdown = max(max_drawdown, dd)

        period_summaries.append({
            "period": key,
            "pnl": round(period_pnl, 4),
            "trades": len(pnls),
            "cumulative": round(cumulative, 4),
        })

    # Best / worst periods
    best = max(period_summaries, key=lambda p: p["pnl"])
    worst = min(period_summaries, key=lambda p: p["pnl"])

    # Sharpe-like ratio (mean / stdev of period returns)
    period_pnls = [p["pnl"] for p in period_summaries]
    mean_pnl = sum(period_pnls) / len(period_pnls) if period_pnls else 0.0
    if len(period_pnls) > 1:
        variance = sum((x - mean_pnl) ** 2 for x in period_pnls) / (len(period_pnls) - 1)
        stdev = math.sqrt(variance)
        sharpe = (mean_pnl / stdev) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    # Streaks (win/loss by individual trade)
    current_streak = 0
    longest_win = 0
    longest_loss = 0
    win_run = 0
    loss_run = 0

    for t in all_trades:
        if t["_pnl"] > 0:
            win_run += 1
            loss_run = 0
            longest_win = max(longest_win, win_run)
        elif t["_pnl"] < 0:
            loss_run += 1
            win_run = 0
            longest_loss = max(longest_loss, loss_run)
        else:
            win_run = 0
            loss_run = 0

    # Current streak direction
    if win_run > 0:
        current_streak = win_run
    elif loss_run > 0:
        current_streak = -loss_run

    total_pnl = sum(t["_pnl"] for t in all_trades)

    report = {
        "timestamp": now,
        "period": period,
        "total_pnl": round(total_pnl, 4),
        "trade_count": len(all_trades),
        "periods": period_summaries,
        "per_pair": per_pair_pnl,
        "streaks": {
            "current": current_streak,
            "longest_win": longest_win,
            "longest_loss": longest_loss,
        },
        "best_period": best,
        "worst_period": worst,
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe, 4),
    }

    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_report(report: dict) -> None:
    """Print colored console summary."""
    green = "\033[92m"
    red = "\033[91m"
    yellow = "\033[93m"
    bold = "\033[1m"
    reset = "\033[0m"

    def c(val: float) -> str:
        color = green if val > 0 else red if val < 0 else yellow
        return f"{color}{val:+.4f}{reset}"

    print(f"\n{'='*65}")
    print(f"  {bold}PORTFOLIO P&L REPORT{reset}  —  {report['timestamp']}")
    print(f"  Period: {report['period']}   |   Trades: {report['trade_count']}")
    print(f"{'='*65}")

    # Per-pair breakdown
    print(f"\n  {bold}Per-Pair Contribution:{reset}")
    for sym, info in report["per_pair"].items():
        pnl_str = c(info["total_pnl"])
        print(f"    {info['name']:10s}  {pnl_str} {info['unit']}  ({info['trade_count']} trades)")

    # Totals
    print(f"\n  {bold}Portfolio Totals:{reset}")
    print(f"    Total P&L:      {c(report['total_pnl'])} pips")
    print(f"    Max Drawdown:   {red}{report['max_drawdown']:.4f}{reset} pips")
    print(f"    Sharpe Ratio:   {report['sharpe_ratio']:.4f}")

    # Streaks
    s = report["streaks"]
    streak_str = f"{green}+{s['current']}{reset}" if s["current"] > 0 else f"{red}{s['current']}{reset}"
    print(f"    Current Streak: {streak_str}")
    print(f"    Longest Win:    {s['longest_win']}   Longest Loss: {s['longest_loss']}")

    # Best / worst period
    if report["best_period"]:
        bp = report["best_period"]
        print(f"\n    Best {report['period']}:   {bp['period']}  {c(bp['pnl'])} ({bp['trades']} trades)")
    if report["worst_period"]:
        wp = report["worst_period"]
        print(f"    Worst {report['period']}:  {wp['period']}  {c(wp['pnl'])} ({wp['trades']} trades)")

    # Period table
    periods = report.get("periods", [])
    if periods:
        print(f"\n  {bold}Period Breakdown:{reset}")
        print(f"    {'Period':<14s}  {'P&L':>10s}  {'Trades':>6s}  {'Cumulative':>12s}")
        print(f"    {'-'*14}  {'-'*10}  {'-'*6}  {'-'*12}")
        for p in periods:
            pnl_color = green if p["pnl"] > 0 else red if p["pnl"] < 0 else ""
            cum_color = green if p["cumulative"] > 0 else red if p["cumulative"] < 0 else ""
            print(
                f"    {p['period']:<14s}  "
                f"{pnl_color}{p['pnl']:>+10.4f}{reset}  "
                f"{p['trades']:>6d}  "
                f"{cum_color}{p['cumulative']:>+12.4f}{reset}"
            )

    print(f"{'='*65}\n")


def save_report(report: dict) -> Path:
    """Save report to logs directory."""
    out = LOGS / "portfolio_pnl_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio P&L Aggregation")
    parser.add_argument(
        "--period",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="Aggregation period (default: daily)",
    )
    args = parser.parse_args()

    report = aggregate(period=args.period)
    print_report(report)
    path = save_report(report)
    print(f"Report saved to {path}")


if __name__ == "__main__":
    main()
