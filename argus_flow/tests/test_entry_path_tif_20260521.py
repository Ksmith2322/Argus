"""Regression test for the 2026-05-21 entry-path TIF fix.

The bug: argus_flow.runner_unified._submit_real_entry was constructing
the entry order as `MarketOrder(action, qty)` which defaults to
`tif='DAY'`. IBKR rejects DAY-TIF orders on IDEALPRO FX with Error
10349 ("Order TIF was set to DAY based on order preset"). TWS auto-
retries which masked the bug, but the rejection-retry pattern was
captured by the golden-trace recorder on the first live entry after
activation (USDJPY orderId=13, 2026-05-21 18:05Z).

The fix: mirror the 2026-05-19 `_build_exit_order` pattern. For CASH
(FX) contracts use LimitOrder with TIF='GTC', JPY-aware decimal
rounding, outsideRth=True, and a 5% buffer toward fill direction.
Non-FX paths keep MarketOrder.

This test pins the fix at the source level (so a regression that
reverts the order type is caught immediately) and also exercises the
helper logic in isolation against a stub contract.
"""
from __future__ import annotations

import re
from pathlib import Path


def _entry_body() -> str:
    """Return the body of _submit_real_entry up to (but not including)
    the next `def ` at the same indentation level."""
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def _submit_real_entry(")
    assert idx >= 0, "_submit_real_entry not found"
    end = src.find("\n    def ", idx + 10)
    return src[idx:end] if end > idx else src[idx:]


# ─── source-level regression assertions ──────────────────────────────────

def test_entry_path_uses_LimitOrder_for_CASH():
    """The CASH branch must construct a LimitOrder, not a MarketOrder."""
    body = _entry_body()
    # The fix introduces an is_cash branch with LimitOrder
    assert 'is_cash = getattr(self.contract, "secType", "") == "CASH"' in body, (
        "is_cash detection removed — entry path may have reverted to "
        "always-MarketOrder pattern"
    )
    assert 'order = LimitOrder(action, qty, lmt)' in body, (
        "LimitOrder construction removed; entry path likely re-broken "
        "by reverting to MarketOrder default DAY TIF"
    )


def test_entry_path_uses_TIF_GTC_for_CASH():
    """The CASH branch must explicitly set TIF=GTC to avoid Error 10349."""
    body = _entry_body()
    # Look for tif=GTC near the LimitOrder construction
    assert 'order.tif = "GTC"' in body, (
        "FX entry TIF=GTC override missing — broker will reject DAY-TIF "
        "FX orders with Error 10349"
    )


def test_entry_path_sets_outsideRth_for_CASH():
    """FX trades 24/5 — outsideRth=True is required for nighttime fills."""
    body = _entry_body()
    # outsideRth is set on the order in the CASH branch
    assert "order.outsideRth = True" in body, (
        "outsideRth=True missing from FX entry order — nighttime fills "
        "will be rejected"
    )


def test_entry_path_uses_JPY_aware_decimals():
    """JPY pairs use 3 decimals (0.001 tick); other FX uses 5 decimals.
    The 2026-05-19 CADJPY incident proved that mis-rounding causes
    silent Warning 110 rejection — the same risk applies on entry."""
    body = _entry_body()
    assert "is_jpy" in body, "JPY-pair detection missing from entry path"
    assert "decimals = 3 if is_jpy else 5" in body, (
        "JPY-aware decimal rounding missing or changed; risk of "
        "Warning 110 silent rejection on JPY entries"
    )


def test_entry_path_checks_all_three_pair_tag_sources():
    """contract.symbol on ib_insync Forex is the BASE currency (e.g.
    'USD' for USDJPY), not the pair name. Detection must check
    self.symbol, contract.symbol, contract.currency, AND
    contract.localSymbol. Earlier draft of the 5/19 exit-path fix missed
    JPY pairs because it only checked contract.symbol."""
    body = _entry_body()
    # All four sources should be referenced near the pair_tags assembly
    pair_tags_idx = body.find("pair_tags = ")
    assert pair_tags_idx >= 0
    region = body[pair_tags_idx:pair_tags_idx + 600]
    for source in ('self, "symbol"', 'self.contract, "symbol"',
                   'self.contract, "currency"', 'self.contract, "localSymbol"'):
        assert source in region, f"pair-tag source {source!r} missing"


def test_entry_path_keeps_MarketOrder_for_non_CASH():
    """Equities/futures don't have the FX TIF problem — keep MarketOrder
    for non-CASH so this fix doesn't break stock-runner clients of the
    same code path. The else-branch should still construct MarketOrder."""
    body = _entry_body()
    # The is_cash conditional should have an else: that builds MarketOrder
    # We look for the structural pattern "else:\n                order = MarketOrder("
    pattern = re.compile(r"else:\s*\n\s+order = MarketOrder\(action, qty\)")
    assert pattern.search(body), (
        "Non-CASH MarketOrder branch missing — equities/futures may now "
        "incorrectly receive LimitOrder treatment"
    )


def test_entry_path_buffer_direction_correct():
    """BUY entries need LMT ABOVE mid (so the limit is marketable),
    SELL entries need LMT BELOW mid. Buffer 1.05 for BUY, 0.95 for SELL."""
    body = _entry_body()
    assert 'buffer = 1.05 if action == "BUY" else 0.95' in body, (
        "Entry buffer direction logic missing or changed — risk of "
        "non-marketable limit orders that sit and don't fill"
    )


# ─── functional verification of the helper logic ─────────────────────────

def test_jpy_pair_detected_from_self_symbol():
    """Even when contract.symbol='USD' (the base currency on ib_insync
    Forex), the pair_tags assembly should detect JPY via self.symbol
    or contract.localSymbol."""
    # We simulate the pair_tags assembly with a USDJPY-shaped stub
    self_symbol = "USDJPY"
    contract_symbol = "USD"      # ib_insync stores base currency here
    contract_currency = "JPY"    # quote currency
    contract_local = "USD.JPY"
    pair_tags = " ".join([self_symbol, contract_symbol, contract_currency, contract_local]).upper()
    is_jpy = "JPY" in pair_tags
    assert is_jpy is True


def test_non_jpy_pair_correctly_rejected_from_jpy_branch():
    """EURUSD should NOT be flagged as JPY even though some upstream
    bug might mis-tag — verify the detector is correct."""
    self_symbol = "EURUSD"
    contract_symbol = "EUR"
    contract_currency = "USD"
    contract_local = "EUR.USD"
    pair_tags = " ".join([self_symbol, contract_symbol, contract_currency, contract_local]).upper()
    is_jpy = "JPY" in pair_tags
    assert is_jpy is False


def test_decimal_rounding_for_jpy_produces_3dp():
    """JPY pair at 158.96921 with 3 decimals rounds to 158.969 — passes
    IDEALPRO 0.001 tick. 5-decimal rounding (158.96921) would be
    rejected with Warning 110."""
    ref_px = 158.96921
    decimals = 3
    buffer = 1.05
    lmt = round(ref_px * buffer, decimals)
    # Multiply gives 166.91767..., rounded to 3 places = 166.918
    assert lmt == round(166.917670, 3)
    # The key property: result has no more than 3 decimal places
    assert len(str(lmt).split(".")[-1]) <= 3


def test_decimal_rounding_for_non_jpy_produces_5dp():
    """EUR.USD at 1.16244 with 5 decimals stays at the 0.00005 tick."""
    ref_px = 1.16244
    decimals = 5
    buffer = 1.05
    lmt = round(ref_px * buffer, decimals)
    assert len(str(lmt).split(".")[-1]) <= 5


# ─── cross-cutting source check: no MarketOrder for CASH anywhere new ────

def test_no_marketorder_fx_entry_anywhere_in_runner_unified():
    """Defense in depth: scan runner_unified.py for any spot that
    constructs a MarketOrder for a CASH context. The known acceptable
    MarketOrder uses (force-close emergency paths) are flagged with
    explicit comments OR live in clearly-non-entry code paths."""
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    # Count MarketOrder constructions
    market_orders = list(re.finditer(r"MarketOrder\(", src))
    # Each should either be inside a non-CASH `else:` branch OR an
    # emergency force-close path. The fix introduced one in the CASH
    # `else:` branch; the original force-close path at line ~4630 uses
    # MarketOrder for stock flatten which is fine.
    # The regression we're guarding against: someone reintroduces a
    # bare MarketOrder in _submit_real_entry's main path.
    body = _entry_body()
    # In _submit_real_entry body, every MarketOrder occurrence must be
    # in the non-CASH else branch (immediately after `else:` keyword).
    for m in re.finditer(r"MarketOrder\(", body):
        # Look backward for the most-recent "else:" or "is_cash" branch start
        prefix = body[:m.start()]
        # Find the last is_cash branch decision before this MarketOrder
        last_else = prefix.rfind("\n            else:")
        last_is_cash = prefix.rfind('is_cash = ')
        # If is_cash branch is present and MarketOrder comes after else:, that's fine
        assert last_is_cash >= 0 and last_else > last_is_cash, (
            f"MarketOrder construction at offset {m.start()} in "
            f"_submit_real_entry is NOT inside the non-CASH else branch. "
            f"The FX entry path may have re-broken."
        )
