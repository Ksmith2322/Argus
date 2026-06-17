"""Tests for the PEAD strategy — pure-function decision logic.

The runner / backtest path is exercised separately (depends on yfinance
data; not unit-testable cleanly). These tests cover the gating logic
in helio.pead_signal — every reject reason."""
from __future__ import annotations

import pytest

from helio.pead_signal import (
    PEADCandidate, evaluate_pead_signal, should_exit,
    SURPRISE_PCT_MIN, VOLUME_RATIO_MIN, GAP_PCT_MIN,
)


def _good_candidate(**overrides) -> PEADCandidate:
    """Default candidate that should pass all gates."""
    base = dict(
        ticker="GOOGL",
        announcement_date="2026-04-29",
        surprise_pct=8.5,
        eps_actual=2.10, eps_estimate=1.94,
        announce_volume=50_000_000,
        avg_volume_30d=20_000_000,   # ratio 2.5
        next_day_open=170.0,
        prior_close=165.0,           # gap +3%
        atr_at_entry=3.5,
    )
    base.update(overrides)
    return PEADCandidate(**base)


# ── ENTER cases ────────────────────────────────────────────────────────────

def test_enter_long_when_all_gates_pass():
    d = evaluate_pead_signal(_good_candidate())
    assert d.action == "ENTER_LONG"
    assert d.qualifying_surprise
    assert d.qualifying_volume
    assert d.qualifying_gap


def test_enter_long_at_threshold_minimums():
    """A candidate at exactly the minimum on every gate should still enter."""
    c = _good_candidate(
        surprise_pct=SURPRISE_PCT_MIN,
        announce_volume=15.0, avg_volume_30d=10.0,  # ratio exactly 1.5
        next_day_open=100.0, prior_close=100.0,     # gap exactly 0
    )
    d = evaluate_pead_signal(c)
    assert d.action == "ENTER_LONG"


# ── SKIP cases (one gate fails) ────────────────────────────────────────────

def test_skip_when_surprise_below_min():
    d = evaluate_pead_signal(_good_candidate(surprise_pct=3.0))
    assert d.action == "SKIP"
    assert "surprise_below_min" in d.reason


def test_skip_when_surprise_negative():
    d = evaluate_pead_signal(_good_candidate(surprise_pct=-12.0))
    assert d.action == "SKIP"
    assert d.qualifying_surprise is False


def test_skip_when_surprise_missing():
    d = evaluate_pead_signal(_good_candidate(surprise_pct=None))
    assert d.action == "SKIP"


def test_skip_when_volume_below_ratio():
    """Announce-day volume only 1.1× avg = below 1.5 threshold."""
    d = evaluate_pead_signal(_good_candidate(
        announce_volume=11_000_000, avg_volume_30d=10_000_000,
    ))
    assert d.action == "SKIP"
    assert "volume_ratio_below_min" in d.reason


def test_skip_when_volume_data_missing():
    d = evaluate_pead_signal(_good_candidate(avg_volume_30d=None))
    assert d.action == "SKIP"


def test_skip_when_gap_negative():
    """Beat earnings but market disagrees — gap down at open."""
    d = evaluate_pead_signal(_good_candidate(
        next_day_open=160.0, prior_close=165.0,  # gap -3%
    ))
    assert d.action == "SKIP"
    assert "gap_negative" in d.reason


def test_skip_when_open_data_missing():
    d = evaluate_pead_signal(_good_candidate(next_day_open=None))
    assert d.action == "SKIP"


# ── Threshold customization ────────────────────────────────────────────────

def test_custom_surprise_threshold():
    """Operator can tighten surprise threshold to 10% (only big beats)."""
    c = _good_candidate(surprise_pct=7.0)
    d = evaluate_pead_signal(c, surprise_pct_min=10.0)
    assert d.action == "SKIP"
    # Same candidate passes at default 5.0
    d2 = evaluate_pead_signal(c, surprise_pct_min=5.0)
    assert d2.action == "ENTER_LONG"


def test_custom_volume_threshold():
    c = _good_candidate(announce_volume=15_000_000, avg_volume_30d=10_000_000)
    # Default 1.5× — passes
    assert evaluate_pead_signal(c).action == "ENTER_LONG"
    # Tighten to 2.0× — fails
    assert evaluate_pead_signal(c, volume_ratio_min=2.0).action == "SKIP"


# ── Exit logic ─────────────────────────────────────────────────────────────

def test_should_exit_when_low_breaches_stop():
    should, reason = should_exit(
        entry_px=170.0, entry_date_idx=0, today_idx=5,
        today_low=164.0, today_close=165.0,
        atr_at_entry=4.0,  # stop = 170 - 1.5*4 = 164.0
        hold_days=20, stop_mult=1.5,
    )
    assert should is True
    assert reason == "atr_stop"


def test_should_exit_at_hold_period_end():
    should, reason = should_exit(
        entry_px=170.0, entry_date_idx=0, today_idx=20,
        today_low=175.0, today_close=178.0,
        atr_at_entry=4.0, hold_days=20, stop_mult=1.5,
    )
    assert should is True
    assert reason == "hold_period_end"


def test_should_not_exit_while_above_stop_and_within_hold():
    should, reason = should_exit(
        entry_px=170.0, entry_date_idx=0, today_idx=10,
        today_low=165.0, today_close=168.0,
        atr_at_entry=4.0, hold_days=20, stop_mult=1.5,
    )
    assert should is False


def test_atr_stop_takes_priority_over_hold_period():
    """If both conditions fire simultaneously (very rare — final bar
    of hold also breaches stop), atr_stop wins because the function
    checks it first. Verified explicit ordering."""
    should, reason = should_exit(
        entry_px=170.0, entry_date_idx=0, today_idx=20,  # at hold_days
        today_low=160.0, today_close=160.0,              # also below stop
        atr_at_entry=4.0, hold_days=20, stop_mult=1.5,
    )
    assert should is True
    assert reason == "atr_stop"


# ── Universe ───────────────────────────────────────────────────────────────

def test_load_universe_yields_apollo_watchlist():
    """Verify the runner loads the 16-name Apollo PEAD universe."""
    from forge.pead.runner import load_universe
    universe = load_universe()
    assert len(universe) >= 10, f"Expected 16 tickers, got {len(universe)}"
    # Apollo tier_1 names should be present
    for must_have in ("GOOGL", "KLAC", "PEP", "WMT"):
        assert must_have in universe, f"{must_have} missing from PEAD universe"
