#!/usr/bin/env python3
"""Build deterministic OHLCV + delta bars from raw trade data.

Part of Argus Cascade — Phase A (Data Truth).
This is truth infrastructure, not research. Bar construction must be
deterministic and replayable.

Usage:
    python -m argus_flow.capture.build_bars_from_trades \
        --trades data/raw/trades_btc.csv \
        --output data/derived/btc_bars_1s.csv \
        --bar-seconds 1

    # Force tick-rule aggressor inference even if column exists:
    python -m argus_flow.capture.build_bars_from_trades \
        --trades data/raw/trades_btc.csv \
        --output data/derived/btc_bars_1s.csv \
        --infer-aggressor
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


# Only ts, price, qty are hard requirements — aggressor can be inferred
HARD_REQUIRED_COLUMNS = {"ts", "price", "qty"}


def infer_aggressor_tick_rule(df: pd.DataFrame) -> pd.DataFrame:
    """Infer aggressor using tick rule:
    - price > prev_price -> buy
    - price < prev_price -> sell
    - equal -> carry forward last direction
    """
    df = df.copy()
    prev_price = df["price"].shift(1)

    direction = np.where(
        df["price"] > prev_price, "buy",
        np.where(df["price"] < prev_price, "sell", None)
    )

    # Forward-fill for equal ticks, default to "buy" for first row
    direction = pd.Series(direction, index=df.index).ffill().fillna("buy")
    df["aggressor"] = direction.values

    return df


def load_trades(csv_path: Path, force_infer: bool = False) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    missing = HARD_REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")

    df = df.dropna(subset=["ts", "price", "qty"])
    df = df[df["price"] > 0]
    df = df[df["qty"] > 0]
    df = df.sort_values("ts").reset_index(drop=True)

    # Aggressor handling
    has_aggressor = "aggressor" in df.columns and not force_infer

    if has_aggressor:
        df["aggressor"] = df["aggressor"].astype(str).str.lower().str.strip()
        valid_mask = df["aggressor"].isin(["buy", "sell"])
        invalid_count = (~valid_mask).sum()
        if invalid_count > 0:
            print(f"[WARN] {invalid_count:,} trades with invalid aggressor, using tick rule for those.")
            df_valid = df[valid_mask].copy()
            df_invalid = df[~valid_mask].copy()
            df_invalid = infer_aggressor_tick_rule(df_invalid)
            df = pd.concat([df_valid, df_invalid]).sort_values("ts").reset_index(drop=True)
        else:
            df = df[valid_mask]
    else:
        reason = "forced via --infer-aggressor" if force_infer else "no aggressor column found"
        print(f"[WARN] {reason}. Using tick rule inference.")
        df = infer_aggressor_tick_rule(df)

    if df.empty:
        raise ValueError("No valid trades remain after cleaning.")

    return df


def build_bars(trades: pd.DataFrame, bar_seconds: int) -> pd.DataFrame:
    df = trades.copy()

    df["buy_qty"] = np.where(df["aggressor"] == "buy", df["qty"], 0.0)
    df["sell_qty"] = np.where(df["aggressor"] == "sell", df["qty"], 0.0)
    df["signed_qty"] = np.where(df["aggressor"] == "buy", df["qty"], -df["qty"])
    df["notional"] = df["price"] * df["qty"]

    rule = f"{bar_seconds}s"

    bars = (
        df.set_index("ts")
        .resample(rule)
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("qty", "sum"),
            buy_volume=("buy_qty", "sum"),
            sell_volume=("sell_qty", "sum"),
            delta=("signed_qty", "sum"),
            trade_count=("price", "count"),
            notional=("notional", "sum"),
        )
    )

    bars = bars.dropna(subset=["open", "high", "low", "close"]).copy()

    bars["bar_range"] = bars["high"] - bars["low"]
    bars["delta_ratio"] = np.where(
        bars["volume"] > 0,
        bars["delta"] / bars["volume"],
        0.0,
    )
    bars["vwap"] = np.where(
        bars["volume"] > 0,
        bars["notional"] / bars["volume"],
        bars["close"],
    )

    bars = bars.reset_index()

    return bars


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build deterministic OHLCV + delta bars from raw trade data."
    )
    parser.add_argument(
        "--trades",
        required=True,
        help="Path to raw trades CSV.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to output bars CSV.",
    )
    parser.add_argument(
        "--bar-seconds",
        type=int,
        default=1,
        help="Bar size in seconds. Default: 1",
    )
    parser.add_argument(
        "--infer-aggressor",
        action="store_true",
        help="Force tick-rule aggressor inference even if column exists.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    trades_path = Path(args.trades)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trades = load_trades(trades_path, force_infer=args.infer_aggressor)
    bars = build_bars(trades, args.bar_seconds)
    bars.to_csv(output_path, index=False)

    print(f"Loaded trades: {len(trades):,}")
    print(f"Built bars:    {len(bars):,}")
    print(f"Saved bars to: {output_path}")


if __name__ == "__main__":
    main()