"""Tests for the per-instrument exit-order builder.

Verifies the fix for the 2026-05-08 EXIT_FAILED incident where FX exits
on argus pairs hit the "EXIT TIMEOUT → EXIT STUCK → EXIT FAILED:
MANUAL BROKER CHECK REQUIRED" cascade. Root cause: MarketOrder on
IdealPro FX can stall around session boundaries; the fix is wide
LimitOrder + outsideRth=True for CASH, MarketOrder for everything else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from argus_flow.runner_unified import InstrumentRunner


class _StubLogger:
    def __init__(self):
        self.records = []

    def info(self, *a, **k): self.records.append(("info", a, k))
    def warning(self, *a, **k): self.records.append(("warning", a, k))
    def error(self, *a, **k): self.records.append(("error", a, k))
    def critical(self, *a, **k): self.records.append(("critical", a, k))


def _make_instrument(sec_type: str) -> SimpleNamespace:
    """A minimal stand-in that exposes only what _build_exit_order touches:
    self.contract.secType and self._log."""
    inst = SimpleNamespace()
    inst.contract = SimpleNamespace(secType=sec_type)
    inst._log = _StubLogger()
    inst._build_exit_order = InstrumentRunner._build_exit_order.__get__(inst, SimpleNamespace)
    return inst


def test_fx_exit_uses_limit_order_with_outside_rth():
    """The 2026-05-08 EXIT_FAILED bug: MarketOrder hung on FX. Fix: LMT
    with 5% adverse buffer + outsideRth=True so the exit fills around
    session boundaries."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=10000, ref_px=1.30, tif="DAY")
    assert type(order).__name__ == "LimitOrder"
    assert order.outsideRth is True
    # SELL of an FX position uses 0.95× the reference (sell at-or-above 95% of mid)
    assert order.lmtPrice == pytest.approx(1.30 * 0.95, rel=1e-4)
    assert order.totalQuantity == 10000
    assert order.action == "SELL"
    assert order.tif == "DAY"


def test_fx_exit_buy_side_uses_high_buffer():
    """Buying-to-close uses the 1.05× upper buffer so the LMT will fill
    even if the market moves against us."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("BUY", qty=5000, ref_px=2.00, tif="DAY")
    assert order.lmtPrice == pytest.approx(2.00 * 1.05, rel=1e-4)
    assert order.action == "BUY"


def test_stk_exit_uses_market_order():
    inst = _make_instrument("STK")
    order = inst._build_exit_order("SELL", qty=100, ref_px=400.0, tif="DAY")
    assert type(order).__name__ == "MarketOrder"
    assert order.totalQuantity == 100
    assert order.action == "SELL"


def test_fut_exit_uses_market_order():
    inst = _make_instrument("FUT")
    order = inst._build_exit_order("BUY", qty=2, ref_px=21000.0, tif="GTC")
    assert type(order).__name__ == "MarketOrder"
    assert order.tif == "GTC"


def test_fx_with_zero_ref_px_does_not_explode():
    """If we ever call with no reference price (shouldn't happen, but be
    defensive), the helper falls back to ref=1.0 rather than producing
    a $0 LMT that would never fill."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=10000, ref_px=0, tif="DAY")
    assert order.lmtPrice > 0


def test_tif_propagates_for_market_orders():
    inst = _make_instrument("STK")
    order = inst._build_exit_order("SELL", qty=10, ref_px=100, tif="IOC")
    assert order.tif == "IOC"


def test_tif_propagates_for_fx_limit_orders():
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("BUY", qty=20000, ref_px=1.5, tif="GTC")
    assert order.tif == "GTC"
