"""Live Drift Report — compare live trading behavior against research baseline.

Answers: "Are we still trading the same thing we validated?"

Usage:
    python -m argus_flow.ops.drift_report
    python -m argus_flow.ops.drift_report --symbol EURUSD
"""
from __future__ import annotations

import csv
import json
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"

# Research baselines (from validated backtests)
RESEARCH_BASELINES = {
    "EURUSD": {
        "expectancy": 4.19, "win_rate": 0.56, "timeout_rate": 0.67,
        "short_exp": 6.08, "long_exp": 0.05,
        "core_session_exp": 4.85, "pf": 1.75,
    },
    "GBPUSD": {
        "expectancy": 9.45, "win_rate": 0.60, "timeout_rate": 0.73,
        "short_exp": 14.99, "long_exp": 4.60,
        "core_session_exp": 11.97, "pf": 3.20,
    },
}


def load_valid_trades(symbol: str) -> list[dict]:
    log_dir = LOGS / symbol.lower()
    tf = log_dir / "trades.csv"
    if not tf.exists():
        return []
    with open(tf) as f:
        trades = list(csv.DictReader(f))
    return [t for t in trades if t.get("experiment_valid", "").lower() == "true"]


def compute_drift(symbol: str) -> dict:
    """Compare live metrics against research baseline."""
    trades = load_valid_trades(symbol)
    baseline = RESEARCH_BASELINES.get(symbol, {})

    result = {
        "symbol": symbol,
        "live_trades": len(trades),
        "has_baseline": bool(baseline),
        "drift_warnings": [],
        "metrics": {},
    }

    if len(trades) < 5:
        result["status"] = "INSUFFICIENT"
        return result

    pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
    pnls = [float(t.get(pnl_field, 0)) for t in trades]
    wins = len([p for p in pnls if p > 0])

    # Live metrics
    live = {
        "expectancy": round(np.mean(pnls), 2),
        "win_rate": round(wins / len(pnls), 3),
        "pf": 0,
        "net_pnl": round(sum(pnls), 1),
    }

    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p <= 0]
    live["pf"] = round(sum(w) / abs(sum(l)), 2) if l and sum(l) != 0 else 999

    # Exit breakdown
    exits = defaultdict(int)
    for t in trades:
        exits[t.get("exit_reason", "?")] += 1
    live["timeout_rate"] = round(exits.get("timeout", 0) / len(trades), 2)
    live["stop_rate"] = round(exits.get("stop", 0) / len(trades), 2)
    live["target_rate"] = round(exits.get("target", 0) / len(trades), 2)

    # Side split
    shorts = [t for t in trades if t.get("direction", "").lower() == "short"]
    longs = [t for t in trades if t.get("direction", "").lower() == "long"]
    live["short_count"] = len(shorts)
    live["long_count"] = len(longs)
    live["short_exp"] = round(np.mean([float(t.get(pnl_field, 0)) for t in shorts]), 2) if shorts else 0
    live["long_exp"] = round(np.mean([float(t.get(pnl_field, 0)) for t in longs]), 2) if longs else 0

    # Session split
    core = []
    for t in trades:
        try:
            h = int(t.get("ts", "")[11:13])
            if 8 <= h <= 14:
                core.append(float(t.get(pnl_field, 0)))
        except (ValueError, IndexError):
            pass
    live["core_session_exp"] = round(np.mean(core), 2) if core else 0
    live["core_session_count"] = len(core)

    # Concentration
    sorted_pnls = sorted(pnls, reverse=True)
    total = sum(pnls)
    if total != 0 and len(pnls) >= 3:
        top3 = sum(sorted_pnls[:3])
        live["top3_concentration"] = round(top3 / total * 100, 0) if total > 0 else 0
    else:
        live["top3_concentration"] = 0

    # Average hold time
    durations = []
    for t in trades:
        try:
            durations.append(float(t.get("duration_min", 0)))
        except (ValueError, TypeError):
            pass
    live["avg_hold_min"] = round(np.mean(durations), 1) if durations else 0

    result["metrics"] = live

    # Drift comparison against baseline
    if baseline:
        diffs = {}
        for key in ["expectancy", "win_rate", "timeout_rate", "short_exp", "long_exp", "core_session_exp"]:
            if key in baseline and key in live:
                b = baseline[key]
                l = live[key]
                diff = l - b
                pct = (diff / abs(b) * 100) if b != 0 else 0
                diffs[key] = {"baseline": b, "live": l, "diff": round(diff, 2), "pct": round(pct, 0)}

                # Warning thresholds
                if key == "expectancy" and l < b * 0.5 and len(trades) >= 10:
                    result["drift_warnings"].append(f"expectancy {l:+.2f} < 50% of baseline {b:+.2f}")
                if key == "win_rate" and abs(l - b) > 0.15:
                    result["drift_warnings"].append(f"win_rate drift: live {l:.0%} vs baseline {b:.0%} (>{15}pp)")
                if key == "timeout_rate" and l > b + 0.15:
                    result["drift_warnings"].append(f"timeout_rate rising: live {l:.0%} vs baseline {b:.0%}")

        result["drift_comparison"] = diffs

    result["status"] = "WARN" if result["drift_warnings"] else "OK"
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Live Drift Report")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else ["EURUSD", "GBPUSD", "USDJPY"]

    print(f"\n{'='*65}")
    print(f"  LIVE DRIFT REPORT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*65}")

    for sym in symbols:
        r = compute_drift(sym)
        status_colors = {"OK": "\033[32m", "WARN": "\033[33m", "INSUFFICIENT": "\033[90m"}
        c = status_colors.get(r["status"], "")
        reset = "\033[0m"

        print(f"\n  {sym}: {c}{r['status']}{reset} ({r['live_trades']} valid trades)")

        if r["status"] == "INSUFFICIENT":
            print(f"    Need 5+ valid trades for drift analysis")
            continue

        m = r["metrics"]
        print(f"    Exp={m['expectancy']:+.2f}  WR={m['win_rate']:.0%}  PF={m['pf']:.2f}  TO={m['timeout_rate']:.0%}")
        print(f"    Short: {m['short_count']} ({m['short_exp']:+.2f}/trade)  Long: {m['long_count']} ({m['long_exp']:+.2f}/trade)")
        print(f"    Core session: {m['core_session_count']} trades ({m['core_session_exp']:+.2f}/trade)")
        print(f"    Avg hold: {m['avg_hold_min']:.0f}min  Top3 concentration: {m['top3_concentration']:.0f}%")

        if r.get("drift_comparison"):
            print(f"    --- Drift vs Research ---")
            for key, d in r["drift_comparison"].items():
                marker = " !!!" if abs(d["pct"]) > 50 else ""
                print(f"      {key}: {d['baseline']} -> {d['live']} ({d['pct']:+.0f}%){marker}")

        for w in r.get("drift_warnings", []):
            print(f"    \033[33mWARN: {w}\033[0m")

    # Save report
    out_dir = LOGS / "drift"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    reports = [compute_drift(sym) for sym in symbols]
    out_file = out_dir / f"drift_{today}.json"
    out_file.write_text(json.dumps(reports, indent=2, default=str) + "\n")
    print(f"\n  Saved: {out_file}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
