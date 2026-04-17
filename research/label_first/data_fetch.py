"""Data fetcher for label-first batch — yfinance loader.

Pulls maximum-depth bars per (instrument, timeframe) cell and caches as parquet.
Re-runs are cheap — only refetch when cache is missing or --refresh.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

INSTRUMENTS = {
    "ES": "ES=F",
    "NQ": "NQ=F",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "CADJPY": "CADJPY=X",
    "GLD": "GLD",
}

# (period, interval) per timeframe — maximize depth within yfinance limits
TIMEFRAMES = {
    "1d": ("max", "1d"),
    "1h": ("730d", "1h"),
    "15m": ("60d", "15m"),
    "5m": ("60d", "5m"),
}


def _cache_path(inst: str, tf: str) -> Path:
    return DATA_DIR / f"{inst}_{tf}.pkl"


def fetch_cell(inst_key: str, tf_key: str, refresh: bool = False) -> pd.DataFrame:
    cache = _cache_path(inst_key, tf_key)
    if cache.exists() and not refresh:
        return pd.read_pickle(cache)

    ticker = INSTRUMENTS[inst_key]
    period, interval = TIMEFRAMES[tf_key]
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker} {tf_key}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    df.to_pickle(cache)
    return df


def fetch_all(refresh: bool = False) -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for inst in INSTRUMENTS:
        for tf in TIMEFRAMES:
            try:
                df = fetch_cell(inst, tf, refresh=refresh)
                out[(inst, tf)] = df
                print(f"  OK  {inst:<8} {tf:<4} {len(df):>7,} bars  {df.index.min()} -> {df.index.max()}")
            except Exception as e:
                print(f"  FAIL {inst:<8} {tf:<4} {type(e).__name__}: {e}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    print(f"Fetching {len(INSTRUMENTS)} instruments × {len(TIMEFRAMES)} timeframes...")
    fetch_all(refresh=args.refresh)
