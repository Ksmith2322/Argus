#!/usr/bin/env python3
"""Validate raw trades CSV before any downstream processing.

Part of Argus Cascade — Phase 0 (Ingestion Sanity).
If raw truth is bad, replay conclusions are fake.

Usage:
    python -m argus_flow.capture.validate_trades \
        --trades data/raw/trades_btc.csv

Checks:
    1. Required columns exist (ts, price, qty; aggressor optional)
    2. No null price/qty
    3. Timestamps monotonically non-decreasing
    4. No giant timestamp gaps (configurable threshold)
    5. No obvious duplicate rows
    6. Price/qty are positive
    7. Aggressor distribution is sane (if present)
    8. Row count sufficient for research

Exit code 0 = all checks pass. Exit code 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def validate(csv_path: Path, max_gap_seconds: int = 60, min_rows: int = 10000) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []

    # --- Load ---
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return [f"FAIL: Cannot read CSV: {e}"]

    row_count = len(df)
    print(f"Rows:    {row_count:,}")

    # --- Required columns ---
    for col in ["ts", "price", "qty"]:
        if col not in df.columns:
            errors.append(f"FAIL: Missing required column: {col}")

    if errors:
        return errors  # can't proceed without basics

    # --- Parse types ---
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")

    # --- Null checks ---
    ts_nulls = df["ts"].isna().sum()
    price_nulls = df["price"].isna().sum()
    qty_nulls = df["qty"].isna().sum()

    if ts_nulls > 0:
        errors.append(f"FAIL: {ts_nulls:,} null/unparseable timestamps")
    if price_nulls > 0:
        errors.append(f"FAIL: {price_nulls:,} null/unparseable prices")
    if qty_nulls > 0:
        errors.append(f"FAIL: {qty_nulls:,} null/unparseable quantities")

    # --- Positive values ---
    neg_price = (df["price"] <= 0).sum()
    neg_qty = (df["qty"] <= 0).sum()
    if neg_price > 0:
        errors.append(f"FAIL: {neg_price:,} rows with price <= 0")
    if neg_qty > 0:
        errors.append(f"FAIL: {neg_qty:,} rows with qty <= 0")

    # --- Row count ---
    if row_count < min_rows:
        warnings.append(f"WARN: Only {row_count:,} rows (minimum {min_rows:,} recommended for research)")

    # --- Timestamp ordering ---
    df_clean = df.dropna(subset=["ts"]).sort_index()
    ts_sorted = df_clean["ts"].is_monotonic_increasing
    if not ts_sorted:
        out_of_order = (df_clean["ts"].diff().dt.total_seconds() < 0).sum()
        warnings.append(f"WARN: Timestamps not monotonically increasing ({out_of_order:,} reversals). Will sort during bar build.")

    # --- Timestamp gaps ---
    if len(df_clean) > 1:
        gaps = df_clean["ts"].diff().dt.total_seconds()
        max_gap = gaps.max()
        large_gaps = (gaps > max_gap_seconds).sum()

        print(f"Time range: {df_clean['ts'].iloc[0]} to {df_clean['ts'].iloc[-1]}")
        print(f"Max gap:    {max_gap:.1f}s")

        if large_gaps > 0:
            warnings.append(f"WARN: {large_gaps:,} gaps > {max_gap_seconds}s (largest: {max_gap:.1f}s)")

    # --- Duplicates ---
    dupes = df.duplicated().sum()
    if dupes > 0:
        dupe_pct = dupes / row_count * 100
        if dupe_pct > 1:
            errors.append(f"FAIL: {dupes:,} duplicate rows ({dupe_pct:.1f}%)")
        else:
            warnings.append(f"WARN: {dupes:,} duplicate rows ({dupe_pct:.2f}%)")

    # --- Aggressor distribution ---
    has_aggressor = "aggressor" in df.columns
    if has_aggressor:
        agg = df["aggressor"].astype(str).str.lower().str.strip()
        valid = agg.isin(["buy", "sell"])
        valid_count = valid.sum()
        invalid_count = (~valid).sum()

        if valid_count > 0:
            buy_pct = (agg == "buy").sum() / valid_count * 100
            sell_pct = (agg == "sell").sum() / valid_count * 100
            print(f"Aggressor: {valid_count:,} valid (buy={buy_pct:.1f}%, sell={sell_pct:.1f}%)")

            if buy_pct > 90 or sell_pct > 90:
                warnings.append(f"WARN: Extreme aggressor skew (buy={buy_pct:.1f}%, sell={sell_pct:.1f}%). Check data quality.")
        if invalid_count > 0:
            warnings.append(f"WARN: {invalid_count:,} rows with invalid aggressor values (will use tick-rule inference)")

        print(f"Aggressor: NATIVE")
    else:
        print(f"Aggressor: NOT PRESENT (will use tick-rule inference)")
        warnings.append("WARN: No aggressor column. Tick-rule inference will be used — delta quality is weaker.")

    # --- Price/qty summary ---
    print(f"Price range: {df['price'].min():.2f} to {df['price'].max():.2f}")
    print(f"Qty range:   {df['qty'].min():.8f} to {df['qty'].max():.8f}")
    print(f"Columns:     {list(df.columns)}")

    # --- Report ---
    print()
    for w in warnings:
        print(w)
    for e in errors:
        print(e)

    if not errors and not warnings:
        print("ALL CHECKS PASSED")
    elif not errors:
        print(f"\nPASSED with {len(warnings)} warning(s)")
    else:
        print(f"\nFAILED: {len(errors)} error(s), {len(warnings)} warning(s)")

    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate raw trades CSV before downstream processing."
    )
    parser.add_argument("--trades", required=True, help="Path to raw trades CSV.")
    parser.add_argument("--max-gap", type=int, default=60, help="Max allowed timestamp gap in seconds (default: 60).")
    parser.add_argument("--min-rows", type=int, default=10000, help="Minimum row count for research (default: 10000).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    errors = validate(Path(args.trades), args.max_gap, args.min_rows)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()