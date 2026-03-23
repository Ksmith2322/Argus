"""Volume Profile (POC/VAH/VAL) Trading Strategy.

MambaFX concept: compute where volume concentrates at each price level.
POC = fair value (mean reversion target)
VAH/VAL = edges of value area (entry zones)

Strategy: mean reversion — short at VAH toward POC, long at VAL toward POC.

Usage:
    python -m argus_flow.strategies.volume_profile --bars argus_flow/data/ibkr_MNQ_nasdaq_micro_1m.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def calculate_volume_profile(df: pd.DataFrame, start_idx: int, end_idx: int,
                              num_bins: int = 50) -> dict | None:
    """Calculate volume profile for a range of bars."""
    chunk = df.iloc[start_idx:end_idx]
    if len(chunk) < 20:
        return None

    price_min = chunk["low"].min()
    price_max = chunk["high"].max()
    if price_max <= price_min:
        return None

    bin_size = (price_max - price_min) / num_bins
    bins = np.zeros(num_bins)
    bin_prices = np.array([price_min + (i + 0.5) * bin_size for i in range(num_bins)])

    for _, row in chunk.iterrows():
        vol = row.get("volume", 1)
        if vol <= 0:
            vol = row["high"] - row["low"]  # use range as proxy if no volume
            if vol <= 0:
                vol = 0.0001

        low_bin = max(0, int((row["low"] - price_min) / bin_size))
        high_bin = min(num_bins - 1, int((row["high"] - price_min) / bin_size))

        if high_bin >= low_bin:
            per_bin = vol / (high_bin - low_bin + 1)
            for b in range(low_bin, high_bin + 1):
                bins[b] += per_bin

    if bins.sum() <= 0:
        return None

    # POC = highest volume bin
    poc_idx = np.argmax(bins)
    poc = float(bin_prices[poc_idx])

    # Value area = 70% of total volume centered on POC
    total_vol = bins.sum()
    target_vol = total_vol * 0.70

    low_idx = poc_idx
    high_idx = poc_idx
    va_vol = bins[poc_idx]

    while va_vol < target_vol:
        look_up = bins[high_idx + 1] if high_idx + 1 < num_bins else 0
        look_down = bins[low_idx - 1] if low_idx > 0 else 0

        if look_up >= look_down and high_idx + 1 < num_bins:
            high_idx += 1
            va_vol += look_up
        elif low_idx > 0:
            low_idx -= 1
            va_vol += look_down
        else:
            break

    return {
        "poc": poc,
        "vah": float(bin_prices[high_idx]),
        "val": float(bin_prices[low_idx]),
        "total_volume": float(total_vol),
        "bin_prices": bin_prices.tolist(),
        "bins": bins.tolist(),
    }


def find_vp_entries(df: pd.DataFrame, profile_lookback: int = 240,
                     update_every: int = 30, session_start: int = 13,
                     session_end: int = 20, entry_buffer_pct: float = 0.0002) -> list[dict]:
    """Find mean-reversion entries at VAH/VAL."""
    entries = []
    last_profile = None
    last_update = 0

    for i in range(profile_lookback, len(df)):
        # Update profile periodically
        if i - last_update >= update_every or last_profile is None:
            last_profile = calculate_volume_profile(df, max(0, i - profile_lookback), i)
            last_update = i

        if last_profile is None:
            continue

        row = df.iloc[i]

        # Session filter
        if "ts" in df.columns:
            try:
                hour = pd.Timestamp(row["ts"]).hour
                if not (session_start <= hour <= session_end):
                    continue
            except Exception:
                pass

        poc = last_profile["poc"]
        vah = last_profile["vah"]
        val = last_profile["val"]
        va_range = vah - val
        if va_range <= 0:
            continue

        px = row["close"]
        buffer = px * entry_buffer_pct

        # Short at VAH (price reaches upper value area boundary)
        if px >= vah - buffer and px <= vah + va_range * 0.3:
            entries.append({
                "index": i,
                "direction": "short",
                "entry_price": float(px),
                "poc": poc,
                "vah": vah,
                "val": val,
                "va_range": va_range,
                "ts": row.get("ts", i),
            })
            last_update = i  # force profile refresh after entry

        # Long at VAL (price reaches lower value area boundary)
        elif px <= val + buffer and px >= val - va_range * 0.3:
            entries.append({
                "index": i,
                "direction": "long",
                "entry_price": float(px),
                "poc": poc,
                "vah": vah,
                "val": val,
                "va_range": va_range,
                "ts": row.get("ts", i),
            })
            last_update = i

    # Deduplicate (min 15 bars between entries)
    deduped = []
    next_allowed = 0
    for e in entries:
        if e["index"] >= next_allowed:
            deduped.append(e)
            next_allowed = e["index"] + 15

    return deduped


def simulate_vp_trades(df: pd.DataFrame, entries: list[dict],
                        timeout_bars: int = 60, fee_rt: float = 0.00003) -> pd.DataFrame:
    """Simulate mean-reversion trades targeting POC."""
    results = []

    for entry in entries:
        idx = entry["index"]
        px = entry["entry_price"]
        direction = entry["direction"]
        poc = entry["poc"]
        va_range = entry["va_range"]

        # Target = POC, Stop = beyond VA boundary by 50% of VA range
        if direction == "long":
            target_px = poc
            stop_px = entry["val"] - va_range * 0.5
        else:
            target_px = poc
            stop_px = entry["vah"] + va_range * 0.5

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
            "entry_px": px, "exit_px": exit_px, "poc": poc,
            "raw_pnl_pct": raw_pnl, "net_pnl_pct": raw_pnl - fee_rt,
        })

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description="Volume Profile Strategy Backtest")
    parser.add_argument("--bars", required=True)
    parser.add_argument("--lookback", type=int, default=240)
    parser.add_argument("--session-start", type=int, default=13)
    parser.add_argument("--session-end", type=int, default=20)
    args = parser.parse_args()

    df = pd.read_csv(args.bars)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

    print(f"Loaded {len(df):,} bars")

    entries = find_vp_entries(df, profile_lookback=args.lookback,
                              session_start=args.session_start, session_end=args.session_end)
    print(f"VP entries: {len(entries)} (long: {sum(1 for e in entries if e['direction']=='long')}, short: {sum(1 for e in entries if e['direction']=='short')})")

    if entries:
        results = simulate_vp_trades(df, entries)
        wr = (results["net_pnl_pct"] > 0).mean()
        tgt = (results["outcome"] == "target").mean()
        stp = (results["outcome"] == "stop").mean()
        tmo = (results["outcome"] == "timeout").mean()
        exp = results["net_pnl_pct"].mean() * 10000
        print(f"Results: n={len(results)} WR={wr:.3f} tgt={tgt:.1%} stp={stp:.1%} tmo={tmo:.1%} exp={exp:+.2f}bps")


if __name__ == "__main__":
    main()