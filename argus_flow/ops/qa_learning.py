"""QA Learning Engine — extract insights from paper trades to improve strategy.

Analyzes completed trades to answer:
  1. Entry Attribution: which features predicted wins vs losses?
  2. Regime Performance: how does the strategy perform per market regime?
  3. Session Heatmap: which hours/days produce edge?
  4. Exit Quality: are we leaving money on the table (MFE analysis)?
  5. Variant Comparison: which watcher variant is performing best?

Outputs JSON report for dashboard consumption + CLI summary.

Usage:
    python -m argus_flow.ops.qa_learning
    python -m argus_flow.ops.qa_learning --symbol EURUSD
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"
CONFIGS_DIR = REPO / "argus_flow" / "configs"
OUTPUT_PATH = LOGS_ROOT / "qa_learning_report.json"


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _read_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_signals(log_dir: Path) -> list[dict]:
    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return []
    try:
        with open(sig_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


# ── Analysis functions ─────────────────────────────────────

def _regime_analysis(trades: list[dict]) -> dict:
    """Performance breakdown by market regime at entry."""
    by_regime = defaultdict(list)
    for t in trades:
        regime = t.get("entry_regime", t.get("regime", "unknown")) or "unknown"
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        by_regime[regime].append(pnl)

    result = {}
    for regime, pnls in sorted(by_regime.items()):
        wins = [p for p in pnls if p > 0]
        result[regime] = {
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0,
            "avg_pnl": round(statistics.mean(pnls), 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
            "best": round(max(pnls), 2) if pnls else 0,
            "worst": round(min(pnls), 2) if pnls else 0,
        }
    return result


def _session_heatmap(trades: list[dict]) -> dict:
    """Performance breakdown by entry hour (UTC)."""
    by_hour = defaultdict(list)
    for t in trades:
        ts_str = t.get("ts", t.get("timestamp", ""))
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            hour = dt.hour
        except Exception:
            continue
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        by_hour[hour].append(pnl)

    result = {}
    for hour in range(24):
        pnls = by_hour.get(hour, [])
        if not pnls:
            result[str(hour)] = {"trades": 0}
            continue
        wins = [p for p in pnls if p > 0]
        result[str(hour)] = {
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 3),
            "avg_pnl": round(statistics.mean(pnls), 2),
            "total_pnl": round(sum(pnls), 2),
        }
    return result


def _exit_quality(trades: list[dict]) -> dict:
    """Analyze exit reasons and whether we're leaving money on the table."""
    by_reason = defaultdict(list)
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        by_reason[reason].append(pnl)

    result = {}
    for reason, pnls in sorted(by_reason.items()):
        wins = [p for p in pnls if p > 0]
        result[reason] = {
            "count": len(pnls),
            "pct_of_trades": 0,  # filled below
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0,
            "avg_pnl": round(statistics.mean(pnls), 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
        }

    total = sum(v["count"] for v in result.values())
    for v in result.values():
        v["pct_of_trades"] = round(v["count"] / total, 3) if total > 0 else 0

    return result


def _direction_analysis(trades: list[dict]) -> dict:
    """Performance breakdown by trade direction."""
    by_dir = defaultdict(list)
    for t in trades:
        direction = (t.get("direction", "") or "unknown").upper()
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        by_dir[direction].append(pnl)

    result = {}
    for direction, pnls in sorted(by_dir.items()):
        wins = [p for p in pnls if p > 0]
        result[direction] = {
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0,
            "avg_pnl": round(statistics.mean(pnls), 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
        }
    return result


def _duration_analysis(trades: list[dict]) -> dict:
    """Performance by trade duration buckets."""
    buckets = {"0-15m": [], "15-30m": [], "30-60m": [], "60m+": []}
    for t in trades:
        dur = _safe_float(t.get("duration_min", 0))
        pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
        if dur < 15:
            buckets["0-15m"].append(pnl)
        elif dur < 30:
            buckets["15-30m"].append(pnl)
        elif dur < 60:
            buckets["30-60m"].append(pnl)
        else:
            buckets["60m+"].append(pnl)

    result = {}
    for bucket, pnls in buckets.items():
        if not pnls:
            result[bucket] = {"trades": 0}
            continue
        wins = [p for p in pnls if p > 0]
        result[bucket] = {
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 3),
            "avg_pnl": round(statistics.mean(pnls), 2),
        }
    return result


def _streak_analysis(trades: list[dict]) -> dict:
    """Analyze winning and losing streaks."""
    pnls = [_safe_float(t.get("pnl_pips", t.get("pnl_pts", 0))) for t in trades]
    if not pnls:
        return {}

    max_win_streak = 0
    max_loss_streak = 0
    current_win = 0
    current_loss = 0
    for p in pnls:
        if p > 0:
            current_win += 1
            current_loss = 0
            max_win_streak = max(max_win_streak, current_win)
        else:
            current_loss += 1
            current_win = 0
            max_loss_streak = max(max_loss_streak, current_loss)

    return {
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "current_streak": current_win if current_win > 0 else -current_loss,
    }


def _variant_comparison() -> dict:
    """Compare signal activity across baseline and variant configs."""
    comparisons = {}

    for config_path in sorted(CONFIGS_DIR.glob("*_watcher_v1.json")):
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        deployment = cfg.get("deployment", {})
        if not deployment.get("managed"):
            continue

        symbol = cfg.get("symbol", "").upper()
        variant_type = deployment.get("variant_type", "baseline")
        variant_label = deployment.get("variant_label", variant_type)
        baseline_name = deployment.get("variant_of", "")

        log_dir = LOGS_ROOT / symbol.lower()
        # Variant log dirs may be named differently
        alt_dirs = [
            LOGS_ROOT / f"{symbol.lower()}_{deployment.get('variant_type', '')}",
            LOGS_ROOT / config_path.stem.replace("_watcher_v1", ""),
        ]

        signals = _read_signals(log_dir)
        if not signals:
            for alt in alt_dirs:
                signals = _read_signals(alt)
                if signals:
                    break

        comparisons.setdefault(symbol, {})[variant_type] = {
            "label": variant_label,
            "config": config_path.name,
            "baseline": baseline_name,
            "signal_count": len(signals),
            "trigger_params": {
                "range_pct_min": cfg.get("trigger", {}).get("range_pct_min"),
                "session_start": cfg.get("trigger", {}).get("session_start_utc"),
                "session_end": cfg.get("trigger", {}).get("session_end_utc"),
            },
            "risk_params": {
                "stop_pips": cfg.get("risk", {}).get("stop_pips"),
                "timeout_min": cfg.get("risk", {}).get("timeout_minutes"),
                "gap_min": cfg.get("risk", {}).get("min_signal_gap_minutes"),
            },
        }

    return comparisons


# ── Main report ────────────────────────────────────────────

def generate_report(symbol_filter: str | None = None) -> dict:
    """Generate comprehensive QA learning report."""
    instruments = {}

    for log_dir in sorted(LOGS_ROOT.iterdir()):
        if not log_dir.is_dir() or log_dir.name.startswith("_"):
            continue

        symbol = log_dir.name.upper()
        if symbol_filter and symbol != symbol_filter.upper():
            continue

        trades = _read_trades(log_dir)
        if not trades:
            continue

        instruments[symbol] = {
            "trade_count": len(trades),
            "regime": _regime_analysis(trades),
            "session_heatmap": _session_heatmap(trades),
            "exit_quality": _exit_quality(trades),
            "direction": _direction_analysis(trades),
            "duration": _duration_analysis(trades),
            "streaks": _streak_analysis(trades),
        }

    variant_comparison = _variant_comparison()

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instruments": instruments,
        "variant_comparison": variant_comparison,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def main():
    parser = argparse.ArgumentParser(description="QA Learning Engine")
    parser.add_argument("--symbol", type=str, default=None)
    args = parser.parse_args()

    print("=" * 60)
    print(f"  QA Learning Engine -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    report = generate_report(args.symbol)

    for symbol, data in report["instruments"].items():
        print(f"\n  {symbol} ({data['trade_count']} trades):")

        # Regime breakdown
        if data["regime"]:
            print("    Regime:")
            for regime, stats in data["regime"].items():
                print(f"      {regime:>12s}: {stats['trades']:>3d} trades, WR={stats['win_rate']:.0%}, avg={stats['avg_pnl']:+.1f}")

        # Direction
        if data["direction"]:
            print("    Direction:")
            for d, stats in data["direction"].items():
                print(f"      {d:>8s}: {stats['trades']:>3d} trades, WR={stats['win_rate']:.0%}, avg={stats['avg_pnl']:+.1f}")

        # Exit quality
        if data["exit_quality"]:
            print("    Exit reasons:")
            for reason, stats in data["exit_quality"].items():
                print(f"      {reason:>10s}: {stats['count']:>3d} ({stats['pct_of_trades']:.0%}), WR={stats['win_rate']:.0%}, avg={stats['avg_pnl']:+.1f}")

        # Duration
        if data["duration"]:
            print("    Duration:")
            for bucket, stats in data["duration"].items():
                if stats["trades"] > 0:
                    print(f"      {bucket:>8s}: {stats['trades']:>3d} trades, WR={stats['win_rate']:.0%}, avg={stats['avg_pnl']:+.1f}")

        # Streaks
        s = data.get("streaks", {})
        if s:
            print(f"    Streaks: max_win={s.get('max_win_streak',0)}, max_loss={s.get('max_loss_streak',0)}, current={s.get('current_streak',0)}")

    # Variant comparison
    vc = report.get("variant_comparison", {})
    if vc:
        print(f"\n  Watcher Variants:")
        for symbol, variants in sorted(vc.items()):
            print(f"    {symbol}:")
            for vtype, vdata in sorted(variants.items()):
                print(f"      {vdata['label']:>14s}: {vdata['signal_count']} signals")

    print(f"\n  Report: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
