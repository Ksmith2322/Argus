"""yfinance bulk historical fetcher for the backtest factory.

Why this exists separately from `helio/yfinance_cache.py`:
    - yfinance_cache.py is the runner-side hot cache (parquet, short windows,
      stale-fallback). It's parquet-based and used by live runners.
    - This module is the factory-side bulk loader (CSV, multi-year windows,
      idempotent on disk). It matches the layout of helio/data_massive/ so
      `python -m helio.backtest_factory.cli screen --data-dir helio/data_yfinance`
      Just Works.

yfinance characteristics for our use case:
    - Daily history is effectively unlimited (1990+ for major ETFs)
    - One `yf.download()` call returns the entire window
    - No documented rate limit but Yahoo can throttle; we keep calls
      sequential with a small sleep between tickers
    - Returns OHLCV DataFrame with Date index

Free, no API key, already in the venv (yfinance 1.2.0).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd


_REPO = Path(__file__).resolve().parents[1]
DATA_YFINANCE_DIR = _REPO / "helio" / "data_yfinance"

DEFAULT_BETWEEN_TICKER_SLEEP_S = 1.0


class YFinanceError(RuntimeError):
    """Raised on yfinance unavailability / empty response / network error."""


@dataclass
class YFFetchResult:
    ticker: str
    rows_fetched: int
    rows_total: int
    cache_path: Optional[Path]
    incremental: bool
    elapsed_s: float


def _import_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise YFinanceError(f"yfinance not installed: {e}") from e


def cache_path(ticker: str, *, root: Optional[Path] = None) -> Path:
    root = root if root is not None else DATA_YFINANCE_DIR
    safe = ticker.upper().replace("/", "_").replace("=", "_").replace("^", "_")
    return root / f"{safe}_daily.csv"


def fetch_daily_bars(
    ticker: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    period: Optional[str] = None,
) -> pd.DataFrame:
    """Fetch daily OHLCV via yfinance. Either provide (start, end) OR period.

    Returns DataFrame indexed by Date (naive midnight pd.Timestamp) with
    columns Open/High/Low/Close/Volume — matching helio/data and
    helio/data_massive CSV schema.

    Raises YFinanceError on empty response or import failure.
    """
    if not ticker:
        raise YFinanceError("ticker required")
    yf = _import_yfinance()

    kwargs = dict(progress=False, threads=False, auto_adjust=False)
    try:
        if period:
            df = yf.download(ticker, period=period, interval="1d", **kwargs)
        else:
            if not start or not end:
                raise YFinanceError("must provide (start, end) or period")
            df = yf.download(ticker, start=start, end=end, interval="1d", **kwargs)
    except Exception as e:
        raise YFinanceError(f"yf.download failed for {ticker}: {e}") from e

    if df is None or df.empty:
        raise YFinanceError(f"yfinance returned empty frame for {ticker}")

    # Drop the MultiIndex columns yfinance sometimes returns for single tickers
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Normalize columns + index
    rename = {}
    for src, dst in (("Adj Close", "AdjClose"),):
        if src in df.columns:
            rename[src] = dst
    if rename:
        df = df.rename(columns=rename)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].copy()

    # Strip tz, normalize to midnight
    if isinstance(df.index, pd.DatetimeIndex):
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
    df.index.name = "Date"
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def fetch_to_cache(
    ticker: str,
    *,
    years_back: float = 10.0,
    refresh: bool = False,
) -> YFFetchResult:
    """Pull `ticker` to CSV cache. Idempotent — incremental on subsequent
    runs (fetches only the gap since last cached bar)."""
    t0 = time.perf_counter()
    path = cache_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: Optional[pd.DataFrame] = None
    if path.exists() and not refresh:
        try:
            existing = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
        except Exception:
            existing = None

    today = date.today()
    end_dt = today
    if existing is not None and len(existing) > 0:
        start_dt = existing.index.max().date() + timedelta(days=1)
        if start_dt > end_dt:
            return YFFetchResult(
                ticker=ticker, rows_fetched=0, rows_total=len(existing),
                cache_path=path, incremental=True,
                elapsed_s=time.perf_counter() - t0,
            )
        new_df = fetch_daily_bars(ticker, start=start_dt.isoformat(), end=end_dt.isoformat())
    else:
        # Initial full pull
        start_dt = today - timedelta(days=int(365 * years_back))
        new_df = fetch_daily_bars(ticker, start=start_dt.isoformat(), end=end_dt.isoformat())

    if existing is not None and len(existing) > 0:
        combined = pd.concat([existing, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = new_df

    combined.to_csv(path, index=True, index_label="Date")
    return YFFetchResult(
        ticker=ticker,
        rows_fetched=len(new_df),
        rows_total=len(combined),
        cache_path=path,
        incremental=(existing is not None and len(existing) > 0),
        elapsed_s=time.perf_counter() - t0,
    )


def fetch_universe(
    tickers: Sequence[str],
    *,
    years_back: float = 10.0,
    refresh: bool = False,
    between_ticker_sleep_s: float = DEFAULT_BETWEEN_TICKER_SLEEP_S,
    on_progress=None,
) -> list[YFFetchResult]:
    """Fetch a list of tickers. Sleep `between_ticker_sleep_s` between
    calls to be polite to Yahoo (no documented limit, but they throttle)."""
    results: list[YFFetchResult] = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        try:
            r = fetch_to_cache(t, years_back=years_back, refresh=refresh)
        except YFinanceError:
            r = YFFetchResult(
                ticker=t, rows_fetched=0, rows_total=0,
                cache_path=None, incremental=False, elapsed_s=0.0,
            )
        results.append(r)
        if on_progress:
            try:
                on_progress(i + 1, total, r)
            except Exception:
                pass
        if i < total - 1 and between_ticker_sleep_s > 0:
            time.sleep(between_ticker_sleep_s)
    return results
