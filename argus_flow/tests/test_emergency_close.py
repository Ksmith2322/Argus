"""Tests for argus_flow.ops.emergency_close._build_close_order.

Covers the 2026-05-19 incident where the tool failed to close CADJPY
because:
  1. The contract pulled from ib.positions() lacks `exchange`, causing
     IBKR to reject MarketOrder with Error 321 "Missing order exchange".
  2. Even with exchange set, MarketOrder on FX stalls around session
     boundaries (the EXIT FAILED cascade from 5/8).
  3. The decimal rounding wasn't JPY-aware.

The fix uses wide LimitOrder + outsideRth=True + GTC + JPY-aware
decimals for CASH, and MarketOrder for everything else."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from argus_flow.ops.emergency_close import _build_close_order


def _contract(sec_type: str, symbol: str = "USD", currency: str = "",
              local: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        secType=sec_type,
        symbol=symbol,
        currency=currency,
        localSymbol=local or symbol,
    )


def test_fx_uses_limit_order_with_outside_rth_and_gtc():
    contract = _contract("CASH", symbol="EUR", currency="USD", local="EUR.USD")
    order = _build_close_order("SELL", qty=10000, contract=contract, ref_px=1.10)
    assert type(order).__name__ == "LimitOrder"
    assert order.outsideRth is True
    assert order.tif == "GTC"


def test_fx_sell_uses_low_buffer():
    """Selling to close — wants to fill at a low price, use 0.95×."""
    contract = _contract("CASH", symbol="GBP", currency="USD", local="GBP.USD")
    order = _build_close_order("SELL", qty=22530, contract=contract, ref_px=1.30)
    assert order.lmtPrice == pytest.approx(1.30 * 0.95, rel=1e-4)


def test_fx_buy_uses_high_buffer():
    """Buying to close a short — wants to fill, use 1.05×."""
    contract = _contract("CASH", symbol="USD", currency="JPY", local="USD.JPY")
    order = _build_close_order("BUY", qty=30142, contract=contract, ref_px=150.0)
    # USDJPY rounds to 3 decimals (JPY) — 150 * 1.05 = 157.500
    assert order.lmtPrice == pytest.approx(157.500, rel=1e-4)


def test_jpy_pair_rounds_to_three_decimals():
    """The 2026-05-19 root cause: 5-decimal price on a JPY pair gets
    rejected by IBKR with Warning 110."""
    contract = _contract("CASH", symbol="CAD", currency="JPY", local="CAD.JPY")
    order = _build_close_order("BUY", qty=82858, contract=contract, ref_px=115.687)
    # 115.687 * 1.05 = 121.47135, rounded to 3 = 121.471
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals <= 3, f"JPY price has {decimals} decimals; IBKR rejects > 3"


def test_jpy_detection_works_via_currency_only():
    """Mirror of the runner_unified fix: on ib.positions() the contract
    may have symbol='CAD' and currency='JPY' but no localSymbol — the
    JPY check must still recognise it."""
    contract = _contract("CASH", symbol="CAD", currency="JPY", local="")
    order = _build_close_order("BUY", qty=10000, contract=contract, ref_px=110.0)
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals <= 3


def test_non_jpy_fx_uses_five_decimals():
    """EUR/GBP/AUD-class pairs use 0.00005 half-pip tick = 5 decimals."""
    contract = _contract("CASH", symbol="GBP", currency="USD", local="GBP.USD")
    order = _build_close_order("SELL", qty=10000, contract=contract, ref_px=1.27345)
    # 1.27345 * 0.95 = 1.2097775, rounded to 5 = 1.20978
    assert order.lmtPrice == pytest.approx(1.20978, abs=1e-6)


def test_stk_uses_market_order():
    contract = _contract("STK", symbol="SPY")
    order = _build_close_order("SELL", qty=26, contract=contract, ref_px=700.0)
    assert type(order).__name__ == "MarketOrder"


def test_fut_uses_market_order():
    contract = _contract("FUT", symbol="MNQ")
    order = _build_close_order("BUY", qty=1, contract=contract, ref_px=21000.0)
    assert type(order).__name__ == "MarketOrder"


def test_zero_ref_px_fx_does_not_explode():
    """Defensive: if positions() returns avg_cost=0 (shouldn't happen but)
    the close should still produce a valid LMT, not blow up."""
    contract = _contract("CASH", symbol="USD", currency="JPY", local="USD.JPY")
    order = _build_close_order("BUY", qty=1000, contract=contract, ref_px=0)
    # Falls back to ref_px=1.0; LMT will be 1.0 * 1.05 = 1.05.
    # This will never fill in practice (price way below market) but won't
    # crash the script and the operator can re-run.
    assert order.lmtPrice > 0


def test_jpy_recognized_via_localSymbol_dot_form():
    """When pulled from ib.positions(), localSymbol is often "CAD.JPY"
    (dotted form). Detection must catch this."""
    contract = _contract("CASH", symbol="CAD", currency="", local="CAD.JPY")
    order = _build_close_order("BUY", qty=10000, contract=contract, ref_px=110.0)
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals <= 3
