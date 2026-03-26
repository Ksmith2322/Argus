"""Fair Value Gap (FVG) Detection & Trading Strategy.

ICT/Tori Trades concept: 3-candle imbalance pattern where price leaves a gap.
Price tends to return and fill the gap — enter on the retrace.

Bullish FVG: candle1.high < candle3.low (gap up after strong displacement)
Bearish FVG: candle1.low > candle3.high (gap down after strong displacement)

Usage:
    python -m argus_flow.strategies.fvg_detector --bars argus_flow/data/ibkr_MNQ_nasdaq_micro_1m.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def detect_fvgs(df: pd.DataFrame, min_displacement_mult: float = 1.5) -> list[dict]:
    """Detect Fair Value Gaps in OHLC data.

    min_displacement_mult: candle 2 range must be this multiple of avg bar range
    """
    avg_range = (df["high"] - df["low"]).rolling(50, min_periods=20).mean()
    fvgs = []

    for i in range(2, len(df)):
        c1_h = df.at[i - 2, "high"]
        c1_l = df.at[i - 2, "low"]
        c2_o = df.at[i - 1, "open"]
        c2_c = df.at[i - 1, "close"]
        c2_range = df.at[i - 1, "high"] - df.at[i - 1, "low"]
        c3_h = df.at[i, "high"]
        c3_l = df.at[i, "low"]

        ar = avg_range.iloc[i - 1] if not pd.isna(avg_range.iloc[i - 1]) else 0
        if ar <= 0 or c2_range < ar * min_displacement_mult:
            continue

        # Displacement direction: candle 2 must close in the direction of the gap
        c2_bullish = c2_c > c2_o  # closes above open
        c2_bearish = c2_c < c2_o  # closes below open

        # Bullish FVG: requires bullish displacement candle
        if c1_h < c3_l and c2_bullish:
            fvgs.append({
                "type": "bullish",
                "top": float(c3_l),
                "bottom": float(c1_h),
                "mid": float((c3_l + c1_h) / 2),
                "size": float(c3_l - c1_h),
                "index": i,
                "ts": df.at[i, "ts"] if "ts" in df.columns else i,
            })

        # Bearish FVG: requires bearish displacement candle
        if c1_l > c3_h and c2_bearish:
            fvgs.append({
                "type": "bearish",
                "top": float(c1_l),
                "bottom": float(c3_h),
                "mid": float((c1_l + c3_h) / 2),
                "size": float(c1_l - c3_h),
                "index": i,
                "ts": df.at[i, "ts"] if "ts" in df.columns else i,
            })

    return fvgs


def find_fvg_fill_entries(df: pd.DataFrame, fvgs: list[dict], max_wait_bars: int = 60,
                           session_start: int = 8, session_end: int = 20) -> list[dict]:
    """Find entries when price retraces to fill an FVG."""
    entries = []
    used_fvgs = set()

    for fvg in fvgs:
        fvg_idx = fvg["index"]
        if fvg_idx in used_fvgs:
            continue

        for j in range(fvg_idx + 1, min(fvg_idx + max_wait_bars, len(df))):
            row = df.iloc[j]

            # Session filter (supports wraparound e.g. 22-08)
            if "ts" in df.columns:
                try:
                    hour = pd.Timestamp(row["ts"]).hour
                    if session_start <= session_end:
                        if not (session_start <= hour <= session_end):
                            continue
                    else:  # wraparound
                        if not (hour >= session_start or hour <= session_end):
                            continue
                except Exception:
                    pass

            # Check if price retraces to FVG mid (consequent encroachment)
            if fvg["type"] == "bullish" and row["low"] <= fvg["mid"]:
                entries.append({
                    "index": j,
                    "direction": "long",
                    "entry_price": fvg["mid"],
                    "fvg_top": fvg["top"],
                    "fvg_bottom": fvg["bottom"],
                    "fvg_size": fvg["size"],
                    "ts": row.get("ts", j),
                })
                used_fvgs.add(fvg_idx)
                break

            elif fvg["type"] == "bearish" and row["high"] >= fvg["mid"]:
                entries.append({
                    "index": j,
                    "direction": "short",
                    "entry_price": fvg["mid"],
                    "fvg_top": fvg["top"],
                    "fvg_bottom": fvg["bottom"],
                    "fvg_size": fvg["size"],
                    "ts": row.get("ts", j),
                })
                used_fvgs.add(fvg_idx)
                break

            # FVG invalidated if price closes beyond it
            if fvg["type"] == "bullish" and row["close"] < fvg["bottom"]:
                break
            if fvg["type"] == "bearish" and row["close"] > fvg["top"]:
                break

    return entries


def simulate_fvg_trades(df: pd.DataFrame, entries: list[dict],
                         rr_mult: float = 2.0, timeout_bars: int = 30,
                         fee_rt: float = 0.00003) -> pd.DataFrame:
    """Simulate trades from FVG entries."""
    results = []

    for entry in entries:
        idx = entry["index"]
        px = entry["entry_price"]
        direction = entry["direction"]
        fvg_size = entry["fvg_size"]

        # Stop beyond FVG, target = RR * stop distance
        if direction == "long":
            stop_px = entry["fvg_bottom"] - fvg_size * 0.1
            stop_dist = px - stop_px
            target_px = px + stop_dist * rr_mult
        else:
            stop_px = entry["fvg_top"] + fvg_size * 0.1
            stop_dist = stop_px - px
            target_px = px - stop_dist * rr_mult

        outcome = "timeout"
        exit_px = px
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
            raw_pnl = (exit_px - px) / px
        else:
            raw_pnl = (px - exit_px) / px

        results.append({
            "index": idx, "direction": direction, "outcome": outcome,
            "entry_px": px, "exit_px": exit_px,
            "raw_pnl_pct": raw_pnl, "net_pnl_pct": raw_pnl - fee_rt,
        })

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description="FVG Detection & Backtest")
    parser.add_argument("--bars", required=True, help="Path to 1-min bars CSV")
    parser.add_argument("--session-start", type=int, default=8)
    parser.add_argument("--session-end", type=int, default=20)
    args = parser.parse_args()

    df = pd.read_csv(args.bars)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

    print(f"Loaded {len(df):,} bars")

    fvgs = detect_fvgs(df)
    print(f"FVGs detected: {len(fvgs)} (bullish: {sum(1 for f in fvgs if f['type']=='bullish')}, bearish: {sum(1 for f in fvgs if f['type']=='bearish')})")

    entries = find_fvg_fill_entries(df, fvgs, session_start=args.session_start, session_end=args.session_end)
    print(f"Fill entries: {len(entries)}")

    if entries:
        results = simulate_fvg_trades(df, entries)
        wr = (results["net_pnl_pct"] > 0).mean()
        tgt = (results["outcome"] == "target").mean()
        stp = (results["outcome"] == "stop").mean()
        tmo = (results["outcome"] == "timeout").mean()
        exp = results["net_pnl_pct"].mean() * 10000
        print(f"Results: n={len(results)} WR={wr:.3f} tgt={tgt:.1%} stp={stp:.1%} tmo={tmo:.1%} exp={exp:+.2f}bps")


if __name__ == "__main__":
    main()