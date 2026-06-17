"""Tests for helio.ibkr_execution OVERSIZED_ORDER threshold helpers.

These cover the pure functions added 2026-05-12 to make the OVERSIZED guard
asset-class-aware. Background: the guard hardcoded 50% of NetLiq for every
symbol, which silently rejected every legitimate futures order from any
strategy using helio.signal_executor (cuebanks, mamba, tori). The fix
classifies the symbol and applies a per-asset-class threshold.
"""
from __future__ import annotations

from helio import ibkr_execution as ie


def test_futures_multiplier_known_symbols():
    assert ie._futures_multiplier("MYM") == 0.50
    assert ie._futures_multiplier("MNQ") == 2.00
    assert ie._futures_multiplier("MES") == 5.00
    assert ie._futures_multiplier("ES") == 50.00
    assert ie._futures_multiplier("NQ") == 20.00
    assert ie._futures_multiplier("YM") == 5.00


def test_futures_multiplier_lowercase_input_normalized():
    assert ie._futures_multiplier("mnq") == 2.00


def test_futures_multiplier_unknown_or_stock_returns_one():
    assert ie._futures_multiplier("SPY") == 1.0
    assert ie._futures_multiplier("AAPL") == 1.0
    assert ie._futures_multiplier("") == 1.0
    assert ie._futures_multiplier(None) == 1.0  # type: ignore[arg-type]


def test_classify_symbol_futures():
    assert ie._classify_symbol("MYM") == "micro_future"
    assert ie._classify_symbol("MNQ") == "micro_future"
    assert ie._classify_symbol("MES") == "micro_future"
    assert ie._classify_symbol("ES") == "future"
    assert ie._classify_symbol("NQ") == "future"


def test_classify_symbol_fx_pair():
    assert ie._classify_symbol("USDJPY") == "fx"
    assert ie._classify_symbol("EURUSD") == "fx"
    assert ie._classify_symbol("GBPUSD") == "fx"


def test_classify_symbol_stock_default():
    assert ie._classify_symbol("SPY") == "stock"
    assert ie._classify_symbol("AAPL") == "stock"
    assert ie._classify_symbol("") == "stock"


def test_oversize_threshold_futures_uses_2_5x():
    # Micro futures: 2.5× anchor
    assert ie._oversize_threshold_usd("MYM", 30_000) == 75_000
    assert ie._oversize_threshold_usd("MNQ", 30_000) == 75_000
    # Full-size futures: 2.5× anchor (same factor for now)
    assert ie._oversize_threshold_usd("ES", 30_000) == 75_000


def test_oversize_threshold_fx_uses_1_5x():
    assert ie._oversize_threshold_usd("USDJPY", 30_000) == 45_000
    assert ie._oversize_threshold_usd("EURUSD", 30_000) == 45_000


def test_oversize_threshold_stocks_uses_0_5x_unchanged():
    # Pre-fix behavior preserved for stocks/ETFs: 50% of anchor
    assert ie._oversize_threshold_usd("SPY", 30_000) == 15_000
    assert ie._oversize_threshold_usd("QQQ", 30_000) == 15_000


def test_oversize_threshold_zero_anchor_returns_zero():
    # Defensive: if anchor unavailable, threshold collapses to 0, which
    # means every nonzero order will be rejected. That's fail-closed.
    assert ie._oversize_threshold_usd("MYM", 0) == 0
    assert ie._oversize_threshold_usd("SPY", 0) == 0


def test_real_cuebanks_scenario_now_passes_threshold():
    """The exact failing scenario from 2026-05-12 15:54:
       2 MYM at index ~49,862 with multiplier $0.50 = ~$49,862 USD notional.
       Pre-fix: rejected at 0.5 × $31,490 = $15,745 (hard stop).
       Post-fix: cleared by 2.5 × $31,490 = $78,725 threshold."""
    anchor = 31_490
    actual_notional = 2 * 49_862 * ie._futures_multiplier("MYM")
    threshold = ie._oversize_threshold_usd("MYM", anchor)
    assert actual_notional < threshold, (
        f"cuebanks 2 MYM notional ${actual_notional:,.0f} should clear "
        f"the new threshold ${threshold:,.0f}"
    )
