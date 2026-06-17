"""Tests for helio.spy_benchmark — SPY-over-active-timestamps comparison.

Coverage focus: alignment to specific trade windows, missing-data
handling, the per-trade benchmark dataclass, and the strategy-level
aggregation summary. Uses synthetic SpyBars so the test is stable
regardless of what live SPY data happens to be checked into helio/data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helio import spy_benchmark as bench


def _ts(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _synthetic_bars() -> list[bench.SpyBar]:
    """Three weeks of SPY at one bar per day, prices climbing 0.5% per day."""
    bars: list[bench.SpyBar] = []
    base = 600.0
    for i in range(21):
        bars.append(bench.SpyBar(
            ts=_ts(2026, 5, 1) + timedelta(days=i),
            close=base * (1.005 ** i),
        ))
    return bars


# ---------------------------------------------------------------------------
# Window lookup
# ---------------------------------------------------------------------------

def test_spy_return_between_inside_data():
    bars = _synthetic_bars()
    start = _ts(2026, 5, 3)
    end = _ts(2026, 5, 10)
    ret = bench.spy_return_between(bars, start, end)
    assert ret is not None
    # 7 daily bars at 0.5% growth ≈ 1.005**7 - 1 ≈ 0.0354
    assert ret == pytest.approx(1.005 ** 7 - 1, rel=1e-6)


def test_spy_return_between_uses_at_or_before():
    bars = _synthetic_bars()
    # Mid-day timestamps should snap to the daily bar at-or-before
    start = _ts(2026, 5, 3, 14)
    end = _ts(2026, 5, 10, 14)
    ret = bench.spy_return_between(bars, start, end)
    assert ret == pytest.approx(1.005 ** 7 - 1, rel=1e-6)


def test_spy_return_between_before_data_returns_none():
    bars = _synthetic_bars()
    ret = bench.spy_return_between(bars, _ts(2024, 1, 1), _ts(2024, 1, 5))
    assert ret is None


def test_spy_return_between_zero_window_returns_none():
    bars = _synthetic_bars()
    t = _ts(2026, 5, 3)
    assert bench.spy_return_between(bars, t, t) is None


def test_spy_return_between_empty_bars_returns_none():
    assert bench.spy_return_between([], _ts(2026, 5, 3), _ts(2026, 5, 10)) is None


# ---------------------------------------------------------------------------
# Per-trade benchmarking
# ---------------------------------------------------------------------------

def test_benchmark_trade_inside_data_produces_excess():
    bars = _synthetic_bars()
    tb = bench.benchmark_trade(
        bars,
        entry_ts="2026-05-03T00:00:00+00:00",
        exit_ts="2026-05-10T00:00:00+00:00",
        strategy_pnl=80.0,        # $80 on $1000 notional = 8% return
        notional=1000.0,
    )
    assert tb.spy_data_available is True
    assert tb.strategy_return_frac == pytest.approx(0.08)
    assert tb.spy_return_frac == pytest.approx(1.005 ** 7 - 1, rel=1e-6)
    assert tb.excess_return_frac == pytest.approx(0.08 - (1.005 ** 7 - 1), rel=1e-3)


def test_benchmark_trade_outside_data_marks_unavailable():
    bars = _synthetic_bars()
    tb = bench.benchmark_trade(
        bars,
        entry_ts="2024-01-01T00:00:00+00:00",
        exit_ts="2024-01-05T00:00:00+00:00",
        strategy_pnl=20.0,
        notional=500.0,
    )
    assert tb.spy_data_available is False
    assert tb.spy_return_frac is None
    assert tb.excess_return_frac is None
    # Strategy return is still computable (independent of SPY)
    assert tb.strategy_return_frac == pytest.approx(0.04)


def test_benchmark_trade_zero_notional_uses_zero_return():
    bars = _synthetic_bars()
    tb = bench.benchmark_trade(
        bars,
        entry_ts="2026-05-03T00:00:00+00:00",
        exit_ts="2026-05-04T00:00:00+00:00",
        strategy_pnl=10.0, notional=0.0,
    )
    assert tb.strategy_return_frac == 0.0


def test_benchmark_trade_unparseable_ts_marks_unavailable():
    bars = _synthetic_bars()
    tb = bench.benchmark_trade(
        bars,
        entry_ts="not a timestamp",
        exit_ts="2026-05-10T00:00:00+00:00",
        strategy_pnl=10.0, notional=1000.0,
    )
    assert tb.spy_data_available is False


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_aggregate_benchmark_zero_coverage_returns_zeros():
    out = bench.aggregate_benchmark([
        bench.TradeBenchmark(
            entry_ts="x", exit_ts="y", strategy_return_frac=0.05,
            spy_return_frac=None, excess_return_frac=None, spy_data_available=False,
        )
    ])
    assert out["n_trades"] == 1
    assert out["n_with_spy_data"] == 0
    assert out["coverage_pct"] == 0.0


def test_aggregate_benchmark_partial_coverage():
    items = [
        bench.TradeBenchmark(
            entry_ts="a", exit_ts="b", strategy_return_frac=0.05,
            spy_return_frac=0.02, excess_return_frac=0.03, spy_data_available=True,
        ),
        bench.TradeBenchmark(
            entry_ts="c", exit_ts="d", strategy_return_frac=0.04,
            spy_return_frac=None, excess_return_frac=None, spy_data_available=False,
        ),
        bench.TradeBenchmark(
            entry_ts="e", exit_ts="f", strategy_return_frac=-0.01,
            spy_return_frac=0.02, excess_return_frac=-0.03, spy_data_available=True,
        ),
    ]
    out = bench.aggregate_benchmark(items)
    assert out["n_trades"] == 3
    assert out["n_with_spy_data"] == 2
    assert out["coverage_pct"] == pytest.approx(2 / 3, rel=1e-3)
    assert out["wins_vs_spy"] == 1
    assert out["losses_vs_spy"] == 1
    assert out["mean_excess_return"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# CSV loader (smoke; real data file may or may not be present)
# ---------------------------------------------------------------------------

def test_load_spy_bars_returns_empty_when_no_data(tmp_path):
    # Pass an explicit path to a missing file; loader returns []
    out = bench.load_spy_bars(tmp_path / "no_such_file.csv")
    assert out == []


def test_load_spy_bars_parses_simple_daily_csv(tmp_path):
    p = tmp_path / "spy_simple.csv"
    p.write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-05-01,500,505,499,503,1000000\n"
        "2026-05-02,503,508,502,507,1000000\n",
        encoding="utf-8",
    )
    bars = bench.load_spy_bars(p)
    assert len(bars) == 2
    assert bars[0].close == 503.0
    assert bars[1].close == 507.0
    assert bars[0].ts < bars[1].ts
