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
    session boundaries.

    Codex audit 2026-05-18 follow-up: a DAY-flagged FX LMT can also stall
    if the order sits past the broker's day boundary. For CASH, the helper
    now upgrades DAY→GTC; tif behavior is verified by
    test_fx_day_is_upgraded_to_gtc below."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=10000, ref_px=1.30, tif="DAY")
    assert type(order).__name__ == "LimitOrder"
    assert order.outsideRth is True
    # SELL of an FX position uses 0.95× the reference (sell at-or-above 95% of mid)
    assert order.lmtPrice == pytest.approx(1.30 * 0.95, rel=1e-4)
    assert order.totalQuantity == 10000
    assert order.action == "SELL"


def test_fx_day_is_upgraded_to_gtc():
    """Codex audit 2026-05-18: TIF=DAY on a CASH exit can silently expire
    around the broker's daily roll boundary, which was a suspected factor
    in the recurring argus_gbpusd EXIT FAILED cascade. The helper now
    upgrades DAY → GTC for CASH so the exit persists across session
    boundaries until it fills."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=10000, ref_px=1.30, tif="DAY")
    assert order.tif == "GTC"


def _make_instrument_with_symbol(sec_type: str, symbol: str) -> SimpleNamespace:
    """Variant of _make_instrument that mimics an ib_insync Forex contract:
    contract.symbol = base currency, contract.currency = quote, localSymbol
    has the pair in dotted form. Also sets inst.symbol to the full pair
    string (which matches how the runner stores it).

    JPY-aware rounding must succeed regardless of which of these fields
    is populated — that was the bug the live caught on 2026-05-19."""
    inst = SimpleNamespace()
    # Parse pair like "USDJPY" -> base "USD", quote "JPY", local "USD.JPY"
    base = symbol[:3].upper() if len(symbol) == 6 else symbol.upper()
    quote = symbol[3:].upper() if len(symbol) == 6 else ""
    inst.contract = SimpleNamespace(
        secType=sec_type,
        symbol=base,
        currency=quote,
        localSymbol=f"{base}.{quote}" if quote else symbol,
    )
    inst.symbol = symbol.upper()
    inst._log = _StubLogger()
    inst._build_exit_order = InstrumentRunner._build_exit_order.__get__(inst, SimpleNamespace)
    return inst


def test_jpy_pair_exit_rounds_to_3_decimals():
    """BUGFIX 2026-05-19: JPY pairs use 0.001 min tick on IdealPro, not
    0.00005 like EUR-class pairs. Rounding to 5 decimals on JPY produces
    sub-tick prices that IBKR rejects with Warning 110. Caught live on
    CADJPY 2026-05-19 18:36 UTC — every retry got Warning 110 and order
    stayed PendingSubmit forever."""
    inst = _make_instrument_with_symbol("CASH", "CADJPY")
    order = inst._build_exit_order("SELL", qty=41479, ref_px=115.65, tif="DAY")
    # 115.65 * 0.95 = 109.8675, rounded to 3 decimals = 109.868
    # Definitely NOT 109.87177 (5 decimals, sub-tick)
    assert order.lmtPrice == pytest.approx(109.868, abs=1e-6), (
        f"JPY exit price {order.lmtPrice} not rounded to 3 decimals — "
        f"will be rejected by IBKR with Warning 110"
    )
    # Validate decimal count
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals_after_dot = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals_after_dot <= 3, (
        f"JPY price has {decimals_after_dot} decimals; IdealPro min tick "
        f"requires <= 3 decimals."
    )


def test_jpy_pair_buy_to_close_rounds_to_3_decimals():
    """Same fix on the BUY side."""
    inst = _make_instrument_with_symbol("CASH", "USDJPY")
    order = inst._build_exit_order("BUY", qty=10000, ref_px=150.123, tif="DAY")
    # 150.123 * 1.05 = 157.62915, rounded to 3 = 157.629
    assert order.lmtPrice == pytest.approx(157.629, abs=1e-6)


def test_non_jpy_pair_still_uses_5_decimals():
    """Regression: EUR-class pairs must still use 5-decimal rounding
    (0.00005 tick = half-pip)."""
    inst = _make_instrument_with_symbol("CASH", "GBPUSD")
    order = inst._build_exit_order("SELL", qty=10000, ref_px=1.27345, tif="DAY")
    # 1.27345 * 0.95 = 1.2097775, rounded to 5 = 1.20978
    assert order.lmtPrice == pytest.approx(1.20978, abs=1e-6)


def test_jpy_pair_recognition_is_case_insensitive():
    """Symbol matching should not be fragile to case."""
    for sym in ("CADJPY", "cadjpy", "USDJPY", "EurJpy"):
        inst = _make_instrument_with_symbol("CASH", sym)
        order = inst._build_exit_order("SELL", qty=10000, ref_px=110.0, tif="DAY")
        # Whatever the symbol case, JPY pairs must round to 3 decimals
        str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
        decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
        assert decimals <= 3, f"{sym}: got {decimals} decimals in {order.lmtPrice}"


def test_jpy_detected_when_only_currency_field_says_jpy():
    """Regression: in ib_insync Forex contracts, contract.symbol is the BASE
    currency (e.g. "USD" for USDJPY), not the pair. The original 5/20 fix
    checked only contract.symbol and missed JPY pairs because their
    contract.symbol was "USD", "CAD", "EUR" etc.

    This test simulates the exact ib_insync Forex contract shape that
    the bug fix initially missed: contract.symbol="USD", currency="JPY".
    Must round to 3 decimals."""
    inst = SimpleNamespace()
    inst.contract = SimpleNamespace(
        secType="CASH",
        symbol="USD",       # base currency — NOT "USDJPY"
        currency="JPY",     # quote currency — this is where JPY lives
        localSymbol="USD.JPY",
    )
    inst.symbol = ""        # NOT set on the runner (worst case)
    inst._log = _StubLogger()
    inst._build_exit_order = InstrumentRunner._build_exit_order.__get__(inst, SimpleNamespace)

    order = inst._build_exit_order("SELL", qty=30142, ref_px=151.02, tif="DAY")
    # 151.02 * 0.95 = 143.469, rounded to 3 = 143.469. Not 143.46900.
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals <= 3, (
        f"JPY detection failed via contract.currency='JPY'. Got "
        f"lmtPrice={order.lmtPrice} with {decimals} decimals. "
        f"IBKR will reject with Warning 110."
    )


def test_jpy_detected_when_only_localSymbol_says_jpy():
    """Similar regression — only localSymbol carries the .JPY tag."""
    inst = SimpleNamespace()
    inst.contract = SimpleNamespace(
        secType="CASH",
        symbol="CAD",
        currency="",         # missing
        localSymbol="CAD.JPY",
    )
    inst.symbol = ""
    inst._log = _StubLogger()
    inst._build_exit_order = InstrumentRunner._build_exit_order.__get__(inst, SimpleNamespace)

    order = inst._build_exit_order("SELL", qty=41479, ref_px=115.53667, tif="DAY")
    str_px = f"{order.lmtPrice:.10f}".rstrip("0").rstrip(".")
    decimals = len(str_px.split(".")[-1]) if "." in str_px else 0
    assert decimals <= 3


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
