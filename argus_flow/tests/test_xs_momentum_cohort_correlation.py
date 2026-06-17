"""Tests for the cohort correlation audit module.

Pin the math (Pearson, Sharpe, DD) so a future refactor doesn't
silently break the audit's quantitative conclusions.
"""
from __future__ import annotations

import math

import pytest

from ops.audit.run_xs_momentum_cohort_correlation import (
    VARIANTS,
    _equal_weight,
    _pearson,
    _portfolio_stats,
)


# ─── Pearson correlation ──────────────────────────────────────────────

def test_pearson_perfectly_correlated_equals_one():
    xs = [0.01, 0.02, 0.03, 0.04, 0.05]
    ys = [0.02, 0.04, 0.06, 0.08, 0.10]  # ys = 2*xs
    assert _pearson(xs, ys) == pytest.approx(1.0, abs=1e-9)


def test_pearson_anti_correlated_equals_minus_one():
    xs = [0.01, 0.02, 0.03, 0.04, 0.05]
    ys = [-x for x in xs]
    assert _pearson(xs, ys) == pytest.approx(-1.0, abs=1e-9)


def test_pearson_independent_series_near_zero():
    """Independent random-like series should have correlation near 0."""
    import random
    random.seed(42)
    xs = [random.gauss(0, 1) for _ in range(200)]
    ys = [random.gauss(0, 1) for _ in range(200)]
    r = _pearson(xs, ys)
    assert abs(r) < 0.2, f"expected near-zero correlation, got {r}"


def test_pearson_handles_constant_series():
    """Zero-variance series should return NaN."""
    xs = [0.01] * 10
    ys = [0.02, 0.03, 0.04] + [0.0] * 7
    assert math.isnan(_pearson(xs, ys))


def test_pearson_returns_nan_for_short_series():
    assert math.isnan(_pearson([0.01], [0.02]))


def test_pearson_returns_nan_for_mismatched_lengths():
    assert math.isnan(_pearson([0.01, 0.02], [0.03]))


# ─── Portfolio stats ──────────────────────────────────────────────────

def test_portfolio_stats_empty_returns_no_div_zero():
    s = _portfolio_stats([])
    assert s["max_dd_pct"] == 0.0
    assert s["cagr_pct"] == 0.0


def test_portfolio_stats_positive_monotonic_no_dd():
    """Strictly positive returns produce zero drawdown."""
    rets = [0.01, 0.02, 0.015, 0.005]
    s = _portfolio_stats(rets)
    assert s["max_dd_pct"] == 0.0
    assert s["cagr_pct"] > 0


def test_portfolio_stats_computes_dd():
    """A peak-then-drawdown sequence gives positive DD."""
    # equity sequence: 1.0 -> 1.1 -> 1.21 -> 0.969 -> 1.066
    # (rets: +10%, +10%, -20%, +10%)
    rets = [0.10, 0.10, -0.20, 0.10]
    s = _portfolio_stats(rets)
    assert s["max_dd_pct"] > 15  # DD ~= 20% from peak 1.21 to trough 0.968
    assert s["max_dd_pct"] < 25


def test_portfolio_stats_sharpe_for_known_series():
    """A series with mean=1%/mo, sd=1%/mo -> Sharpe = 1.0 * sqrt(12) ≈ 3.46"""
    rets = [0.02, 0.00] * 50  # alternating 2% and 0% -> mean 1%, sd ~1%
    s = _portfolio_stats(rets)
    assert s["sharpe"] is not None
    assert 3.0 < s["sharpe"] < 4.0


# ─── Equal-weight combining ──────────────────────────────────────────

def test_equal_weight_two_series():
    a = [0.10, 0.20]
    b = [0.30, 0.40]
    combined = _equal_weight([a, b])
    assert combined == pytest.approx([0.20, 0.30])


def test_equal_weight_empty_input():
    assert _equal_weight([]) == []


def test_equal_weight_aligns_to_shortest():
    """Different-length series align to the shortest."""
    a = [0.10, 0.20, 0.30]
    b = [0.40, 0.50]
    combined = _equal_weight([a, b])
    assert len(combined) == 2


# ─── Variant catalog ─────────────────────────────────────────────────

def test_variant_catalog_matches_runner_registry():
    """The cohort-audit VARIANTS dict must include every variant in
    forge.xs_momentum.runner._VARIANT_REGISTRY."""
    from forge.xs_momentum.runner import _VARIANT_REGISTRY
    runner_variants = set(_VARIANT_REGISTRY.keys())
    audit_variants = set(VARIANTS.keys())
    missing = runner_variants - audit_variants
    assert not missing, (
        f"audit module missing variants from runner registry: {missing}"
    )


def test_style_and_style_top3_share_universe_key():
    """The two style variants must point at the same universe so the
    correlation reflects concentration difference, not universe drift."""
    s_uk, s_frac = VARIANTS["style"]
    t_uk, t_frac = VARIANTS["style_top3"]
    assert s_uk == t_uk == "style_factors_8"
    assert s_frac != t_frac, "concentration must differ between variants"
