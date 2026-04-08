#!/usr/bin/env python3
"""titan/ops/backtest.py -- Swing trade backtester for Titan strategies.

Replays historical data through the swing engine to find edge.

Usage:
    python -m titan.ops.backtest                        # All symbols, all strategies
    python -m titan.ops.backtest --symbols NVDA GDX     # Specific symbols
    python -m titan.ops.backtest --strategy BREAKOUT     # Single strategy
    python -m titan.ops.backtest --min-strength 70       # Higher threshold
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from titan.ops.data_pipeline import UNIVERSE, DATA_DIR, get_data
from titan.strategies.swing_engine import SwingEngine, SwingSignal

RESULTS_DIR = REPO / "titan" / "data" / "backtest_results"


class SwingBacktester:
    """Walk-forward backtest for swing strategies."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.engine = SwingEngine(cfg)
        self.min_strength = cfg.get("min_strength", 60)
        self.max_hold_days = cfg.get("max_hold_days", 20)
        self.warmup_bars = cfg.get("warmup_bars", 60)

    def run(self, symbol: str, daily: pd.DataFrame,
            h4: pd.DataFrame | None = None,
            strategy_filter: str | None = None) -> list[dict]:
        """Backtest a single symbol. Returns list of trade records."""
        if daily is None or len(daily) < self.warmup_bars + 20:
            return []

        trades = []
        position = None  # {direction, entry_price, stop, target, entry_idx, entry_date, strategy}

        for i in range(self.warmup_bars, len(daily)):
            bar_date = daily.index[i]
            close = daily["Close"].iloc[i]
            high = daily["High"].iloc[i]
            low = daily["Low"].iloc[i]

            # Check exit if in position
            if position is not None:
                exit_reason = None
                exit_price = close

                days_held = i - position["entry_idx"]

                if position["direction"] == "long":
                    if low <= position["stop"]:
                        exit_reason = "stop"
                        exit_price = position["stop"]
                    elif high >= position["target"]:
                        exit_reason = "target"
                        exit_price = position["target"]
                elif position["direction"] == "short":
                    if high >= position["stop"]:
                        exit_reason = "stop"
                        exit_price = position["stop"]
                    elif low <= position["target"]:
                        exit_reason = "target"
                        exit_price = position["target"]

                if days_held >= self.max_hold_days:
                    exit_reason = "timeout"
                    exit_price = close

                if exit_reason:
                    if position["direction"] == "long":
                        pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                    else:
                        pnl_pct = (position["entry_price"] - exit_price) / position["entry_price"] * 100

                    trades.append({
                        "symbol": symbol,
                        "strategy": position["strategy"],
                        "direction": position["direction"],
                        "entry_date": str(position["entry_date"])[:10],
                        "exit_date": str(bar_date)[:10],
                        "entry_price": round(position["entry_price"], 2),
                        "exit_price": round(exit_price, 2),
                        "stop_price": round(position["stop"], 2),
                        "target_price": round(position["target"], 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "exit_reason": exit_reason,
                        "days_held": days_held,
                        "strength": position["strength"],
                        "risk_reward": position["risk_reward"],
                        "reason": position["reason"],
                    })
                    position = None

            # Evaluate for new entry (only if flat)
            if position is None:
                # Build lookback slices
                daily_slice = daily.iloc[max(0, i - 250):i + 1]
                h4_slice = None
                if h4 is not None and len(h4) > 30:
                    # Find h4 bars up to this daily bar's date (normalize tz)
                    try:
                        bd = pd.Timestamp(bar_date)
                        if h4.index.tz is not None and bd.tz is None:
                            bd = bd.tz_localize(h4.index.tz)
                        elif h4.index.tz is None and bd.tz is not None:
                            bd = bd.tz_localize(None)
                        h4_mask = h4.index <= bd
                        if h4_mask.sum() >= 30:
                            h4_slice = h4.loc[h4_mask].iloc[-250:]
                    except Exception:
                        pass  # skip h4 for this bar

                signal = self.engine.evaluate(symbol, daily_slice, h4_slice)

                if signal and signal.strength >= self.min_strength:
                    if strategy_filter and signal.strategy != strategy_filter:
                        continue

                    position = {
                        "direction": signal.direction,
                        "entry_price": signal.entry_price,
                        "stop": signal.stop_price,
                        "target": signal.target_price,
                        "entry_idx": i,
                        "entry_date": bar_date,
                        "strategy": signal.strategy,
                        "strength": signal.strength,
                        "risk_reward": signal.risk_reward,
                        "reason": signal.reason,
                    }

        return trades


def summarize_trades(trades: list[dict], label: str = "") -> dict:
    """Generate summary statistics from trade list."""
    if not trades:
        return {"label": label, "trades": 0}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    exits = defaultdict(int)
    strategies = defaultdict(int)
    for t in trades:
        exits[t["exit_reason"]] += 1
        strategies[t["strategy"]] += 1

    peak = 0
    equity = 0
    max_dd = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "label": label,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 999,
        "total_pnl_pct": round(sum(pnls), 2),
        "avg_win_pct": round(np.mean(wins), 2) if wins else 0,
        "avg_loss_pct": round(np.mean(losses), 2) if losses else 0,
        "max_dd_pct": round(max_dd, 2),
        "avg_days_held": round(np.mean([t["days_held"] for t in trades]), 1),
        "expectancy_pct": round(np.mean(pnls), 3),
        "exits": dict(exits),
        "strategies": dict(strategies),
    }


def main():
    parser = argparse.ArgumentParser(description="Titan Swing Backtester")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols")
    parser.add_argument("--strategy", help="Filter to one strategy (TREND_FOLLOW, BREAKOUT, MEAN_REVERSION, TRENDLINE)")
    parser.add_argument("--min-strength", type=int, default=60, help="Min signal strength (default: 60)")
    parser.add_argument("--max-hold", type=int, default=20, help="Max hold days (default: 20)")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    symbols = args.symbols or [s for s, info in UNIVERSE.items() if info["tier"] <= 2]
    config = {"min_strength": args.min_strength, "max_hold_days": args.max_hold}
    bt = SwingBacktester(config)

    print(f"{'=' * 70}")
    print(f"TITAN SWING BACKTEST")
    print(f"Symbols: {len(symbols)} | Strategy: {args.strategy or 'ALL'} | "
          f"Min strength: {args.min_strength} | Max hold: {args.max_hold}d")
    print(f"{'=' * 70}")

    all_trades = []
    per_symbol = {}

    for sym in symbols:
        daily = get_data(sym, "daily")
        h4 = get_data(sym, "4h")

        if daily is None:
            print(f"  {sym}: no data")
            continue

        trades = bt.run(sym, daily, h4, strategy_filter=args.strategy)
        all_trades.extend(trades)
        per_symbol[sym] = trades

        s = summarize_trades(trades, sym)
        if s["trades"] > 0:
            print(f"  {sym:8s} | {s['trades']:3d} trades | WR {s['win_rate']:5.1f}% | "
                  f"PF {s['profit_factor']:5.2f} | PnL {s['total_pnl_pct']:+7.1f}% | "
                  f"Avg hold {s['avg_days_held']:.0f}d | DD {s['max_dd_pct']:.1f}%")
        else:
            print(f"  {sym:8s} | no trades")

    # Fleet summary
    print(f"\n{'=' * 70}")
    print("FLEET SUMMARY")
    print(f"{'=' * 70}")
    fleet = summarize_trades(all_trades, "FLEET")
    if fleet["trades"] > 0:
        print(f"  Total trades:  {fleet['trades']}")
        print(f"  Win rate:      {fleet['win_rate']}%")
        print(f"  Profit factor: {fleet['profit_factor']}")
        print(f"  Total PnL:     {fleet['total_pnl_pct']:+.1f}%")
        print(f"  Expectancy:    {fleet['expectancy_pct']:+.3f}%/trade")
        print(f"  Max drawdown:  {fleet['max_dd_pct']:.1f}%")
        print(f"  Avg hold:      {fleet['avg_days_held']:.1f} days")
        print(f"  Exits:         {fleet['exits']}")
        print(f"  Strategies:    {fleet['strategies']}")

        # Per-strategy breakdown
        print(f"\nPer-Strategy:")
        for strat in set(t["strategy"] for t in all_trades):
            strat_trades = [t for t in all_trades if t["strategy"] == strat]
            s = summarize_trades(strat_trades, strat)
            print(f"  {strat:18s} | {s['trades']:3d} trades | WR {s['win_rate']:5.1f}% | "
                  f"PF {s['profit_factor']:5.2f} | PnL {s['total_pnl_pct']:+7.1f}% | "
                  f"Exp {s['expectancy_pct']:+.3f}%/trade")

        # Per-direction breakdown
        print(f"\nPer-Direction:")
        for d in ["long", "short"]:
            d_trades = [t for t in all_trades if t["direction"] == d]
            s = summarize_trades(d_trades, d)
            if s["trades"] > 0:
                print(f"  {d:8s} | {s['trades']:3d} trades | WR {s['win_rate']:5.1f}% | "
                      f"PF {s['profit_factor']:5.2f} | PnL {s['total_pnl_pct']:+7.1f}%")
    else:
        print("  No trades generated.")

    # Save results
    if all_trades:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        trades_path = RESULTS_DIR / f"trades_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(trades_path, index=False)

        summary_path = RESULTS_DIR / f"summary_{ts}.json"
        summary_path.write_text(json.dumps({
            "fleet": fleet,
            "per_symbol": {sym: summarize_trades(trades, sym) for sym, trades in per_symbol.items()},
            "config": config,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))

        print(f"\n  Trades saved: {trades_path}")
        print(f"  Summary saved: {summary_path}")


if __name__ == "__main__":
    main()
