"""Trace loading + mock IB for deterministic regression tests.

Pair with helio.event_recorder. The recorder captures real broker events
to JSONL during a paper trade; this module loads those traces back,
provides a TraceFakeIB stand-in for ib_insync, and lets tests assert
runner behavior at any point in the recorded sequence.

Design choice: we do NOT do real-time event re-emission. The runner's
event-loop integration with ib_insync is intricate and reproducing it
deterministically would be a large simulator. Instead, tests walk the
trace event-by-event, mutate TraceFakeIB state to match each event,
and call the runner method under test. This is enough to convert any
real incident into a regression test that fails on the bug and passes
on the fix.

Usage in a test:

    trace = load_trace("argus_flow/tests/fixtures/cascade_race_20260519.jsonl")
    fake_ib = TraceFakeIB()
    for evt in trace:
        fake_ib.apply(evt)
        if evt["event"] == "post_cancel_checkpoint":
            assert runner._read_broker_position() == 0
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


# ─── trace loading ────────────────────────────────────────────────────────

def load_trace(path: str | Path) -> list[dict]:
    """Load a JSONL trace, return list of event dicts in original order."""
    events: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


# ─── mock objects mirroring ib_insync's surface ───────────────────────────

@dataclass
class FakeOrder:
    orderId: int = 0
    action: str = ""
    totalQuantity: int = 0
    orderType: str = "LMT"
    lmtPrice: float = 0.0
    tif: str = "DAY"


@dataclass
class FakeOrderStatus:
    status: str = "PendingSubmit"
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class FakeTrade:
    order: FakeOrder
    orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)
    contract: Any = None
    fills: list = field(default_factory=list)


@dataclass
class FakeContract:
    symbol: str = ""
    secType: str = "CASH"
    currency: str = ""
    localSymbol: str = ""
    exchange: str = "IDEALPRO"


@dataclass
class FakePosition:
    contract: FakeContract
    position: float = 0.0
    avgCost: float = 0.0
    account: str = "DUTEST"


# ─── the TraceFakeIB ──────────────────────────────────────────────────────

class TraceFakeIB:
    """A mock ib_insync.IB whose state is mutated by trace events.

    The runner under test calls methods on this object instead of a real
    IB instance; the test driver advances state via .apply(event)."""

    def __init__(self) -> None:
        self._positions: dict[str, FakePosition] = {}  # keyed by localSymbol or symbol+currency
        self._trades_by_order_id: dict[int, FakeTrade] = {}
        self._next_order_id = 1
        self._connected = True
        self.cancel_calls: list[int] = []
        self.placeOrder_calls: list[tuple[Any, FakeOrder]] = []

    # ── trace event application ──
    def apply(self, evt: dict) -> None:
        """Mutate state to reflect one trace event."""
        kind = evt.get("event")
        data = evt.get("data", {}) or {}
        if kind == "position":
            self._apply_position(data)
        elif kind == "orderStatus":
            self._apply_order_status(data)
        elif kind == "execDetails":
            self._apply_exec_details(data)
        elif kind == "disconnected":
            self._connected = False
        elif kind == "connected":
            self._connected = True
        # Other events (errors, newOrder, etc.) are recorded but do not
        # mutate state in this minimal mock.

    def _position_key(self, symbol: str, currency: str, local: str = "") -> str:
        if local:
            return f"local:{local}"
        return f"sc:{symbol}:{currency}"

    def _apply_position(self, data: dict) -> None:
        sym = data.get("symbol") or ""
        curr = data.get("currency") or ""
        local = data.get("local_symbol") or ""
        key = self._position_key(sym, curr, local)
        qty = float(data.get("position", 0))
        if qty == 0:
            self._positions.pop(key, None)
            return
        contract = FakeContract(symbol=sym, currency=curr, localSymbol=local)
        self._positions[key] = FakePosition(
            contract=contract, position=qty,
            avgCost=float(data.get("avg_cost", 0) or 0),
            account=data.get("account") or "DUTEST",
        )

    def _apply_order_status(self, data: dict) -> None:
        oid = data.get("order_id")
        if oid is None:
            return
        trade = self._trades_by_order_id.get(oid)
        if trade is None:
            trade = FakeTrade(order=FakeOrder(orderId=oid))
            self._trades_by_order_id[oid] = trade
        trade.orderStatus.status = data.get("status") or trade.orderStatus.status
        if data.get("filled") is not None:
            trade.orderStatus.filled = float(data["filled"])
        if data.get("remaining") is not None:
            trade.orderStatus.remaining = float(data["remaining"])
        if data.get("avg_fill_price") is not None:
            trade.orderStatus.avgFillPrice = float(data["avg_fill_price"])

    def _apply_exec_details(self, data: dict) -> None:
        oid = data.get("order_id")
        if oid is None:
            return
        trade = self._trades_by_order_id.get(oid)
        if trade is None:
            trade = FakeTrade(order=FakeOrder(orderId=oid))
            self._trades_by_order_id[oid] = trade
        fill = SimpleNamespace(execution=SimpleNamespace(
            shares=float(data.get("shares", 0)),
            price=float(data.get("price", 0)),
            side=data.get("side") or "",
            execId=data.get("exec_id") or "",
        ))
        trade.fills.append(fill)

    # ── direct state setters (for tests that don't need a full trace) ──
    def set_position(self, symbol: str, currency: str, qty: float,
                     local_symbol: str = "", avg_cost: float = 0.0) -> None:
        self._apply_position({
            "symbol": symbol, "currency": currency, "local_symbol": local_symbol,
            "position": qty, "avg_cost": avg_cost,
        })

    def clear_positions(self) -> None:
        self._positions.clear()

    # ── ib_insync.IB surface used by runner_unified ──
    def positions(self) -> list[FakePosition]:
        return list(self._positions.values())

    def placeOrder(self, contract: Any, order: FakeOrder) -> FakeTrade:
        if not order.orderId:
            order.orderId = self._next_order_id
            self._next_order_id += 1
        trade = FakeTrade(order=order, contract=contract,
                          orderStatus=FakeOrderStatus(status="PendingSubmit",
                                                     remaining=order.totalQuantity))
        self._trades_by_order_id[order.orderId] = trade
        self.placeOrder_calls.append((contract, order))
        return trade

    def cancelOrder(self, order: FakeOrder) -> None:
        self.cancel_calls.append(order.orderId)
        trade = self._trades_by_order_id.get(order.orderId)
        if trade is not None and trade.orderStatus.status not in ("Filled",):
            trade.orderStatus.status = "Cancelled"

    def isConnected(self) -> bool:
        return self._connected

    def sleep(self, seconds: float) -> None:
        return None  # no-op in mock

    def qualifyContracts(self, *contracts) -> list:
        return list(contracts)


# ─── analyzer: scan a trace for known anti-patterns ───────────────────────

@dataclass
class TraceAnomaly:
    kind: str
    at_index: int
    summary: str


def find_anomalies(trace: list[dict]) -> list[TraceAnomaly]:
    """Heuristic scan for race-condition / drift patterns. Returns a
    list of findings; empty list = no patterns matched.

    A finding here is a SIGNAL TO INVESTIGATE, not necessarily a bug.
    For hard invariant violations, use helio.trace_invariants.

    Currently detects:
      - FILL_DURING_CANCEL: exec fill arriving after a cancel order
        was sent on the same order_id
      - DOUBLE_FILL: two exec details on the same order_id
      - ORPHAN_POSITION_CHANGE: position changes by N units but no
        execDetails event(s) summing to N preceded it (suggests the
        runner missed a fill callback OR broker sent a corrupted
        position update like the 2026-05-19 negative-cost garbage)
      - RAPID_REVERSAL: position flips long -> short -> long (or
        mirror) within a small index window
      - STATUS_REGRESSION: orderStatus goes backward (e.g. Filled
        followed by Submitted on the same order_id)
    """
    anomalies: list[TraceAnomaly] = []
    cancel_seen_at: dict[int, int] = {}  # order_id -> trace index of cancel
    fill_count: dict[int, int] = {}      # order_id -> exec fill count
    cumulative_fills: dict[str, float] = {}  # symbol+currency -> signed total fills
    last_position: dict[str, tuple[float, int]] = {}  # key -> (qty, idx)
    last_status: dict[int, tuple[str, int]] = {}  # order_id -> (status, idx)

    # Status ordering for regression detection: higher = later in lifecycle
    status_rank = {
        "PendingSubmit": 0, "PreSubmitted": 1, "Submitted": 2,
        "PendingCancel": 3, "Filled": 4, "Cancelled": 4,
        "ApiCancelled": 4, "Inactive": 4,
    }

    for i, evt in enumerate(trace):
        kind = evt.get("event")
        data = evt.get("data") or {}

        if kind == "orderStatus":
            oid = data.get("order_id")
            status = data.get("status")
            if status in ("PendingCancel", "Cancelled") and oid is not None:
                cancel_seen_at[oid] = i
            if oid is not None and status is not None and oid in last_status:
                prev_status, prev_idx = last_status[oid]
                if (status_rank.get(status, -1) < status_rank.get(prev_status, -1)
                        and prev_status != status):
                    anomalies.append(TraceAnomaly(
                        kind="STATUS_REGRESSION",
                        at_index=i,
                        summary=f"order_id {oid}: status went backwards "
                                f"{prev_status!r} (idx {prev_idx}) -> "
                                f"{status!r} (idx {i})",
                    ))
            if oid is not None and status is not None:
                last_status[oid] = (status, i)

        elif kind == "execDetails":
            oid = data.get("order_id")
            if oid is None:
                continue
            fill_count[oid] = fill_count.get(oid, 0) + 1
            if oid in cancel_seen_at and i > cancel_seen_at[oid]:
                anomalies.append(TraceAnomaly(
                    kind="FILL_DURING_CANCEL",
                    at_index=i,
                    summary=f"order_id {oid} filled after cancel was sent "
                            f"(cancel at idx {cancel_seen_at[oid]}, fill at idx {i})",
                ))
            if fill_count[oid] > 1:
                anomalies.append(TraceAnomaly(
                    kind="DOUBLE_FILL",
                    at_index=i,
                    summary=f"order_id {oid} has {fill_count[oid]} fills",
                ))
            # Track cumulative fills per symbol+currency for orphan check
            sym = data.get("symbol") or ""
            side = (data.get("side") or "").upper()
            shares = float(data.get("shares") or 0)
            sign = 1 if side in ("BOT", "BUY") else (-1 if side in ("SLD", "SELL") else 0)
            if sym:
                cumulative_fills[sym] = cumulative_fills.get(sym, 0) + sign * shares

        elif kind == "position":
            sym = data.get("symbol") or ""
            curr = data.get("currency") or ""
            local = data.get("local_symbol") or ""
            key = local or f"{sym}:{curr}"
            qty = float(data.get("position") or 0)
            if key in last_position:
                prev_qty, prev_idx = last_position[key]
                delta = qty - prev_qty
                # Orphan check: did fills cover the delta?
                fills_for_sym = cumulative_fills.get(sym, 0)
                # We don't have a clean per-symbol delta in our heuristic;
                # heuristic: if position changes but NO execDetails appeared
                # for sym since the last position update, that's orphan.
                if abs(delta) > 0.01:
                    # Scan back to previous position event for this key —
                    # any execDetails for this symbol in between?
                    saw_fill = False
                    for j in range(prev_idx + 1, i):
                        ej = trace[j]
                        if ej.get("event") == "execDetails":
                            ed = ej.get("data") or {}
                            if ed.get("symbol") == sym:
                                saw_fill = True
                                break
                    if not saw_fill:
                        anomalies.append(TraceAnomaly(
                            kind="ORPHAN_POSITION_CHANGE",
                            at_index=i,
                            summary=f"position {key} changed {prev_qty} -> {qty} "
                                    f"(delta {delta}) with no execDetails between "
                                    f"idx {prev_idx} and {i}",
                        ))
                # Rapid reversal check: long -> short -> long
                if prev_qty > 0 and qty < 0:
                    anomalies.append(TraceAnomaly(
                        kind="RAPID_REVERSAL",
                        at_index=i,
                        summary=f"position {key} flipped {prev_qty} -> {qty} "
                                f"(long to short in one update)",
                    ))
                elif prev_qty < 0 and qty > 0:
                    anomalies.append(TraceAnomaly(
                        kind="RAPID_REVERSAL",
                        at_index=i,
                        summary=f"position {key} flipped {prev_qty} -> {qty} "
                                f"(short to long in one update)",
                    ))
            last_position[key] = (qty, i)

    return anomalies
