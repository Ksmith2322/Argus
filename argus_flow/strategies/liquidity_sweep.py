"""Liquidity Sweep Detection & Trading Strategy.

ICT concept: price wicks beyond swing highs/lows (running stops) then reverses.
The "smart money" needs liquidity — they run stops before the real move.

Entry: OPPOSITE direction of the sweep (sweep highs = SHORT, sweep lows = LONG)

Usage:
    python -m argus_flow.strategies.liquidity_sweep --bars argus_flow/data/ibkr_MNQ_nasdaq_micro_1m.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def find_swing_points(df: pd.DataFrame, lookback: int = 20) -> tuple[list[dict], list[dict]]:
    """Find swing highs and lows."""
    highs = []
    lows = []

    for i in range(lookback, len(df) - lookback):
        is_high = True
        is_low = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if j < 0 or j >= len(df):
                continue
            if df.at[j, "high"] > df.at[i, "high"]:
                is_high = False
            if df.at[j, "low"] < df.at[i, "low"]:
                is_low = False

        if is_high:
            highs.append({"price": float(df.at[i, "high"]), "index": i})
        if is_low:
            lows.append({"price": float(df.at[i, "low"]), "index": i})

    return highs, lows


def detect_sweeps(df: pd.DataFrame, swing_highs: list[dict], swing_lows: list[dict],
                   wick_ratio_min: float = 0.3, max_lookback: int = 100,
                   session_start: int = 8, session_end: int = 20) -> list[dict]:
    """Detect liquidity sweeps — wicks beyond swings that close back inside."""
    sweeps = []
    used_swings = set()

    for i in range(len(df)):
        row = df.iloc[i]

        # Session filter
        if "ts" in df.columns:
            try:
                hour = pd.Timestamp(row["ts"]).hour
                if not (session_start <= hour <= session_end):
                    continue
            except Exception:
                pass

        body_top = max(row["open"], row["close"])
        body_bottom = min(row["open"], row["close"])
        body = body_top - body_bottom

        if body <= 0:
            continue

        # Buy-side sweep: wick above swing high, close below it
        for sh in swing_highs:
            if sh["index"] in used_swings:
                continue
            if i - sh["index"] > max_lookback or i <= sh["index"]:
                continue

            if row["high"] > sh["price"] and body_top < sh["price"]:
                upper_wick = row["high"] - body_top
                if upper_wick / body >= wick_ratio_min:
                    sweeps.append({
                        "type": "buy_side_sweep",
                        "direction": "short",  # fade the sweep
                        "swept_level": sh["price"],
                        "wick_high": float(row["high"]),
                        "close": float(row["close"]),
                        "index": i,
                        "swing_index": sh["index"],
                        "ts": row.get("ts", i),
                    })
                    used_swings.add(sh["index"])
                    break

        # Sell-side sweep: wick below swing low, close above it
        for sl in swing_lows:
            if sl["index"] in used_swings:
                continue
            if i - sl["index"] > max_lookback or i <= sl["index"]:
                continue

            if row["low"] < sl["price"] and body_bottom > sl["price"]:
                lower_wick = body_bottom - row["low"]
                if lower_wick / body >= wick_ratio_min:
                    sweeps.append({
                        "type": "sell_side_sweep",
                        "direction": "long",  # fade the sweep
                        "swept_level": sl["price"],
                        "wick_low": float(row["low"]),
                        "close": float(row["close"]),
                        "index": i,
                        "swing_index": sl["index"],
                        "ts": row.get("ts", i),
                    })
                    used_swings.add(sl["index"])
                    break

    return sweeps


def simulate_sweep_trades(df: pd.DataFrame, sweeps: list[dict],
                           rr_mult: float = 2.0, timeout_bars: int = 60,
                           fee_rt: float = 0.00003) -> pd.DataFrame:
    """Simulate trades from sweep signals."""
    results = []

    for sweep in sweeps:
        idx = sweep["index"]
        direction = sweep["direction"]
        entry_px = float(df.at[idx, "close"])  # enter at close of sweep candle

        # Stop beyond the sweep wick
        if direction == "long":
            stop_px = sweep.get("wick_low", entry_px * 0.998)
            stop_dist = entry_px - stop_px
            target_px = entry_px + stop_dist * rr_mult
        else:
            stop_px = sweep.get("wick_high", entry_px * 1.002)
            stop_dist = stop_px - entry_px
            target_px = entry_px - stop_dist * rr_mult

        if stop_dist <= 0:
            continue

        outcome = "timeout"
        exit_px = entry_px
        for j in range(1, timeout_bars + 1):
            bi = idx + j
            if bi >= len(df):
                break
            bar = df.iloc[bi]
            if direction == "long":
                if bar["low"] <= stop_px:
                    outcome, exit_px = "stop", stop_px
                    break
                if bar["high"] >= target_px:
                    outcome, exit_px = "target", target_px
                    break
            else:
                if bar["high"] >= stop_px:
                    outcome, exit_px = "stop", stop_px
                    break
                if bar["low"] <= target_px:
                    outcome, exit_px = "target", target_px
                    break

        if outcome == "timeout":
            exit_px = float(df.at[min(idx + timeout_bars, len(df) - 1), "close"])

        if direction == "long":
            raw_pnl = (exit_px - entry_px) / entry_px
        else:
            raw_pnl = (entry_px - exit_px) / entry_px

        results.append({
            "index": idx, "direction": direction, "outcome": outcome,
            "sweep_type": sweep["type"],
            "entry_px": entry_px, "exit_px": exit_px,
            "raw_pnl_pct": raw_pnl, "net_pnl_pct": raw_pnl - fee_rt,
        })

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description="Liquidity Sweep Detection & Backtest")
    parser.add_argument("--bars", required=True)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--session-start", type=int, default=8)
    parser.add_argument("--session-end", type=int, default=20)
    args = parser.parse_args()

    df = pd.read_csv(args.bars)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

    print(f"Loaded {len(df):,} bars")

    highs, lows = find_swing_points(df, lookback=args.lookback)
    print(f"Swing points: {len(highs)} highs, {len(lows)} lows")

    sweeps = detect_sweeps(df, highs, lows, session_start=args.session_start, session_end=args.session_end)
    print(f"Sweeps: {len(sweeps)} (buy-side: {sum(1 for s in sweeps if s['type']=='buy_side_sweep')}, sell-side: {sum(1 for s in sweeps if s['type']=='sell_side_sweep')})")

    if sweeps:
        results = simulate_sweep_trades(df, sweeps)
        wr = (results["net_pnl_pct"] > 0).mean()
        tgt = (results["outcome"] == "target").mean()
        stp = (results["outcome"] == "stop").mean()
        tmo = (results["outcome"] == "timeout").mean()
        exp = results["net_pnl_pct"].mean() * 10000
        print(f"Results: n={len(results)} WR={wr:.3f} tgt={tgt:.1%} stp={stp:.1%} tmo={tmo:.1%} exp={exp:+.2f}bps")


if __name__ == "__main__":
    main()