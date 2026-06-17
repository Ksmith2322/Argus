"""Tests for forge.nov_spy.backtest + .runner.

Pure-math tests (no yfinance). Synthesize a deterministic price series
to validate entry/exit calendar logic + pnl math.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from forge.nov_spy import backtest as nov_backtest
from forge.nov_spy import runner as nov_runner


# ─── Calendar logic ──────────────────────────────────────────────────

def test_entry_day_first_weekday_of_nov_2026():
    """Nov 1, 2026 is a Sunday. First weekday is Nov 2 (Mon)."""
    assert nov_runner.is_entry_day(date(2026, 11, 2)) is True
    assert nov_runner.is_entry_day(date(2026, 11, 1)) is False  # Sun
    assert nov_runner.is_entry_day(date(2026, 11, 3)) is False


def test_exit_day_last_weekday_of_nov_2026():
    """Nov 30, 2026 is a Monday. Last weekday is Nov 30."""
    assert nov_runner.is_exit_day(date(2026, 11, 30)) is True
    assert nov_runner.is_exit_day(date(2026, 11, 27)) is False  # Fri before


def test_entry_day_when_nov1_is_weekday():
    """Nov 1, 2024 is a Friday. First weekday IS Nov 1."""
    assert nov_runner.is_entry_day(date(2024, 11, 1)) is True
    assert nov_runner.is_entry_day(date(2024, 11, 4)) is False  # next Mon


def test_exit_day_when_nov30_is_weekend():
    """Nov 30, 2024 is a Saturday. Last weekday is Nov 29 (Fri)."""
    assert nov_runner.is_exit_day(date(2024, 11, 29)) is True
    assert nov_runner.is_exit_day(date(2024, 11, 30)) is False


def test_october_dates_never_action_days():
    assert nov_runner.is_entry_day(date(2026, 10, 30)) is False
    assert nov_runner.is_exit_day(date(2026, 10, 30)) is False


def test_december_dates_never_action_days():
    assert nov_runner.is_entry_day(date(2026, 12, 1)) is False
    assert nov_runner.is_exit_day(date(2026, 12, 1)) is False


# ─── backtest pnl math ───────────────────────────────────────────────

def _make_series_with_november(year: int, nov_start: float,
                                  nov_end: float) -> pd.DataFrame:
    """Build a daily series with the November endpoints set explicitly.
    Other days linearly interpolated. Excludes weekends."""
    idx = pd.date_range(start=f"{year}-10-01", end=f"{year}-12-31", freq="D")
    idx = idx[idx.weekday < 5]
    nov_bars = [d for d in idx if d.month == 11]
    df = pd.DataFrame({"Close": [100.0] * len(idx)}, index=idx)
    if nov_bars:
        df.loc[nov_bars[0], "Close"] = nov_start
        df.loc[nov_bars[-1], "Close"] = nov_end
    return df


def test_backtest_computes_pnl_pct_from_close_to_close(monkeypatch):
    """Single year, controlled November move from 500 → 525 = +5.0%."""
    df = _make_series_with_november(2025, nov_start=500.0, nov_end=525.0)
    monkeypatch.setattr(nov_backtest, "_fetch_daily", lambda *a, **kw: df)
    result = nov_backtest.backtest(ticker="SPY", period="ignored")
    trades = result["trades_detail"]
    assert len(trades) == 1
    assert trades[0]["pnl_pct"] == pytest.approx(5.0)
    assert trades[0]["entry_px"] == 500.0
    assert trades[0]["exit_px"] == 525.0


def test_backtest_handles_negative_november(monkeypatch):
    df = _make_series_with_november(2026, nov_start=600.0, nov_end=570.0)
    monkeypatch.setattr(nov_backtest, "_fetch_daily", lambda *a, **kw: df)
    result = nov_backtest.backtest(ticker="SPY", period="ignored")
    assert result["trades_detail"][0]["pnl_pct"] == pytest.approx(-5.0)


def test_backtest_skips_year_with_fewer_than_2_nov_bars(monkeypatch):
    # Synthesize a series where November is missing
    idx = pd.date_range("2025-01-01", "2025-09-30", freq="D")
    idx = idx[idx.weekday < 5]
    df = pd.DataFrame({"Close": [100.0] * len(idx)}, index=idx)
    monkeypatch.setattr(nov_backtest, "_fetch_daily", lambda *a, **kw: df)
    result = nov_backtest.backtest(ticker="SPY", period="ignored")
    assert "error" in result or result.get("n_trades", 0) == 0


def test_per_trade_helpers(monkeypatch):
    df = _make_series_with_november(2024, nov_start=400.0, nov_end=420.0)
    monkeypatch.setattr(nov_backtest, "_fetch_daily", lambda *a, **kw: df)
    result = nov_backtest.backtest(ticker="SPY", period="ignored")
    pnls = nov_backtest.per_trade_pnls(result)
    dates = nov_backtest.per_trade_dates(result)
    assert len(pnls) == len(dates) == 1
    assert pnls[0] == pytest.approx(5.0)


# ─── runner control-flow ─────────────────────────────────────────────

def test_evaluate_noops_on_non_nov_day(monkeypatch, tmp_path):
    monkeypatch.setattr(nov_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(nov_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(nov_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(nov_runner, "is_entry_day", lambda d: False)
    monkeypatch.setattr(nov_runner, "is_exit_day", lambda d: False)
    def _boom(*a, **kw):
        raise AssertionError("must not contact IBKR on non-Nov day")
    monkeypatch.setattr(nov_runner.ibkr, "connect_with_retry", _boom)
    summary = nov_runner.evaluate_once()
    assert summary["action"] == "noop"


def test_evaluate_blocked_when_allocation_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(nov_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(nov_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(nov_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(nov_runner, "is_entry_day", lambda d: True)
    monkeypatch.setattr(nov_runner, "is_exit_day", lambda d: False)
    monkeypatch.setattr(nov_runner, "get_allocation_factor", lambda l: 0.0)
    def _boom(*a, **kw):
        raise AssertionError("must not connect IBKR at alloc=0")
    monkeypatch.setattr(nov_runner.ibkr, "connect_with_retry", _boom)
    summary = nov_runner.evaluate_once()
    assert summary["action"] == "blocked"
    assert "alloc_factor" in summary["reason"]


def test_evaluate_skips_entry_when_position_open(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"open_trade": {"entry_ts": "2025-11-03T20:00:00Z", '
        '"entry_px": 500.0, "qty": 100}, "trade_count": 1, "session_id": "x"}',
        encoding="utf-8")
    monkeypatch.setattr(nov_runner, "STATE_PATH", state_path)
    monkeypatch.setattr(nov_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(nov_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(nov_runner, "is_entry_day", lambda d: True)
    monkeypatch.setattr(nov_runner, "is_exit_day", lambda d: False)
    def _boom(*a, **kw):
        raise AssertionError("must not connect when position open")
    monkeypatch.setattr(nov_runner.ibkr, "connect_with_retry", _boom)
    monkeypatch.setattr(nov_runner, "get_allocation_factor", _boom)
    summary = nov_runner.evaluate_once()
    assert "skip_entry" in summary["action"]
