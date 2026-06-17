"""Tests for helio.backtest_factory.cross_sectional.

These tests use synthetic price grids where the cross-sectional ranking
outcome is deterministic, so we can assert exactly which tickers get
picked at each rebalance.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from helio.backtest_factory.cross_sectional import (
    XSMomentumResult,
    _load_universe_closes,
    _month_starts,
    run_xs_momentum,
)


def _write_csv(tmp_path: Path, ticker: str, dates: pd.DatetimeIndex, closes: list) -> None:
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                       "Close": closes, "Volume": [1_000_000] * len(closes)},
                      index=dates)
    df.index.name = "Date"
    df.to_csv(tmp_path / f"{ticker}_daily.csv", index=True, index_label="Date")


# ─── universe loading ────────────────────────────────────────────────

def test_load_universe_closes_returns_wide_df(tmp_path):
    dates = pd.date_range(start="2024-01-02", periods=10, freq="B")
    _write_csv(tmp_path, "AAA", dates, [100.0 + i for i in range(10)])
    _write_csv(tmp_path, "BBB", dates, [50.0 + i for i in range(10)])
    closes = _load_universe_closes(["AAA", "BBB"], tmp_path)
    assert closes.shape == (10, 2)
    assert "AAA" in closes.columns and "BBB" in closes.columns
    assert closes["AAA"].iloc[0] == 100.0


def test_load_universe_closes_drops_dates_with_any_missing(tmp_path):
    # AAA has 10 days; BBB has only 7 — should restrict to 7 common dates
    dates_a = pd.date_range(start="2024-01-02", periods=10, freq="B")
    dates_b = pd.date_range(start="2024-01-08", periods=7, freq="B")
    _write_csv(tmp_path, "AAA", dates_a, [100.0] * 10)
    _write_csv(tmp_path, "BBB", dates_b, [50.0] * 7)
    closes = _load_universe_closes(["AAA", "BBB"], tmp_path)
    # Common window: intersection
    assert len(closes) <= 7


def test_load_universe_closes_raises_when_no_files(tmp_path):
    with pytest.raises(ValueError, match="no usable tickers"):
        _load_universe_closes(["DOES_NOT_EXIST"], tmp_path)


# ─── month_starts ────────────────────────────────────────────────────

def test_month_starts_picks_first_trading_day():
    # Two months of business days
    dates = pd.date_range(start="2024-01-02", end="2024-02-29", freq="B")
    starts = _month_starts(dates)
    # First trading day of Jan (2024-01-02) and Feb (2024-02-01)
    assert pd.Timestamp("2024-01-02") in starts
    assert pd.Timestamp("2024-02-01") in starts


# ─── run_xs_momentum end-to-end ──────────────────────────────────────

def test_xs_momentum_picks_top_by_momentum(tmp_path):
    """Synthetic: 4 tickers, only AAA has strong recent momentum.
    With top_k=1, AAA should be the only pick at each rebalance."""
    # 400 business days (~19 months)
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0 + 0.5 * i for i in range(n)])  # strong uptrend
    _write_csv(tmp_path, "BBB", dates, [100.0 - 0.1 * i for i in range(n)])  # downtrend
    _write_csv(tmp_path, "CCC", dates, [100.0] * n)                             # flat
    _write_csv(tmp_path, "DDD", dates, [100.0 + np.sin(i / 20) for i in range(n)])  # oscillating

    result = run_xs_momentum(
        ["AAA", "BBB", "CCC", "DDD"], data_dir=tmp_path,
        lookback_days=63, skip_recent_days=5, top_k=1, slippage_bps=0.0,
    )
    assert isinstance(result, XSMomentumResult)
    # AAA should account for ALL the trades (only ticker picked)
    assert result.n_trades > 0
    tickers_traded = {t["ticker"] for t in result.trades}
    assert tickers_traded == {"AAA"}


def test_xs_momentum_slippage_reduces_pnl(tmp_path):
    """Slippage of 10bps per round trip should subtract 0.1% from each
    trade's pnl_pct (deterministic, not random)."""
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0 + 0.5 * i for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [100.0 - 0.1 * i for i in range(n)])
    _write_csv(tmp_path, "CCC", dates, [100.0] * n)
    _write_csv(tmp_path, "DDD", dates, [100.0 + 0.05 * i for i in range(n)])

    no_slip = run_xs_momentum(
        ["AAA", "BBB", "CCC", "DDD"], data_dir=tmp_path,
        lookback_days=63, skip_recent_days=5, top_k=1, slippage_bps=0.0,
    )
    with_slip = run_xs_momentum(
        ["AAA", "BBB", "CCC", "DDD"], data_dir=tmp_path,
        lookback_days=63, skip_recent_days=5, top_k=1, slippage_bps=10.0,
    )
    # Same number of trades (slippage doesn't change signals)
    assert with_slip.n_trades == no_slip.n_trades
    # Each with-slip trade is exactly 0.1pct lower (gross - 10bps)
    for tns, ts in zip(no_slip.trades, with_slip.trades):
        assert tns["ticker"] == ts["ticker"]
        assert tns["entry_dt"] == ts["entry_dt"]
        assert ts["pnl_pct"] == pytest.approx(tns["pnl_pct"] - 0.10, abs=1e-3)
        # gross_pnl_pct preserved unchanged
        assert ts["gross_pnl_pct"] == pytest.approx(tns["gross_pnl_pct"], abs=1e-3)


def test_xs_momentum_rejects_top_k_too_large(tmp_path):
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0] * n)
    _write_csv(tmp_path, "BBB", dates, [100.0] * n)
    with pytest.raises(ValueError, match="top_k.*> universe"):
        run_xs_momentum(["AAA", "BBB"], data_dir=tmp_path, top_k=5)


def test_xs_momentum_skips_warmup_with_no_history(tmp_path):
    """If the universe has < lookback_days bars, no trades should emit
    (warm-up not satisfied)."""
    dates = pd.date_range(start="2024-01-02", periods=100, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0 + i for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [100.0 - i for i in range(n)])
    # lookback=252 > 100 bars available → zero trades
    result = run_xs_momentum(
        ["AAA", "BBB"], data_dir=tmp_path,
        lookback_days=252, skip_recent_days=21, top_k=1,
    )
    assert result.n_trades == 0


def test_xs_momentum_trade_dates_match_rebalance_schedule(tmp_path):
    """Trades' entry_dt and exit_dt should land on month-start dates."""
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0 + 0.5 * i for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [100.0 - 0.1 * i for i in range(n)])
    _write_csv(tmp_path, "CCC", dates, [100.0] * n)
    _write_csv(tmp_path, "DDD", dates, [100.0 + 0.05 * i for i in range(n)])

    result = run_xs_momentum(
        ["AAA", "BBB", "CCC", "DDD"], data_dir=tmp_path,
        lookback_days=63, skip_recent_days=5, top_k=1, slippage_bps=0.0,
    )
    closes = _load_universe_closes(["AAA", "BBB", "CCC", "DDD"], tmp_path)
    month_starts = set(_month_starts(closes.index))
    for t in result.trades:
        assert t["entry_dt"] in month_starts, f"entry {t['entry_dt']} not a month-start"
        assert t["exit_dt"] in month_starts, f"exit {t['exit_dt']} not a month-start"


def test_dual_momentum_drops_negative_momentum_winners(tmp_path):
    """absolute_filter=True: if the top-ranked ticker has negative absolute
    momentum (price 252-21 days ago > price 5 days ago), it should be SKIPPED
    even if it's the highest of a universe of negatives."""
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    # All tickers in decline; AAA declines least but still negative
    _write_csv(tmp_path, "AAA", dates, [200.0 - i * 0.05 for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [200.0 - i * 0.10 for i in range(n)])
    _write_csv(tmp_path, "CCC", dates, [200.0 - i * 0.15 for i in range(n)])
    _write_csv(tmp_path, "DDD", dates, [200.0 - i * 0.20 for i in range(n)])

    # Without absolute filter, AAA would be picked (least negative mom)
    plain = run_xs_momentum(["AAA","BBB","CCC","DDD"], data_dir=tmp_path,
                            lookback_days=63, skip_recent_days=5, top_k=1)
    plain_tickers = {t["ticker"] for t in plain.trades}
    assert plain_tickers == {"AAA"}, f"plain xs should pick AAA, got {plain_tickers}"

    # With absolute filter, AAA is dropped (negative absolute momentum) → no trades
    dual = run_xs_momentum(["AAA","BBB","CCC","DDD"], data_dir=tmp_path,
                           lookback_days=63, skip_recent_days=5, top_k=1,
                           absolute_filter=True)
    assert dual.n_trades == 0, (
        f"dual_momentum should hold no positions when all assets are negative; "
        f"got {dual.n_trades} trades on {set(t['ticker'] for t in dual.trades)}"
    )


def test_dual_momentum_with_safe_ticker_rotates_to_bonds_in_drawdown(tmp_path):
    """When the universe is in drawdown and a safe ticker is given, dual_momentum
    rotates to the safe asset instead of sitting in cash."""
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    # All equity-style assets decline; SAFE (treasury) rises
    _write_csv(tmp_path, "AAA", dates, [200.0 - i * 0.05 for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [200.0 - i * 0.10 for i in range(n)])
    _write_csv(tmp_path, "SAFE", dates, [100.0 + i * 0.01 for i in range(n)])

    dual = run_xs_momentum(["AAA","BBB"], data_dir=tmp_path,
                           lookback_days=63, skip_recent_days=5, top_k=1,
                           absolute_filter=True, safe_ticker="SAFE")
    tickers = {t["ticker"] for t in dual.trades}
    assert tickers == {"SAFE"}, f"expected all positions in SAFE, got {tickers}"


def test_xs_momentum_trades_have_required_fields(tmp_path):
    """Every emitted trade must have the schema bootstrap_profit_factor expects."""
    dates = pd.date_range(start="2023-01-02", periods=400, freq="B")
    n = len(dates)
    _write_csv(tmp_path, "AAA", dates, [100.0 + 0.5 * i for i in range(n)])
    _write_csv(tmp_path, "BBB", dates, [100.0] * n)
    result = run_xs_momentum(
        ["AAA", "BBB"], data_dir=tmp_path,
        lookback_days=63, skip_recent_days=5, top_k=1,
    )
    required = {"ticker", "entry_dt", "exit_dt", "entry_price", "exit_price",
                "pnl_pct", "gross_pnl_pct", "bars_held", "direction", "exit_reason"}
    for t in result.trades:
        missing = required - set(t.keys())
        assert not missing, f"trade missing fields: {missing}"
        assert t["direction"] == "LONG"
        assert t["exit_reason"] == "rebalance"
