"""Trade Metrics — aggregate performance and execution quality reporting.

Reads trades.csv from all runner log dirs and computes:
- Fleet-wide and per-instrument PnL, win rate, profit factor
- Duration analysis (avg, median, by exit reason)
- Rolling performance (last 20 trades)
- Execution timing (entry hour distribution)

Outputs JSON suitable for dashboard consumption.

Usage:
    python -m argus_flow.ops.trade_metrics
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"
OUTPUT_PATH = LOGS_ROOT / "trade_metrics.json"


def _read_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _compute_metrics(trades: list[dict], label: str) -> dict:
    """Compute aggregate metrics for a list of trade dicts."""
    if not trades:
        return {"label": label, "trade_count": 0}

    # Determine PnL field
    pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
    pnls = [_safe_float(t.get(pnl_field, t.get("pnl_pts", 0))) for t in trades]
    pnl_usd = [_safe_float(t.get("pnl_usd", 0)) for t in trades]
    durations = [_safe_float(t.get("duration_min", 0)) for t in trades]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_usd = [p for p in pnl_usd if p > 0]
    loss_usd = [p for p in pnl_usd if p < 0]

    win_rate = len(wins) / len(pnls) if pnls else 0
    total_pnl = sum(pnls)
    total_pnl_usd = sum(pnl_usd)
    gross_wins = sum(win_usd) if win_usd else 0
    gross_losses = abs(sum(loss_usd)) if loss_usd else 0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Duration stats
    avg_dur = statistics.mean(durations) if durations else 0
    med_dur = statistics.median(durations) if durations else 0

    # Rolling last-20 metrics
    last_20 = pnls[-20:]
    last_20_usd = pnl_usd[-20:]
    rolling_wr = sum(1 for p in last_20 if p > 0) / len(last_20) if last_20 else 0
    rolling_pnl_usd = sum(last_20_usd)

    # Entry hour distribution (UTC)
    hour_dist = {}
    for t in trades:
        ts_str = t.get("ts", t.get("timestamp", ""))
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                h = ts.hour
                hour_dist[h] = hour_dist.get(h, 0) + 1
            except Exception:
                pass

    # Max drawdown (sequential peak-to-trough)
    cumulative = []
    running = 0
    for p in pnl_usd:
        running += p
        cumulative.append(running)
    peak = 0
    max_dd = 0
    for c in cumulative:
        peak = max(peak, c)
        dd = peak - c
        max_dd = max(max_dd, dd)

    # Consecutive losses
    max_consec_loss = 0
    current_streak = 0
    for p in pnls:
        if p <= 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    return {
        "label": label,
        "trade_count": len(trades),
        "win_rate": round(win_rate, 3),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_usd": round(total_pnl_usd, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
        "avg_win": round(statistics.mean(wins), 2) if wins else 0,
        "avg_loss": round(statistics.mean(losses), 2) if losses else 0,
        "avg_win_usd": round(statistics.mean(win_usd), 2) if win_usd else 0,
        "avg_loss_usd": round(statistics.mean(loss_usd), 2) if loss_usd else 0,
        "max_drawdown_usd": round(max_dd, 2),
        "max_consecutive_losses": max_consec_loss,
        "avg_duration_min": round(avg_dur, 1),
        "median_duration_min": round(med_dur, 1),
        "exit_reasons": exit_reasons,
        "entry_hour_dist": {str(k): v for k, v in sorted(hour_dist.items())},
        "rolling_20_win_rate": round(rolling_wr, 3),
        "rolling_20_pnl_usd": round(rolling_pnl_usd, 2),
    }


def generate_report() -> dict:
    """Generate fleet-wide and per-instrument trade metrics."""
    all_trades = []
    per_instrument = {}

    if not LOGS_ROOT.exists():
        return {"timestamp": datetime.now(timezone.utc).isoformat(), "fleet": {}, "instruments": {}}

    for log_dir in sorted(LOGS_ROOT.iterdir()):
        if not log_dir.is_dir() or log_dir.name.startswith("_"):
            continue
        trades = _read_trades(log_dir)
        if trades:
            symbol = log_dir.name.upper()
            per_instrument[symbol] = _compute_metrics(trades, symbol)
            all_trades.extend(trades)

    fleet = _compute_metrics(all_trades, "FLEET")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fleet": fleet,
        "instruments": per_instrument,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def main():
    print("=" * 55)
    print(f"  Trade Metrics — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 55)

    report = generate_report()
    fleet = report["fleet"]

    if fleet.get("trade_count", 0) == 0:
        print("\n  No trades found.")
        return

    print(f"\n  Fleet Summary ({fleet['trade_count']} trades):")
    print(f"    Win Rate: {fleet['win_rate']:.1%}")
    print(f"    PnL: ${fleet['total_pnl_usd']:.2f}")
    print(f"    Profit Factor: {fleet['profit_factor']}")
    print(f"    Max DD: ${fleet['max_drawdown_usd']:.2f}")
    print(f"    Max Consec Losses: {fleet['max_consecutive_losses']}")
    print(f"    Avg Duration: {fleet['avg_duration_min']:.1f} min")
    print(f"    Rolling 20 WR: {fleet['rolling_20_win_rate']:.1%}")

    for sym, metrics in report["instruments"].items():
        if metrics["trade_count"] == 0:
            continue
        print(f"\n  {sym} ({metrics['trade_count']} trades):")
        print(f"    WR: {metrics['win_rate']:.1%} | PnL: ${metrics['total_pnl_usd']:.2f} | PF: {metrics['profit_factor']}")

    print(f"\n  Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
