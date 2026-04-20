"""Bar storage primitive — shared infrastructure for strategies that need
real historical bars outside yfinance's cap.

yfinance limits for intraday intervals:
    1m:  7 days
    5m:  60 days
    15m: 60 days
    1h:  730 days
    daily: unlimited

For any strategy whose edge depends on intrabar microstructure (Mamba's
rejection wicks, failed breaks, sweep-reclaim), synthetic bars resampled
from coarser data are not good enough. This module is the single place
to plug in a real-data provider.

Design:
    - Parquet files at `forge/data/<strategy>/<interval>/<ticker>.parquet`
    - Schema: DataFrame index = pandas DatetimeIndex (tz-aware UTC),
      columns = Open, High, Low, Close, Volume
    - Loader validates schema before returning; raises clear errors
    - Writers (per-provider ingestion scripts) live with their strategy
    - This module has NO network code — it's a local-file abstraction

Usage:
    from helio.bar_store import load_bars, validate_bar_df
    df = load_bars("mamba", "1m", "NQ=F")  # raises if missing/invalid

Providers to wire up later (not this module's responsibility):
    - IBKR historical API (free for IBKR account holders)
    - Polygon.io (paid, ~$30/mo)
    - Databento (institutional)
    - Alpaca (free equities/crypto, paid futures)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
BAR_STORE_ROOT = _REPO / "forge" / "data"

REQUIRED_COLS = ("Open", "High", "Low", "Close", "Volume")
VALID_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


class BarStoreError(RuntimeError):
    """Raised on schema validation failure or missing bars."""


def bar_path(strategy: str, interval: str, ticker: str) -> Path:
    """Canonical disk location for one (strategy, interval, ticker) set."""
    safe_ticker = ticker.replace("/", "_").replace("=", "_")
    return BAR_STORE_ROOT / strategy / interval / f"{safe_ticker}.parquet"


def validate_bar_df(df: pd.DataFrame, *, min_rows: int = 100) -> None:
    """Validates the shape expected by strategy backtests. Raises
    BarStoreError on violation. No side effects."""
    if not isinstance(df, pd.DataFrame):
        raise BarStoreError(f"expected pd.DataFrame, got {type(df).__name__}")
    if len(df) < min_rows:
        raise BarStoreError(f"bars too few: {len(df)} < min_rows={min_rows}")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise BarStoreError(f"missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise BarStoreError(f"index must be DatetimeIndex, got {type(df.index).__name__}")
    if df.index.tz is None:
        raise BarStoreError("index must be tz-aware (UTC)")
    # OHLC sanity: high >= max(open, close), low <= min(open, close)
    bad_high = (df["High"] < df[["Open", "Close"]].max(axis=1)).any()
    bad_low = (df["Low"] > df[["Open", "Close"]].min(axis=1)).any()
    if bad_high or bad_low:
        raise BarStoreError("OHLC integrity violation: high < max(open,close) or low > min(open,close)")


def load_bars(
    strategy: str,
    interval: str,
    ticker: str,
    *,
    min_rows: int = 100,
) -> pd.DataFrame:
    """Load a validated bar DataFrame from disk.

    Raises BarStoreError on any of: file missing, parquet read failure,
    schema violation.
    """
    if interval not in VALID_INTERVALS:
        raise BarStoreError(f"interval '{interval}' not in {VALID_INTERVALS}")
    path = bar_path(strategy, interval, ticker)
    if not path.exists():
        raise BarStoreError(
            f"bars not found at {path}. Run the per-provider ingestion "
            f"script for strategy='{strategy}' to populate."
        )
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        raise BarStoreError(f"parquet read failed for {path}: {e}") from e
    validate_bar_df(df, min_rows=min_rows)
    return df


def write_bars(
    df: pd.DataFrame,
    strategy: str,
    interval: str,
    ticker: str,
) -> Path:
    """Validate + write a DataFrame to the canonical location. Used by
    per-provider ingestion scripts. Not called from runners — runners
    only read via `load_bars`."""
    validate_bar_df(df)
    path = bar_path(strategy, interval, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=True)
    return path


def available_tickers(strategy: str, interval: str) -> list[str]:
    """Return the tickers that have bars on disk for (strategy, interval).
    Useful for "which instruments can I actually trade right now?" checks."""
    root = BAR_STORE_ROOT / strategy / interval
    if not root.exists():
        return []
    return sorted(p.stem.replace("_F", "=F") for p in root.glob("*.parquet"))


def audit(strategies: Iterable[str] | None = None) -> dict:
    """Dashboard-friendly audit: what data do we actually have on disk?

    Returns: {
        "strategies": [{strategy, interval, ticker, n_rows, start, end, valid}, ...],
        "missing": [str]   # strategies listed but with no data at all
    }
    """
    strategies = list(strategies) if strategies else []
    if not strategies:
        # Auto-discover from folder layout
        if BAR_STORE_ROOT.exists():
            strategies = sorted(p.name for p in BAR_STORE_ROOT.iterdir() if p.is_dir())

    records = []
    missing = []
    for strat in strategies:
        strat_root = BAR_STORE_ROOT / strat
        if not strat_root.exists():
            missing.append(strat)
            continue
        for iv_dir in strat_root.iterdir():
            if not iv_dir.is_dir():
                continue
            for pq in iv_dir.glob("*.parquet"):
                ticker = pq.stem.replace("_F", "=F")
                rec = {
                    "strategy": strat,
                    "interval": iv_dir.name,
                    "ticker": ticker,
                    "path": str(pq),
                }
                try:
                    df = pd.read_parquet(pq)
                    validate_bar_df(df, min_rows=0)
                    rec.update({
                        "n_rows": len(df),
                        "start": str(df.index.min()) if len(df) else None,
                        "end": str(df.index.max()) if len(df) else None,
                        "valid": True,
                    })
                except BarStoreError as e:
                    rec.update({"valid": False, "error": str(e)})
                except Exception as e:
                    rec.update({"valid": False, "error": f"read failed: {e}"})
                records.append(rec)
    return {"strategies": records, "missing": missing}
