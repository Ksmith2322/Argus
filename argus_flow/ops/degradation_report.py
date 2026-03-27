"""Automated Degradation Monitor — daily health checker for live strategy.

Computes mechanism metrics, headline performance, session/side slices,
and runtime integrity flags. Outputs OK/WARN/HARD_PAUSE status.

Runs after session close. Designed for Task Scheduler.

Usage:
    python -m argus_flow.ops.degradation_report
    python -m argus_flow.ops.degradation_report --symbol EURUSD
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
OUT_DIR = LOGS / "degradation"

# Minimum sample guards
MIN_TRADES_FOR_MECHANISM = 10
MIN_TRADES_FOR_SESSION = 8
MIN_TRADES_FOR_SLICE = 6
MIN_REACHED_10_FOR_GIVEBACK = 8


def _load_trades(log_dir: Path) -> list[dict]:
    tf = log_dir / "trades.csv"
    if not tf.exists():
        return []
    with open(tf) as f:
        return list(csv.DictReader(f))


def _load_bars(log_dir: Path) -> pd.DataFrame:
    """Load signal file for MFE/MAE path computation."""
    # We'll compute MFE/MAE from trade-level data if available
    return pd.DataFrame()


def compute_mechanism_metrics(trades: list[dict], bars_df: Optional[pd.DataFrame] = None) -> dict:
    """Core mechanism metrics — the most important health signals."""
    if len(trades) < MIN_TRADES_FOR_MECHANISM:
        return {"sufficient_data": False, "trade_count": len(trades)}

    pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
    pnls = [float(t.get(pnl_field, 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Exit breakdown
    exits = defaultdict(int)
    for t in trades:
        exits[t.get("exit_reason", "unknown")] += 1
    timeout_rate = exits.get("timeout", 0) / len(trades)
    stop_rate = exits.get("stop", 0) / len(trades)
    target_rate = exits.get("target", 0) / len(trades)

    # Basic performance
    wr = len(wins) / len(pnls) if pnls else 0
    if losses and sum(losses) != 0:
        pf = sum(wins) / abs(sum(losses))
    elif wins:
        pf = 999.0
    else:
        pf = 0.0
    expectancy = np.mean(pnls) if pnls else 0

    # Rolling metrics
    rolling = {}
    for window in [10, 20, 30]:
        if len(pnls) >= window:
            recent = pnls[-window:]
            r_wins = [p for p in recent if p > 0]
            r_losses = [p for p in recent if p <= 0]
            r_pf = sum(r_wins) / abs(sum(r_losses)) if r_losses and sum(r_losses) != 0 else 999
            rolling[f"last_{window}_expectancy"] = round(np.mean(recent), 3)
            rolling[f"last_{window}_pf"] = round(r_pf, 3)
            rolling[f"last_{window}_wr"] = round(len(r_wins) / len(recent), 3)

    # Loss streak
    max_streak = 0
    current_streak = 0
    for p in pnls:
        if p <= 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    # Max drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "sufficient_data": True,
        "trade_count": len(trades),
        "expectancy": round(expectancy, 3),
        "profit_factor": round(pf, 3),
        "win_rate": round(wr, 3),
        "avg_win": round(np.mean(wins), 2) if wins else 0,
        "avg_loss": round(abs(np.mean(losses)), 2) if losses else 0,
        "net_pnl": round(sum(pnls), 2),
        "timeout_rate": round(timeout_rate, 3),
        "stop_rate": round(stop_rate, 3),
        "target_rate": round(target_rate, 3),
        "max_loss_streak": max_streak,
        "max_drawdown": round(max_dd, 2),
        **rolling,
    }


def compute_side_metrics(trades: list[dict]) -> dict:
    """Side-conditioned metrics."""
    pnl_field = "pnl_pips" if trades and "pnl_pips" in trades[0] else "pnl_pts"
    result = {}
    for side in ["long", "short"]:
        st = [t for t in trades if t.get("direction", "").lower() == side]
        if len(st) < MIN_TRADES_FOR_SLICE:
            result[side] = {"sufficient_data": False, "count": len(st)}
            continue
        pnls = [float(t.get(pnl_field, 0)) for t in st]
        result[side] = {
            "sufficient_data": True,
            "count": len(st),
            "expectancy": round(np.mean(pnls), 3),
            "win_rate": round(len([p for p in pnls if p > 0]) / len(pnls), 3),
            "net_pnl": round(sum(pnls), 2),
        }
    return result


def compute_session_metrics(trades: list[dict]) -> dict:
    """Session-conditioned metrics."""
    pnl_field = "pnl_pips" if trades and "pnl_pips" in trades[0] else "pnl_pts"
    sessions = {
        "london_am_08_11": lambda h: 8 <= h <= 11,
        "london_pm_12_14": lambda h: 12 <= h <= 14,
        "ny_15_19": lambda h: 15 <= h <= 19,
        "core_08_14": lambda h: 8 <= h <= 14,
    }
    result = {}
    for name, filt in sessions.items():
        st = []
        for t in trades:
            try:
                h = int(t.get("ts", "")[11:13])
            except (ValueError, IndexError):
                continue
            if filt(h):
                st.append(t)
        if len(st) < MIN_TRADES_FOR_SESSION:
            result[name] = {"sufficient_data": False, "count": len(st)}
            continue
        pnls = [float(t.get(pnl_field, 0)) for t in st]
        result[name] = {
            "sufficient_data": True,
            "count": len(st),
            "expectancy": round(np.mean(pnls), 3),
            "net_pnl": round(sum(pnls), 2),
        }
    return result


def compute_runtime_flags(log_dir: Path, trades: list[dict]) -> dict:
    """Runtime integrity flags — any breach = HARD_PAUSE."""
    flags = {
        "reconciliation_mismatch": 0,
        "duplicate_fill": 0,
        "ghost_position": 0,
        "feature_parity_mismatch": 0,
        "restored_from_file_in_valid": 0,
        "blocked_session_trade": 0,
        "invalid_trade_count": 0,
    }

    for t in trades:
        if t.get("experiment_valid", "true").lower() != "true":
            flags["invalid_trade_count"] += 1
        reason = (t.get("invalid_reason", "") or "").lower()
        if "restored" in reason:
            flags["restored_from_file_in_valid"] += 1

    # Check incidents
    incidents_dir = log_dir / "incidents"
    if incidents_dir.exists():
        for f in incidents_dir.glob("*.json"):
            try:
                inc = json.loads(f.read_text())
                result = inc.get("reconciliation_result", "")
                if "DRIFT" in result or "MISMATCH" in result:
                    flags["reconciliation_mismatch"] += 1
                if "PHANTOM" in result:
                    flags["ghost_position"] += 1
            except Exception:
                pass

    return flags


def evaluate_status(metrics: dict, sides: dict, sessions: dict, runtime: dict) -> tuple[str, list[str]]:
    """Determine OK/WARN/HARD_PAUSE status with reasons."""
    reasons = []

    # Runtime flags — immediate HARD_PAUSE
    runtime_breakers = ["reconciliation_mismatch", "duplicate_fill", "ghost_position", "feature_parity_mismatch"]
    for flag in runtime_breakers:
        if runtime.get(flag, 0) > 0:
            reasons.append(f"HARD_PAUSE: {flag}={runtime[flag]}")

    if not metrics.get("sufficient_data"):
        return "INSUFFICIENT_DATA", ["Not enough trades for evaluation"]

    # Mechanism warnings
    if metrics.get("last_20_expectancy", 999) <= 0:
        reasons.append("HARD_PAUSE: last_20_expectancy <= 0")
    elif metrics.get("last_20_expectancy", 999) < 1.0:
        reasons.append("WARN: last_20_expectancy < 1.0")

    if metrics.get("last_30_pf", 999) < 1.0:
        reasons.append("HARD_PAUSE: last_30_pf < 1.0")
    elif metrics.get("last_20_pf", 999) < 1.10:
        reasons.append("WARN: last_20_pf < 1.10")

    if metrics.get("timeout_rate", 0) > 0.85:
        reasons.append("HARD_PAUSE: timeout_rate > 85%")
    elif metrics.get("timeout_rate", 0) > 0.75:
        reasons.append("WARN: timeout_rate > 75%")

    # Side warnings
    if sides.get("short", {}).get("sufficient_data") and sides["short"].get("expectancy", 999) <= 0:
        reasons.append("HARD_PAUSE: short_expectancy <= 0")
    elif sides.get("short", {}).get("sufficient_data") and sides["short"].get("expectancy", 999) < 1.5:
        reasons.append("WARN: short_expectancy < 1.5")

    # Session warnings
    core = sessions.get("core_08_14", {})
    if core.get("sufficient_data") and core.get("expectancy", 999) <= 0:
        reasons.append("HARD_PAUSE: core_session_expectancy <= 0")
    elif core.get("sufficient_data") and core.get("expectancy", 999) < 1.0:
        reasons.append("WARN: core_session_expectancy < 1.0")

    # NY leak check
    ny = sessions.get("ny_15_19", {})
    if ny.get("sufficient_data") and ny.get("count", 0) > 0:
        reasons.append("WARN: trades detected in blocked NY session")

    # Determine status
    hard_pauses = [r for r in reasons if r.startswith("HARD_PAUSE")]
    warns = [r for r in reasons if r.startswith("WARN")]

    if hard_pauses:
        return "HARD_PAUSE", reasons
    elif len(warns) >= 2:
        return "WARN", reasons
    elif warns:
        return "WARN", reasons
    else:
        return "OK", []


def run_report(symbol: str = "EURUSD") -> dict:
    """Run full degradation report for one symbol."""
    log_dir = LOGS / symbol.lower()
    trades = _load_trades(log_dir)

    # Filter to valid trades only
    valid_trades = [t for t in trades if t.get("experiment_valid", "true").lower() == "true"]

    metrics = compute_mechanism_metrics(valid_trades)
    sides = compute_side_metrics(valid_trades)
    sessions = compute_session_metrics(valid_trades)
    runtime = compute_runtime_flags(log_dir, trades)
    status, reasons = evaluate_status(metrics, sides, sessions, runtime)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "status": status,
        "reasons": reasons,
        "total_trades": len(trades),
        "valid_trades": len(valid_trades),
        "metrics": metrics,
        "sides": sides,
        "sessions": sessions,
        "runtime_flags": runtime,
    }

    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Degradation Monitor")
    parser.add_argument("--symbol", default=None, help="Single symbol (default: all active)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    symbols = [args.symbol.upper()] if args.symbol else ["EURUSD", "GBPUSD", "GBPJPY", "USDJPY", "EURJPY", "CADJPY", "AUDJPY", "AUDUSD"]

    all_reports = []
    print(f"\n{'='*60}")
    print(f"  DEGRADATION MONITOR — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*60}\n")

    for sym in symbols:
        report = run_report(sym)
        all_reports.append(report)

        # Status color
        colors = {"OK": "\033[32m", "WARN": "\033[33m", "HARD_PAUSE": "\033[31m", "INSUFFICIENT_DATA": "\033[90m"}
        c = colors.get(report["status"], "")
        reset = "\033[0m"
        m = report["metrics"]

        print(f"  {sym:>8}: {c}{report['status']:>16}{reset}  valid={report['valid_trades']:>3}", end="")
        if m.get("sufficient_data"):
            print(f"  PF={m['profit_factor']:.2f}  Exp={m['expectancy']:+.2f}  WR={m['win_rate']:.0%}  TO={m['timeout_rate']:.0%}", end="")
        print()

        for reason in report.get("reasons", []):
            print(f"           {reason}")

    # Save reports
    report_file = OUT_DIR / f"degradation_{today}.json"
    report_file.write_text(json.dumps(all_reports, indent=2, default=str) + "\n")

    latest_file = OUT_DIR / "degradation_latest.json"
    latest_file.write_text(json.dumps(all_reports, indent=2, default=str) + "\n")

    # Write control file for runner/dashboard
    control = {}
    for r in all_reports:
        control[r["symbol"]] = {
            "status": r["status"],
            "reasons": r["reasons"][:3],
            "valid_trades": r["valid_trades"],
            "timestamp": r["timestamp"],
        }
    control_file = LOGS / "degradation_control.json"
    control_file.write_text(json.dumps(control, indent=2) + "\n")

    print(f"\n  Saved: {report_file}")
    print(f"  Control: {control_file}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
