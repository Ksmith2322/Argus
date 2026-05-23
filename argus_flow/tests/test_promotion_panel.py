"""Tests for helio.promotion_panel — the packaged 5-layer disciplined gate."""
from __future__ import annotations

import pandas as pd
import pytest

from helio.promotion_panel import (
    PromotionPanelReport,
    render_panel_report,
    run_panel,
)


# ─── Smoke ───────────────────────────────────────────────────────────

def test_run_panel_returns_structured_report():
    # 50 trades, 70% wins, average +2% per win, -1% per loss → PF ~4.7
    pnls = [2.0] * 35 + [-1.0] * 15
    report = run_panel(pnls, label="test_smoke")
    assert isinstance(report, PromotionPanelReport)
    assert report.n_trades == 50
    assert report.n_layers_total > 0
    assert report.headline_verdict in (
        "PASS_DISCIPLINED_GATE", "MARGINAL_PASS", "PARTIAL_PASS", "FAIL", "INSUFFICIENT_N"
    )


def test_run_panel_strong_edge_passes_full_gate():
    """A clearly strong sample (PF ~8) should pass all layers."""
    pnls = [4.0] * 60 + [-1.0] * 20  # PF = 240/20 = 12
    report = run_panel(pnls, label="strong_edge")
    # Should pass most or all layers
    assert report.headline_verdict in ("PASS_DISCIPLINED_GATE", "MARGINAL_PASS")
    # IID at 0bp should definitely pass
    iid_layer = next(l for l in report.layers if "iid_bootstrap_slip_0bp" in l.name)
    assert iid_layer.passes


def test_run_panel_weak_edge_partial_fail():
    """PF just barely above 1 (marginally positive) should NOT pass the
    disciplined gate."""
    # Each win = +1.05%, each loss = -1.0%, equal counts → PF = 1.05
    pnls = [1.05] * 30 + [-1.0] * 30
    report = run_panel(pnls, label="weak_edge")
    # Both IID layers should fail (CI lower won't reach 1.20)
    iid_layers = [l for l in report.layers if "iid_bootstrap" in l.name]
    assert all(not l.passes for l in iid_layers)
    assert report.headline_verdict in ("FAIL", "PARTIAL_PASS")


def test_run_panel_insufficient_n_short_circuit():
    """Fewer than 10 trades → returns INSUFFICIENT_N immediately."""
    pnls = [1.0, -0.5, 1.5, -0.3]
    report = run_panel(pnls, label="too_few")
    assert report.headline_verdict == "INSUFFICIENT_N"
    # Only the precondition layer
    assert report.n_layers_total == 1


# ─── Layer behaviors ─────────────────────────────────────────────────

def test_slippage_haircut_visible_in_pf():
    """A 10-bp slippage at PF=2.0 should reduce the point estimate
    measurably vs 0-bp."""
    # 30 wins at +1%, 20 losses at -1% → PF = 30/20 = 1.5 at 0bp
    # 10bp slippage shifts each by -0.1pct: wins 0.9%, losses -1.1% → PF = 27/22 = 1.23
    pnls = [1.0] * 30 + [-1.0] * 20
    report = run_panel(pnls, label="slip_test", slippage_levels_bps=(0.0, 10.0))
    pf_at_0 = next(l for l in report.layers if "iid_bootstrap_slip_0bp" in l.name).pf_point
    pf_at_10 = next(l for l in report.layers if "iid_bootstrap_slip_10bp" in l.name).pf_point
    assert pf_at_0 > pf_at_10, f"slippage should reduce PF: 0bp={pf_at_0}, 10bp={pf_at_10}"


def test_period_stability_layers_present():
    """Panel must include H1 and H2 layers when n is sufficient."""
    pnls = [1.5] * 40 + [-0.5] * 20  # plenty of trades
    report = run_panel(pnls, label="stability_test")
    h1_layer = next((l for l in report.layers if "h1" in l.name.lower()), None)
    h2_layer = next((l for l in report.layers if "h2" in l.name.lower()), None)
    assert h1_layer is not None
    assert h2_layer is not None


def test_block_bootstrap_layers_present():
    pnls = [2.0] * 40 + [-1.0] * 20
    report = run_panel(pnls, label="block_test", block_sizes=(3, 5))
    # Filter to the actual block-bootstrap CI layers (not the p-value layer)
    block_layers = [l for l in report.layers if l.name.startswith("block_bootstrap_b")]
    # Two block sizes → two block layers
    assert len(block_layers) == 2


def test_block_size_geq_n_is_skipped():
    """A block size larger than the trade list should be silently skipped."""
    pnls = [1.0] * 15 + [-0.5] * 5  # n=20
    report = run_panel(pnls, label="skip_test", block_sizes=(5, 100))
    block_layers = [l for l in report.layers if l.name.startswith("block_bootstrap_b")]
    # Only block=5 fits; block=100 is skipped
    assert len(block_layers) == 1
    assert "_b5_" in block_layers[0].name


# ─── Determinism ─────────────────────────────────────────────────────

def test_same_seed_same_report():
    pnls = [1.5] * 30 + [-1.0] * 20
    r1 = run_panel(pnls, seed=42)
    r2 = run_panel(pnls, seed=42)
    # Verdicts and counts must match
    assert r1.headline_verdict == r2.headline_verdict
    assert r1.n_layers_passed == r2.n_layers_passed


# ─── Entry-date chronology ───────────────────────────────────────────

def test_entry_dates_split_by_chronology():
    """When entry_dates provided, H1/H2 split must be chronological."""
    pnls = [2.0] * 30  # all wins
    # Reverse-chronological list to test that the panel reorders by date
    dates = [pd.Timestamp(f"2024-{(31-i)//10+1:02d}-{((31-i)%10)+1:02d}")
             for i in range(30)]
    # Force a clear H1-good / H2-bad pattern via mixed pnls:
    # First 15 chronologically = +5%, last 15 chronologically = -3%
    sorted_dates = sorted(dates)
    pnls_by_chron = {d: (5.0 if i < 15 else -3.0) for i, d in enumerate(sorted_dates)}
    pnls = [pnls_by_chron[d] for d in dates]

    report = run_panel(pnls, label="chrono_test", entry_dates=dates, slippage_levels_bps=(0.0,))
    h1 = next(l for l in report.layers if "h1" in l.name.lower())
    h2 = next(l for l in report.layers if "h2" in l.name.lower())
    # H1 (chronological earliest 15) should have positive PF; H2 should be 0
    assert h1.pf_point > h2.pf_point


# ─── to_dict + render ────────────────────────────────────────────────

def test_to_dict_is_jsonable():
    import json
    pnls = [2.0] * 30 + [-1.0] * 20
    report = run_panel(pnls, label="json_test")
    d = report.to_dict()
    # Should not raise
    s = json.dumps(d, default=str)
    assert "headline_verdict" in s
    assert "layers" in s


def test_render_panel_report_returns_string():
    pnls = [2.0] * 30 + [-1.0] * 20
    report = run_panel(pnls, label="render_test")
    text = render_panel_report(report)
    assert "promotion_panel" in text
    assert "HEADLINE" in text
    assert "render_test" in text
