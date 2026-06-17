"""Tests for the C1+C3+H2+M1 regime-gate fixes (2026-05-25 audit).

Pin the fail-CLOSED behavior so a future refactor can't accidentally
revert it.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest


def _set_baseline(xs):
    """Reset runner state to baseline + clear any open picks."""
    xs.configure_variant("baseline")
    xs.PARAMS["regime_gate"] = None


def _set_regime_variant(xs):
    """Reset + configure the regime variant for testing."""
    xs.configure_variant("legacy15_regime")
    assert xs.PARAMS.get("regime_gate") == "spy_above_200dma"


@pytest.fixture(autouse=True)
def _restore_runner():
    from forge.xs_momentum import runner as xs
    yield
    _set_baseline(xs)


# ─── C1 fix: fail-CLOSED on missing data ────────────────────────────

def test_regime_gate_blocks_when_data_missing(tmp_path, monkeypatch):
    """When fetch_regime_data returns empty, the runner must block
    the rebalance, not 'fail-OPEN and trade anyway'."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Force missing regime data
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data", lambda **_: {}
    )
    # Force month-rebalance-due (force=True)
    summary = xs.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "regime_data_missing" in summary["reason"]


def test_regime_gate_blocks_when_gate_name_unknown(tmp_path, monkeypatch):
    """An unknown gate name should fail-CLOSED, not silently skip."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Override with garbage gate name
    xs.PARAMS["regime_gate"] = "definitely_not_a_real_gate"
    summary = xs.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "unknown_regime_gate" in summary["reason"]


def test_regime_gate_blocks_on_check_exception(tmp_path, monkeypatch):
    """Any unexpected exception in the gate check should fail-CLOSED."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Make the gate function raise
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    summary = xs.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "regime_check_error" in summary["reason"]


# ─── C3 fix: stale-data validation ──────────────────────────────────

def test_regime_gate_blocks_on_stale_data(tmp_path, monkeypatch):
    """If the latest regime bar is >4 days old, refuse to trade."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Mock a series with last bar from 10 days ago
    old_ts = pd.Timestamp.now() - pd.Timedelta(days=10)
    stale_series = pd.Series([400.0] * 250,
                             index=pd.date_range(end=old_ts, periods=250))
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": stale_series},
    )
    summary = xs.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "regime_data_stale" in summary["reason"]


# ─── H2 fix: regime gate before allocation check ────────────────────

def test_regime_gate_runs_before_allocation_check(tmp_path, monkeypatch):
    """Even if allocation is 0, a bearish regime should produce a
    regime_skip (exiting any open positions), not an alloc-only block."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Bearish regime: rising series flipped to declining
    bear_series = pd.Series(
        [400.0 - i * 0.5 for i in range(250)],
        index=pd.date_range(end=pd.Timestamp.now(), periods=250),
    )
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": bear_series},
    )
    # Force allocation to 0 so we can verify the regime check runs
    # BEFORE the alloc check (it must, to exit open positions)
    # Patch at the import site (forge.xs_momentum.runner imports the
    # name directly), not at the helio.fleet_sizing module.
    monkeypatch.setattr(
        "forge.xs_momentum.runner.get_allocation_factor",
        lambda label: 0.0,
    )
    summary = xs.evaluate_once(force=True)
    # Must be regime_skip, NOT alloc_factor=0 block
    assert summary["action"] == "regime_skip"
    assert "regime_gate" in summary["reason"]


# ─── Sanity pin: bullish regime allows trading ──────────────────────

def test_regime_gate_allows_when_bullish(tmp_path, monkeypatch):
    """When regime is bullish (SPY above 200dma), the runner should
    proceed past the gate to the allocation check."""
    from forge.xs_momentum import runner as xs
    _set_regime_variant(xs)
    # Build a strongly bullish series so SMA200 < latest price
    bull_series = pd.Series(
        [100.0 + i * 0.5 for i in range(300)],
        index=pd.date_range(end=pd.Timestamp.now(), periods=300),
    )
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": bull_series},
    )
    # Block at allocation (proves regime gate passed)
    # Patch at the import site (forge.xs_momentum.runner imports the
    # name directly), not at the helio.fleet_sizing module.
    monkeypatch.setattr(
        "forge.xs_momentum.runner.get_allocation_factor",
        lambda label: 0.0,
    )
    summary = xs.evaluate_once(force=True)
    # Bullish regime + alloc=0 -> alloc-block (NOT regime_skip)
    assert summary["action"] == "blocked"
    assert "alloc_factor" in summary["reason"]
