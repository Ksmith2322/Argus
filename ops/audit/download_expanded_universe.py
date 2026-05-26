"""Download daily history for expanded test universe.

Targets three groups missing from the current helio/data_yfinance/ store:
  - Mega-cap individual stocks (long history, deep liquidity)
  - Specialty sector / thematic ETFs
  - Commodity / energy ETFs

Output format matches existing tickers: Date,Open,High,Low,Close,Volume
written to helio/data_yfinance/<TICKER>_daily.csv (20y back when available).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

_REPO = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO / "helio" / "data_yfinance"

# Mega-cap individual stocks (deep history + liquidity).
# Picked for breakout / calendar pattern testing -- NOT for daytrading.
STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "BRK-B",
    "JPM", "V", "MA", "JNJ", "UNH", "XOM", "HD", "PG", "WMT",
]

# Specialty ETFs (sector / thematic) we haven't tested yet
SPECIALTY_ETFS = [
    "SMH",    # semiconductors
    "KRE",    # regional banks
    "XBI",    # biotech
    "ITB",    # homebuilders
    "KWEB",   # China internet
    "EWY",    # Korea
    "EWT",    # Taiwan
    "EWA",    # Australia
    "EWU",    # UK
    "EWC",    # Canada
]

# Commodity / energy ETFs
COMMODITY_ETFS = [
    "USO",    # crude oil
    "UNG",    # natural gas
    "SLV",    # silver
    "COPX",   # copper miners
    "URA",    # uranium
    "DBA",    # agriculture
    "LIT",    # lithium
    "HYG",    # high-yield bonds
    "EMB",    # emerging-market bonds
    "LQD",    # investment-grade bonds
    "TIP",    # TIPS (inflation-linked)
]

ALL_TICKERS = STOCKS + SPECIALTY_ETFS + COMMODITY_ETFS


def download_ticker(ticker: str, period: str = "20y") -> bool:
    path = _DATA_DIR / f"{ticker}_daily.csv"
    if path.exists():
        print(f"  SKIP {ticker:6s}  (already cached)")
        return False
    try:
        df = yf.download(ticker, period=period, interval="1d",
                          progress=False, auto_adjust=False)
    except Exception as exc:
        print(f"  FAIL {ticker:6s}  download error: {exc}")
        return False
    if df is None or df.empty:
        print(f"  FAIL {ticker:6s}  no data returned")
        return False
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index.name = "Date"
    df.to_csv(path)
    years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"  OK   {ticker:6s}  n={len(df):5d}  years={years:5.1f}  -> {path.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if cached")
    args = parser.parse_args()
    if args.force:
        # Delete the existing CSVs so download proceeds
        for t in ALL_TICKERS:
            p = _DATA_DIR / f"{t}_daily.csv"
            if p.exists():
                p.unlink()
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== Expanded universe download ===")
    print(f"  {len(STOCKS)} stocks + {len(SPECIALTY_ETFS)} specialty ETFs + "
          f"{len(COMMODITY_ETFS)} commodity/bond ETFs = {len(ALL_TICKERS)} total")
    print()
    fresh = 0
    for ticker in ALL_TICKERS:
        if download_ticker(ticker):
            fresh += 1
    print()
    print(f"=== DONE: {fresh} new tickers downloaded ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
