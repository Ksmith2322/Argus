"""Asset Fitness Scorer — evaluate how well each instrument fits its strategy.

Reads trade history and signal data to score each asset on:
1. Strategy fit: does the asset's behavior match what the strategy needs?
2. Signal quality: are entry signals leading to profitable trades?
3. Regime alignment: is the asset in a regime that suits the strategy?
4. Risk profile: are stops/targets appropriately sized?

Usage:
    python -m argus_flow.ops.asset_fitness
    python -m argus_flow.ops.asset_fitness --min-trades 10
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

INSTRUMENTS = [
    # FX
    {"symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd", "strategy": "range_accel", "asset_class": "fx"},
    {"symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd", "strategy": "T4_full_stack", "asset_class": "fx"},
    {"symbol": "EURJPY", "log_dir": "argus_flow/logs/eurjpy", "strategy": "T4_full_stack", "asset_class": "fx"},
    {"symbol": "GBPJPY", "log_dir": "argus_flow/logs/gbpjpy", "strategy": "T4_full_stack", "asset_class": "fx"},
    {"symbol": "CADJPY", "log_dir": "argus_flow/logs/cadjpy", "strategy": "T4_full_stack", "asset_class": "fx"},
    {"symbol": "AUDJPY", "log_dir": "argus_flow/logs/audjpy", "strategy": "T4_full_stack", "asset_class": "fx"},
    {"symbol": "USDJPY", "log_dir": "argus_flow/logs/usdjpy", "strategy": "range_accel", "asset_class": "fx"},
    {"symbol": "AUDUSD", "log_dir": "argus_flow/logs/audusd", "strategy": "range_accel", "asset_class": "fx"},
    # Futures
    {"symbol": "MES", "log_dir": "argus_flow/logs/mes", "strategy": "range_accel", "asset_class": "equity_index"},
    {"symbol": "MNQ", "log_dir": "argus_flow/logs/mnq", "strategy": "range_accel", "asset_class": "equity_index"},
    {"symbol": "MYM", "log_dir": "argus_flow/logs/mym", "strategy": "range_accel", "asset_class": "equity_index"},
    {"symbol": "M2K", "log_dir": "argus_flow/logs/m2k", "strategy": "range_accel", "asset_class": "equity_index"},
    {"symbol": "MGC", "log_dir": "argus_flow/logs/mgc", "strategy": "range_accel", "asset_class": "commodity"},
    {"symbol": "MCL", "log_dir": "argus_flow/logs/mcl", "strategy": "range_accel", "asset_class": "commodity"},
    {"symbol": "NKD", "log_dir": "argus_flow/logs/nkd", "strategy": "range_accel", "asset_class": "equity_index"},
]


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def score_asset(instrument: dict, min_trades: int = 5) -> dict:
    """Score a single asset's fitness for its assigned strategy.

    Returns a dict with scores (0-100) and recommendations.
    """
    log_dir = REPO / instrument["log_dir"]
    all_trades = _load_csv(log_dir / "trades.csv")
    # Score using VALID trades only — contaminated trades distort fitness assessment
    trades = [t for t in all_trades if t.get("experiment_valid", "true").lower() == "true"]
    signals = _load_csv(log_dir / "signals.csv")
    symbol = instrument["symbol"]
    strategy = instrument["strategy"]

    result = {
        "symbol": symbol,
        "strategy": strategy,
        "asset_class": instrument["asset_class"],
        "trade_count": len(trades),
        "signal_count": len(signals),
        "scores": {},
        "overall_score": 0,
        "recommendation": "INSUFFICIENT_DATA",
        "details": {},
    }

    if len(trades) < min_trades:
        result["recommendation"] = f"INSUFFICIENT_DATA (need {min_trades}, have {len(trades)})"
        return result

    # ── 1. Profitability Score (0-100) ──
    pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
    pnls = [float(t.get(pnl_field, 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) if pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else 0
    expectancy = sum(pnls) / len(pnls) if pnls else 0

    # Score: 0 at PF=0, 50 at PF=1.0, 100 at PF=2.0+
    pf_score = min(100, max(0, profit_factor * 50))
    result["scores"]["profitability"] = round(pf_score)
    result["details"]["win_rate"] = round(win_rate, 3)
    result["details"]["profit_factor"] = round(profit_factor, 3)
    result["details"]["expectancy"] = round(expectancy, 3)
    result["details"]["avg_win"] = round(avg_win, 2)
    result["details"]["avg_loss"] = round(avg_loss, 2)

    # ── 2. Exit Quality Score (0-100) ──
    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.get("exit_reason", "unknown")] += 1

    timeout_rate = exit_reasons.get("timeout", 0) / len(trades)
    stop_rate = exit_reasons.get("stop", 0) / len(trades)
    target_rate = exit_reasons.get("target", 0) / len(trades)

    # Good: high target rate, low stop rate. Bad: all timeouts (strategy not decisive)
    exit_score = min(100, max(0, int(target_rate * 200 + (1 - stop_rate) * 50 - timeout_rate * 20)))
    result["scores"]["exit_quality"] = round(exit_score)
    result["details"]["timeout_rate"] = round(timeout_rate, 3)
    result["details"]["stop_rate"] = round(stop_rate, 3)
    result["details"]["target_rate"] = round(target_rate, 3)

    # ── 3. Regime Alignment Score (0-100) ──
    # Check if entry signals align with suitable regime for the strategy
    entry_signals = [s for s in signals if s.get("action") == "ENTRY" and s.get("regime")]
    if entry_signals:
        regime_compat = {
            "range_accel": {"RANGING"},
            "T4_full_stack": {"RANGING", "TRENDING"},
            "range_accel_NY": {"RANGING"},
        }
        suitable = regime_compat.get(strategy, {"RANGING"})
        aligned = sum(1 for s in entry_signals if s.get("regime") in suitable)
        regime_score = int(aligned / len(entry_signals) * 100) if entry_signals else 50
    else:
        regime_score = 50  # no data yet, neutral
    result["scores"]["regime_alignment"] = regime_score

    # Regime distribution
    regime_dist = defaultdict(int)
    for s in entry_signals:
        regime_dist[s.get("regime", "?")] += 1
    result["details"]["regime_distribution"] = dict(regime_dist)

    # ── 4. Consistency Score (0-100) ──
    # Rolling win rate stability — low variance = consistent
    if len(pnls) >= 10:
        window = min(10, len(pnls))
        rolling_wr = []
        for i in range(window, len(pnls) + 1):
            chunk = pnls[i - window:i]
            wr = len([p for p in chunk if p > 0]) / len(chunk)
            rolling_wr.append(wr)
        wr_std = (sum((x - win_rate) ** 2 for x in rolling_wr) / len(rolling_wr)) ** 0.5
        # Lower std = more consistent. Score: 100 at std=0, 0 at std=0.3+
        consistency_score = max(0, min(100, int((1 - wr_std / 0.3) * 100)))
    else:
        consistency_score = 50  # insufficient for rolling analysis
    result["scores"]["consistency"] = consistency_score

    # ── 5. Risk/Reward Score (0-100) ──
    # Is the avg_win / avg_loss ratio healthy?
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    rr_score = min(100, max(0, int(rr_ratio * 50)))
    result["scores"]["risk_reward"] = rr_score
    result["details"]["rr_ratio"] = round(rr_ratio, 3)

    # ── Overall Score (weighted) ──
    weights = {
        "profitability": 0.35,
        "exit_quality": 0.10,
        "regime_alignment": 0.20,
        "consistency": 0.20,
        "risk_reward": 0.15,
    }
    overall = sum(result["scores"][k] * weights[k] for k in weights)
    result["overall_score"] = round(overall)

    # ── Recommendation ──
    if overall >= 70:
        result["recommendation"] = "STRONG_FIT"
    elif overall >= 50:
        result["recommendation"] = "MODERATE_FIT"
    elif overall >= 30:
        result["recommendation"] = "WEAK_FIT — consider strategy change or parameter tuning"
    else:
        result["recommendation"] = "POOR_FIT — pause trading, investigate"

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Asset Fitness Scorer")
    parser.add_argument("--min-trades", type=int, default=15, help="Minimum valid trades for scoring (default 15)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = [score_asset(inst, min_trades=args.min_trades) for inst in INSTRUMENTS]

    if args.json:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "min_trades": args.min_trades,
            "instruments": results,
        }
        out = REPO / "argus_flow" / "logs" / "asset_fitness_report.json"
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report written to {out}")
        return

    # Console output
    print(f"\n{'='*80}")
    print(f" ASSET FITNESS REPORT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f" Min trades for scoring: {args.min_trades}")
    print(f"{'='*80}\n")

    scored = [r for r in results if r["overall_score"] > 0]
    unscored = [r for r in results if r["overall_score"] == 0]

    if scored:
        print(f"{'Symbol':<10} {'Class':<14} {'Strategy':<18} {'Score':>5} {'WR':>6} {'PF':>6} {'Exp':>7} {'Regime':>8} {'Rec'}")
        print("-" * 100)
        for r in sorted(scored, key=lambda x: x["overall_score"], reverse=True):
            d = r["details"]
            print(
                f"{r['symbol']:<10} {r['asset_class']:<14} {r['strategy']:<18} "
                f"{r['overall_score']:>5} {d['win_rate']:>5.0%} {d['profit_factor']:>6.2f} "
                f"{d['expectancy']:>+7.2f} {r['scores']['regime_alignment']:>7}% "
                f"{r['recommendation']}"
            )

    if unscored:
        print(f"\nInsufficient data ({args.min_trades}+ trades needed):")
        for r in unscored:
            print(f"  {r['symbol']:<10} {r['asset_class']:<14} {r['trade_count']} trades")

    # Summary
    if scored:
        avg_score = sum(r["overall_score"] for r in scored) / len(scored)
        print(f"\nFleet average fitness: {avg_score:.0f}/100")
        strong = len([r for r in scored if r["overall_score"] >= 70])
        weak = len([r for r in scored if r["overall_score"] < 30])
        print(f"Strong fit: {strong} | Weak/poor fit: {weak}")


if __name__ == "__main__":
    main()
