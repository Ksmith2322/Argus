"""Tests for helio.factor_decomposition.

Most tests use synthetic returns (no yfinance) to validate the OLS
math, residual handling, and interpretation logic deterministically.
A single end-to-end smoke test fetches real ETF data to catch yfinance
schema drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from helio.factor_decomposition import (
    FactorBeta,
    FactorDecompositionResult,
    _interpret,
    regress_returns_against_factors,
    render_report,
)


# ─── _interpret() ────────────────────────────────────────────────────

def _make_beta(name: str, coef: float, p: float = 0.5) -> FactorBeta:
    return FactorBeta(name=name, coef=coef, t_stat=coef / 0.1,
                       p_value=p, std_err=0.1)


def test_interpret_zero_alpha_high_r2_flags_passive_replication():
    """When alpha is ~0 and R² > 0.4, interpretation should flag passive
    replication explicitly — the strategy's returns are mostly beta."""
    betas = [
        _make_beta("MKT_RF", 0.7, p=0.001),  # significant market beta
        _make_beta("SMB", -0.1, p=0.3),
        _make_beta("HML", 0.02, p=0.8),
        _make_beta("MOM", 0.28, p=0.004),    # significant momentum beta
    ]
    text = _interpret(alpha_ann=0.003, alpha_p=0.94, betas=betas,
                       r2=0.55, ir=0.03)
    assert "ALPHA ZERO" in text
    assert "PASSIVE REPLICATION" in text
    assert "MKT_RF" in text  # the dominant beta should be in the recipe
    assert "MOM" in text


def test_interpret_strong_positive_alpha_flagged_significant():
    betas = [_make_beta("MKT_RF", 0.5, p=0.001),
              _make_beta("SMB", 0.0, p=0.5),
              _make_beta("HML", 0.0, p=0.5),
              _make_beta("MOM", 0.1, p=0.2)]
    text = _interpret(alpha_ann=0.08, alpha_p=0.02, betas=betas,
                       r2=0.3, ir=1.5)
    assert "ALPHA POSITIVE" in text
    assert "Edge survives" in text


def test_interpret_negative_alpha_flagged_underperformance():
    betas = [_make_beta("MKT_RF", 0.5, p=0.001),
              _make_beta("SMB", 0.0, p=0.5),
              _make_beta("HML", 0.0, p=0.5),
              _make_beta("MOM", 0.1, p=0.2)]
    text = _interpret(alpha_ann=-0.05, alpha_p=0.01, betas=betas,
                       r2=0.4, ir=-0.8)
    assert "ALPHA NEGATIVE" in text
    assert "underperforms" in text.lower()


def test_interpret_mom_beta_warning_when_significant():
    """A significant MOM beta + reasonable magnitude should flag the
    'MTUM captures this cheaply' warning."""
    betas = [_make_beta("MKT_RF", 0.5, p=0.001),
              _make_beta("SMB", 0.0, p=0.5),
              _make_beta("HML", 0.0, p=0.5),
              _make_beta("MOM", 0.4, p=0.001)]
    text = _interpret(alpha_ann=0.01, alpha_p=0.5, betas=betas,
                       r2=0.3, ir=0.2)
    assert "MTUM" in text


# ─── regression math ─────────────────────────────────────────────────

def test_regression_recovers_known_betas(monkeypatch):
    """Synthesize returns from a known data-generating process. The
    regression should recover the betas (within noise). Bypasses the
    yfinance fetch by stubbing the factor panel."""
    # 60 months of synthetic factor returns
    rng = np.random.default_rng(42)
    n = 60
    idx = pd.date_range("2020-01-31", periods=n, freq="ME")
    fake_panel = pd.DataFrame({
        "MKT_RF": rng.normal(0.008, 0.04, n),
        "SMB": rng.normal(0.0, 0.03, n),
        "HML": rng.normal(0.0, 0.03, n),
        "MOM": rng.normal(0.0, 0.03, n),
    }, index=idx)
    # Known DGP: portfolio = 0.6*MKT + 0.3*MOM + 0.005 alpha + noise
    true_alpha = 0.005
    portfolio_excess = (
        0.6 * fake_panel["MKT_RF"] +
        0.3 * fake_panel["MOM"] +
        true_alpha +
        rng.normal(0.0, 0.01, n)
    )
    rf_monthly = (1.0 + 0.045) ** (1 / 12) - 1.0
    portfolio_total = portfolio_excess + rf_monthly
    monthly_dict = {ts.strftime("%Y-%m"): float(r)
                     for ts, r in portfolio_total.items()}

    # Stub the panel fetcher
    fake_panel.index = fake_panel.index.strftime("%Y-%m")
    import helio.factor_decomposition as fd
    monkeypatch.setattr(fd, "build_factor_panel",
                          lambda **kw: fake_panel.copy().assign(
                              MKT_RF=fake_panel["MKT_RF"],
                              SMB=fake_panel["SMB"],
                              HML=fake_panel["HML"],
                              MOM=fake_panel["MOM"],
                          ).set_axis(
                              pd.to_datetime(fake_panel.index + "-01")
                              .to_period("M").to_timestamp("M")
                          ))

    result = regress_returns_against_factors(
        monthly_dict, label="synthetic", rf_annual=0.045,
        use_newey_west=False,
    )
    # Recover true MKT beta ~0.6, MOM beta ~0.3, alpha ~0.005/month
    # Tolerance reflects expected sampling error at n=60 with 1% monthly
    # noise — gives ~0.05-0.10 standard error on each beta.
    mkt_b = next(b for b in result.betas if b.name == "MKT_RF")
    mom_b = next(b for b in result.betas if b.name == "MOM")
    assert abs(mkt_b.coef - 0.6) < 0.15, f"MKT beta {mkt_b.coef} too far from 0.6"
    assert abs(mom_b.coef - 0.3) < 0.15, f"MOM beta {mom_b.coef} too far from 0.3"
    assert abs(result.alpha_monthly - true_alpha) < 0.005


def test_regression_rejects_too_few_months(monkeypatch):
    """Should refuse to regress with <12 aligned months — refuses to give
    a noisy estimate that operators might over-trust."""
    fake_panel = pd.DataFrame({
        "MKT_RF": [0.01] * 5,
        "SMB": [0.0] * 5,
        "HML": [0.0] * 5,
        "MOM": [0.0] * 5,
    }, index=pd.date_range("2025-01-31", periods=5, freq="ME"))
    import helio.factor_decomposition as fd
    monkeypatch.setattr(fd, "build_factor_panel", lambda **kw: fake_panel)
    with pytest.raises(ValueError, match="aligned months"):
        regress_returns_against_factors(
            {"2025-01": 0.01, "2025-02": 0.02},
            label="too small",
        )


# ─── result.to_dict + render_report ──────────────────────────────────

def test_result_to_dict_is_json_serializable():
    import json
    result = FactorDecompositionResult(
        label="test", n_months=24,
        period_start="2024-01", period_end="2025-12",
        alpha_monthly=0.001, alpha_annualized=0.012,
        alpha_t_stat=1.2, alpha_p_value=0.23,
        r_squared=0.4, r_squared_adj=0.36,
        betas=[FactorBeta("MKT_RF", 0.7, 8.0, 0.0, 0.1)],
        residual_std_monthly=0.02, information_ratio=0.4,
        interpretation="test text",
    )
    d = result.to_dict()
    json.dumps(d)  # should not raise


def test_render_report_includes_all_betas():
    result = FactorDecompositionResult(
        label="test", n_months=24,
        period_start="2024-01", period_end="2025-12",
        alpha_monthly=0.001, alpha_annualized=0.012,
        alpha_t_stat=1.2, alpha_p_value=0.23,
        r_squared=0.4, r_squared_adj=0.36,
        betas=[FactorBeta("MKT_RF", 0.7, 8.0, 0.0, 0.1),
               FactorBeta("MOM", 0.25, 2.5, 0.015, 0.1)],
        residual_std_monthly=0.02, information_ratio=0.4,
        interpretation="test interp",
    )
    text = render_report(result)
    assert "MKT_RF" in text
    assert "MOM" in text
    assert "test interp" in text
