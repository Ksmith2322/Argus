"""Tests for helio.roi_proof_core: the pure-function stats layer of the
ROI proof engine. Sample-size discipline + bootstrap CI + risk-adjusted
ratios are tested in isolation here so the orchestrator can be tested
against a small synthetic dataset.
"""

from __future__ import annotations

import math

import pytest

from helio import roi_proof_core as core


# ---------------------------------------------------------------------------
# Sample-size discipline
# ---------------------------------------------------------------------------

def test_sample_size_constants_match_protocol():
    """Per project_decisive_test_protocol.md: 30 / 75 / 138."""
    assert core.CONTINUATION_N == 30
    assert core.SERIOUS_N == 75
    assert core.CONVICTION_N == 138


def test_insufficient_sample_is_explicit_type():
    """A small-n result must be a non-numeric sentinel so callers cannot
    accidentally arithmetic-promote it."""
    bad = core.InsufficientSample(actual_n=10, required_n=30)
    assert bad.actual_n == 10
    assert bad.required_n == 30
    with pytest.raises(TypeError):
        _ = bad + 1  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Annualized return
# ---------------------------------------------------------------------------

def test_total_return_matches_pnls_over_capital():
    pnls = [10, -5, 20]
    assert core.total_return(pnls, 100.0) == pytest.approx(0.25)


def test_total_return_zero_capital_returns_zero():
    assert core.total_return([10, 20], 0.0) == 0.0


def test_annualized_return_scales_simple():
    """30-day return of 5% annualizes to ~60.83% (5% * 365/30)."""
    pnls = [50.0]
    ann = core.annualized_return(pnls, deployed_capital_usd=1000.0, days_observed=30.0)
    assert ann == pytest.approx(0.05 * 365 / 30, rel=1e-3)


def test_annualized_return_zero_days_returns_zero():
    assert core.annualized_return([100], 1000, 0) == 0.0


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------

def test_bootstrap_ci_brackets_a_known_mean():
    values = [0.05, 0.04, 0.06, 0.03, 0.07, 0.05, 0.04, 0.06, 0.05, 0.05] * 5
    lo, hi = core.bootstrap_ci(values, statistic_fn=lambda xs: sum(xs) / len(xs),
                                n_resamples=400, ci=0.95, seed=1)
    assert lo < 0.05 < hi


def test_bootstrap_ci_empty_input_returns_zeros():
    lo, hi = core.bootstrap_ci([], lambda xs: sum(xs))
    assert lo == 0.0 and hi == 0.0


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def test_max_drawdown_simple():
    """Equity goes 0 -> 10 -> 5 -> 8 -> 2. Peak at 10, trough at 2 = DD 8."""
    pnls = [10, -5, 3, -6]
    assert core.max_drawdown(pnls) == 8


def test_max_drawdown_no_drawdown():
    pnls = [1, 2, 3, 4]
    assert core.max_drawdown(pnls) == 0


def test_return_over_drawdown_ratio():
    pnls = [10, -5, 3, -6]  # total 2, max DD 8
    assert core.return_over_drawdown(pnls) == pytest.approx(2 / 8)


def test_return_over_drawdown_no_dd_returns_zero():
    assert core.return_over_drawdown([1, 2, 3]) == 0.0


# ---------------------------------------------------------------------------
# Sortino + Information Ratio
# ---------------------------------------------------------------------------

def test_sortino_blocked_below_continuation_n():
    short_returns = [0.01] * 5
    res = core.sortino_ratio(short_returns)
    assert isinstance(res, core.InsufficientSample)
    assert res.required_n == core.CONTINUATION_N


def test_sortino_returns_number_at_continuation_n():
    """At exactly CONTINUATION_N, the function should produce a number,
    even if uncertain — the gate is sample-size, not statistical power."""
    returns = [0.01, -0.005, 0.02, -0.01] * (core.CONTINUATION_N // 4 + 1)
    returns = returns[: core.CONTINUATION_N]
    res = core.sortino_ratio(returns)
    assert isinstance(res, float)


def test_sortino_zero_downside_returns_zero():
    """When no observations are below the target, downside_deviation is
    zero and Sortino is reported as 0 (rather than +inf which would
    overstate the signal)."""
    returns = [0.01] * core.CONTINUATION_N
    res = core.sortino_ratio(returns)
    assert res == 0.0


def test_information_ratio_length_mismatch_raises():
    with pytest.raises(ValueError):
        core.information_ratio([0.01] * 30, [0.01] * 29)


def test_information_ratio_blocked_below_continuation_n():
    res = core.information_ratio([0.01] * 5, [0.005] * 5)
    assert isinstance(res, core.InsufficientSample)


def test_information_ratio_positive_when_strategy_consistently_beats_bench():
    """Strategy returns 0.02, benchmark 0.01, alternating with small noise.
    Strategy should produce a positive IR."""
    strat = [0.02, 0.025, 0.018] * (core.CONTINUATION_N // 3 + 1)
    bench = [0.01, 0.012, 0.009] * (core.CONTINUATION_N // 3 + 1)
    strat = strat[: core.CONTINUATION_N]
    bench = bench[: core.CONTINUATION_N]
    res = core.information_ratio(strat, bench)
    assert isinstance(res, float)
    assert res > 0


# ---------------------------------------------------------------------------
# Trade-removed stress
# ---------------------------------------------------------------------------

def test_trade_removed_stress_basic():
    pnls = [100, 50, -30, -20, 200]  # total 300; best=200; worst=-30
    out = core.trade_removed_stress(pnls)
    assert out["n"] == 5
    assert out["total_pnl"] == 300
    assert out["net_remove_best_1"] == 100  # 300-200
    assert out["net_remove_worst_1"] == 330  # 300 - (-30)
    assert out["concentration_score"] == pytest.approx(200 / 300, rel=1e-3)


def test_trade_removed_stress_short_series():
    pnls = [10, 20]  # less than 3
    out = core.trade_removed_stress(pnls)
    # remove_best_3 etc. fall back to total when n<3
    assert out["net_remove_best_3"] == 30
    assert out["net_remove_worst_3"] == 30


def test_trade_removed_stress_empty():
    out = core.trade_removed_stress([])
    assert out["n"] == 0


def test_trade_removed_stress_negative_total_concentration_zero():
    """When total PnL is non-positive, concentration_score is 0 (not
    negative). Concentration is only meaningful for profitable series."""
    pnls = [-10, -20, -30]
    out = core.trade_removed_stress(pnls)
    assert out["concentration_score"] == 0.0


# ---------------------------------------------------------------------------
# Friction model
# ---------------------------------------------------------------------------

def test_friction_subtracts_commission_and_slippage_per_asset_class():
    """A $10 stock trade with $1000 notional: stock friction is
    $1.30 commission + 0.08% slippage = $2.10 friction. Net PnL = $7.90."""
    out = core.friction_adjusted_pnls([10.0], [1000.0], "stock")
    assert out[0] == pytest.approx(10.0 - 1.30 - 1000 * 0.0008)


def test_friction_unknown_asset_class_falls_back_to_stock():
    out = core.friction_adjusted_pnls([10.0], [1000.0], "this_is_not_a_class")
    expected = core.friction_adjusted_pnls([10.0], [1000.0], "stock")
    assert out == expected


def test_friction_overrides_apply():
    out = core.friction_adjusted_pnls(
        [10.0], [1000.0], "stock",
        overrides={"commission_per_trade_usd": 0.0, "slippage_pct": 0.0},
    )
    assert out[0] == 10.0


def test_friction_length_mismatch_raises():
    with pytest.raises(ValueError):
        core.friction_adjusted_pnls([1, 2, 3], [100, 200], "stock")


def test_friction_forex_class_has_zero_commission():
    """Per the default model, forex via IdealPro has zero commission and
    only spread-based slippage."""
    out = core.friction_adjusted_pnls([10.0], [50000.0], "forex")
    cfg = core.DEFAULT_FRICTION["forex"]
    expected = 10.0 - cfg["commission_per_trade_usd"] - 50000 * cfg["slippage_pct"]
    assert out[0] == pytest.approx(expected)
