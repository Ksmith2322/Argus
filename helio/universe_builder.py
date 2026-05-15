#!/usr/bin/env python3
"""helio/universe_builder.py — Nasdaq Trader symbol-file ingest.

Pulls the daily list of all US-listed common stocks from Nasdaq Trader's
public symbol directory, filters for sub-$10 names with meaningful daily
volume, and outputs a universe file consumable by helio.breakout_research.

This replaces hand-curated ticker lists. Goes from ~82 names to ~1,500-2,000
candidates, which is what we need for statistical power on catalyst-conditional
edges (n=46 on S-3 today is too small; expansion gets us n=500+).

Sources (all free, no key):
  - https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt  (NASDAQ)
  - https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt   (NYSE/AMEX)

Filters (defaults match Agent 1's recommendation):
  - Common stock only (ETF=N, Test Issue=N)
  - Last close < $10
  - 20d average volume > 200K shares (rules out OTC + microcap illiquid)

Usage:
    python -m helio.universe_builder --max-price 10 --min-vol 200000
    python -m helio.universe_builder --refresh-symbols  # re-pull from Nasdaq Trader
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    import pandas as pd
    import yfinance as yf
except ImportError:
    print("ERROR: pip install pandas yfinance")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[1]
UNIVERSE_DIR = REPO / "helio" / "data" / "universes"
RAW_SYMBOLS_DIR = UNIVERSE_DIR / "_raw_symbol_files"

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"


def _http_get_text(url: str, timeout: int = 30) -> str | None:
    """GET a text URL with a polite User-Agent."""
    req = Request(url, headers={
        "User-Agent": "Argus Research (ksmith2322@yahoo.com)",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (URLError, HTTPError) as e:
        print(f"  HTTP error fetching {url}: {e}")
        return None


def fetch_nasdaq_symbol_files(force_refresh: bool = False) -> tuple[Path, Path]:
    """Download both Nasdaq Trader symbol files. Caches locally. Returns
    (nasdaqlisted_path, otherlisted_path)."""
    RAW_SYMBOLS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    nasdaq_path = RAW_SYMBOLS_DIR / f"nasdaqlisted_{today}.txt"
    other_path = RAW_SYMBOLS_DIR / f"otherlisted_{today}.txt"

    if not nasdaq_path.exists() or force_refresh:
        print(f"Downloading {NASDAQ_LISTED_URL}...")
        text = _http_get_text(NASDAQ_LISTED_URL)
        if not text:
            raise RuntimeError("Failed to fetch nasdaqlisted.txt")
        nasdaq_path.write_text(text, encoding="utf-8")
    if not other_path.exists() or force_refresh:
        print(f"Downloading {OTHER_LISTED_URL}...")
        text = _http_get_text(OTHER_LISTED_URL)
        if not text:
            raise RuntimeError("Failed to fetch otherlisted.txt")
        other_path.write_text(text, encoding="utf-8")
    return nasdaq_path, other_path


def parse_nasdaq_listed(path: Path) -> list[dict]:
    """Parse nasdaqlisted.txt. Schema:
    Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    """
    rows = []
    text = path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    if not lines:
        return rows
    header = lines[0].split("|")
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        # Footer row: "File Creation Time: ..."
        if parts[0].startswith("File Creation"):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def parse_other_listed(path: Path) -> list[dict]:
    """Parse otherlisted.txt (NYSE/AMEX). Schema:
    ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    """
    rows = []
    text = path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    if not lines:
        return rows
    header = lines[0].split("|")
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        if parts[0].startswith("File Creation"):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def get_common_stock_tickers(force_refresh: bool = False) -> list[str]:
    """Return list of all US-listed common stock tickers, ETFs + test issues
    filtered out. Combines NASDAQ + NYSE + AMEX listings."""
    nasdaq_path, other_path = fetch_nasdaq_symbol_files(force_refresh=force_refresh)
    tickers: set[str] = set()

    for row in parse_nasdaq_listed(nasdaq_path):
        if row.get("ETF", "N") != "N":
            continue
        if row.get("Test Issue", "N") != "N":
            continue
        sym = (row.get("Symbol") or "").strip().upper()
        if not sym or "$" in sym or "." in sym:
            # Skip warrants ($), preferred-share class qualifiers (.A etc)
            continue
        tickers.add(sym)

    for row in parse_other_listed(other_path):
        if row.get("ETF", "N") != "N":
            continue
        if row.get("Test Issue", "N") != "N":
            continue
        sym = (row.get("ACT Symbol") or "").strip().upper()
        if not sym or "$" in sym or "." in sym:
            continue
        tickers.add(sym)

    print(f"  Parsed {len(tickers)} common-stock tickers from Nasdaq/NYSE/AMEX listings")
    return sorted(tickers)


def filter_by_price_and_volume(tickers: list[str], max_price: float = 10.0,
                                min_price: float = 0.0,
                                min_avg_vol: float = 200_000,
                                batch_size: int = 200) -> list[dict]:
    """Batch-download recent OHLCV via yfinance and filter to:
      min_price <= last_close <= max_price AND 20d avg volume >= min_avg_vol

    Returns list of dicts: [{ticker, last_close, avg_vol_20d}, ...] sorted
    by avg_vol_20d descending. Tickers with no data or zero volume are
    dropped silently.
    """
    print(f"\nScreening {len(tickers)} tickers (price ${min_price:.2f}-${max_price:.2f}, "
          f"ADV>={min_avg_vol:,.0f}, batch_size={batch_size})...")
    survivors: list[dict] = []
    n_batches = (len(tickers) + batch_size - 1) // batch_size

    for batch_i in range(n_batches):
        batch = tickers[batch_i * batch_size : (batch_i + 1) * batch_size]
        try:
            df = yf.download(
                batch, period="1mo", interval="1d",
                progress=False, auto_adjust=False, threads=True,
                group_by="ticker",
            )
        except Exception as e:
            print(f"  batch {batch_i+1}/{n_batches}: download failed: {e}")
            continue

        for sym in batch:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym not in df.columns.get_level_values(0):
                        continue
                    sub = df[sym]
                else:
                    sub = df
                if sub.empty or "Close" not in sub.columns or "Volume" not in sub.columns:
                    continue
                last_close = float(sub["Close"].dropna().iloc[-1]) if not sub["Close"].dropna().empty else 0.0
                avg_vol = float(sub["Volume"].dropna().tail(20).mean()) if not sub["Volume"].dropna().empty else 0.0
                if last_close <= 0 or avg_vol <= 0:
                    continue
                if last_close > max_price or last_close < min_price or avg_vol < min_avg_vol:
                    continue
                survivors.append({
                    "ticker": sym,
                    "last_close": round(last_close, 2),
                    "avg_vol_20d": int(avg_vol),
                })
            except Exception:
                continue

        time.sleep(0.5)  # polite to yfinance
        if (batch_i + 1) % 5 == 0 or batch_i == n_batches - 1:
            print(f"  batch {batch_i+1}/{n_batches} done — {len(survivors)} survivors so far")

    survivors.sort(key=lambda r: r["avg_vol_20d"], reverse=True)
    return survivors


def build_universe(max_price: float = 10.0,
                    min_price: float = 0.0,
                    min_avg_vol: float = 200_000,
                    force_refresh: bool = False,
                    output_name: str = "sub10_expanded") -> Path:
    """End-to-end: pull symbol files → filter → price/volume screen → save."""
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{'='*60}")
    print(f"Universe Builder: price ${min_price:.2f}-${max_price:.2f}, "
          f"min_avg_vol={min_avg_vol:,.0f}")
    print(f"{'='*60}")

    all_tickers = get_common_stock_tickers(force_refresh=force_refresh)
    survivors = filter_by_price_and_volume(all_tickers,
                                            max_price=max_price, min_price=min_price,
                                            min_avg_vol=min_avg_vol)

    # Save as JSON metadata + plain ticker list
    out_json = UNIVERSE_DIR / f"{output_name}.json"
    out_txt = UNIVERSE_DIR / f"{output_name}.txt"
    out_json.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_price": max_price,
        "min_avg_vol": min_avg_vol,
        "n_screened": len(all_tickers),
        "n_survived": len(survivors),
        "tickers": survivors,
    }, indent=2))
    out_txt.write_text("\n".join(r["ticker"] for r in survivors))
    print(f"\nSaved {len(survivors)} survivors:")
    print(f"  JSON (with metadata): {out_json}")
    print(f"  Plain ticker list:    {out_txt}")
    if survivors:
        print(f"\nTop 20 by 20d avg volume:")
        for r in survivors[:20]:
            print(f"  {r['ticker']:6s}  ${r['last_close']:>6.2f}  vol={r['avg_vol_20d']:>12,d}")
    return out_txt


def main():
    parser = argparse.ArgumentParser(description="Nasdaq Trader Universe Builder")
    parser.add_argument("--max-price", type=float, default=10.0,
                        help="Maximum last close price (default: $10)")
    parser.add_argument("--min-price", type=float, default=0.0,
                        help="Minimum last close price (default: $0). Use $5 for "
                             "small-cap/mid-priced PEAD universe.")
    parser.add_argument("--min-vol", type=float, default=200_000,
                        help="Minimum 20d avg volume (default: 200,000)")
    parser.add_argument("--refresh-symbols", action="store_true",
                        help="Re-pull symbol files from Nasdaq Trader")
    parser.add_argument("--output", default="sub10_expanded",
                        help="Output filename prefix (default: sub10_expanded)")
    args = parser.parse_args()

    build_universe(
        max_price=args.max_price,
        min_price=args.min_price,
        min_avg_vol=args.min_vol,
        force_refresh=args.refresh_symbols,
        output_name=args.output,
    )


if __name__ == "__main__":
    main()
