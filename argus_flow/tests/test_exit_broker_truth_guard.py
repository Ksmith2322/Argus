"""Tests for the broker-truth guard added to all exit paths after the
2026-05-19 cascade-double-fill incident.

The bug observed live:
  - argus_cadjpy was LONG 41,479
  - bot submitted SELL 41,479 → filled
  - cascade race fired SECOND SELL → also filled
  - position went LONG → SHORT (reversed, not closed)
  - this pattern repeated across CADJPY (1×), USDJPY (twice — 30K → 60K → 120K),
    and GBPUSD (twice — 22K → 45K → 90K) over a 2-hour window

The guard: every exit-submission path now queries `ib.positions()` first.
If broker shows flat (our previous exit must have filled) or shows the
opposite direction (our local state drifted), refuse the submission and
clear local state so the next reconciliation cycle adopts broker truth."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from argus_flow.runner_unified import InstrumentRunner


class _StubLogger:
    def __init__(self):
        self.records = []
    def info(self, *a, **k): self.records.append(("info", a, k))
    def warning(self, *a, **k): self.records.append(("warning", a, k))
    def error(self, *a, **k): self.records.append(("error", a, k))
    def critical(self, *a, **k): self.records.append(("critical", a, k))


class _FakeIB:
    """Just enough IB to test the broker-truth check. `positions_data` is
    a list of (localSymbol, symbol, currency, qty) tuples."""
    def __init__(self, positions_data):
        self._positions = []
        for local, sym, curr, qty in positions_data:
            pc = SimpleNamespace(
                localSymbol=local, symbol=sym, currency=curr,
            )
            self._positions.append(SimpleNamespace(
                contract=pc, position=qty,
            ))

    def positions(self):
        return self._positions


def _make_runner_stub(symbol_pair: str, local: str, base: str, quote: str,
                     position: str = "LONG", position_size: int = 41479,
                     broker_positions=None) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking InstrumentRunner with enough state
    to exercise _read_broker_position + _broker_state_allows_exit +
    _submit_real_exit's broker check."""
    inst = SimpleNamespace()
    inst.symbol = symbol_pair
    inst.contract = SimpleNamespace(
        secType="CASH",
        localSymbol=local,
        symbol=base,
        currency=quote,
    )
    inst._log = _StubLogger()
    inst._ib = _FakeIB(broker_positions or [])
    inst.state = SimpleNamespace(
        position=position, position_size=position_size,
        exit_pending=False, exit_order_id="", exit_submitted_ts="",
        stop_order_id="", target_order_id="",
        entry_price=115.5, entry_order_id="",
    )
    inst.state.save = lambda: None
    inst.state.clear_trade_state = lambda: None

    inst._read_broker_position = InstrumentRunner._read_broker_position.__get__(inst, SimpleNamespace)
    inst._broker_state_allows_exit = InstrumentRunner._broker_state_allows_exit.__get__(inst, SimpleNamespace)
    return inst


# ── _read_broker_position ──────────────────────────────────────────────────

def test_read_broker_position_matches_via_localSymbol():
    """Most reliable match: contract.localSymbol vs broker localSymbol."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        broker_positions=[
            ("CAD.JPY", "CAD", "JPY", 41479.0),
            ("EUR.USD", "EUR", "USD", 5000.0),  # noise
        ],
    )
    assert inst._read_broker_position() == 41479.0


def test_read_broker_position_matches_via_symbol_plus_currency():
    """Fallback match when localSymbol is empty: symbol + currency."""
    inst = _make_runner_stub(
        symbol_pair="USDJPY", local="", base="USD", quote="JPY",
        broker_positions=[
            ("USD.JPY", "USD", "JPY", 30142.0),
        ],
    )
    # localSymbol on contract is empty but broker has USD/JPY
    # Match via symbol="USD" + currency="JPY"
    assert inst._read_broker_position() == 30142.0


def test_read_broker_position_returns_zero_when_not_in_positions():
    """If the broker doesn't list our instrument, we're flat."""
    inst = _make_runner_stub(
        symbol_pair="GBPUSD", local="GBP.USD", base="GBP", quote="USD",
        broker_positions=[
            ("USD.JPY", "USD", "JPY", 30142.0),
        ],
    )
    assert inst._read_broker_position() == 0.0


def test_read_broker_position_none_when_ib_unreachable():
    """If _ib is None (signal-only mode or pre-connect), return None."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        broker_positions=[],
    )
    inst._ib = None
    assert inst._read_broker_position() is None


# ── _broker_state_allows_exit ──────────────────────────────────────────────

def test_allows_exit_when_broker_long_and_we_intend_to_sell():
    """The happy path: broker LONG 41479, we want to SELL 41479."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        broker_positions=[("CAD.JPY", "CAD", "JPY", 41479.0)],
    )
    ok, reason = inst._broker_state_allows_exit("SELL", 41479)
    assert ok is True


def test_REFUSES_exit_when_broker_flat():
    """THE main bug case: previous exit filled, broker is flat, our state
    still thinks we have a position. The race-protection MUST refuse."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        position="LONG", position_size=41479,
        broker_positions=[],  # broker shows nothing — already flat
    )
    ok, reason = inst._broker_state_allows_exit("SELL", 41479)
    assert ok is False
    assert "broker_flat_already" in reason


def test_REFUSES_sell_when_broker_already_short():
    """We think LONG, broker actually SHORT. SELL would deepen the short.
    Refuse and flag drift."""
    inst = _make_runner_stub(
        symbol_pair="USDJPY", local="USD.JPY", base="USD", quote="JPY",
        position="LONG", position_size=30142,
        broker_positions=[("USD.JPY", "USD", "JPY", -30142.0)],  # actually short
    )
    ok, reason = inst._broker_state_allows_exit("SELL", 30142)
    assert ok is False
    assert "broker_short" in reason
    assert "deepen" in reason


def test_REFUSES_buy_when_broker_already_long():
    """Mirror: we think SHORT, broker actually LONG. BUY would deepen long."""
    inst = _make_runner_stub(
        symbol_pair="GBPUSD", local="GBP.USD", base="GBP", quote="USD",
        position="SHORT", position_size=22530,
        broker_positions=[("GBP.USD", "GBP", "USD", 22530.0)],
    )
    ok, reason = inst._broker_state_allows_exit("BUY", 22530)
    assert ok is False
    assert "broker_long" in reason


def test_resize_signaled_when_local_thinks_bigger_than_broker():
    """If broker has LONG 25K but we think LONG 41K, the exit should
    resize down to actual broker quantity."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        broker_positions=[("CAD.JPY", "CAD", "JPY", 25000.0)],  # smaller than local
    )
    ok, reason = inst._broker_state_allows_exit("SELL", 41479)
    assert ok is True  # action is correct direction
    assert "resize_needed" in reason
    assert "25000" in reason


def test_passes_through_when_broker_unreachable():
    """2026-05-20 BUGFIX (operational audit): broker-unreachable now fails
    CLOSED. The original passthrough was itself fail-open inside the
    cascade-retry path — _submit_real_exit calls ib.placeOrder directly
    (bypassing submit_bracket/cluster_exposure fail-closed layers), so
    a network blip silently produced duplicate exits. Better to under-
    trade one cycle than double-exit."""
    inst = _make_runner_stub(
        symbol_pair="CADJPY", local="CAD.JPY", base="CAD", quote="JPY",
        broker_positions=[],
    )
    inst._ib = None  # unreachable
    ok, reason = inst._broker_state_allows_exit("SELL", 41479)
    assert ok is False
    assert "unreachable" in reason
    assert "fail_closed" in reason


# ── End-to-end behaviour via _submit_real_exit (source-level check) ───────

def test_submit_real_exit_calls_broker_truth_check():
    """Source-level verification that _submit_real_exit invokes the guard."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    # Find the _submit_real_exit function body
    idx = src.find("def _submit_real_exit(")
    assert idx >= 0
    # Look ahead 3000 chars (function body) for the guard call
    body = src[idx:idx + 3500]
    assert "_broker_state_allows_exit" in body, (
        "_submit_real_exit no longer calls _broker_state_allows_exit. "
        "Without this check the 2026-05-19 cascade-double-fill recurs."
    )
    assert "EXIT_ABORTED" in body, (
        "_submit_real_exit lost its EXIT_ABORTED log line — operators rely on "
        "this to distinguish 'no exit needed' from 'exit failed'."
    )


def test_cascade_uses_centralized_broker_truth_helper():
    """The cascade timer (_check_order_timeouts) was the first place we
    added the broker-truth check inline. Now it should use the same
    centralized helper _read_broker_position so behaviour is consistent."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def _check_order_timeouts(")
    assert idx >= 0
    body = src[idx:idx + 5000]
    assert "_read_broker_position" in body, (
        "Cascade lost its broker-truth check. The 2026-05-19 double-fill "
        "(IOC retry filled + GTC retry fired anyway) recurs without this."
    )
    assert "EXIT_RACE_RESOLVED" in body


def _entry_body() -> str:
    """Return the source of _submit_real_entry up to (but not including)
    the next `def `. Used by several tests below."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def _submit_real_entry(")
    assert idx >= 0
    end = src.find("\n    def ", idx + 10)
    return src[idx:end] if end > idx else src[idx:]


def test_submit_real_entry_has_broker_position_guard():
    """Source-level: _submit_real_entry must check broker position
    before submitting an entry. If broker already has a position, that's
    an orphan; submitting another entry doubles exposure."""
    body = _entry_body()
    assert "_read_broker_position" in body, (
        "_submit_real_entry lost its broker-position check (Guard 4). "
        "Without it, a fresh entry signal while an orphan exists at the "
        "broker doubles our exposure silently."
    )
    assert "REAL_ENTRY ABORTED" in body, (
        "_submit_real_entry lost the REAL_ENTRY ABORTED log marker — "
        "operators rely on this to spot the broker-truth-refused entries "
        "vs other entry rejections."
    )


def test_submit_real_entry_guards_all_fail_closed():
    """All 4 guards (real_money, halt, FX min, cluster cap, broker pos)
    must fail CLOSED on exception. Codex X5 sweep doctrine. The
    'allowing trade' log marker is forbidden in this function."""
    body = _entry_body()
    assert "allowing trade" not in body.lower(), (
        "_submit_real_entry still contains 'allowing trade' (fail-open). "
        "Codex X5 — every guard exception must fail closed. Replace with "
        "REFUSING and return False."
    )
    # Must have explicit return False in every except block following a guard
    return_false_count = body.count("return False")
    assert return_false_count >= 6, (
        f"Expected >=6 'return False' in _submit_real_entry (4 guards + "
        f"broker check + final exception), found {return_false_count}."
    )
