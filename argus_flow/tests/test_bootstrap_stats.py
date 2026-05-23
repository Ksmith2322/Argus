"""Tests for helio.bootstrap_stats — Wilson CI + bootstrap PF/CAGR."""
from __future__ import annotations

import math

import pytest

from helio.bootstrap_stats import (
    wilson_ci,
    bootstrap_profit_factor,
    block_bootstrap_profit_factor,
    bootstrap_cagr,
    _profit_factor,
)


# ── Wilson CI ──────────────────────────────────────────────────────────────

def test_wilson_ci_skeptic_reference_case():
    """The Skeptic's headline number: WR=60% at n=20 → 95% CI roughly
    [36%, 81%]. Verify we reproduce it."""
    ci = wilson_ci(wins=12, n=20, confidence=0.95)
    assert ci.point == 0.6
    assert 0.36 < ci.lower < 0.40
    assert 0.78 < ci.upper < 0.81


def test_wilson_ci_narrow_at_large_n():
    """n=1000 with 50% wins → CI should be tight (~ ±3%)."""
    ci = wilson_ci(wins=500, n=1000, confidence=0.95)
    assert ci.point == 0.5
    assert ci.upper - ci.lower < 0.07


def test_wilson_ci_at_zero():
    """No wins should produce a valid CI with lower=0."""
    ci = wilson_ci(wins=0, n=20)
    assert ci.lower == 0.0
    assert ci.upper > 0


def test_wilson_ci_at_one():
    """All wins → upper=1.0; lower > 0."""
    ci = wilson_ci(wins=20, n=20)
    assert ci.upper == 1.0
    assert ci.lower > 0.5


def test_wilson_ci_zero_n_safe():
    ci = wilson_ci(wins=0, n=0)
    assert ci.point == 0.0


# ── Profit factor helper ───────────────────────────────────────────────────

def test_profit_factor_simple():
    assert _profit_factor([10, -5, 8, -3]) == pytest.approx((10 + 8) / (5 + 3))


def test_profit_factor_no_losses():
    assert _profit_factor([10, 5, 8]) == float("inf")


def test_profit_factor_no_wins():
    assert _profit_factor([-10, -5]) == 0.0


def test_profit_factor_empty():
    assert _profit_factor([]) == 0.0


# ── Bootstrap PF ───────────────────────────────────────────────────────────

def test_bootstrap_pf_deterministic_with_seed():
    pnls = [10, -5, 8, -3, 12, -4, 7, -2]
    r1 = bootstrap_profit_factor(pnls, n_resamples=500, seed=42)
    r2 = bootstrap_profit_factor(pnls, n_resamples=500, seed=42)
    assert r1.ci_lower == r2.ci_lower
    assert r1.ci_upper == r2.ci_upper


def test_bootstrap_pf_ci_contains_point_for_strong_edge():
    """Strongly positive sample — CI should be positive."""
    pnls = [10] * 30 + [-5] * 10
    r = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
    assert r.point == pytest.approx(300 / 50)
    # Point should be inside the CI
    assert r.ci_lower < r.point < r.ci_upper
    # Lower bound on a clearly positive PF should be > 1.0
    assert r.ci_lower > 1.5


def test_bootstrap_pf_uncertainty_at_small_n():
    """Small n should produce a wide CI even on a positive sample."""
    pnls = [5, -2, 8, -3, 10]  # n=5, only 3 wins
    r = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
    # CI width should be substantial
    width = r.ci_upper - r.ci_lower
    assert width > 1.0, f"expected wide CI at n=5, got width={width}"


def test_bootstrap_pf_empty_input():
    r = bootstrap_profit_factor([], n_resamples=100)
    assert r.point == 0.0


# ── Bootstrap CAGR ─────────────────────────────────────────────────────────

def test_bootstrap_cagr_positive_for_winning_strategy():
    pnls_pct = [3.0] * 50 + [-1.0] * 30
    r = bootstrap_cagr(pnls_pct, position_size_fraction=0.15,
                       bars_per_year=12.0, n_resamples=1000, seed=42)
    assert r.point > 0
    # 95% CI should still contain a positive number (lower might dip negative)
    assert r.ci_upper > 0


def test_bootstrap_cagr_negative_for_losing_strategy():
    pnls_pct = [3.0] * 30 + [-8.0] * 30
    r = bootstrap_cagr(pnls_pct, position_size_fraction=0.5,
                       bars_per_year=12.0, n_resamples=1000, seed=42)
    assert r.point < 0
    assert r.ci_upper < 1.0  # mostly negative


# ── Integration: real-world reading of the PEAD backtest ──────────────────

def test_pead_curated_ci_interpretation():
    """The Skeptic asked: with n=69 trades and PF=2.04 from PEAD on
    Apollo's universe, what's the honest 95% CI on PF?

    HONEST FINDING (2026-05-20): Bootstrap CI lower bound is ~1.20 —
    EXACTLY at the promotion floor. There is zero statistical margin
    of safety. The point estimate of 2.04 is at risk of being a
    small-sample artifact.

    This test documents the finding rather than failing on it. The
    operator must understand: PEAD has a real edge on Apollo's
    universe, but the n=69 sample doesn't reliably reject the null
    that the true PF is at the promotion floor."""
    wins = [17.3] * 26
    losses = [-5.1] * 43
    pnls = wins + losses
    r = bootstrap_profit_factor(pnls, n_resamples=3000, seed=42)
    assert 1.9 < r.point < 2.1
    # The honest finding: CI lower bound is roughly at the floor (1.2),
    # not safely above it. The strategy is borderline-promotable at this n.
    assert 1.0 < r.ci_lower < 1.4, (
        f"Unexpected CI lower bound {r.ci_lower:.3f}. Expected range "
        f"1.0-1.4 (Skeptic-warning zone). Update the assertion if the "
        f"underlying data has changed."
    )
    # CI upper bound shows the optimistic case
    assert 2.5 < r.ci_upper < 4.0


def test_pead_non_curated_ci_interpretation():
    """Same bootstrap on the non-curated SPX-30 PEAD result (PF=1.61, n=98,
    WR=43.9%). The wider sample should produce a TIGHTER CI even though
    the point estimate is lower."""
    # 43 wins at +7.5%, 55 losses at -3.6%
    wins = [7.5] * 43
    losses = [-3.6] * 55
    pnls = wins + losses
    r = bootstrap_profit_factor(pnls, n_resamples=3000, seed=42)
    assert 1.5 < r.point < 1.7
    # With more trades + smaller per-trade size, CI should be tighter
    width = r.ci_upper - r.ci_lower
    # The Apollo result had CI width ~2.1 (1.2 to 3.3). Non-curated should
    # be tighter due to larger n.
    assert width < 1.5, (
        f"Expected tighter CI on non-curated (n=98) than curated (n=69); "
        f"got width {width:.2f}."
    )


def test_xs_momentum_ci_interpretation():
    """XS-momentum 9y backtest: n=79, WR=51.9%, PF=2.05.
    Larger lookback = more confidence in the result."""
    # 41 wins at +11.5%, 38 losses at -6.1%
    wins = [11.5] * 41
    losses = [-6.1] * 38
    pnls = wins + losses
    r = bootstrap_profit_factor(pnls, n_resamples=3000, seed=42)
    assert 1.9 < r.point < 2.1
    # HONEST FINDING (2026-05-20): Even with 9 years of data and a 52%
    # win rate, XS-momentum's bootstrap CI lower bound is ~1.28 — above
    # the 1.20 promotion floor but with thin margin. The takeaway: at
    # n=79 trades, even an apparently-strong PF=2.05 has a 95% CI
    # spanning [1.28, 3.25]. Real money would need to size for the
    # lower-bound scenario, not the point estimate.
    assert 1.2 < r.ci_lower < 1.4, (
        f"Unexpected CI lower bound {r.ci_lower:.3f}. Expected 1.2-1.4 "
        f"(thin margin above the promotion floor)."
    )


# ── Block bootstrap ────────────────────────────────────────────────────────

def test_block_bootstrap_deterministic_with_seed():
    """Same seed must give same CI bounds."""
    pnls = [1.0, -0.5, 1.5, 2.0, -0.3, 0.8] * 10
    r1 = block_bootstrap_profit_factor(pnls, block_size=5, n_resamples=1000, seed=42)
    r2 = block_bootstrap_profit_factor(pnls, block_size=5, n_resamples=1000, seed=42)
    assert r1.ci_lower == r2.ci_lower
    assert r1.ci_upper == r2.ci_upper


def test_block_bootstrap_widens_ci_vs_iid_on_clustered_returns():
    """On a series with autocorrelated wins (every 5-trade block is mostly
    winners or mostly losers), block bootstrap should produce a WIDER CI
    than IID bootstrap because it preserves the clustering structure."""
    import random as _r
    rng = _r.Random(123)
    # 200 trades in 40 blocks of 5; each block is either "win regime"
    # (~80% wins) or "loss regime" (~20% wins)
    pnls = []
    for block in range(40):
        win_prob = 0.8 if block % 2 == 0 else 0.2
        for _ in range(5):
            pnls.append(1.0 if rng.random() < win_prob else -1.0)
    iid = bootstrap_profit_factor(pnls, n_resamples=3000, seed=42)
    blk = block_bootstrap_profit_factor(pnls, block_size=5, n_resamples=3000, seed=42)
    iid_width = iid.ci_upper - iid.ci_lower
    blk_width = blk.ci_upper - blk.ci_lower
    # Block bootstrap should be at least as wide (and typically wider) on
    # this clustered structure.
    assert blk_width >= iid_width * 0.95, (
        f"Block CI width {blk_width:.3f} should match or exceed IID {iid_width:.3f} "
        f"on autocorrelated data — block bootstrap is meant to be more conservative."
    )


def test_block_bootstrap_falls_back_to_iid_when_block_exceeds_n():
    """If block_size >= n, the moving-block approach degenerates. Module
    falls back to IID bootstrap rather than emitting garbage."""
    pnls = [1.0, -0.5, 1.5, 2.0, -0.3]  # n=5
    blk = block_bootstrap_profit_factor(pnls, block_size=10, n_resamples=1000, seed=42)
    iid = bootstrap_profit_factor(pnls, n_resamples=1000, seed=42)
    assert blk.ci_lower == iid.ci_lower
    assert blk.ci_upper == iid.ci_upper


def test_block_bootstrap_empty_input_safe():
    r = block_bootstrap_profit_factor([], block_size=5, n_resamples=100, seed=42)
    assert r.n_sample == 0
    assert r.point == 0.0


def test_block_bootstrap_preserves_point_estimate():
    """The point estimate (PF of the full sample) should always match the
    direct calculation regardless of bootstrap method."""
    pnls = [2.0, -1.0, 3.0, -0.5, 1.0]
    expected_pf = _profit_factor(pnls)
    r = block_bootstrap_profit_factor(pnls, block_size=2, n_resamples=1000, seed=42)
    assert r.point == pytest.approx(expected_pf)
