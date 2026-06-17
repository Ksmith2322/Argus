"""Real-time event dispatcher for golden-trace replay.

Phase 3 of the activity-multiplication / bug-discovery rollout. Phase 2's
TraceFakeIB mutates state but does NOT fire events to subscribers; this
dispatcher closes that gap. It reads a JSONL trace, reconstructs minimal
ib_insync-shaped event objects, and fires them to a target's event
slots in either original order, accelerated time, or with chaos
transformations applied (swap, delay, drop, duplicate) to probe
callback-ordering bugs.

Use case: take a real recorded trace from helio.event_recorder, run it
through the dispatcher against a runner stub (or a recording subscriber
that just logs calls), inject chaos, observe what diverges. Bugs that
depend on event timing — fill arriving before status update, two
position events out of order, exec arriving during cancel sleep — are
exposed deterministically without waiting for them to recur live.

Limitations (deliberate, not bugs):
  - Doesn't run a real asyncio loop; events fire synchronously on the
    dispatcher's thread. This catches state-machine bugs but not
    asyncio-scheduler interleavings.
  - Doesn't drive the full runner. Handlers are tested against stub
    targets; per-handler I/O sinks (canonical_fills writes etc.) must
    be mocked by the test author. Generic infrastructure here; specific
    test setup per handler.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ─── chaos transformations ────────────────────────────────────────────────

@dataclass
class ChaosTransform:
    """One mutation applied to the event stream before dispatch."""
    kind: str  # "swap" | "delay_ms" | "drop" | "duplicate"
    target_idx: int = 0
    other_idx: int = 0       # for swap
    delay_ms: int = 0        # for delay_ms


def apply_chaos(events: list[dict], transforms: list[ChaosTransform]) -> list[dict]:
    """Return a new event list with transforms applied. Original list
    not mutated. Indices in transforms refer to positions in the ORIGINAL
    event list; later transforms see the result of earlier ones."""
    out = [dict(e) for e in events]
    for t in transforms:
        if t.kind == "swap":
            if 0 <= t.target_idx < len(out) and 0 <= t.other_idx < len(out):
                out[t.target_idx], out[t.other_idx] = out[t.other_idx], out[t.target_idx]
        elif t.kind == "drop":
            if 0 <= t.target_idx < len(out):
                out.pop(t.target_idx)
        elif t.kind == "duplicate":
            if 0 <= t.target_idx < len(out):
                out.insert(t.target_idx + 1, dict(out[t.target_idx]))
        elif t.kind == "delay_ms":
            if 0 <= t.target_idx < len(out):
                out[t.target_idx] = {**out[t.target_idx], "_dispatch_delay_ms": t.delay_ms}
        else:
            raise ValueError(f"unknown chaos transform kind: {t.kind!r}")
    return out


# ─── event object reconstruction ──────────────────────────────────────────

def _reconstruct_trade(data: dict) -> SimpleNamespace:
    """Build a minimal Trade-shaped object from an event data dict.

    Subscribers receive Trade objects with .order, .orderStatus, .contract,
    .fills. Reconstruct just enough for ordinary handlers to work."""
    contract = SimpleNamespace(
        symbol=data.get("symbol", ""),
        secType="CASH",
        currency=data.get("currency", ""),
        localSymbol=data.get("local_symbol", ""),
    )
    order = SimpleNamespace(
        orderId=data.get("order_id"),
        permId=data.get("perm_id"),
        action=data.get("action"),
        totalQuantity=data.get("qty"),
        orderType=data.get("type", "LMT"),
        lmtPrice=data.get("lmt_price", 0.0),
        tif=data.get("tif", "DAY"),
    )
    order_status = SimpleNamespace(
        status=data.get("status", "PendingSubmit"),
        filled=data.get("filled", 0),
        remaining=data.get("remaining", 0),
        avgFillPrice=data.get("avg_fill_price", 0.0),
    )
    return SimpleNamespace(
        order=order, orderStatus=order_status, contract=contract, fills=[],
    )


def _reconstruct_fill(data: dict) -> SimpleNamespace:
    execution = SimpleNamespace(
        execId=data.get("exec_id"),
        shares=data.get("shares"),
        price=data.get("price"),
        side=data.get("side"),
        time=data.get("time"),
    )
    return SimpleNamespace(execution=execution)


def _reconstruct_position(data: dict) -> SimpleNamespace:
    contract = SimpleNamespace(
        symbol=data.get("symbol", ""),
        secType="CASH",
        currency=data.get("currency", ""),
        localSymbol=data.get("local_symbol", ""),
    )
    return SimpleNamespace(
        contract=contract,
        position=data.get("position", 0),
        avgCost=data.get("avg_cost", 0),
        account=data.get("account", ""),
    )


def _reconstruct_portfolio_item(data: dict) -> SimpleNamespace:
    """ib_insync's PortfolioItem is a NamedTuple-ish with contract, position,
    marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL,
    account. Reconstruct from a trace data dict."""
    contract = SimpleNamespace(
        symbol=data.get("symbol", ""),
        currency=data.get("currency", ""),
        localSymbol=data.get("local_symbol", ""),
    )
    return SimpleNamespace(
        contract=contract,
        position=data.get("position", 0),
        marketPrice=data.get("market_price", 0),
        marketValue=data.get("market_value", 0),
        averageCost=data.get("avg_cost", 0),
        unrealizedPNL=data.get("unrealized_pnl", 0),
        realizedPNL=data.get("realized_pnl", 0),
        account=data.get("account", ""),
    )


# ─── dispatcher ───────────────────────────────────────────────────────────

@dataclass
class DispatchRecord:
    event_index: int
    event_kind: str
    fired: bool
    reason: str = ""


class EventDispatcher:
    """Fires trace events to a target's event slots.

    target must expose ib_insync-style event slots that support `+=`:
      orderStatusEvent, execDetailsEvent, errorEvent, positionEvent,
      newOrderEvent, disconnectedEvent, connectedEvent

    Mode controls timing:
      "instant"      — fire all events back-to-back, no waits
      "accelerated"  — wait monotonic_ms gaps / speedup
      "realtime"     — wait actual monotonic_ms gaps
    """

    def __init__(self, target: Any):
        self.target = target

    def dispatch(self, events: list[dict], *, mode: str = "instant",
                 speedup: float = 1.0,
                 chaos: list[ChaosTransform] | None = None) -> list[DispatchRecord]:
        if chaos:
            events = apply_chaos(events, chaos)
        records: list[DispatchRecord] = []
        prev_monotonic_ms: Optional[int] = None
        for i, evt in enumerate(events):
            wait_ms = 0
            cur_monotonic_ms = evt.get("monotonic_ms")
            if mode == "realtime" and prev_monotonic_ms is not None and cur_monotonic_ms is not None:
                wait_ms = max(0, cur_monotonic_ms - prev_monotonic_ms)
            elif mode == "accelerated" and prev_monotonic_ms is not None and cur_monotonic_ms is not None:
                wait_ms = max(0, int((cur_monotonic_ms - prev_monotonic_ms) / max(speedup, 0.01)))
            wait_ms += int(evt.get("_dispatch_delay_ms", 0) or 0)
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
            rec = self._fire(i, evt)
            records.append(rec)
            if cur_monotonic_ms is not None:
                prev_monotonic_ms = cur_monotonic_ms
        return records

    def _fire(self, index: int, evt: dict) -> DispatchRecord:
        kind = evt.get("event")
        data = evt.get("data") or {}
        try:
            if kind == "orderStatus":
                slot = getattr(self.target, "orderStatusEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no orderStatusEvent slot")
                trade = _reconstruct_trade(data)
                slot.fire(trade) if hasattr(slot, "fire") else slot(trade)
            elif kind == "execDetails":
                slot = getattr(self.target, "execDetailsEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no execDetailsEvent slot")
                trade = _reconstruct_trade(data)
                fill = _reconstruct_fill(data)
                trade.fills.append(fill)
                slot.fire(trade, fill) if hasattr(slot, "fire") else slot(trade, fill)
            elif kind == "error":
                slot = getattr(self.target, "errorEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no errorEvent slot")
                contract = SimpleNamespace(symbol=data.get("symbol"))
                args = (data.get("req_id"), data.get("code"), data.get("msg"), contract)
                slot.fire(*args) if hasattr(slot, "fire") else slot(*args)
            elif kind == "position":
                slot = getattr(self.target, "positionEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no positionEvent slot")
                pos = _reconstruct_position(data)
                slot.fire(pos) if hasattr(slot, "fire") else slot(pos)
            elif kind == "newOrder":
                slot = getattr(self.target, "newOrderEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no newOrderEvent slot")
                trade = _reconstruct_trade(data)
                slot.fire(trade) if hasattr(slot, "fire") else slot(trade)
            elif kind == "disconnected":
                slot = getattr(self.target, "disconnectedEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no disconnectedEvent slot")
                slot.fire() if hasattr(slot, "fire") else slot()
            elif kind == "connected":
                slot = getattr(self.target, "connectedEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no connectedEvent slot")
                slot.fire() if hasattr(slot, "fire") else slot()
            elif kind == "updatePortfolio":
                slot = getattr(self.target, "updatePortfolioEvent", None)
                if slot is None:
                    return DispatchRecord(index, kind, False, "no updatePortfolioEvent slot")
                item = _reconstruct_portfolio_item(data)
                slot.fire(item) if hasattr(slot, "fire") else slot(item)
            else:
                return DispatchRecord(index, kind or "?", False, f"unhandled event kind {kind!r}")
            return DispatchRecord(index, kind, True)
        except Exception as e:
            log.warning("dispatcher fire error on event %d (%s): %s", index, kind, e)
            return DispatchRecord(index, kind or "?", False, f"exception: {e}")


# ─── recording subscriber (test helper) ───────────────────────────────────

class RecordingSubscriber:
    """Drop-in subscriber that logs every call. Tests assert against
    `.calls` to verify event order, args, and any dropped events."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def on_order_status(self, trade):
        self.calls.append(("orderStatus",
                           (getattr(trade.order, "orderId", None),
                            getattr(trade.orderStatus, "status", None)),
                           {}))

    def on_exec_details(self, trade, fill):
        self.calls.append(("execDetails",
                           (getattr(trade.order, "orderId", None),
                            getattr(fill.execution, "shares", None),
                            getattr(fill.execution, "price", None)),
                           {}))

    def on_error(self, req_id, code, msg, contract):
        self.calls.append(("error", (req_id, code, msg), {}))

    def on_position(self, pos):
        self.calls.append(("position",
                           (getattr(pos.contract, "symbol", None),
                            getattr(pos, "position", None)),
                           {}))

    def on_new_order(self, trade):
        self.calls.append(("newOrder",
                           (getattr(trade.order, "orderId", None),
                            getattr(trade.order, "action", None)),
                           {}))

    def on_disconnected(self):
        self.calls.append(("disconnected", (), {}))

    def on_connected(self):
        self.calls.append(("connected", (), {}))

    def on_update_portfolio(self, item):
        self.calls.append(("updatePortfolio",
                           (getattr(item.contract, "symbol", None),
                            getattr(item, "position", None),
                            getattr(item, "unrealizedPNL", None)),
                           {}))


# ─── helper: build a target with ib_insync-style event slots ──────────────

class _DispatcherEventSlot:
    """Minimal +=/-= subscription slot supporting fire()."""
    def __init__(self):
        self.handlers: list[Callable] = []
    def __iadd__(self, h: Callable):
        self.handlers.append(h)
        return self
    def __isub__(self, h: Callable):
        try:
            self.handlers.remove(h)
        except ValueError:
            pass
        return self
    def fire(self, *args, **kwargs):
        for h in list(self.handlers):
            h(*args, **kwargs)


def make_target() -> SimpleNamespace:
    """Build a stand-in target with empty event slots, ready for the
    dispatcher to fire to and tests to subscribe to."""
    return SimpleNamespace(
        orderStatusEvent=_DispatcherEventSlot(),
        execDetailsEvent=_DispatcherEventSlot(),
        errorEvent=_DispatcherEventSlot(),
        positionEvent=_DispatcherEventSlot(),
        newOrderEvent=_DispatcherEventSlot(),
        disconnectedEvent=_DispatcherEventSlot(),
        connectedEvent=_DispatcherEventSlot(),
        updatePortfolioEvent=_DispatcherEventSlot(),
    )


def subscribe_recording(target: SimpleNamespace, sub: RecordingSubscriber) -> None:
    """Wire a RecordingSubscriber to all event slots on a target."""
    target.orderStatusEvent += sub.on_order_status
    target.execDetailsEvent += sub.on_exec_details
    target.errorEvent += sub.on_error
    target.positionEvent += sub.on_position
    target.newOrderEvent += sub.on_new_order
    target.disconnectedEvent += sub.on_disconnected
    target.connectedEvent += sub.on_connected
    target.updatePortfolioEvent += sub.on_update_portfolio
