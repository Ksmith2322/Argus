"""Tests for the 2026-05-21 RAW_SIZE_RATIO sanity gate in fx_units_for_risk.

Background: the FX sizing formula computes raw_units = risk_amount / (stop_pips
* pip_value). Mathematically correct — but when the configured (risk_pct,
stop_pips) pair demands more notional than the account has, downstream
NOTIONAL_CAP clamps to ~equity ratio. The operator only sees post-cap
effective risk and can't tell that the strategy is structurally
undersized.

Today's USDJPY trade is the canonical example:
  equity=$29,448, risk_pct=0.75% → intended risk=$220
  stop_pips=5 → raw_units=701,800 → raw_notional=$701,800
  ratio = 701,800/29,448 = 23.8x equity
  NOTIONAL_CAP clamps to ~29,362 units → effective risk=$9.23

The 23.8x ratio is the operator-actionable diagnostic. This batch
surfaces it as a WARNING. Extreme ratios (>=50x) get refused outright.
"""
from __future__ import annotations

import logging

import pytest

from argus_flow.sizing import (
    RAW_SIZE_RATIO_REFUSE,
    RAW_SIZE_RATIO_WARN,
    fx_units_for_risk,
)


# ─── constants are reasonable ────────────────────────────────────────────

def test_warn_threshold_is_above_1():
    """1x equity ratio is the natural NOTIONAL_CAP boundary; warn should
    fire above that, not at every trade."""
    assert RAW_SIZE_RATIO_WARN > 1.0
    assert RAW_SIZE_RATIO_WARN < 10.0  # but not absurdly high


def test_refuse_threshold_is_well_above_warn():
    """REFUSE is a defense against runaway formulas; should be many
    multiples above WARN."""
    assert RAW_SIZE_RATIO_REFUSE > RAW_SIZE_RATIO_WARN * 5


# ─── behavior ───────────────────────────────────────────────────────────

def test_usdjpy_today_scenario_triggers_warn(caplog):
    """The exact scenario from 2026-05-21 13:55 USDJPY trade: $29.4K
    equity, 0.75% risk, 5-pip stop. Should fire FX_SIZE_OVERSIZED warning
    with ratio ~23x."""
    with caplog.at_level(logging.WARNING):
        size = fx_units_for_risk(
            equity_usd=29448.0,
            risk_pct=0.0075,  # 0.5 allocation * 0.015 base risk_pct
            stop_pips=5.0,
            symbol="USDJPY",
            quote_price=159.0,
            usd_jpy_price=159.0,
        )
    # Sized value unchanged (defense-in-depth: we still return a number)
    assert size > 0
    # Warning fired
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("FX_SIZE_OVERSIZED" in m for m in msgs), (
        f"Expected FX_SIZE_OVERSIZED warning; got: {msgs}"
    )
    # Ratio in the message
    oversized = [m for m in msgs if "FX_SIZE_OVERSIZED" in m][0]
    # Today's scenario should have ratio around 23x
    assert "ratio=2" in oversized  # "ratio=23.8x" or similar


def test_oversize_warning_includes_diagnosis(caplog):
    """The warning must include the actionable diagnosis — operator
    needs to know to tighten the stop or lower risk_pct. Scenario sized
    to fall in WARN zone (ratio between WARN and REFUSE)."""
    with caplog.at_level(logging.WARNING):
        fx_units_for_risk(
            equity_usd=30000.0,
            risk_pct=0.005,
            stop_pips=10.0,
            symbol="USDJPY",
            quote_price=150.0,
            usd_jpy_price=150.0,
        )
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    oversized = [m for m in msgs if "FX_SIZE_OVERSIZED" in m]
    assert oversized
    text = oversized[0]
    assert "intended_risk" in text
    assert "effective_risk_after_cap" in text
    assert "Tighten stop OR lower risk_pct" in text


def test_normal_sizing_does_not_warn(caplog):
    """A sensible (risk_pct, stop_pips) combination that fits within the
    account should NOT fire the warning. e.g. $100K account, 0.5% risk,
    50-pip stop → ~100K units which is at-equity not 20x equity."""
    with caplog.at_level(logging.WARNING):
        size = fx_units_for_risk(
            equity_usd=100000.0,
            risk_pct=0.005,
            stop_pips=50.0,
            symbol="EURUSD",
            quote_price=1.10,
        )
    assert size > 0
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("FX_SIZE_OVERSIZED" in m for m in msgs), (
        f"Normal sizing should not warn; got: {msgs}"
    )


def test_extreme_ratio_refuses_sizing(caplog):
    """When the formula produces a notional ratio >= RAW_SIZE_RATIO_REFUSE
    (50x), refuse to size. This is defense against a totally broken
    parameter combination (e.g. someone sets stop_pips=0.1)."""
    with caplog.at_level(logging.ERROR):
        size = fx_units_for_risk(
            equity_usd=10000.0,
            risk_pct=0.02,
            stop_pips=0.1,  # tiny stop -> huge raw_units
            symbol="USDJPY",
            quote_price=150.0,
            usd_jpy_price=150.0,
        )
    assert size == 0, "Extreme ratio must refuse to size"
    msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any("FX_SIZE_REFUSED" in m for m in msgs)


def test_ratio_calculation_correct_for_usdjpy():
    """USDJPY units are USD-denominated, so raw_notional = raw_units * 1
    (since notional_per_unit for USD-base = 1). Verify the math."""
    # equity=$30K, risk=2%, stop=5 pips → risk_amount=$600
    # pip_value for USDJPY at 150 = 0.01/150 = 0.0000667
    # raw_units = 600 / (5 * 0.0000667) = 1,800,000
    # raw_notional = 1,800,000 * 1 = $1,800,000
    # ratio = 1,800,000 / 30,000 = 60x → REFUSE
    size = fx_units_for_risk(
        equity_usd=30000.0,
        risk_pct=0.02,
        stop_pips=5.0,
        symbol="USDJPY",
        quote_price=150.0,
        usd_jpy_price=150.0,
    )
    assert size == 0  # refused at 60x


def test_eurusd_oversize_also_triggers(caplog):
    """EURUSD oversize should also trigger. EUR is base, USD is quote;
    notional_per_unit = quote_price. Scenario sized for WARN not REFUSE."""
    with caplog.at_level(logging.WARNING):
        size = fx_units_for_risk(
            equity_usd=30000.0,
            risk_pct=0.005,
            stop_pips=10.0,
            symbol="EURUSD",
            quote_price=1.10,
        )
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    # Should warn (oversize) but not refuse
    assert any("FX_SIZE_OVERSIZED" in m for m in msgs), (
        f"Expected FX_SIZE_OVERSIZED for EURUSD oversize; got: {msgs}"
    )
    assert size > 0


def test_zero_equity_returns_zero_without_log_spam(caplog):
    """Pre-existing guard: equity<=0 returns 0 with no log noise from
    the new sanity gate."""
    with caplog.at_level(logging.WARNING):
        size = fx_units_for_risk(
            equity_usd=0.0,
            risk_pct=0.01,
            stop_pips=10.0,
            symbol="USDJPY",
            quote_price=150.0,
        )
    assert size == 0
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("FX_SIZE_OVERSIZED" in m for m in msgs), (
        "Zero-equity should not trip the oversize warning"
    )


def test_sanity_gate_does_not_alter_return_value_in_normal_case():
    """Critical contract: behavior must be unchanged for normal sizing.
    The sanity gate ONLY adds logging — same numeric return value as
    before."""
    # Pick a scenario in the warning-only zone (not refused)
    size = fx_units_for_risk(
        equity_usd=29448.0,
        risk_pct=0.0075,
        stop_pips=5.0,
        symbol="USDJPY",
        quote_price=159.0,
        usd_jpy_price=159.0,
        min_units=1000,
    )
    # Reproduce the formula manually:
    pip_value = 0.01 / 159.0
    risk_amount = 29448.0 * 0.0075
    raw_units = risk_amount / (5.0 * pip_value)
    expected_sized = int(raw_units // 1000) * 1000
    assert size == expected_sized, (
        f"Sanity gate changed return value: expected {expected_sized}, got {size}"
    )
