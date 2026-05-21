"""Regression test for the 2026-05-21 placeOrder -> state-write race fix.

The bug: argus_flow.runner_unified had 6 placeOrder call sites where the
order's orderId was read from the returned Trade object AFTER placeOrder
returned, and stored in state via s.save(). Between placeOrder returning
and s.save() completing, ib_insync's event loop processes incoming TWS
messages — including potential execDetails for the just-submitted order.
The fill handler would look up state by order_id and find it empty.

The fix: pre-allocate the orderId via ib.client.getReqId() BEFORE
placeOrder, set it on the order object, write to state, THEN call
placeOrder. This eliminates the race window.

Each of the 6 sites has the same pattern. This test pins the pattern
at the source level (so any reversion is caught immediately) AND
exercises the helper in isolation.

Sites covered (line numbers in runner_unified.py at fix time):
  - _submit_real_entry: entry order (~line 2434)
  - _submit_bracket_orders: stop order (~line 2519)
  - _submit_bracket_orders: target order (~line 2536)
  - _submit_real_exit: exit order (~line 2793)
  - _check_order_timeouts: IOC retry (~line 3312)
  - _check_order_timeouts: GTC retry (~line 3351)
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace


def _src() -> str:
    return (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")


# ─── helper exists ───────────────────────────────────────────────────────

def test_pre_allocate_helper_exists():
    """_pre_allocate_order_id helper must be defined on InstrumentRunner."""
    src = _src()
    assert "def _pre_allocate_order_id(" in src, (
        "_pre_allocate_order_id helper missing — pre-allocation pattern "
        "cannot be reused across the 6 sites"
    )


def test_pre_allocate_helper_uses_client_getReqId():
    """The helper must call ib.client.getReqId() — that's the only
    correct way to pre-allocate an orderId compatible with ib_insync."""
    src = _src()
    idx = src.find("def _pre_allocate_order_id(")
    assert idx >= 0
    body = src[idx:idx + 1500]
    assert "ib.client.getReqId()" in body
    # Must return int (so caller can assign to order.orderId without coercion)
    assert "return int(" in body or "int(ib.client.getReqId())" in body


def test_pre_allocate_helper_falls_back_on_failure():
    """If ib.client isn't available (mock IB, signal-only mode), the
    helper must return None so the caller can fall back to the legacy
    post-placeOrder pattern. The fallback is degraded (race window
    present) but not crashed."""
    src = _src()
    idx = src.find("def _pre_allocate_order_id(")
    body = src[idx:idx + 1500]
    assert "return None" in body
    assert "AttributeError" in body or "Exception" in body, (
        "Helper must catch exceptions from ib.client.getReqId() — bare "
        "crash on attribute error would break legacy/mock IB callers"
    )


def test_pre_allocate_returns_none_for_stub_ib():
    """Behavioral: when called against an IB stub without .client, the
    helper returns None instead of raising."""
    from argus_flow.runner_unified import InstrumentRunner
    inst = SimpleNamespace()
    inst._log = SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )
    method = InstrumentRunner._pre_allocate_order_id.__get__(inst, SimpleNamespace)
    # IB stub without .client attribute
    result = method(SimpleNamespace())
    assert result is None


def test_pre_allocate_returns_int_when_available():
    """When ib.client.getReqId() works, helper returns an int."""
    from argus_flow.runner_unified import InstrumentRunner
    inst = SimpleNamespace()
    inst._log = SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )
    method = InstrumentRunner._pre_allocate_order_id.__get__(inst, SimpleNamespace)
    fake_ib = SimpleNamespace(client=SimpleNamespace(getReqId=lambda: 12345))
    result = method(fake_ib)
    assert result == 12345
    assert isinstance(result, int)


# ─── each of the 6 sites uses the pattern ────────────────────────────────

def test_entry_path_pre_allocates_order_id():
    """_submit_real_entry must call _pre_allocate_order_id and write
    state BEFORE placeOrder."""
    src = _src()
    idx = src.find("def _submit_real_entry(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    assert "self._pre_allocate_order_id(ib)" in body, (
        "Entry path no longer pre-allocates orderId — race window present"
    )
    pre_id_idx = body.find("if entry_pre_id is not None:")
    assert pre_id_idx >= 0
    branch = body[pre_id_idx:pre_id_idx + 1500]
    state_write_idx = branch.find("s.entry_order_id = str(entry_pre_id)")
    place_order_idx = branch.find("trade = ib.placeOrder(self.contract, order)")
    assert state_write_idx >= 0 and place_order_idx >= 0
    assert state_write_idx < place_order_idx, (
        "Entry path: state write must happen BEFORE placeOrder"
    )


def test_bracket_stop_pre_allocates_order_id():
    """Bracket stop in _submit_bracket_orders must pre-allocate."""
    src = _src()
    idx = src.find("def _submit_bracket_orders(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    assert "stop_pre_id = self._pre_allocate_order_id(ib)" in body, (
        "Bracket stop no longer pre-allocates orderId"
    )
    # State write order: s.stop_order_id BEFORE stop_trade = ib.placeOrder
    branch_idx = body.find("if stop_pre_id is not None:")
    branch = body[branch_idx:branch_idx + 1000]
    state_write_idx = branch.find("s.stop_order_id = str(stop_pre_id)")
    place_order_idx = branch.find("stop_trade = ib.placeOrder")
    assert 0 <= state_write_idx < place_order_idx


def test_bracket_target_pre_allocates_order_id():
    """Bracket target in _submit_bracket_orders must pre-allocate."""
    src = _src()
    idx = src.find("def _submit_bracket_orders(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    assert "tgt_pre_id = self._pre_allocate_order_id(ib)" in body, (
        "Bracket target no longer pre-allocates orderId"
    )
    branch_idx = body.find("if tgt_pre_id is not None:")
    branch = body[branch_idx:branch_idx + 1000]
    state_write_idx = branch.find("s.target_order_id = str(tgt_pre_id)")
    place_order_idx = branch.find("target_trade = ib.placeOrder")
    assert 0 <= state_write_idx < place_order_idx


def test_exit_path_pre_allocates_order_id():
    """_submit_real_exit must pre-allocate."""
    src = _src()
    idx = src.find("def _submit_real_exit(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    assert "exit_pre_id = self._pre_allocate_order_id(ib)" in body
    branch_idx = body.find("if exit_pre_id is not None:")
    branch = body[branch_idx:branch_idx + 1500]
    state_write_idx = branch.find("s.exit_order_id = str(exit_pre_id)")
    place_order_idx = branch.find("trade = ib.placeOrder(self.contract, order)")
    assert 0 <= state_write_idx < place_order_idx


def test_exit_retries_pre_allocate_order_id():
    """Both IOC and GTC retry paths in _check_order_timeouts must
    pre-allocate."""
    src = _src()
    idx = src.find("def _check_order_timeouts(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    # IOC retry
    assert "retry_pre_id = self._pre_allocate_order_id(ib)" in body, (
        "IOC retry no longer pre-allocates orderId"
    )
    # GTC retry
    assert "gtc_pre_id = self._pre_allocate_order_id(ib)" in body, (
        "GTC retry no longer pre-allocates orderId"
    )


# ─── cross-cutting: no bare `trade = ib.placeOrder` followed immediately
#     by `s.<name>_order_id = str(getattr(trade.order...))` without a
#     guarding `if X_pre_id is None:` (fallback path) ────────────────────

def test_all_state_tracked_placeOrder_sites_have_pre_allocate_branch():
    """For every `s.X_order_id = str(...)` write that comes immediately
    after a placeOrder, there must be a paired pre-allocate branch above
    it. This catches a regression where someone reverts a site to the
    old pattern."""
    src = _src()
    # Look for the legacy pattern: placeOrder followed by state write
    # within ~3 lines, NOT inside an else branch
    pattern = re.compile(
        r"trade(?:[a-z_]*)? = ib\.placeOrder\(self\.contract,[^)]+\)\s*\n"
        r"\s+s\.(entry|exit|stop|target)_order_id = str\(getattr\(",
        re.MULTILINE,
    )
    legacy_matches = []
    for m in pattern.finditer(src):
        # Acceptable if the match is INSIDE an `else:` block — that's
        # the fallback path of the pre-allocate pattern. Detect by
        # looking back for `else:` between the last `if .._pre_id is not None:`
        # and this match.
        prefix = src[:m.start()]
        # Indent-agnostic search for else: (the retry paths are nested
        # deeper than the entry/exit paths)
        else_pattern = re.compile(r"\n[ \t]+else:\n", re.MULTILINE)
        else_matches = list(else_pattern.finditer(prefix))
        last_else = else_matches[-1].start() if else_matches else -1
        last_pre_check = prefix.rfind("_pre_id is not None:")
        if last_pre_check > 0 and last_else > last_pre_check:
            continue  # legitimate fallback branch
        # Allow site 4679 (emergency flatten) which doesn't track state
        legacy_matches.append((m.start(), m.group()))

    assert not legacy_matches, (
        f"Found {len(legacy_matches)} bare placeOrder→state-write patterns "
        f"without a pre-allocate guard. Each is a race-window regression.\n  "
        + "\n  ".join(f"at offset {off}: {snippet[:120]}" for off, snippet in legacy_matches[:5])
    )
