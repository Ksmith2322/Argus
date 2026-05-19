"""End-to-end smoke test for the 5/18 ENTRY canonical_fills write path.

After fix-now #6 shipped 2026-05-18, canonical_fills.jsonl is still showing
ALL EXIT rows / zero ENTRY rows as of 2026-05-19. That can mean three things:

  1. The code is broken (writes never reach disk).
  2. The fleet processes haven't restarted since the change (long-lived
     daemons hold the pre-change `submit_bracket` in memory).
  3. No new entries have fired since the restart.

This test rules out (1) by exercising the actual submit_bracket function
end-to-end with a fake IB, against a tmp_path canonical log. If the test
passes, the code is correct and the production gap is operational — a
restart is needed."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


class _FakeOrder:
    def __init__(self, order_id=12345):
        self.orderId = order_id


class _FakeTrade:
    def __init__(self, order_id=12345):
        self.order = _FakeOrder(order_id)


class _FakeFill:
    def __init__(self, filled=True, fill_price=100.0, order_id=12345):
        self.filled = filled
        self.fill_price = fill_price
        self.order_id = order_id
        self.reject_reason = None


class _FakeClient:
    def __init__(self, client_id=999):
        self.clientId = client_id


class _FakeIB:
    """Just enough IB to drive submit_bracket through its happy path."""
    def __init__(self):
        self.client = _FakeClient(client_id=999)
        self._placed_orders = []

    def placeOrder(self, contract, order):
        self._placed_orders.append((contract, order))
        return _FakeTrade(order_id=len(self._placed_orders))

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def positions(self):
        return []

    def sleep(self, *_a, **_k):
        pass

    def managedAccounts(self):
        return []


class _FakeContract:
    def __init__(self, symbol="GLD", sec_type="STK", currency="USD"):
        self.symbol = symbol
        self.secType = sec_type
        self.currency = currency
        self.localSymbol = ""
        self.exchange = "SMART"


def test_submit_bracket_writes_entry_row_to_canonical_fills(tmp_path, monkeypatch):
    """Drive submit_bracket through to entry-fill confirmation and verify
    that canonical_fills.jsonl gains exactly one ENTRY row stamped with a
    lineage_id."""
    # Point canonical_fills at a tmp file
    import helio.canonical_fills as cf
    monkeypatch.setattr(cf, "CANONICAL_FILLS_PATH", tmp_path / "canonical_fills.jsonl")
    monkeypatch.setattr(cf, "CANONICAL_FAILOVER_PATH", tmp_path / "canonical_fills_failover.jsonl")

    # Stub out _current_anchor so the test doesn't need a live broker
    monkeypatch.setattr(cf, "_current_anchor", lambda: 30_000.0)

    # Stub out submit_bracket's external dependencies that need a live IB
    import helio.ibkr_execution as ibe
    monkeypatch.setattr(ibe, "is_fleet_halted", lambda: (False, ""))
    monkeypatch.setattr(ibe, "is_flatten_active", lambda: (False, ""))
    monkeypatch.setattr(ibe, "is_market_open", lambda _c: True)
    monkeypatch.setattr(ibe, "query_position", lambda _ib, _c: 0)

    # Force entry to fill cleanly
    monkeypatch.setattr(
        ibe, "_wait_for_fill",
        lambda _ib, _trade, timeout_s: _FakeFill(filled=True, fill_price=300.0, order_id=12345),
    )

    # Bypass the real_money boundary (paper port) — submit_bracket imports
    # enforce_real_money_boundary inside the function, so we patch the
    # source module helio.real_money instead.
    import helio.real_money as rm
    monkeypatch.setattr(rm, "enforce_real_money_boundary",
                        lambda *_a, **_k: None)

    # Stub pending_fills writes
    import helio.pending_fills as pf
    monkeypatch.setattr(pf, "write_pending", lambda **_k: None)
    monkeypatch.setattr(pf, "clear_pending", lambda **_k: None)

    # Stub cluster_exposure to not block
    import helio.cluster_exposure as ce
    monkeypatch.setattr(ce, "would_breach_cluster_cap", lambda *_a, **_k: None)

    # Stub the oversize check helpers
    monkeypatch.setattr(
        ibe, "_oversize_threshold_usd", lambda _sym, _anchor: 1_000_000.0,
    )

    ib = _FakeIB()
    contract = _FakeContract(symbol="TESTSYM", sec_type="STK")

    result = ibe.submit_bracket(
        ib, contract,
        direction="long",
        size=10,
        stop_px=290.0,
        target_px=310.0,
        price_decimals=2,
        est_entry_px=300.0,
        strategy_label="forge_smoke_test",
    )

    assert result.entry.filled, (
        f"submit_bracket didn't reach the fill path: reject={result.entry.reject_reason}"
    )

    # Now verify the canonical log has exactly one ENTRY row.
    log_path = tmp_path / "canonical_fills.jsonl"
    assert log_path.exists(), "submit_bracket didn't write to canonical_fills at all"
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    entry_rows = [r for r in rows if r.get("side") == "ENTRY"]
    assert len(entry_rows) == 1, (
        f"Expected 1 ENTRY row from submit_bracket, got {len(entry_rows)}. "
        f"All rows: {rows}"
    )

    e = entry_rows[0]
    assert e["strategy"] == "forge_smoke_test"
    assert e["symbol"] == "TESTSYM"
    assert e["direction"] == "long"
    assert e["entry_px"] == 300.0
    assert e["size"] == 10
    # Lineage id format: <strategy>.<session_id>.<entry_order_id>
    lineage = e.get("lineage_id")
    assert lineage is not None, "ENTRY row missing lineage_id stamp"
    assert lineage.startswith("forge_smoke_test."), f"unexpected lineage_id: {lineage!r}"
    assert "12345" in lineage, f"entry_order_id not propagated into lineage: {lineage!r}"


def test_argus_entry_write_path_emits_row(tmp_path, monkeypatch):
    """Argus runner_unified has its own ENTRY-write path in _on_fill.
    Source-level check that it actually calls write_fill_typed with side='ENTRY'
    + a lineage_id. If this test ever passes BUT production still shows zero
    ENTRY rows from argus, the issue is operational not code."""
    src = Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py"
    text = src.read_text(encoding="utf-8")
    # Locate the _on_fill ENTRY-side dual-write region
    idx = text.find("ENTRY FILLED")
    tail = text[idx:idx + 3000]
    assert 'side="ENTRY"' in tail
    assert "write_fill_typed" in tail
    assert "make_lineage_id" in tail
