"""Tests for helio.yfinance_data.

yfinance network calls are stubbed via monkeypatching the module-level
yfinance import. We exercise the CSV cache + incremental refresh path,
and one fake-import failure path."""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from helio import yfinance_data as yfd
from helio.yfinance_data import (
    YFinanceError,
    YFFetchResult,
    cache_path,
    fetch_daily_bars,
    fetch_to_cache,
    fetch_universe,
)


def _ohlcv_frame(dates: list[str], close_start: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="Date")
    n = len(dates)
    df = pd.DataFrame({
        "Open": [close_start + i for i in range(n)],
        "High": [close_start + i + 1 for i in range(n)],
        "Low": [close_start + i - 1 for i in range(n)],
        "Close": [close_start + i + 0.5 for i in range(n)],
        "Adj Close": [close_start + i + 0.5 for i in range(n)],
        "Volume": [1_000_000] * n,
    }, index=idx)
    return df


def _install_fake_yfinance(monkeypatch, df: pd.DataFrame, raise_exc: Exception | None = None):
    """Build a fake yfinance module + stick it in sys.modules so the
    helio.yfinance_data import inside fetch_daily_bars picks it up."""
    fake = types.ModuleType("yfinance")

    def _download(*args, **kwargs):
        if raise_exc:
            raise raise_exc
        return df.copy()

    fake.download = _download
    monkeypatch.setitem(sys.modules, "yfinance", fake)


# ─── fetch_daily_bars happy / empty / error ──────────────────────────

def test_fetch_daily_bars_normalizes_schema(monkeypatch):
    src = _ohlcv_frame(["2024-01-02", "2024-01-03", "2024-01-04"])
    _install_fake_yfinance(monkeypatch, src)
    df = fetch_daily_bars("SPY", start="2024-01-02", end="2024-01-04")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.name == "Date"
    assert df.index.tz is None
    assert len(df) == 3


def test_fetch_daily_bars_empty_raises(monkeypatch):
    _install_fake_yfinance(monkeypatch, pd.DataFrame())
    with pytest.raises(YFinanceError, match="empty frame"):
        fetch_daily_bars("ZZZ", start="2024-01-02", end="2024-01-03")


def test_fetch_daily_bars_yf_exception_wrapped(monkeypatch):
    _install_fake_yfinance(monkeypatch, pd.DataFrame(), raise_exc=RuntimeError("boom"))
    with pytest.raises(YFinanceError, match="yf.download failed"):
        fetch_daily_bars("SPY", start="2024-01-02", end="2024-01-03")


def test_fetch_daily_bars_requires_period_or_range(monkeypatch):
    _install_fake_yfinance(monkeypatch, pd.DataFrame())
    with pytest.raises(YFinanceError, match="must provide"):
        fetch_daily_bars("SPY")


def test_fetch_daily_bars_strips_multiindex_columns(monkeypatch):
    # Simulate yfinance's MultiIndex shape for single ticker calls
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")], name="Date"
    )
    cols = pd.MultiIndex.from_product([
        ["Open", "High", "Low", "Close", "Adj Close", "Volume"], ["SPY"]
    ])
    data = [
        [100, 101, 99, 100.5, 100.5, 1_000_000],
        [101, 102, 100, 101.5, 101.5, 1_000_000],
    ]
    df = pd.DataFrame(data, index=idx, columns=cols)
    _install_fake_yfinance(monkeypatch, df)
    out = fetch_daily_bars("SPY", start="2024-01-02", end="2024-01-03")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_fetch_daily_bars_strips_tz(monkeypatch):
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-02", tz="America/New_York"),
         pd.Timestamp("2024-01-03", tz="America/New_York")], name="Date"
    )
    df = pd.DataFrame({
        "Open": [100, 101], "High": [101, 102], "Low": [99, 100],
        "Close": [100.5, 101.5], "Volume": [1_000_000, 1_000_000],
    }, index=idx)
    _install_fake_yfinance(monkeypatch, df)
    out = fetch_daily_bars("SPY", start="2024-01-02", end="2024-01-03")
    assert out.index.tz is None


# ─── fetch_to_cache ──────────────────────────────────────────────────

def test_fetch_to_cache_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(yfd, "DATA_YFINANCE_DIR", tmp_path)
    src = _ohlcv_frame(["2024-01-02", "2024-01-03", "2024-01-04"])
    _install_fake_yfinance(monkeypatch, src)
    result = fetch_to_cache("SPY", years_back=10)
    assert result.cache_path is not None
    assert result.cache_path.exists()
    assert result.rows_fetched == 3
    assert result.rows_total == 3
    assert not result.incremental
    df = pd.read_csv(result.cache_path, parse_dates=["Date"])
    assert len(df) == 3
    assert "Close" in df.columns


def test_fetch_to_cache_incremental_path(tmp_path, monkeypatch):
    monkeypatch.setattr(yfd, "DATA_YFINANCE_DIR", tmp_path)
    # Seed cache with two old bars
    seed_path = cache_path("SPY", root=tmp_path)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    _ohlcv_frame(["2024-01-02", "2024-01-03"]).to_csv(
        seed_path, index=True, index_label="Date"
    )
    # Subsequent fetch returns ONE more bar
    src = _ohlcv_frame(["2024-01-04"], close_start=110)
    _install_fake_yfinance(monkeypatch, src)

    result = fetch_to_cache("SPY", years_back=10)
    assert result.incremental
    assert result.rows_fetched == 1
    assert result.rows_total == 3
    df = pd.read_csv(result.cache_path, parse_dates=["Date"]).set_index("Date").sort_index()
    assert len(df) == 3


def test_fetch_to_cache_skips_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(yfd, "DATA_YFINANCE_DIR", tmp_path)
    today_iso = date.today().isoformat()
    seed_path = cache_path("SPY", root=tmp_path)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    _ohlcv_frame([today_iso]).to_csv(seed_path, index=True, index_label="Date")

    # If we accidentally call yf, we'd raise — assert no call happened
    called = {"n": 0}
    fake = types.ModuleType("yfinance")

    def _download(*args, **kwargs):
        called["n"] += 1
        return pd.DataFrame()

    fake.download = _download
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    result = fetch_to_cache("SPY", years_back=10)
    assert result.rows_fetched == 0
    assert result.incremental
    assert called["n"] == 0


# ─── fetch_universe ──────────────────────────────────────────────────

def test_fetch_universe_progress_called(tmp_path, monkeypatch):
    monkeypatch.setattr(yfd, "DATA_YFINANCE_DIR", tmp_path)
    src = _ohlcv_frame(["2024-01-02", "2024-01-03"])
    _install_fake_yfinance(monkeypatch, src)

    progress = []

    def _cb(i, n, r):
        progress.append((i, n, r.ticker, r.rows_total))

    results = fetch_universe(["SPY", "DIA"], between_ticker_sleep_s=0.0, on_progress=_cb)
    assert len(results) == 2
    assert progress == [(1, 2, "SPY", 2), (2, 2, "DIA", 2)]


def test_fetch_universe_continues_past_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(yfd, "DATA_YFINANCE_DIR", tmp_path)

    # First ticker raises, second returns OK
    calls = {"n": 0}
    good = _ohlcv_frame(["2024-01-02"])
    fake = types.ModuleType("yfinance")

    def _download(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call bombs")
        return good.copy()

    fake.download = _download
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    results = fetch_universe(["BAD", "SPY"], between_ticker_sleep_s=0.0)
    assert len(results) == 2
    assert results[0].cache_path is None  # failed
    assert results[1].cache_path is not None  # succeeded
    assert results[1].rows_total == 1
