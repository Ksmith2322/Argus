"""Massive.com (Polygon.io rebrand) REST data fetcher.

Free tier (5 Basic plans, established 2026-05-22):
    - 2-year EOD daily history per ticker
    - 5 requests / minute rate limit
    - Schema is polygon-compatible: {v, vw, o, c, h, l, t, n}

This module provides a rate-aware client + CSV cache. Pulls write to
`helio/data_massive/<ticker>_daily.csv` in the same column schema used
by `helio/data/<ticker>_daily.csv` (Date, Open, High, Low, Close,
Volume), so the existing factory CLI can load either source with the
same loader.

Why REST, not S3 flat files: at the Basic free tier, S3 flat files
expose the same 2-year window as the REST API. Flat files are organized
by date (every ticker for one day in one file), which is the wrong
shape for our "many tickers, one history" workload. REST is one call
per ticker. If we upgrade to a paid tier with deeper history, switching
to flat-file ingestion is a future-day project.

Usage:
    from helio.massive_data import MassiveClient, fetch_to_cache
    client = MassiveClient.from_env()
    df = client.fetch_daily_bars("SPY", "2024-05-22", "2026-05-21")

    # Or cache to disk:
    path = fetch_to_cache("SPY", client=client)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd
import requests
from dotenv import load_dotenv


_REPO = Path(__file__).resolve().parents[1]
DATA_MASSIVE_DIR = _REPO / "helio" / "data_massive"
SECRETS_PATH = _REPO / "secrets" / "massive.env"

API_BASE = "https://api.polygon.io"
DEFAULT_MIN_INTERVAL_S = 13.0  # 5 req/min nominally = 12s; +1s headroom
MAX_RETRIES_ON_429 = 3


class MassiveError(RuntimeError):
    """Raised on credential / rate-limit / response errors."""


@dataclass
class FetchResult:
    ticker: str
    rows_fetched: int
    rows_total: int
    cache_path: Optional[Path]
    incremental: bool
    elapsed_s: float


def load_credentials(env_path: Path = SECRETS_PATH) -> dict:
    """Load Massive credentials from secrets/massive.env. Raises if
    MASSIVE_API_KEY is missing."""
    if not env_path.exists():
        raise MassiveError(f"credentials file not found: {env_path}")
    load_dotenv(env_path, override=False)
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        raise MassiveError(f"MASSIVE_API_KEY not set after loading {env_path}")
    return {
        "api_key": key,
        "s3_access_key_id": os.environ.get("MASSIVE_S3_ACCESS_KEY_ID", "").strip(),
        "s3_secret": os.environ.get("MASSIVE_S3_SECRET", "").strip(),
        "s3_endpoint": os.environ.get("MASSIVE_S3_ENDPOINT", "").strip(),
        "s3_bucket": os.environ.get("MASSIVE_S3_BUCKET", "").strip(),
    }


class MassiveClient:
    """Rate-limited REST client. One instance per process — the rate
    limiter is per-client and not coordinated across processes.
    """

    def __init__(
        self,
        api_key: str,
        *,
        min_request_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise MassiveError("api_key required")
        self.api_key = api_key
        self.min_interval = float(min_request_interval_s)
        self._last_request_at: float = 0.0
        self._session = session or requests.Session()

    @classmethod
    def from_env(cls, env_path: Path = SECRETS_PATH, **kwargs) -> "MassiveClient":
        creds = load_credentials(env_path)
        return cls(creds["api_key"], **kwargs)

    def _wait_for_slot(self) -> None:
        if self._last_request_at == 0.0:
            return
        elapsed = time.monotonic() - self._last_request_at
        deficit = self.min_interval - elapsed
        if deficit > 0:
            time.sleep(deficit)

    def fetch_daily_bars(
        self,
        ticker: str,
        start: str,
        end: str,
        *,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """Fetch daily bars [start, end] inclusive. Dates as YYYY-MM-DD.

        Returns DataFrame indexed by Date (UTC midnight pd.Timestamp)
        with columns Open/High/Low/Close/Volume — matching the existing
        helio/data CSV layout so the factory loader works unchanged.

        Empty DataFrame on no-data-in-window. Raises MassiveError on
        plan / auth failure, or after MAX_RETRIES_ON_429 backoff cycles.
        """
        if not ticker:
            raise MassiveError("ticker required")
        url = f"{API_BASE}/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}"
        params = {
            "apiKey": self.api_key,
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": 50000,
        }

        for attempt in range(MAX_RETRIES_ON_429 + 1):
            self._wait_for_slot()
            try:
                r = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                raise MassiveError(f"network error fetching {ticker}: {e}") from e
            self._last_request_at = time.monotonic()

            if r.status_code == 200:
                return _parse_aggs_response(r.json(), ticker=ticker)
            if r.status_code == 429:
                if attempt >= MAX_RETRIES_ON_429:
                    raise MassiveError(
                        f"rate-limited fetching {ticker} after "
                        f"{MAX_RETRIES_ON_429} retries: {r.text[:200]}"
                    )
                backoff = 30.0 * (2 ** attempt)
                time.sleep(backoff)
                continue
            if r.status_code in (401, 403):
                raise MassiveError(
                    f"plan/auth error ({r.status_code}) for {ticker}: "
                    f"{r.text[:300]}"
                )
            raise MassiveError(
                f"unexpected status {r.status_code} for {ticker}: {r.text[:200]}"
            )

        raise MassiveError(f"fetch failed for {ticker} after retries")


def _parse_aggs_response(payload: dict, *, ticker: str) -> pd.DataFrame:
    """Translate polygon-style aggs response to OHLCV DataFrame."""
    if payload.get("status") not in ("OK", "DELAYED"):
        # Empty window is allowed (status NOT_FOUND with no results)
        if not payload.get("results"):
            return _empty_bars_df()
    results = payload.get("results") or []
    if not results:
        return _empty_bars_df()
    rows = []
    for r in results:
        ts = r.get("t")  # epoch ms (UTC midnight aligned for daily bars)
        if ts is None:
            continue
        rows.append({
            "Date": pd.Timestamp(ts, unit="ms", tz="UTC").tz_localize(None).normalize(),
            "Open": r.get("o"),
            "High": r.get("h"),
            "Low": r.get("l"),
            "Close": r.get("c"),
            "Volume": r.get("v"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_bars_df()
    df = df.set_index("Date").sort_index()
    # Drop any row with NaN OHLC (corrupt entries)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def _empty_bars_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], name="Date"),
    )


# ─── disk cache ───────────────────────────────────────────────────────

def cache_path(ticker: str, *, root: Optional[Path] = None) -> Path:
    # Resolve at call time so tests can monkey-patch DATA_MASSIVE_DIR.
    root = root if root is not None else DATA_MASSIVE_DIR
    safe = ticker.upper().replace("/", "_").replace("=", "_")
    return root / f"{safe}_daily.csv"


def fetch_to_cache(
    ticker: str,
    *,
    client: Optional[MassiveClient] = None,
    years_back: float = 2.0,
    refresh: bool = False,
) -> FetchResult:
    """Pull `ticker` to CSV cache. Idempotent — re-fetches only the
    incremental window since the last cached bar (or full window if no
    cache exists / refresh=True).

    years_back is capped by the plan's actual window; if the cached file
    already covers more than years_back, we still pull the incremental
    window from cache_end to today.
    """
    t0 = time.perf_counter()
    if client is None:
        client = MassiveClient.from_env()
    path = cache_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: Optional[pd.DataFrame] = None
    if path.exists() and not refresh:
        try:
            existing = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
        except Exception:
            existing = None

    # End date: yesterday rather than today. Daily aggregates for the
    # current session aren't settled until market close + ~5 min, and the
    # free tier rejects in-progress days.
    today = date.today()
    end_dt = today - timedelta(days=1)
    if existing is not None and len(existing) > 0:
        start_dt = existing.index.max().date() + timedelta(days=1)
        if start_dt > end_dt:
            # Already up-to-date
            return FetchResult(
                ticker=ticker, rows_fetched=0, rows_total=len(existing),
                cache_path=path, incremental=True,
                elapsed_s=time.perf_counter() - t0,
            )
    else:
        requested_days = int(365 * years_back)
        # Free tier hard-caps at ~730 days back from today. Stay 10 days
        # inside that boundary so an off-by-one in the moving cutoff
        # doesn't trip plan/auth errors.
        safe_max_days = 720
        days_back = min(requested_days, safe_max_days)
        start_dt = end_dt - timedelta(days=days_back)

    new_df = client.fetch_daily_bars(
        ticker,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
    )

    if existing is not None and len(existing) > 0:
        combined = pd.concat([existing, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = new_df

    combined.to_csv(path, index=True, index_label="Date")
    return FetchResult(
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
    client: Optional[MassiveClient] = None,
    years_back: float = 2.0,
    refresh: bool = False,
    on_progress=None,
) -> list[FetchResult]:
    """Fetch a list of tickers sequentially, respecting the client's
    rate limit. `on_progress(idx, total, result)` callback optional."""
    if client is None:
        client = MassiveClient.from_env()
    results: list[FetchResult] = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        try:
            r = fetch_to_cache(t, client=client, years_back=years_back, refresh=refresh)
        except MassiveError as e:
            r = FetchResult(
                ticker=t, rows_fetched=0, rows_total=0,
                cache_path=None, incremental=False, elapsed_s=0.0,
            )
            # Caller can inspect cache_path=None to detect failures.
            # Re-raise only on auth/plan issues, since those are not
            # recoverable across the loop.
            if "plan/auth" in str(e) or "401" in str(e) or "403" in str(e):
                raise
        results.append(r)
        if on_progress:
            try:
                on_progress(i + 1, total, r)
            except Exception:
                pass
    return results
