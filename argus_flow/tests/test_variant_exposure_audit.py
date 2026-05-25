"""Tests for ops.audit.run_variant_exposure_audit.

Pin the math (per-pick notional, fleet-pct, concentration thresholds)
so future allocation changes don't silently break the audit's output.
"""
from __future__ import annotations

import pytest

from ops.audit.run_variant_exposure_audit import (
    SINGLE_TICKER_RED_PCT,
    SINGLE_TICKER_WARN_PCT,
    _compute_combined_exposure,
)


def _stub_picks(variants: list[dict]) -> list[dict]:
    """Build a list of synthetic variant pick dicts."""
    return variants


# ─── Combined exposure math ──────────────────────────────────────────

def test_single_variant_single_pick_unknown_label():
    """A strategy_label not in allocation_factors -> 0 notional."""
    picks = [{
        "strategy_label": "forge_does_not_exist_in_alloc_factors",
        "picks": [{"ticker": "SPY", "score": 25.0}],
    }]
    result = _compute_combined_exposure(picks, anchor_usd=100_000)
    assert result["fleet_notional_usd"] == 0
    assert result["per_ticker"][0]["ticker"] == "SPY"


def test_two_variants_no_overlap(monkeypatch):
    """Two variants holding different tickers — no concentration."""
    import ops.audit.run_variant_exposure_audit as mod
    monkeypatch.setattr(mod, "_allocation_for", lambda label: 1.0)
    monkeypatch.setattr(mod, "_per_strategy_cap_for", lambda label: 0.4)
    picks = [
        {"strategy_label": "A", "picks": [{"ticker": "SPY", "score": 25.0}]},
        {"strategy_label": "B", "picks": [{"ticker": "QQQ", "score": 22.0}]},
    ]
    result = _compute_combined_exposure(picks, anchor_usd=100_000)
    # Each variant: 100K × 1.0 × 0.4 = 40K notional, 1 pick each
    # Fleet total: 80K
    assert result["fleet_notional_usd"] == 80_000
    # Each ticker = 50% of fleet — would trigger RED warning
    spy_row = next(t for t in result["per_ticker"] if t["ticker"] == "SPY")
    assert spy_row["pct_of_fleet"] == pytest.approx(50.0)
    assert any(w["ticker"] == "SPY" for w in result["warnings"])


def test_two_variants_overlap_double_counts_notional(monkeypatch):
    """When two variants pick the SAME ticker, the combined notional
    is the sum — concentration matters."""
    import ops.audit.run_variant_exposure_audit as mod
    monkeypatch.setattr(mod, "_allocation_for", lambda label: 1.0)
    monkeypatch.setattr(mod, "_per_strategy_cap_for", lambda label: 0.4)
    picks = [
        {"strategy_label": "A", "picks": [{"ticker": "GLD", "score": 30.0}]},
        {"strategy_label": "B", "picks": [{"ticker": "GLD", "score": 28.0}]},
    ]
    result = _compute_combined_exposure(picks, anchor_usd=100_000)
    # Each variant: 40K. Both pick GLD -> GLD notional = 80K = fleet
    assert result["fleet_notional_usd"] == 80_000
    gld = next(t for t in result["per_ticker"] if t["ticker"] == "GLD")
    assert gld["notional_usd"] == 80_000
    assert gld["pct_of_fleet"] == pytest.approx(100.0)
    assert gld["n_holders"] == 2
    # 100% > RED threshold -> warning
    assert any(w["ticker"] == "GLD" and w["level"] == "RED"
               for w in result["warnings"])


def test_per_pick_weight_within_variant(monkeypatch):
    """A variant with 3 picks gives each pick 1/3 of variant notional."""
    import ops.audit.run_variant_exposure_audit as mod
    monkeypatch.setattr(mod, "_allocation_for", lambda label: 1.0)
    monkeypatch.setattr(mod, "_per_strategy_cap_for", lambda label: 0.6)
    picks = [{
        "strategy_label": "A",
        "picks": [
            {"ticker": "XLK", "score": 30.0},
            {"ticker": "XLE", "score": 25.0},
            {"ticker": "XLF", "score": 20.0},
        ],
    }]
    result = _compute_combined_exposure(picks, anchor_usd=300_000)
    # Variant notional: 300K × 1.0 × 0.6 = 180K, split 3 ways = 60K each
    for tkr in ("XLK", "XLE", "XLF"):
        row = next(t for t in result["per_ticker"] if t["ticker"] == tkr)
        assert row["notional_usd"] == 60_000


def test_warning_thresholds_match_constants():
    """Pin the warning + red thresholds so they're auditable."""
    assert SINGLE_TICKER_WARN_PCT == 30.0
    assert SINGLE_TICKER_RED_PCT == 50.0


def test_skip_variants_with_errors(monkeypatch):
    """An errored variant shouldn't crash the audit."""
    import ops.audit.run_variant_exposure_audit as mod
    monkeypatch.setattr(mod, "_allocation_for", lambda label: 1.0)
    monkeypatch.setattr(mod, "_per_strategy_cap_for", lambda label: 0.4)
    picks = [
        {"variant": "X", "error": "rank failed"},
        {"strategy_label": "B", "picks": [{"ticker": "SPY", "score": 25.0}]},
    ]
    result = _compute_combined_exposure(picks, anchor_usd=100_000)
    # Only B contributes
    assert result["fleet_notional_usd"] == 40_000


def test_warn_level_threshold(monkeypatch):
    """30-49% exposure -> WARN, not RED."""
    import ops.audit.run_variant_exposure_audit as mod
    monkeypatch.setattr(mod, "_allocation_for", lambda label: 1.0)
    monkeypatch.setattr(mod, "_per_strategy_cap_for", lambda label: 0.4)
    # 3 variants, A+B pick SPY, C picks QQQ. Each variant 1 pick.
    # Each variant notional 40K. SPY total = 80K. QQQ = 40K. Fleet 120K.
    # SPY = 66.7% -> RED. QQQ = 33.3% -> WARN.
    picks = [
        {"strategy_label": "A", "picks": [{"ticker": "SPY", "score": 25.0}]},
        {"strategy_label": "B", "picks": [{"ticker": "SPY", "score": 24.0}]},
        {"strategy_label": "C", "picks": [{"ticker": "QQQ", "score": 22.0}]},
    ]
    result = _compute_combined_exposure(picks, anchor_usd=100_000)
    levels = {w["ticker"]: w["level"] for w in result["warnings"]}
    assert levels["SPY"] == "RED"
    assert levels["QQQ"] == "WARN"
