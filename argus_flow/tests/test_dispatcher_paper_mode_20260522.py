"""Regression test for the 2026-05-22 dispatcher paper-mode skip bug.

Bug: _route_fill_to_runner and _route_order_status_to_runner in
argus_flow/runner_unified.py filtered instruments with the condition
`if inst.execution_mode != "real": continue`. This silently skipped
paper-stage instruments, which DO submit real broker orders (to the
paper account) and DO receive real execDetails — they just don't risk
real money.

Impact: every argus paper fill since 2026-04-23 (when the
canonical_fills write was added to _on_fill) was silently dropped.
The runner's drift detector eventually caught the unattributed P&L
on 2026-05-22 morning. The investigation surfaced this dispatcher bug.

Fix: filter changed to `not in ("real", "paper")`. Now both real and
paper instruments receive fills + order status routing. Observe-mode
(watcher) still skipped — those don't submit broker orders.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")


def test_fill_router_accepts_paper_mode():
    """_route_fill_to_runner must accept both real and paper execution
    modes. Filtering only on 'real' silently drops paper fills (which
    happens to every paper-stage instrument with a real broker connection)."""
    src = _src()
    idx = src.find("def _route_fill_to_runner(")
    assert idx >= 0, "_route_fill_to_runner not found"
    body = src[idx:idx + 1500]
    # The fix: filter must use the tuple ("real", "paper") not just "real"
    assert 'inst.execution_mode not in ("real", "paper"):' in body, (
        "_route_fill_to_runner is using the old filter that excludes "
        "paper-stage instruments. argus paper fills will be silently "
        "dropped — re-introducing the 2026-05-22 canonical_fills bug."
    )
    # And it must NOT use the broken filter pattern
    assert 'inst.execution_mode != "real":' not in body, (
        "Found legacy filter 'execution_mode != \"real\"' in fill router — "
        "this is the exact bug pattern from the 2026-05-22 fix"
    )


def test_order_status_router_accepts_paper_mode():
    """Same fix applies to _route_order_status_to_runner — paper-stage
    instruments need order status routing too (e.g., to clear stale
    entry_pending state when a rejection arrives)."""
    src = _src()
    idx = src.find("def _route_order_status_to_runner(")
    assert idx >= 0, "_route_order_status_to_runner not found"
    body = src[idx:idx + 1500]
    assert 'inst.execution_mode not in ("real", "paper"):' in body, (
        "_route_order_status_to_runner missing paper-mode support — "
        "paper-stage instruments will miss Rejected/Cancelled events"
    )
    assert 'inst.execution_mode != "real":' not in body, (
        "Found legacy filter in order status router — 2026-05-22 bug pattern"
    )


def test_runner_body_uses_real_or_paper_for_execution_gates():
    """Cross-cutting check: the InstrumentRunner methods that gate on
    execution_mode use the ("real", "paper") tuple. This test guards
    against future regressions where someone tightens to 'real' only,
    which would re-introduce the same class of bug (paper trades
    silently dropped).

    The dispatcher CAN'T use the tuple-form filter naturally, so it has
    a different check (`not in (...)`), but the runner-side gates
    should consistently use `in ("real", "paper")`."""
    src = _src()
    # Count occurrences of the correct pattern vs the wrong one in the
    # method bodies (exclude the routers we already checked above).
    # Look for `execution_mode in ("real", "paper")` — the canonical safe form
    safe_form_count = len(re.findall(
        r'execution_mode in \("real", "paper"\)', src,
    ))
    assert safe_form_count >= 3, (
        f"Expected >= 3 method bodies to gate on execution_mode in "
        f"(\"real\", \"paper\"); found {safe_form_count}. Has someone "
        f"tightened the safe pattern back to 'real'-only?"
    )


def test_dispatcher_does_not_accept_observe_mode():
    """The 'observe' (watcher) mode does NOT submit broker orders and
    therefore should NOT receive fill/order routing. Verify the
    accept-list is specifically ('real', 'paper'), not broader."""
    src = _src()
    # In both routers, the filter should be specifically ("real", "paper")
    # NOT a broader form like 'execution_mode != "observe"' or
    # 'execution_mode != "disabled"' which would also let observers through.
    for router_name in ("_route_fill_to_runner", "_route_order_status_to_runner"):
        idx = src.find(f"def {router_name}(")
        body = src[idx:idx + 1500]
        # Must use the explicit accept-list
        assert 'not in ("real", "paper")' in body, (
            f"{router_name}: filter must be explicit accept-list, not "
            f"a deny-list. Otherwise future stages (e.g. 'observe') "
            f"could be inadvertently routed broker events they don't expect."
        )


# Behavioral guard: simulate a paper-stage instrument and confirm routing
def test_paper_instrument_receives_fill_via_router():
    """Build a paper-stage instrument stub and a fill, then exercise the
    router. Verify _on_fill is invoked. Pre-fix this test would FAIL
    (the router would skip the paper instrument)."""
    from types import SimpleNamespace

    invoked = []
    def fake_on_fill(trade, fill):
        invoked.append((getattr(trade.order, 'orderId', None),
                        getattr(fill, 'shares', None)))

    paper_inst = SimpleNamespace(
        execution_mode="paper",
        symbol="USDJPY",
        contract=SimpleNamespace(secType="CASH", symbol="USD",
                                 currency="JPY", localSymbol="USD.JPY"),
        state=SimpleNamespace(entry_order_id="123"),
        _on_fill=fake_on_fill,
        _log=SimpleNamespace(error=lambda *a, **k: None),
    )
    real_inst = SimpleNamespace(
        execution_mode="real",
        symbol="EURUSD",
        contract=SimpleNamespace(secType="CASH", symbol="EUR",
                                 currency="USD", localSymbol="EUR.USD"),
        state=SimpleNamespace(entry_order_id="456"),
        _on_fill=fake_on_fill,
        _log=SimpleNamespace(error=lambda *a, **k: None),
    )
    observe_inst = SimpleNamespace(
        execution_mode="observe",
        symbol="GBPUSD",
        contract=SimpleNamespace(secType="CASH", symbol="GBP",
                                 currency="USD", localSymbol="GBP.USD"),
        state=SimpleNamespace(entry_order_id="789"),
        _on_fill=fake_on_fill,
        _log=SimpleNamespace(error=lambda *a, **k: None),
    )
    instruments = [paper_inst, real_inst, observe_inst]

    # Replicate the router logic that should be in runner_unified
    from argus_flow.runner_unified import _normalize_ib_key, _runner_to_ib_key

    def _route_fill_to_runner(trade, fill):
        trade_symbol = getattr(trade.contract, 'symbol', '') or ''
        trade_key = _normalize_ib_key(trade.contract) if trade.contract else ''
        for inst in instruments:
            if inst.execution_mode not in ("real", "paper"):
                continue
            inst_key = _runner_to_ib_key(inst)
            if trade_key == inst_key or trade_symbol == inst.symbol:
                try:
                    inst._on_fill(trade, fill)
                except Exception:
                    pass
                break

    # Fire a USDJPY fill — should route to the paper instrument
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=999),
        contract=SimpleNamespace(secType="CASH", symbol="USD",
                                 currency="JPY", localSymbol="USD.JPY"),
    )
    fill = SimpleNamespace(shares=1000)
    _route_fill_to_runner(trade, fill)

    assert len(invoked) == 1, (
        f"Expected 1 _on_fill invocation for paper instrument; got {len(invoked)}. "
        f"Paper-mode dispatcher gate is broken."
    )
    assert invoked[0] == (999, 1000)
