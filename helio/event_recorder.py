"""Golden-trace event recorder for ib_insync IB instances.

Captures every broker-side event (orderStatus, execDetails, position,
error, disconnect/connect) to a JSONL trace file so that any live
incident can be reverse-engineered later — or replayed deterministically
via helio.trace_replay against a MockIB.

Why this exists: the operational audits on 2026-05-19/20 caught 15+ bugs
that surfaced only during real fills. Each one was reconstructed from
unstructured logs after the fact, which lost timing fidelity. With a
structured trace, every incident becomes a candidate regression test.

Usage:
    from ib_insync import IB
    from helio.event_recorder import attach_recorder

    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=53)
    recorder = attach_recorder(ib, "argus_flow/logs/traces/usdjpy_20260520.jsonl")
    # ... runner does its thing ...
    recorder.close()

The recorder appends one JSON line per event. Schema (no field is
optional — missing values are recorded as null):

  {
    "ts": "2026-05-20T15:30:01.123456+00:00",
    "monotonic_ms": 12345,
    "event": "orderStatus" | "execDetails" | "error" | "position" |
             "newOrder" | "openOrder" | "disconnected" | "connected"
             | "commissionReport",
    "data": { event-specific payload }
  }

No side effects beyond appending to the trace file. Safe to attach to a
running IB instance. Detach via recorder.close() to stop capturing.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def _safe(obj: Any) -> Any:
    """Best-effort JSON-friendly serialization for ib_insync objects."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe(x) for x in obj]
    if is_dataclass(obj):
        try:
            return _safe(asdict(obj))
        except Exception:
            pass
    # ib_insync objects are NamedTuple-like; try _asdict
    if hasattr(obj, "_asdict"):
        try:
            return _safe(dict(obj._asdict()))
        except Exception:
            pass
    # Fall back to public attrs
    if hasattr(obj, "__dict__"):
        try:
            return {k: _safe(v) for k, v in obj.__dict__.items()
                    if not k.startswith("_")}
        except Exception:
            pass
    return str(obj)


class EventRecorder:
    """Subscribes to IB events, appends JSONL trace, detaches on close."""

    def __init__(self, ib: Any, trace_path: str | Path):
        self.ib = ib
        self.path = Path(trace_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0_monotonic = time.monotonic()
        self._attached = False
        self._handlers: list[tuple[Any, Any]] = []  # (event_slot, handler) for detach
        self._attach()

    def _now_payload(self) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "monotonic_ms": int((time.monotonic() - self._t0_monotonic) * 1000),
        }

    def _write(self, event_kind: str, data: Any) -> None:
        line = self._now_payload()
        line["event"] = event_kind
        line["data"] = _safe(data)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except Exception as e:
            # Trace failure must NEVER take down the runner
            log.warning("event_recorder write failed: %s", e)

    # ── handlers ─────────────────────────────────────────────────────────
    def _on_order_status(self, trade) -> None:
        self._write("orderStatus", {
            "order_id": getattr(trade.order, "orderId", None),
            "perm_id": getattr(trade.order, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "filled": getattr(trade.orderStatus, "filled", None),
            "remaining": getattr(trade.orderStatus, "remaining", None),
            "avg_fill_price": getattr(trade.orderStatus, "avgFillPrice", None),
            "symbol": getattr(getattr(trade, "contract", None), "symbol", None),
        })

    def _on_exec_details(self, trade, fill) -> None:
        self._write("execDetails", {
            "order_id": getattr(trade.order, "orderId", None),
            "exec_id": getattr(getattr(fill, "execution", None), "execId", None),
            "shares": getattr(getattr(fill, "execution", None), "shares", None),
            "price": getattr(getattr(fill, "execution", None), "price", None),
            "side": getattr(getattr(fill, "execution", None), "side", None),
            "time": str(getattr(getattr(fill, "execution", None), "time", None)),
            "symbol": getattr(getattr(trade, "contract", None), "symbol", None),
        })

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        self._write("error", {
            "req_id": reqId, "code": errorCode, "msg": errorString,
            "symbol": getattr(contract, "symbol", None) if contract else None,
        })

    def _on_position(self, position) -> None:
        self._write("position", {
            "account": getattr(position, "account", None),
            "symbol": getattr(getattr(position, "contract", None), "symbol", None),
            "currency": getattr(getattr(position, "contract", None), "currency", None),
            "local_symbol": getattr(getattr(position, "contract", None), "localSymbol", None),
            "position": getattr(position, "position", None),
            "avg_cost": getattr(position, "avgCost", None),
        })

    def _on_new_order(self, trade) -> None:
        self._write("newOrder", {
            "order_id": getattr(trade.order, "orderId", None),
            "action": getattr(trade.order, "action", None),
            "qty": getattr(trade.order, "totalQuantity", None),
            "type": getattr(trade.order, "orderType", None),
            "lmt_price": getattr(trade.order, "lmtPrice", None),
            "tif": getattr(trade.order, "tif", None),
            "symbol": getattr(getattr(trade, "contract", None), "symbol", None),
        })

    def _on_disconnected(self) -> None:
        self._write("disconnected", {})

    def _on_connected(self) -> None:
        self._write("connected", {})

    # ── attach / close ───────────────────────────────────────────────────
    def _attach(self) -> None:
        wirings = [
            (getattr(self.ib, "orderStatusEvent", None), self._on_order_status),
            (getattr(self.ib, "execDetailsEvent", None), self._on_exec_details),
            (getattr(self.ib, "errorEvent", None), self._on_error),
            (getattr(self.ib, "positionEvent", None), self._on_position),
            (getattr(self.ib, "newOrderEvent", None), self._on_new_order),
            (getattr(self.ib, "disconnectedEvent", None), self._on_disconnected),
            (getattr(self.ib, "connectedEvent", None), self._on_connected),
        ]
        for slot, handler in wirings:
            if slot is None:
                continue
            try:
                slot += handler
                self._handlers.append((slot, handler))
            except Exception as e:
                log.warning("event_recorder could not subscribe to %s: %s",
                            getattr(slot, "__name__", "?"), e)
        self._write("connected", {"recorder_attached": True})
        self._attached = True

    def close(self) -> None:
        if not self._attached:
            return
        self._write("recorder_closed", {})
        for slot, handler in self._handlers:
            try:
                slot -= handler
            except Exception:
                pass
        self._handlers.clear()
        self._attached = False


def attach_recorder(ib: Any, trace_path: str | Path) -> EventRecorder:
    """Convenience: build + attach a recorder in one call."""
    return EventRecorder(ib, trace_path)


def attach_via_env(ib: Any, env_var: str = "GOLDEN_TRACE_PATH") -> "EventRecorder | None":
    """Read trace path from an env var; attach recorder if set; otherwise no-op.

    This is the entry point for runners using helio.ibkr_execution.connect()
    so each forge / Greek strategy can pick up recording with a single
    env-var flip and no code change. Argus uses the explicit attach_recorder
    path in runner_unified.

    Returns the EventRecorder (so the caller can call .close() at shutdown)
    OR None if the env var is unset. Errors are logged and swallowed —
    trace recording must NEVER take down a running strategy.
    """
    import os

    path = os.environ.get(env_var, "").strip()
    if not path:
        return None
    try:
        rec = EventRecorder(ib, path)
        log.warning("GOLDEN_TRACE_RECORDER attached -> %s", path)
        return rec
    except Exception as e:
        log.warning("attach_via_env failed: %s", e)
        return None
