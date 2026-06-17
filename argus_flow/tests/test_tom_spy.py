"""Tests for forge.tom_spy.backtest.

Pure-math tests (no yfinance). Synthesize a deterministic price series
and verify the backtest enters/exits on the correct calendar days.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from forge.tom_spy import backtest as tom


# ─── _month_groups ───────────────────────────────────────────────────

def _make_daily_series(start: str, end: str, weekday_only: bool = True) -> pd.DataFrame:
    """Daily price series ascending linearly. Excludes weekends by default."""
    idx = pd.date_range(start=start, end=end, freq="D")
    if weekday_only:
        idx = idx[idx.weekday < 5]
    df = pd.DataFrame(
        {"Close": [100.0 + 0.1 * i for i in range(len(idx))]},
        index=idx,
    )
    return df


def test_month_groups_partitions_by_yyyy_mm():
    df = _make_daily_series("2024-01-01", "2024-03-31")
    groups = tom._month_groups(df)
    assert "2024-01" in groups
    assert "2024-02" in groups
    assert "2024-03" in groups
    # Jan 2024 has 23 weekdays
    assert len(groups["2024-01"]) == 23
    # Feb 2024 has 21 weekdays
    assert len(groups["2024-02"]) == 21


# ─── backtest entry/exit-day logic ───────────────────────────────────

def test_backtest_enters_at_4th_to_last_bar_of_month(monkeypatch):
    """With entry_offset=4 and exit_offset=3, the first trade should
    enter at the 4th-to-last weekday of the first month and exit at
    the 3rd weekday of the second month."""
    df = _make_daily_series("2024-01-01", "2024-04-01")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)

    result = tom.backtest(ticker="SPY", period="ignored",
                            entry_offset=4, exit_offset=3)
    trades = result["trades_detail"]
    assert len(trades) >= 2
    # Jan 2024 has 23 weekdays. 4th-to-last weekday is index 19
    # (counting from 0). Jan 31 is the last weekday; Jan 30 = -1,
    # Jan 29 = -2, Jan 26 = -3 (skip weekend), Jan 25 = -4.
    # 0-indexed: last is index 22, -4 = index 18 → but we want
    # idx[-(entry_offset + 1)] = idx[-5] which is index 18.
    first_entry = trades[0]["entry_date"]
    # Jan 2024 weekdays = [1,2,3,4,5, 8,9,10,11,12, 15,16,17,18,19,
    #                      22,23,24,25,26, 29,30,31] → 23 dates,
    # 4th-to-last weekday is index 19 = Jan 25.
    assert first_entry == "2024-01-25"
    # Exit at 3rd weekday of Feb 2024: Feb weekdays start
    # [1,2, 5,6,7, ...]. 0-indexed: idx 0=Feb 1, idx 1=Feb 2, idx 2=Feb 5.
    # exit_offset=3 means we use idx 2 (0-indexed 3rd).
    first_exit = trades[0]["exit_date"]
    assert first_exit == "2024-02-05"


def test_backtest_skips_months_with_too_few_bars(monkeypatch):
    """If a month has fewer trading days than entry_offset+1, skip it."""
    # Create a series with one fully-traded month and one near-empty
    # month (only 3 weekdays) at the end so the last won't qualify.
    df = _make_daily_series("2024-01-01", "2024-02-03")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)

    result = tom.backtest(ticker="SPY", period="ignored",
                            entry_offset=4, exit_offset=3)
    # Jan has 23 weekdays (enough); Feb has 1-2 + the 1st = 1 weekday.
    # We expect 0 trades because there's no usable next month.
    trades = result.get("trades_detail", [])
    assert len(trades) == 0 or all(t["entry_date"].startswith("2024-01")
                                       for t in trades)


def test_backtest_per_trade_pnl_computed_from_closes(monkeypatch):
    """Verify pnl_pct = (exit_px / entry_px - 1) * 100."""
    df = _make_daily_series("2024-01-01", "2024-03-31")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)

    result = tom.backtest(ticker="SPY", period="ignored",
                            entry_offset=4, exit_offset=3)
    trades = result["trades_detail"]
    for t in trades:
        expected = (t["exit_px"] / t["entry_px"] - 1.0) * 100.0
        assert t["pnl_pct"] == pytest.approx(expected, abs=1e-4)


def test_backtest_handles_no_data():
    """Empty data should return an error dict, not raise."""
    import forge.tom_spy.backtest as tom_mod
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    original = tom_mod._fetch_daily
    tom_mod._fetch_daily = lambda *a, **kw: empty
    try:
        result = tom_mod.backtest(ticker="SPY", period="1y")
        assert "error" in result
        assert "insufficient" in result["error"].lower()
    finally:
        tom_mod._fetch_daily = original


def test_per_trade_helpers_extract_correct_fields(monkeypatch):
    df = _make_daily_series("2024-01-01", "2024-04-01")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)
    result = tom.backtest(ticker="SPY", period="ignored")
    pnls = tom.per_trade_pnls(result)
    dates = tom.per_trade_dates(result)
    assert len(pnls) == len(result["trades_detail"])
    assert all(isinstance(p, float) for p in pnls)
    assert all(isinstance(d, str) for d in dates)


# ─── summary statistics ──────────────────────────────────────────────

def test_backtest_summary_contains_expected_keys(monkeypatch):
    df = _make_daily_series("2024-01-01", "2024-12-31")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)
    result = tom.backtest(ticker="SPY", period="ignored")
    for key in ("n_trades", "win_rate", "profit_factor", "avg_pnl_pct",
                "cagr_pct", "max_drawdown_pct", "spy_buyhold_cagr_pct"):
        assert key in result, f"missing {key}"


def test_backtest_with_steady_uptrend_yields_positive_pf(monkeypatch):
    """Linearly-increasing prices → every TOM trade is positive →
    PF should be infinity (no losses) and WR=100%."""
    df = _make_daily_series("2020-01-01", "2024-12-31")
    monkeypatch.setattr(tom, "_fetch_daily", lambda *a, **kw: df)
    result = tom.backtest(ticker="SPY", period="ignored")
    # Every trade should win because prices monotonically increase
    assert result["win_rate"] == 1.0
    import math
    assert math.isinf(result["profit_factor"]) or result["profit_factor"] > 100
