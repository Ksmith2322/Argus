#!/usr/bin/env python3
"""Download and normalize Binance Futures historical trade data.

Part of Argus Cascade — Phase 0 (Data Acquisition).

Downloads daily trade CSVs from Binance's public data repo, unzips,
merges, and normalizes to the standard schema:
    ts, price, qty, aggressor

Source: https://github.com/binance/binance-public-data
Files:  data/futures/um/daily/trades/BTCUSDT/BTCUSDT-trades-YYYY-MM-DD.zip

IMPORTANT: Binance isBuyerMaker inversion:
    isBuyerMaker = True  -> SELL aggressor (maker was buyer, taker was seller)
    isBuyerMaker = False -> BUY aggressor  (maker was seller, taker was buyer)
    Getting this wrong inverts ALL delta logic.

Usage:
    # Download last 14 days of BTC perp trades:
    python -m argus_flow.capture.ingest_binance_trades \
        --symbol BTCUSDT \
        --days 14 \
        --output data/raw/trades_btc.csv

    # Download specific date range:
    python -m argus_flow.capture.ingest_binance_trades \
        --symbol BTCUSDT \
        --start 2026-03-01 \
        --end 2026-03-14 \
        --output data/raw/trades_btc.csv
"""
from __future__ import annotations

import argparse
import io
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import pandas as pd


BASE_URL = "https://data.binance.vision/data/futures/um/daily/trades"


def download_day(symbol: str, date: str, tmp_dir: Path) -> Path | None:
    """Download and extract one day's trade CSV. Returns path or None on failure."""
    filename = f"{symbol}-trades-{date}.zip"
    url = f"{BASE_URL}/{symbol}/{filename}"

    try:
        req = Request(url, headers={"User-Agent": "ArgusFlow/1.0"})
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
    except HTTPError as e:
        if e.code == 404:
            print(f"  [SKIP] {date} — not available (404)")
            return None
        raise

    zip_path = tmp_dir / filename
    zip_path.write_bytes(data)

    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not csv_names:
            print(f"  [SKIP] {date} — zip contains no CSV")
            return None
        extracted = zf.extract(csv_names[0], tmp_dir)
        return Path(extracted)


def normalize_binance_trades(csv_path: Path) -> pd.DataFrame:
    """Read a Binance trades CSV and normalize to standard schema.

    Binance columns: id, price, qty, quoteQty, time, isBuyerMaker
    """
    df = pd.read_csv(csv_path, header=None)

    # Binance daily trade files may or may not have headers
    if df.iloc[0, 0] == "id" or str(df.iloc[0, 0]).lower() == "id":
        df.columns = df.iloc[0]
        df = df.iloc[1:].reset_index(drop=True)

    # Handle both header and no-header cases
    if len(df.columns) >= 6:
        col_map = {
            df.columns[0]: "trade_id",
            df.columns[1]: "price",
            df.columns[2]: "qty",
            df.columns[3]: "quote_qty",
            df.columns[4]: "time",
            df.columns[5]: "is_buyer_maker",
        }
        df = df.rename(columns=col_map)

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")

    # Convert epoch ms to UTC timestamp
    df["ts"] = pd.to_datetime(df["time"], unit="ms", utc=True)

    # CRITICAL: isBuyerMaker inversion
    # isBuyerMaker=True  -> taker was SELLING (aggressor = sell)
    # isBuyerMaker=False -> taker was BUYING  (aggressor = buy)
    is_buyer_maker = df["is_buyer_maker"].astype(str).str.lower().str.strip()
    df["aggressor"] = is_buyer_maker.map(
        {"true": "sell", "false": "buy", "1": "sell", "0": "buy"}
    )

    # Keep only what we need
    out = df[["ts", "price", "qty", "aggressor"]].copy()
    out = out.dropna(subset=["ts", "price", "qty", "aggressor"])
    out = out[out["price"] > 0]
    out = out[out["qty"] > 0]

    return out


def generate_dates(start: str, end: str) -> list[str]:
    """Generate list of date strings between start and end (inclusive)."""
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    current = start_dt
    while current <= end_dt:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and normalize Binance Futures historical trades."
    )
    parser.add_argument(
        "--symbol", default="BTCUSDT",
        help="Trading pair (default: BTCUSDT).",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Download last N days (alternative to --start/--end).",
    )
    parser.add_argument(
        "--start", default=None,
        help="Start date YYYY-MM-DD (inclusive).",
    )
    parser.add_argument(
        "--end", default=None,
        help="End date YYYY-MM-DD (inclusive).",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path for merged output CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Determine date range
    if args.days:
        end_dt = datetime.utcnow() - timedelta(days=1)  # yesterday (today may be incomplete)
        start_dt = end_dt - timedelta(days=args.days - 1)
        start = start_dt.strftime("%Y-%m-%d")
        end = end_dt.strftime("%Y-%m-%d")
    elif args.start and args.end:
        start = args.start
        end = args.end
    else:
        raise ValueError("Provide either --days or both --start and --end")

    dates = generate_dates(start, end)
    print(f"Symbol:     {args.symbol}")
    print(f"Date range: {start} to {end} ({len(dates)} days)")
    print(f"Output:     {args.output}")
    print()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = output_path.parent / "_tmp_binance_download"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    all_frames: list[pd.DataFrame] = []
    success_count = 0

    for date in dates:
        print(f"  Downloading {args.symbol} {date}...", end=" ", flush=True)
        csv_path = download_day(args.symbol, date, tmp_dir)
        if csv_path is None:
            continue

        df = normalize_binance_trades(csv_path)
        all_frames.append(df)
        success_count += 1
        print(f"{len(df):,} trades")

        # Clean up extracted file
        csv_path.unlink(missing_ok=True)

    # Clean up zip files
    for f in tmp_dir.glob("*.zip"):
        f.unlink(missing_ok=True)
    for f in tmp_dir.glob("*.csv"):
        f.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    if not all_frames:
        print("\nERROR: No data downloaded. Check symbol and date range.")
        return

    merged = pd.concat(all_frames, ignore_index=True)
    merged = merged.sort_values("ts").reset_index(drop=True)
    merged.to_csv(output_path, index=False)

    buy_count = (merged["aggressor"] == "buy").sum()
    sell_count = (merged["aggressor"] == "sell").sum()
    buy_pct = buy_count / len(merged) * 100

    print(f"\n{'='*50}")
    print(f"Days downloaded: {success_count}/{len(dates)}")
    print(f"Total trades:    {len(merged):,}")
    print(f"Time range:      {merged['ts'].iloc[0]} to {merged['ts'].iloc[-1]}")
    print(f"Aggressor:       NATIVE (isBuyerMaker)")
    print(f"Buy/Sell:        {buy_count:,} ({buy_pct:.1f}%) / {sell_count:,} ({100-buy_pct:.1f}%)")
    print(f"Saved to:        {output_path}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()