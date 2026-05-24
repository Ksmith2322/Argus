"""Thin file-backed cache for yfinance.download calls.

Problem: many forge runners hit yfinance directly with no fallback. yfinance
is rate-limited (~2000 req/hr), occasionally returns stale data on ex-div
days, and has no SLA. A 15-min rate-limit blip can silence the entire forge
family.

Solution (opt-in, non-invasive): a thin wrapper that:
  1. Checks on-disk cache for a (ticker, period, interval) tuple.
  2. If cache hit younger than `fresh_seconds`, return cache.
  3. Otherwise call yf.download; on success, write to cache.
  4. On yf failure (HTTPError, empty frame, etc.), fall back to the LAST
     known good cache even if older than fresh_seconds. Log the fallback.

Cache location: `<repo>/forge/data/.yfinance_cache/<ticker>_<interval>_<period>.parquet`
with a pickle fallback at the same path stem when no parquet engine is
installed. Parquet is compact + fast, but the fallback keeps production
from losing its last-known-good cache because pyarrow/fastparquet is absent.

Usage (opt-in — no runners auto-switch):
    from helio.yfinance_cache import download_cached
    df = download_cached("NQ=F", period="7d", interval="5m", fresh_seconds=300)

    # Equivalent fallback if yfinance is unavailable:
    df = download_cached("NQ=F", period="7d", interval="5m", fresh_seconds=0,
                         allow_stale_fallback=True)

Migration path: a runner opts in by replacing `yf.download(...)` with
`download_cached(...)`. No API change to the rest of the runner.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO / "forge" / "data" / ".yfinance_cache"


def _cache_path(ticker: str, interval: str, period: str) -> Path:
    safe_ticker = (ticker or "NA").replace("=", "_").replace("/", "_").replace("^", "_")
    return CACHE_DIR / f"{safe_ticker}_{interval}_{period}.parquet"


def _fallback_cache_path(path: Path) -> Path:
    return path.with_suffix(".pkl")


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    candidates = [path, _fallback_cache_path(path)]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".parquet":
                df = pd.read_parquet(candidate)
            else:
                df = pd.read_pickle(candidate)
            if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df
        except Exception:
            continue
    return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=True, compression="snappy")
        return
    except Exception:
        pass
    try:
        df.to_pickle(_fallback_cache_path(path))
    except Exception:
        pass  # cache write failure is non-fatal


def _cache_age_seconds(path: Path) -> float:
    ages = []
    for candidate in (path, _fallback_cache_path(path)):
        try:
            ages.append(time.time() - os.path.getmtime(candidate))
        except OSError:
            continue
    return min(ages) if ages else float("inf")


def download_cached(
    ticker: str,
    *,
    period: str = "7d",
    interval: str = "5m",
    fresh_seconds: int = 300,
    allow_stale_fallback: bool = True,
    progress: bool = False,
    auto_adjust: bool = False,
    threads: bool = False,
) -> pd.DataFrame:
    """Cached yf.download wrapper.

    fresh_seconds: cache hits younger than this skip the network call.
        0 = always hit yf first.
    allow_stale_fallback: on yf error, serve the last cache even if >fresh.
    """
    path = _cache_path(ticker, interval, period)
    cache_age = _cache_age_seconds(path)

    # Hot cache path
    if cache_age < fresh_seconds:
        cached = _read_cache(path)
        if cached is not None and not cached.empty:
            return cached

    # Call yf
    try:
        import yfinance as yf
        df = yf.download(
            ticker, period=period, interval=interval,
            progress=progress, auto_adjust=auto_adjust, threads=threads,
        )
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            _write_cache(path, df)
            return df
        # Empty frame — treat as failure
        raise RuntimeError(f"yfinance returned empty frame for {ticker}/{period}/{interval}")
    except Exception as e:
        if not allow_stale_fallback:
            raise
        cached = _read_cache(path)
        if cached is not None and not cached.empty:
            # Log stale fallback (best effort — no logger dependency)
            try:
                from forge.logging_setup import setup_logging
                log = setup_logging("yf_cache")
                log.warning(
                    "yf.download failed for %s (%s/%s) — serving stale cache (age %.0fs). err=%s",
                    ticker, period, interval, cache_age, str(e)[:200],
                )
            except Exception:
                pass
            return cached
        # Nothing we can do
        raise
