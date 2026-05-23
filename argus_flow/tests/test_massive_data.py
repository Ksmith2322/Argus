"""Tests for helio.massive_data.

Network requests are stubbed via a fake requests.Session. We exercise:
    - parse of the polygon-compat aggs response
    - rate-limit waiting between requests
    - 429 backoff retry path
    - 401/403 plan-auth raises immediately
    - CSV cache write + incremental refresh
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, List
from unittest.mock import MagicMock

import pandas as pd
import pytest

from helio import massive_data
from helio.massive_data import (
    MassiveClient,
    MassiveError,
    _parse_aggs_response,
    cache_path,
    fetch_to_cache,
    fetch_universe,
)


# ─── helpers ──────────────────────────────────────────────────────────

def _ms(d: str) -> int:
    return int(pd.Timestamp(d).timestamp() * 1000)


def _ok_payload(bars: list[dict]) -> dict:
    return {"status": "OK", "resultsCount": len(bars), "queryCount": len(bars),
            "results": bars}


def _bar(d: str, *, o=100.0, h=101.0, l=99.0, c=100.5, v=1_000_000) -> dict:
    return {"t": _ms(d), "o": o, "h": h, "l": l, "c": c, "v": v, "n": 1}


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._body = json_body
        self.text = str(json_body)[:1000]

    def json(self):
        return self._body


class FakeSession:
    """Returns queued responses in FIFO. records each get() call."""

    def __init__(self, responses: Iterable[FakeResponse]):
        self._responses = list(responses)
        self.calls: List[tuple] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if not self._responses:
            raise AssertionError("FakeSession ran out of queued responses")
        return self._responses.pop(0)


# ─── parsing ──────────────────────────────────────────────────────────

def test_parse_aggs_basic():
    payload = _ok_payload([
        _bar("2026-01-02"),
        _bar("2026-01-03", c=101.0),
    ])
    df = _parse_aggs_response(payload, ticker="SPY")
    assert len(df) == 2
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index[0] == pd.Timestamp("2026-01-02")
    assert df["Close"].iloc[1] == 101.0


def test_parse_aggs_empty_window():
    payload = {"status": "OK", "resultsCount": 0, "queryCount": 0, "results": []}
    df = _parse_aggs_response(payload, ticker="SPY")
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_parse_aggs_drops_nan_rows():
    payload = _ok_payload([
        _bar("2026-01-02"),
        {"t": _ms("2026-01-03"), "o": None, "h": 101, "l": 99, "c": 100.5, "v": 1000},
    ])
    df = _parse_aggs_response(payload, ticker="SPY")
    assert len(df) == 1


# ─── credentials ──────────────────────────────────────────────────────

def test_load_credentials_missing_file(tmp_path):
    bogus = tmp_path / "missing.env"
    with pytest.raises(MassiveError, match="not found"):
        massive_data.load_credentials(bogus)


def test_load_credentials_no_key(tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text("MASSIVE_API_KEY=\n", encoding="utf-8")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(MassiveError, match="MASSIVE_API_KEY not set"):
        massive_data.load_credentials(f)


# ─── client + 200 path ───────────────────────────────────────────────

def test_client_requires_key():
    with pytest.raises(MassiveError, match="api_key required"):
        MassiveClient("")


def test_fetch_daily_bars_happy_path():
    session = FakeSession([
        FakeResponse(200, _ok_payload([_bar("2026-01-02"), _bar("2026-01-03")])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    df = c.fetch_daily_bars("SPY", "2026-01-02", "2026-01-03")
    assert len(df) == 2
    assert session.calls[0][1]["apiKey"] == "k"
    assert session.calls[0][1]["sort"] == "asc"


# ─── rate limit ──────────────────────────────────────────────────────

def test_rate_limit_enforces_wait_between_calls(monkeypatch):
    # Two queued OK responses; assert the second call waits ≥ min_interval
    session = FakeSession([
        FakeResponse(200, _ok_payload([])),
        FakeResponse(200, _ok_payload([])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.10, session=session)
    sleeps: List[float] = []
    monkeypatch.setattr(massive_data.time, "sleep", lambda s: sleeps.append(s))

    c.fetch_daily_bars("SPY", "2026-01-02", "2026-01-03")
    c.fetch_daily_bars("SPY", "2026-01-02", "2026-01-03")
    # Second call should have triggered a non-trivial sleep
    assert any(s > 0 for s in sleeps), f"expected a sleep, got {sleeps}"


# ─── 429 backoff ─────────────────────────────────────────────────────

def test_fetch_429_retries_then_succeeds(monkeypatch):
    session = FakeSession([
        FakeResponse(429, {"error": "rate limit"}),
        FakeResponse(200, _ok_payload([_bar("2026-01-02")])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    monkeypatch.setattr(massive_data.time, "sleep", lambda s: None)
    df = c.fetch_daily_bars("SPY", "2026-01-02", "2026-01-02")
    assert len(df) == 1
    assert len(session.calls) == 2


def test_fetch_429_gives_up_after_max_retries(monkeypatch):
    session = FakeSession([
        FakeResponse(429, {"error": "rate limit"}),
        FakeResponse(429, {"error": "rate limit"}),
        FakeResponse(429, {"error": "rate limit"}),
        FakeResponse(429, {"error": "rate limit"}),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    monkeypatch.setattr(massive_data.time, "sleep", lambda s: None)
    with pytest.raises(MassiveError, match="rate-limited"):
        c.fetch_daily_bars("SPY", "2026-01-02", "2026-01-02")


# ─── plan/auth ───────────────────────────────────────────────────────

def test_fetch_403_raises_immediately():
    session = FakeSession([
        FakeResponse(403, {"status": "NOT_AUTHORIZED", "message": "plan"}),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    with pytest.raises(MassiveError, match="plan/auth error"):
        c.fetch_daily_bars("SPY", "2020-01-02", "2020-01-03")


# ─── cache write/incremental ─────────────────────────────────────────

def test_fetch_to_cache_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(massive_data, "DATA_MASSIVE_DIR", tmp_path)
    session = FakeSession([
        FakeResponse(200, _ok_payload([_bar("2026-05-20"), _bar("2026-05-21")])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    result = fetch_to_cache("SPY", client=c, years_back=2.0)
    assert result.cache_path is not None
    assert result.cache_path.exists()
    assert result.rows_fetched == 2
    assert result.rows_total == 2
    assert not result.incremental
    df = pd.read_csv(result.cache_path, parse_dates=["Date"])
    assert len(df) == 2
    assert "Close" in df.columns


def test_fetch_to_cache_incremental_only_fetches_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(massive_data, "DATA_MASSIVE_DIR", tmp_path)
    # Seed cache with old data
    seed_path = cache_path("SPY", root=tmp_path)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "Date": [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")],
        "Open": [100.0, 101.0], "High": [101, 102], "Low": [99, 100],
        "Close": [100.5, 101.5], "Volume": [1_000_000, 1_000_000],
    }).set_index("Date").to_csv(seed_path, index=True, index_label="Date")

    session = FakeSession([
        FakeResponse(200, _ok_payload([_bar("2026-01-04", c=102.0)])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    result = fetch_to_cache("SPY", client=c, years_back=2.0)
    assert result.incremental
    assert result.rows_fetched == 1
    assert result.rows_total == 3
    # CSV now has 3 rows
    df = pd.read_csv(result.cache_path, parse_dates=["Date"]).set_index("Date").sort_index()
    assert len(df) == 3
    assert df["Close"].iloc[-1] == 102.0
    # The API params should request start = day after last cached bar
    _, params, _ = session.calls[0]
    assert params["sort"] == "asc"
    # The url path embeds the start date — easier to assert via call
    url, _, _ = session.calls[0]
    assert "2026-01-04" in url


def test_fetch_to_cache_skips_if_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(massive_data, "DATA_MASSIVE_DIR", tmp_path)
    today = date.today()
    seed_path = cache_path("SPY", root=tmp_path)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "Date": [pd.Timestamp(today)],
        "Open": [100.0], "High": [101.0], "Low": [99.0],
        "Close": [100.5], "Volume": [1_000_000],
    }).set_index("Date").to_csv(seed_path, index=True, index_label="Date")

    session = FakeSession([])  # No responses queued — we must NOT call API
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    result = fetch_to_cache("SPY", client=c, years_back=2.0)
    assert result.rows_fetched == 0
    assert result.incremental is True
    assert len(session.calls) == 0


# ─── universe loop ───────────────────────────────────────────────────

def test_fetch_universe_progress_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(massive_data, "DATA_MASSIVE_DIR", tmp_path)
    session = FakeSession([
        FakeResponse(200, _ok_payload([_bar("2026-01-02")])),
        FakeResponse(200, _ok_payload([_bar("2026-01-02"), _bar("2026-01-03")])),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    progress: List[tuple] = []
    results = fetch_universe(
        ["SPY", "DIA"], client=c,
        on_progress=lambda i, n, r: progress.append((i, n, r.ticker, r.rows_total)),
    )
    assert len(results) == 2
    assert progress == [(1, 2, "SPY", 1), (2, 2, "DIA", 2)]


def test_fetch_universe_reraises_on_auth_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(massive_data, "DATA_MASSIVE_DIR", tmp_path)
    session = FakeSession([
        FakeResponse(403, {"status": "NOT_AUTHORIZED", "message": "plan"}),
    ])
    c = MassiveClient("k", min_request_interval_s=0.0, session=session)
    with pytest.raises(MassiveError, match="plan/auth"):
        fetch_universe(["SPY"], client=c)
