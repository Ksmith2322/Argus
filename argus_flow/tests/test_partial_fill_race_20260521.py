"""Regression test for the 2026-05-21 partial-fill state-ordering race.

Bug: in _on_fill, the stop/target handlers treated ANY fill as terminal,
calling _finalize_real_exit which clears trade state. But ib_insync
fires fill events per execution, so a partial fill on the stop or
target leg would:
  1. Fire _on_fill with fill_qty < position_size
  2. Call _finalize_real_exit, clearing trade state
  3. OCA-cancel the other leg
  4. Leave the REMAINING position open at broker with NO bracket
  5. Runner thinks it's flat; broker has unprotected exposure

The fix: broker-truth check before finalizing. If broker still has a
position, log PARTIAL_*_FILL and return without finalizing. The
bracket_health check (60s interval) re-arms protection on the
remaining quantity.

Documented as 5/20 P2 #4 (Partial-fill state ordering on bracket legs).
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone


def _on_fill_body() -> str:
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def _on_fill(")
    end = src.find("\n    def ", idx + 10)
    return src[idx:end] if end > idx else src[idx:]


# ─── source-level regression assertions ──────────────────────────────────

def test_stop_fill_handler_calls_broker_truth_check():
    """Stop fill handler must call _read_broker_position before
    _finalize_real_exit to detect partial fills."""
    body = _on_fill_body()
    # Find the stop-fill branch
    stop_idx = body.find("─ Stop fill ─")
    assert stop_idx >= 0, "Stop fill section header missing"
    # Section runs to the next ── divider
    next_section = body.find("─", stop_idx + 20)
    stop_section = body[stop_idx:next_section] if next_section > stop_idx else body[stop_idx:stop_idx + 1500]
    assert "self._read_broker_position()" in stop_section, (
        "Stop fill handler no longer calls _read_broker_position — "
        "partial-fill race window restored"
    )
    assert "PARTIAL_STOP_FILL" in stop_section, (
        "PARTIAL_STOP_FILL log marker removed — operator can no longer "
        "tell when a partial fill is in progress"
    )


def test_target_fill_handler_calls_broker_truth_check():
    """Target fill handler must call _read_broker_position before
    _finalize_real_exit to detect partial fills."""
    body = _on_fill_body()
    tgt_idx = body.find("─ Target fill ─")
    assert tgt_idx >= 0, "Target fill section header missing"
    next_section = body.find("─", tgt_idx + 20)
    tgt_section = body[tgt_idx:next_section] if next_section > tgt_idx else body[tgt_idx:tgt_idx + 1500]
    assert "self._read_broker_position()" in tgt_section
    assert "PARTIAL_TARGET_FILL" in tgt_section


def test_finalize_only_called_when_broker_flat():
    """The branch order must be: check broker → early-return if not flat →
    only finalize if broker is flat. Verify by source pattern."""
    body = _on_fill_body()
    # For each branch, locate the PARTIAL_*_FILL warning and verify a
    # `return` follows it BEFORE the next call to _finalize_real_exit.
    for section_name, marker in (("Stop fill", "PARTIAL_STOP_FILL"),
                                  ("Target fill", "PARTIAL_TARGET_FILL")):
        marker_idx = body.find(marker)
        assert marker_idx >= 0, f"{marker} log marker missing in {section_name} branch"
        # From marker onward, the FIRST return should come BEFORE the FIRST
        # _finalize_real_exit. If finalize comes first, the partial-fill
        # branch was deleted / reordered.
        tail = body[marker_idx:]
        partial_return = tail.find("return")
        finalize_call = tail.find("_finalize_real_exit")
        assert 0 <= partial_return < finalize_call, (
            f"{section_name} branch: after {marker} log, the early "
            f"`return` must come BEFORE the next _finalize_real_exit. "
            f"Otherwise the broker-truth check is bypassed and partial "
            f"fills clear trade state with broker still open."
        )


def test_stop_cancel_target_still_present_for_full_fill():
    """After confirming full fill, the OCA-paired leg must still be
    cancelled explicitly (defense in depth: don't rely solely on
    broker's OCA propagation)."""
    body = _on_fill_body()
    stop_idx = body.find("─ Stop fill ─")
    next_section = body.find("─", stop_idx + 20)
    section = body[stop_idx:next_section] if next_section > stop_idx else body[stop_idx:stop_idx + 1500]
    assert 'self._cancel_order_by_id(s.target_order_id, "target")' in section, (
        "After full stop fill, the target leg cancel was removed — "
        "broker's OCA may already cover this but defense-in-depth says cancel anyway"
    )
    # Same for target → stop
    tgt_idx = body.find("─ Target fill ─")
    next_section = body.find("─", tgt_idx + 20)
    section = body[tgt_idx:next_section] if next_section > tgt_idx else body[tgt_idx:tgt_idx + 1500]
    assert 'self._cancel_order_by_id(s.stop_order_id, "stop")' in section


# ─── behavioral verification with a stub runner ──────────────────────────

class _RecordingLog:
    def __init__(self):
        self.records = []
    def info(self, *a, **k): self.records.append(("info", a, k))
    def warning(self, *a, **k): self.records.append(("warning", a, k))
    def error(self, *a, **k): self.records.append(("error", a, k))
    def critical(self, *a, **k): self.records.append(("critical", a, k))


def _build_stub_runner(position_size=29362.0, broker_position=14000.0):
    """Stub a partial-runner with just enough state for _on_fill to
    exercise the partial-fill branches."""
    from argus_flow.runner_unified import InstrumentRunner
    inst = SimpleNamespace()
    inst.symbol = "USDJPY"
    inst.contract = SimpleNamespace(secType="CASH", symbol="USD", currency="JPY",
                                    localSymbol="USD.JPY")
    inst._log = _RecordingLog()

    # Broker state — partial fill scenario: bracket fired qty=15000 but
    # broker still shows position remaining
    class _FakeIB:
        def positions(self):
            from types import SimpleNamespace as SN
            return [SN(contract=SN(symbol="USD", currency="JPY", localSymbol="USD.JPY"),
                       position=broker_position, avgCost=159.0)]
    inst._ib = _FakeIB()

    inst.state = SimpleNamespace(
        position="LONG", position_size=position_size,
        stop_order_id="100", target_order_id="101", exit_order_id="",
        entry_order_id="99", entry_pending=False, exit_pending=False,
    )
    inst.state.save = lambda: None
    inst.state.clear_trade_state = lambda: None
    inst._processed_fill_ids = set()
    inst._persist_fill_id = lambda fid: inst._processed_fill_ids.add(fid)
    inst._cancel_order_by_id = lambda *a, **k: None
    inst._finalize_real_exit = lambda *a, **k: setattr(inst, "_finalize_called", True)

    inst._read_broker_position = InstrumentRunner._read_broker_position.__get__(inst, SimpleNamespace)
    inst._on_fill = InstrumentRunner._on_fill.__get__(inst, SimpleNamespace)
    return inst


def _make_fill(order_id, qty, price=159.0, exec_id=None):
    return SimpleNamespace(
        execution=SimpleNamespace(execId=exec_id or f"e_{order_id}_{qty}"),
        shares=qty, price=price,
    ), SimpleNamespace(
        order=SimpleNamespace(orderId=order_id),
        contract=SimpleNamespace(symbol="USD"),
    )


def test_partial_stop_fill_does_NOT_finalize_when_broker_still_open():
    """The bug scenario: stop fires partial, broker still shows position.
    _finalize_real_exit must NOT be called."""
    inst = _build_stub_runner(position_size=29362.0, broker_position=14000.0)
    fill, trade = _make_fill(order_id=100, qty=15000.0)  # partial fill
    inst._on_fill(trade, fill)
    assert not getattr(inst, "_finalize_called", False), (
        "_finalize_real_exit called on partial fill — state would have "
        "been cleared while broker still has open position"
    )
    # And the partial warning fired
    warnings = [r for r in inst._log.records if r[0] == "warning"]
    assert any("PARTIAL_STOP_FILL" in str(r[1]) for r in warnings)


def test_full_stop_fill_DOES_finalize_when_broker_flat():
    """Stop fires fully → broker flat → finalize must be called."""
    inst = _build_stub_runner(position_size=29362.0, broker_position=0.0)
    fill, trade = _make_fill(order_id=100, qty=29362.0)
    inst._on_fill(trade, fill)
    assert getattr(inst, "_finalize_called", False), (
        "_finalize_real_exit NOT called on full stop fill — "
        "position is closed but state will never be reset"
    )


def test_partial_target_fill_does_NOT_finalize():
    """Same race on target side."""
    inst = _build_stub_runner(position_size=29362.0, broker_position=20000.0)
    fill, trade = _make_fill(order_id=101, qty=9362.0)  # partial
    inst._on_fill(trade, fill)
    assert not getattr(inst, "_finalize_called", False)
    warnings = [r for r in inst._log.records if r[0] == "warning"]
    assert any("PARTIAL_TARGET_FILL" in str(r[1]) for r in warnings)


def test_full_target_fill_DOES_finalize():
    inst = _build_stub_runner(position_size=29362.0, broker_position=0.0)
    fill, trade = _make_fill(order_id=101, qty=29362.0)
    inst._on_fill(trade, fill)
    assert getattr(inst, "_finalize_called", False)


def test_broker_unreachable_treats_fill_as_full():
    """When _ib is None / broker unreachable, _read_broker_position
    returns None. The handler's `is not None` check means the partial
    guard doesn't trigger — falls through to _finalize. This is the
    fail-tolerant path (don't get stuck waiting if we can't ask broker).
    Document this contract."""
    inst = _build_stub_runner(position_size=29362.0)
    inst._ib = None  # broker unreachable
    fill, trade = _make_fill(order_id=100, qty=15000.0)
    inst._on_fill(trade, fill)
    # With broker unreachable, falls through to _finalize (legacy behavior)
    assert getattr(inst, "_finalize_called", False), (
        "Broker-unreachable path: handler should finalize (no way to "
        "detect partial) — wait-forever would be worse"
    )
