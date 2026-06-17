"""Tests for helio.regime — regime-detection gates."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from helio.regime import (
    REGIME_GATES,
    spy_above_200dma,
    vix_below_rolling,
    vix_below_threshold,
)


# ─── spy_above_200dma ───────────────────────────────────────────────

def test_spy_above_200dma_returns_true_when_price_above_sma():
    # Build a rising series — last price > 200-day mean
    closes = [100.0 + i * 0.5 for i in range(250)]
    assert spy_above_200dma(closes) is True


def test_spy_above_200dma_returns_false_when_price_below_sma():
    # Build a falling series — last price < 200-day mean
    closes = [200.0 - i * 0.5 for i in range(250)]
    assert spy_above_200dma(closes) is False


def test_spy_above_200dma_returns_false_with_insufficient_history():
    # Only 100 bars, but window=200
    closes = [100.0 + i for i in range(100)]
    assert spy_above_200dma(closes) is False


def test_spy_above_200dma_accepts_pandas_series():
    s = pd.Series([100.0 + i * 0.5 for i in range(250)])
    assert spy_above_200dma(s) is True


def test_spy_above_200dma_supports_asof_index():
    """Calling with asof_index=N should evaluate as-if at bar N
    (not the last bar)."""
    # 300 bars of rising series — at bar 250 still above SMA
    closes = [100.0 + i * 0.5 for i in range(300)]
    assert spy_above_200dma(closes, asof_index=250) is True


# ─── vix_below_threshold ───────────────────────────────────────────

def test_vix_below_threshold_true_for_low_vix():
    closes = [15.0, 16.5, 14.0]
    assert vix_below_threshold(closes, threshold=25.0) is True


def test_vix_below_threshold_false_for_high_vix():
    closes = [15.0, 16.5, 28.0]
    assert vix_below_threshold(closes, threshold=25.0) is False


def test_vix_below_threshold_uses_threshold_argument():
    closes = [27.0]
    assert vix_below_threshold(closes, threshold=25.0) is False
    assert vix_below_threshold(closes, threshold=30.0) is True


# ─── vix_below_rolling ─────────────────────────────────────────────

def test_vix_below_rolling_true_when_vix_below_recent_mean():
    # Last value 15, mean over last 60 = ~25 -> below mean -> True
    closes = [25.0] * 60 + [15.0]
    assert vix_below_rolling(closes, window=60) is True


def test_vix_below_rolling_false_when_vix_above_recent_mean():
    closes = [15.0] * 60 + [25.0]
    assert vix_below_rolling(closes, window=60) is False


def test_vix_below_rolling_insufficient_history_returns_false():
    closes = [15.0] * 30
    assert vix_below_rolling(closes, window=60) is False


# ─── Gate registry ─────────────────────────────────────────────────

def test_registry_has_four_canonical_gates():
    expected = {
        "spy_above_200dma",
        "vix_below_25",
        "vix_below_30",
        "vix_below_rolling_60",
    }
    assert set(REGIME_GATES) == expected


def test_each_registered_gate_is_callable():
    """The lambdas in the registry must invoke without parameter
    errors when given a single series."""
    series = [15.0] * 100 + [20.0]
    for name, cfg in REGIME_GATES.items():
        result = cfg["fn"](series)
        assert isinstance(result, bool), f"{name} did not return bool"


def test_registry_declares_data_requirements():
    """Each gate must declare what data series it needs so the
    runtime can pre-fetch it."""
    for name, cfg in REGIME_GATES.items():
        assert "needs" in cfg
        assert isinstance(cfg["needs"], list)
        assert all(n in ("SPY_closes", "VIX_closes") for n in cfg["needs"])
