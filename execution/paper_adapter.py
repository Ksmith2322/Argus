#!/usr/bin/env python3
# execution/paper_adapter.py

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from execution.adapter import (
    AccountState,
    CancelResult,
    ExecutionAdapter,
    FillState,
    OrderRequest,
    OrderState,
    PositionState,
)


class PaperAdapter(ExecutionAdapter):
    """
    Persistent paper execution adapter for Argus.

    Canonical truth rules:
    - fills.csv is the primary execution truth for position/account reconstruction
    - orders.csv is the primary order history surface
    - positions.csv and account.csv are diagnostic snapshot surfaces
    - snapshot surfaces may be used for comparison, but not to override canonical fill truth

    Goals:
    - stable order/fill/trade IDs
    - persisted execution artifacts for restart reconciliation
    - exactly-once-friendly fill records
    - explicit order status transition history
    - no magical in-memory-only execution truth
    - controllable fill realism for failure-path testing
    - recovered OPEN must behave the same as same-process OPEN

    Artifact files:
    - orders.csv         -> order snapshots at material transitions
    - order_events.csv   -> append-only status/event history
    - fills.csv          -> canonical fill truth
    - positions.csv      -> diagnostic position snapshots
    - account.csv        -> diagnostic account snapshots
    - id_state.json      -> persistent sequence counters
    - fill_plans.json    -> persisted per-order fill behavior
    """

    VALID_FILL_MODES = {"immediate", "delayed", "partial", "manual", "no_fill"}

    TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
    OPEN_ORDER_STATUSES = {"NEW", "ACKED", "PARTIALLY_FILLED"}

    ORDERS_HEADERS = [
        "ts",
        "event_type",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "order_type",
        "status",
        "qty",
        "filled_qty",
        "remaining_qty",
        "avg_fill_px",
        "limit_px",
        "stop_px",
        "adapter",
        "raw_json",
    ]

    ORDER_EVENTS_HEADERS = [
        "ts",
        "event_type",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "status",
        "filled_qty",
        "remaining_qty",
        "adapter",
        "raw_json",
    ]

    FILLS_HEADERS = [
        "ts",
        "fill_id",
        "trade_id",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "price",
        "fee",
        "fee_currency",
        "liquidity",
        "adapter",
        "raw_json",
    ]

    POSITIONS_HEADERS = [
        "ts",
        "reason",
        "symbol",
        "qty",
        "avg_entry_px",
        "mark_px",
        "unrealized_pnl",
        "realized_pnl",
        "side",
        "adapter",
        "raw_json",
    ]

    ACCOUNT_HEADERS = [
        "ts",
        "reason",
        "equity",
        "cash",
        "buying_power",
        "realized_pnl",
        "unrealized_pnl",
        "currency",
        "adapter",
        "raw_json",
    ]

    def __init__(
        self,
        *,
        starting_cash: Decimal | str = Decimal("10000"),
        currency: str = "USD",
        fee_bps: Decimal | str = Decimal("0"),
        slippage_bps: Decimal | str = Decimal("0"),
        stop_loss_slippage_bps: Decimal | str = Decimal("0"),
        qty_precision: str = "0.00000001",
        px_precision: str = "0.01",
        money_precision: str = "0.00000001",
        artifact_dir: str = "ops/logs",
        fill_mode: str = "immediate",
        fill_delay_ms: int = 0,
        partial_fill_ratio: Decimal | str = Decimal("0.50"),
        default_manual_fill_px: Decimal | str | None = None,
        lock_timeout_seconds: float = 10.0,
        debug_instrumentation: bool = True,
    ) -> None:
        self._name = "paper"
        self._currency = currency
        self._starting_cash = self._to_decimal(starting_cash, "starting_cash")
        self._fee_bps = self._to_decimal(fee_bps, "fee_bps")
        self._slippage_bps = self._to_decimal(slippage_bps, "slippage_bps")
        self._stop_loss_slippage_bps = self._to_decimal(stop_loss_slippage_bps, "stop_loss_slippage_bps")
        self._qty_quant = Decimal(str(qty_precision))
        self._px_quant = Decimal(str(px_precision))
        self._money_quant = Decimal(str(money_precision))
        self._cash = self._q_money(self._starting_cash)

        self._fill_mode = str(fill_mode).strip().lower()
        if self._fill_mode not in self.VALID_FILL_MODES:
            raise ValueError(
                f"Unsupported fill_mode={fill_mode!r}; valid={sorted(self.VALID_FILL_MODES)}"
            )

        self._fill_delay_ms = int(fill_delay_ms)
        if self._fill_delay_ms < 0:
            raise ValueError("fill_delay_ms must be >= 0")

        self._partial_fill_ratio = self._to_decimal(partial_fill_ratio, "partial_fill_ratio")
        if self._partial_fill_ratio <= 0 or self._partial_fill_ratio > 1:
            raise ValueError("partial_fill_ratio must be > 0 and <= 1")

        self._default_manual_fill_px = (
            self._to_decimal(default_manual_fill_px, "default_manual_fill_px")
            if default_manual_fill_px is not None
            else None
        )

        self._artifact_dir = artifact_dir
        self._orders_csv = os.path.join(self._artifact_dir, "orders.csv")
        self._order_events_csv = os.path.join(self._artifact_dir, "order_events.csv")
        self._fills_csv = os.path.join(self._artifact_dir, "fills.csv")
        self._positions_csv = os.path.join(self._artifact_dir, "positions.csv")
        self._account_csv = os.path.join(self._artifact_dir, "account.csv")
        self._id_state_json = os.path.join(self._artifact_dir, "id_state.json")
        self._fill_plans_json = os.path.join(self._artifact_dir, "fill_plans.json")

        # line above: self._fill_plans_json = os.path.join(self._artifact_dir, "fill_plans.json")
        self._global_lock_path = os.path.join(self._artifact_dir, ".paper_adapter.lock")

        self._orders_by_id: Dict[str, OrderState] = {}
        self._order_id_by_client_id: Dict[str, str] = {}
        self._fills_by_id: Dict[str, FillState] = {}
        self._fills_by_order_id: Dict[str, List[str]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._last_quote_by_symbol: Dict[str, Dict[str, Decimal | int | None]] = {}
        self._realized_pnl = Decimal("0")

        self._persisted_fill_ids: set[str] = set()
        self._fill_plans: Dict[str, Dict[str, Any]] = {}
        self._replay_fill_ids: List[str] = []

        self._seq_order = 0
        self._seq_fill = 0
        self._seq_trade = 0

        self._lock_timeout_seconds = float(lock_timeout_seconds)
        self._debug_instrumentation = bool(debug_instrumentation)
        self._instance_id = f"paper-{uuid.uuid4().hex[:10]}"

        # preserves lifecycle identity for flat snapshots after a close
        self._last_position_symbol: str = ""

        self._recovery_diagnostics: Dict[str, Any] = {
            "source": "uninitialized",
            "ok": True,
            "warnings": [],
            "contradictions": [],
            "position_snapshot_check": {},
            "account_snapshot_check": {},
        }

        self._ensure_artifacts()
        self._load_id_state()
        self._load_fill_plans()
        self._load_from_artifacts()
        self._bootstrap_snapshots_if_empty()

        self._debug(
            f"[PAPER_ADAPTER_INIT] instance_id={self._instance_id} "
            f"artifact_dir={self._artifact_dir}"
        )

    @property
    def name(self) -> str:
        return self._name

    # -------------------------------------------------------------------------
    # Debug / locking helpers
    # -------------------------------------------------------------------------

    def _debug(self, msg: str) -> None:
        if self._debug_instrumentation:
            print(msg)

    @contextmanager
    def _artifact_lock(self):
        start = time.time()
        fd: Optional[int] = None

        while True:
            try:
                fd = os.open(self._global_lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, f"{os.getpid()}|{self._instance_id}".encode("utf-8"))
                break
            except FileExistsError:
                if (time.time() - start) >= self._lock_timeout_seconds:
                    raise TimeoutError(
                        f"Timed out waiting for artifact lock: {self._global_lock_path}"
                    )
                time.sleep(0.05)

        try:
            yield
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                try:
                    if os.path.exists(self._global_lock_path):
                        os.remove(self._global_lock_path)
                except OSError:
                    pass

    # -------------------------------------------------------------------------
    # Public adapter contract
    # -------------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        unrealized = Decimal("0")
        market_value = Decimal("0")

        for symbol, pos in self._positions.items():
            qty = self._as_decimal(pos["qty"])
            if qty == 0:
                continue

            avg_entry_px = self._as_decimal(pos["avg_entry_px"])
            mark_px = self._get_mark_px(symbol)
            if mark_px is None:
                mark_px = avg_entry_px

            pos_unrealized = (mark_px - avg_entry_px) * qty
            unrealized += pos_unrealized
            market_value += qty * mark_px

        equity = self._cash + market_value

        return AccountState(
            equity=self._q_money(equity),
            cash=self._q_money(self._cash),
            buying_power=self._q_money(self._cash),
            realized_pnl=self._q_money(self._realized_pnl),
            unrealized_pnl=self._q_money(unrealized),
            currency=self._currency,
            raw={
                "adapter": self.name,
                "starting_cash": str(self._starting_cash),
                "fill_mode": self._fill_mode,
                "fill_delay_ms": self._fill_delay_ms,
                "partial_fill_ratio": str(self._partial_fill_ratio),
                "open_symbols": len(
                    [s for s, p in self._positions.items() if self._as_decimal(p["qty"]) != 0]
                ),
                "recovery_diagnostics": self._recovery_diagnostics,
                "instance_id": self._instance_id,
            },
        )

    def get_positions(self) -> List[PositionState]:
        out: List[PositionState] = []

        for symbol, pos in sorted(self._positions.items()):
            qty = self._as_decimal(pos["qty"])
            if qty == 0:
                continue

            avg_entry_px = self._as_decimal(pos["avg_entry_px"])
            mark_px = self._get_mark_px(symbol)
            unrealized = Decimal("0")
            if mark_px is not None:
                unrealized = (mark_px - avg_entry_px) * qty

            out.append(
                PositionState(
                    symbol=symbol,
                    qty=self._q_qty(qty),
                    avg_entry_px=self._q_px(avg_entry_px),
                    mark_px=self._q_px(mark_px) if mark_px is not None else None,
                    unrealized_pnl=self._q_money(unrealized),
                    realized_pnl=self._q_money(self._realized_pnl),
                    side="LONG" if qty > 0 else "FLAT",
                    raw={
                        "adapter": self.name,
                        "trade_id": self._safe_str(pos.get("trade_id")),
                        "last_quote": self._quote_raw(symbol),
                        "instance_id": self._instance_id,
                    },
                )
            )
        return out

    def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderState]:
        rows = [
            o
            for o in self._orders_by_id.values()
            if o.status in self.OPEN_ORDER_STATUSES and (symbol is None or o.symbol == symbol)
        ]
        rows.sort(key=lambda x: ((x.ts or 0), x.order_id))
        return rows

    def get_recovery_diagnostics(self) -> Dict[str, Any]:
        return dict(self._recovery_diagnostics)

    def place_order(self, req: OrderRequest) -> OrderState:
        self.validate_order_request(req)

        existing_order_id = self._order_id_by_client_id.get(req.client_order_id)
        if existing_order_id:
            return self._orders_by_id[existing_order_id]

        self._validate_order_against_canonical_state(req)

        ts = self._now_ms()
        order_id = self._next_order_id()
        qty = self._q_qty(req.qty)

        order = OrderState(
            order_id=order_id,
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=qty,
            order_type=req.order_type,
            status="ACKED",
            limit_px=self._q_px(req.limit_px) if req.limit_px is not None else None,
            stop_px=self._q_px(req.stop_px) if req.stop_px is not None else None,
            filled_qty=Decimal("0"),
            remaining_qty=qty,
            avg_fill_px=None,
            ts=ts,
            raw={
                "adapter": self.name,
                "time_in_force": req.time_in_force,
                "reduce_only": req.reduce_only,
                "post_only": req.post_only,
                "tags": dict(req.tags),
                "fill_mode": self._resolve_fill_mode_from_tags(req.tags),
                "recovery_source": self._recovery_diagnostics.get("source"),
                "instance_id": self._instance_id,
            },
        )

        self._orders_by_id[order_id] = order
        self._order_id_by_client_id[req.client_order_id] = order_id
        self._fills_by_order_id.setdefault(order_id, [])

        self._persist_order_snapshot(order, event_type="ORDER_ACCEPTED")
        self._persist_order_event(order, event_type="ORDER_ACCEPTED")
        self._persist_positions_snapshot(reason="ORDER_ACCEPTED", force_symbol=req.symbol)
        self._persist_account_snapshot(reason="ORDER_ACCEPTED")

        quote = self._last_quote_by_symbol.get(req.symbol)
        plan = self._build_fill_plan(order=order, req=req, quote=quote)
        self._fill_plans[order_id] = plan
        self._save_fill_plans()

        self._try_fill_or_rest(order_id=order_id, quote=quote)

        return self._orders_by_id[order_id]

    def cancel(
        self,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> CancelResult:
        resolved_order_id = self._resolve_order_id(order_id=order_id, client_order_id=client_order_id)
        if not resolved_order_id:
            return CancelResult(
                ok=False,
                order_id=order_id or "",
                client_order_id=client_order_id,
                status="NOT_FOUND",
                message="Order not found",
                raw={"adapter": self.name},
            )

        order = self._orders_by_id[resolved_order_id]
        if order.status in self.TERMINAL_ORDER_STATUSES:
            return CancelResult(
                ok=False,
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                status=order.status,
                message=f"Order already terminal: {order.status}",
                raw={"adapter": self.name},
            )

        updated = replace(order, status="CANCELED", remaining_qty=order.qty - order.filled_qty)
        self._orders_by_id[resolved_order_id] = updated

        plan = self._fill_plans.get(resolved_order_id)
        if plan:
            plan["mode"] = "canceled"
            plan["terminal"] = True
            plan["updated_ts"] = self._now_ms()
            self._save_fill_plans()

        self._persist_order_snapshot(updated, event_type="ORDER_CANCELED")
        self._persist_order_event(updated, event_type="ORDER_CANCELED")
        self._persist_positions_snapshot(reason="ORDER_CANCELED")
        self._persist_account_snapshot(reason="ORDER_CANCELED")

        return CancelResult(
            ok=True,
            order_id=updated.order_id,
            client_order_id=updated.client_order_id,
            status=updated.status,
            message="Canceled",
            raw={"adapter": self.name},
        )

    def fetch_fills(self, since_ts: Optional[int] = None) -> List[FillState]:
        self._process_due_orders()

        fills = list(self._fills_by_id.values())
        if since_ts is not None:
            fills = [f for f in fills if (f.ts or 0) >= since_ts]

        replay_rows: List[FillState] = []
        if self._replay_fill_ids:
            for fill_id in self._replay_fill_ids:
                fill = self._fills_by_id.get(fill_id)
                if fill is not None:
                    replay_rows.append(fill)
            self._replay_fill_ids = []

        out = fills + replay_rows
        out.sort(key=lambda x: ((x.ts or 0), x.fill_id))
        return out

    def heartbeat(self) -> bool:
        return True

    # -------------------------------------------------------------------------
    # Optional paper-specific helpers
    # -------------------------------------------------------------------------

    def update_market(
        self,
        symbol: str,
        *,
        bid: Decimal | str | None = None,
        ask: Decimal | str | None = None,
        last: Decimal | str | None = None,
        ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        q_bid = self._q_px(self._to_decimal(bid, "bid")) if bid is not None else None
        q_ask = self._q_px(self._to_decimal(ask, "ask")) if ask is not None else None
        q_last = self._q_px(self._to_decimal(last, "last")) if last is not None else None

        if q_bid is None and q_ask is not None:
            q_bid = q_ask
        if q_ask is None and q_bid is not None:
            q_ask = q_bid
        if q_last is None:
            if q_bid is not None and q_ask is not None:
                q_last = self._q_px((q_bid + q_ask) / Decimal("2"))
            else:
                q_last = q_bid or q_ask

        if q_bid is None and q_ask is None and q_last is None:
            raise ValueError("update_market requires at least one of bid/ask/last")

        self._last_quote_by_symbol[symbol] = {
            "bid": q_bid,
            "ask": q_ask,
            "last": q_last,
            "ts": ts if ts is not None else self._now_ms(),
        }

        for order in self.get_open_orders(symbol=symbol):
            self._try_fill_or_rest(order_id=order.order_id, quote=self._last_quote_by_symbol[symbol])

        return self._quote_raw(symbol)

    def get_order(
        self,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[OrderState]:
        resolved_order_id = self._resolve_order_id(order_id=order_id, client_order_id=client_order_id)
        if not resolved_order_id:
            return None
        return self._orders_by_id[resolved_order_id]

    def get_order_fills(
        self,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> List[FillState]:
        resolved_order_id = self._resolve_order_id(order_id=order_id, client_order_id=client_order_id)
        if not resolved_order_id:
            return []
        fill_ids = self._fills_by_order_id.get(resolved_order_id, [])
        fills = [self._fills_by_id[fid] for fid in fill_ids if fid in self._fills_by_id]
        fills.sort(key=lambda x: ((x.ts or 0), x.fill_id))
        return fills

    def trigger_manual_fill(
        self,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        fill_qty: Decimal | str | None = None,
        fill_px: Decimal | str | None = None,
    ) -> Optional[OrderState]:
        resolved_order_id = self._resolve_order_id(order_id=order_id, client_order_id=client_order_id)
        if not resolved_order_id:
            return None

        order = self._orders_by_id[resolved_order_id]
        if order.status in self.TERMINAL_ORDER_STATUSES:
            return order

        qty = order.remaining_qty if fill_qty is None else self._q_qty(self._to_decimal(fill_qty, "fill_qty"))
        px = (
            self._resolve_fill_px_for_order(order)
            if fill_px is None
            else self._q_px(self._to_decimal(fill_px, "fill_px"))
        )

        self._apply_fill(order_id=order.order_id, fill_qty=qty, fill_px=px)

        plan = self._fill_plans.get(order.order_id)
        if plan:
            plan["updated_ts"] = self._now_ms()
            if self._orders_by_id[order.order_id].remaining_qty == 0:
                plan["terminal"] = True
            self._save_fill_plans()

        return self._orders_by_id[resolved_order_id]

    def replay_fill(
        self,
        *,
        fill_id: str,
    ) -> bool:
        if fill_id not in self._fills_by_id:
            return False
        self._replay_fill_ids.append(fill_id)
        return True

    def set_order_fill_mode(
        self,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        mode: str,
        fill_delay_ms: Optional[int] = None,
        partial_fill_ratio: Optional[Decimal | str] = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_order_id = self._resolve_order_id(order_id=order_id, client_order_id=client_order_id)
        if not resolved_order_id:
            return None

        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in self.VALID_FILL_MODES:
            raise ValueError(f"Unsupported fill mode: {mode}")

        plan = self._fill_plans.get(resolved_order_id)
        if plan is None:
            order = self._orders_by_id[resolved_order_id]
            dummy_req = OrderRequest(
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                order_type=order.order_type,
                limit_px=order.limit_px,
                stop_px=order.stop_px,
                client_order_id=order.client_order_id,
                tags={},
            )
            plan = self._build_fill_plan(
                order=order,
                req=dummy_req,
                quote=self._last_quote_by_symbol.get(order.symbol),
            )
            self._fill_plans[resolved_order_id] = plan

        plan["mode"] = normalized_mode
        if fill_delay_ms is not None:
            plan["fill_delay_ms"] = int(fill_delay_ms)
        if partial_fill_ratio is not None:
            ratio = self._to_decimal(partial_fill_ratio, "partial_fill_ratio")
            if ratio <= 0 or ratio > 1:
                raise ValueError("partial_fill_ratio must be > 0 and <= 1")
            plan["partial_fill_ratio"] = str(ratio)

        now_ms = self._now_ms()
        plan["next_fill_ts"] = now_ms + int(plan.get("fill_delay_ms", self._fill_delay_ms))
        plan["updated_ts"] = now_ms
        self._save_fill_plans()
        return plan

    # -------------------------------------------------------------------------
    # Core execution logic
    # -------------------------------------------------------------------------

    def _validate_order_against_canonical_state(self, req: OrderRequest) -> None:
        side = self._safe_str(req.side).upper()
        symbol = self._safe_str(req.symbol)
        req_qty = self._q_qty(req.qty)

        if side == "BUY":
            return

        if side != "SELL":
            raise ValueError(f"Unsupported side: {req.side}")

        pos = self._positions.get(symbol, {})
        current_qty = self._as_decimal(pos.get("qty", Decimal("0")))

        if current_qty <= 0:
            raise ValueError(
                f"PaperAdapter long-only violation: attempted SELL qty={req_qty} "
                f"against position qty=0 for {symbol}"
            )

        if req_qty > current_qty:
            raise ValueError(
                f"PaperAdapter long-only violation: attempted SELL qty={req_qty} "
                f"against position qty={current_qty} for {symbol}"
            )

    def _try_fill_or_rest(
        self,
        *,
        order_id: str,
        quote: Optional[Dict[str, Decimal | int | None]],
    ) -> None:
        order = self._orders_by_id[order_id]

        if order.status in self.TERMINAL_ORDER_STATUSES:
            return

        plan = self._fill_plans.get(order_id)
        if plan is None:
            dummy_req = OrderRequest(
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                order_type=order.order_type,
                limit_px=order.limit_px,
                stop_px=order.stop_px,
                client_order_id=order.client_order_id,
                tags={},
            )
            plan = self._build_fill_plan(order=order, req=dummy_req, quote=quote)
            self._fill_plans[order_id] = plan
            self._save_fill_plans()

        mode = str(plan.get("mode", self._fill_mode)).strip().lower()
        if mode == "canceled":
            return

        if quote is None:
            if mode in {"manual", "no_fill"}:
                self._set_order_status_if_needed(order_id, "NEW", event_type="ORDER_WAITING_MANUAL_FILL")
                return

            if order.order_type == "MARKET" and mode == "immediate":
                px = self._resolve_fill_px_for_order(order)
                self._apply_fill(order_id=order.order_id, fill_qty=order.remaining_qty, fill_px=px)
                self._mark_plan_post_fill(order_id)
                return

            self._set_order_status_if_needed(order_id, "NEW", event_type="ORDER_RESTING")
            return

        if not self._order_can_fill_now(order=order, quote=quote):
            waiting_event = self._waiting_event_type(order=order)
            self._set_order_status_if_needed(order_id, "NEW", event_type=waiting_event)
            return

        now_ms = self._now_ms()
        next_fill_ts = int(plan.get("next_fill_ts", 0) or 0)

        if mode == "immediate":
            px = self._resolve_fill_px_for_order(order)
            self._apply_fill(order_id=order.order_id, fill_qty=order.remaining_qty, fill_px=px)
            self._mark_plan_post_fill(order_id)
            return

        if mode == "delayed":
            if next_fill_ts and now_ms < next_fill_ts:
                self._set_order_status_if_needed(order_id, "NEW", event_type="ORDER_DELAYED_WAIT")
                return
            px = self._resolve_fill_px_for_order(order)
            self._apply_fill(order_id=order.order_id, fill_qty=order.remaining_qty, fill_px=px)
            self._mark_plan_post_fill(order_id)
            return

        if mode == "partial":
            if next_fill_ts and now_ms < next_fill_ts:
                self._set_order_status_if_needed(
                    order_id,
                    "PARTIALLY_FILLED" if order.filled_qty > 0 else "NEW",
                    event_type="ORDER_PARTIAL_WAIT",
                )
                return

            remaining = self._orders_by_id[order_id].remaining_qty
            ratio = self._to_decimal(
                plan.get("partial_fill_ratio", str(self._partial_fill_ratio)),
                "partial_fill_ratio",
            )
            if ratio <= 0 or ratio > 1:
                ratio = self._partial_fill_ratio

            chunk_qty = self._q_qty(remaining * ratio)
            if chunk_qty <= 0:
                chunk_qty = remaining
            if chunk_qty > remaining:
                chunk_qty = remaining

            px = self._resolve_fill_px_for_order(self._orders_by_id[order_id])
            self._apply_fill(order_id=order_id, fill_qty=chunk_qty, fill_px=px)

            updated = self._orders_by_id[order_id]
            plan["updated_ts"] = now_ms
            if updated.remaining_qty == 0:
                plan["terminal"] = True
            else:
                plan["next_fill_ts"] = now_ms + int(plan.get("fill_delay_ms", self._fill_delay_ms))
            self._save_fill_plans()
            return

        if mode in {"manual", "no_fill"}:
            self._set_order_status_if_needed(order_id, "NEW", event_type="ORDER_WAITING_MANUAL_FILL")
            return

        updated = replace(order, status="REJECTED")
        self._orders_by_id[order_id] = updated
        self._persist_order_snapshot(updated, event_type="ORDER_REJECTED")
        self._persist_order_event(updated, event_type="ORDER_REJECTED")
        self._persist_positions_snapshot(reason="ORDER_REJECTED")
        self._persist_account_snapshot(reason="ORDER_REJECTED")

    def _apply_fill(
        self,
        *,
        order_id: str,
        fill_qty: Decimal,
        fill_px: Decimal,
    ) -> None:
        order = self._orders_by_id[order_id]
        fill_qty = self._q_qty(fill_qty)
        fill_px = self._q_px(fill_px)

        self._debug(
            f"[APPLY_FILL_ENTER] instance_id={self._instance_id} "
            f"order_id={order_id} seq_fill_before={self._seq_fill}"
        )

        if fill_qty <= 0:
            return
        if fill_qty > order.remaining_qty:
            raise ValueError(
                f"fill_qty {fill_qty} exceeds remaining_qty {order.remaining_qty} for {order.order_id}"
            )

        if order.side == "SELL":
            pos = self._positions.get(order.symbol, {})
            current_qty = self._as_decimal(pos.get("qty", Decimal("0")))
            if fill_qty > current_qty:
                raise ValueError(
                    f"PaperAdapter long-only violation: attempted SELL qty={fill_qty} "
                    f"against position qty={current_qty} for {order.symbol}"
                )

        fee = self._calc_fee(notional=fill_qty * fill_px)
        ts = self._now_ms()
        fill_id = self._next_fill_id()
        trade_id = self._resolve_trade_id_for_fill(order=order, fill_qty=fill_qty)

        if fill_id in self._persisted_fill_ids or fill_id in self._fills_by_id:
            self._debug(
                f"[APPLY_FILL_SKIP_DUP_ID] instance_id={self._instance_id} "
                f"fill_id={fill_id} order_id={order_id}"
            )
            return

        fill = FillState(
            fill_id=fill_id,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=fill_qty,
            price=fill_px,
            fee=fee,
            fee_currency=self._currency,
            liquidity="TAKER" if order.order_type == "MARKET" else "UNKNOWN",
            ts=ts,
            trade_id=trade_id,
            raw={
                "adapter": self.name,
                "order_type": order.order_type,
                "fill_mode": self._fill_plans.get(order_id, {}).get("mode", self._fill_mode),
                "instance_id": self._instance_id,
            },
        )

        self._persist_fill(fill)

        self._debug(
            f"[APPLY_FILL_AFTER_PERSIST] instance_id={self._instance_id} "
            f"fill_id={fill_id} order_id={order_id}"
        )

        self._fills_by_id[fill_id] = fill
        self._fills_by_order_id.setdefault(order.order_id, [])
        if fill_id not in self._fills_by_order_id[order.order_id]:
            self._fills_by_order_id[order.order_id].append(fill_id)

        new_filled_qty = self._q_qty(order.filled_qty + fill_qty)
        remaining_qty = self._q_qty(order.qty - new_filled_qty)
        avg_fill_px = self._weighted_avg_fill_px(order.order_id)

        new_status = "FILLED" if remaining_qty == 0 else "PARTIALLY_FILLED"

        updated_order = replace(
            order,
            status=new_status,
            filled_qty=new_filled_qty,
            remaining_qty=remaining_qty,
            avg_fill_px=avg_fill_px,
            ts=ts,
        )
        self._orders_by_id[order_id] = updated_order

        self._apply_position_fill(
            symbol=order.symbol,
            side=order.side,
            qty=fill_qty,
            px=fill_px,
            fee=fee,
            trade_id=trade_id,
        )

        self._persist_order_snapshot(
            updated_order,
            event_type="ORDER_FILLED" if new_status == "FILLED" else "ORDER_PARTIAL_FILL",
        )
        self._persist_order_event(
            updated_order,
            event_type="ORDER_FILLED" if new_status == "FILLED" else "ORDER_PARTIAL_FILL",
        )
        self._persist_positions_snapshot(reason="FILL_APPLIED", force_symbol=order.symbol)
        self._persist_account_snapshot(reason="FILL_APPLIED")

    def _apply_position_fill(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        px: Decimal,
        fee: Decimal,
        trade_id: Optional[str],
    ) -> None:
        if symbol:
            self._last_position_symbol = symbol

        pos = self._positions.setdefault(
            symbol,
            {
                "qty": Decimal("0"),
                "avg_entry_px": Decimal("0"),
                "trade_id": None,
            },
        )

        old_qty = self._as_decimal(pos["qty"])
        old_avg = self._as_decimal(pos["avg_entry_px"])
        old_trade_id = self._safe_str(pos.get("trade_id")) or None

        if side == "BUY":
            new_qty = old_qty + qty
            if new_qty == 0:
                new_avg = Decimal("0")
            elif old_qty <= 0:
                new_avg = px
            else:
                new_avg = ((old_qty * old_avg) + (qty * px)) / new_qty

            pos["qty"] = self._q_qty(new_qty)
            pos["avg_entry_px"] = self._q_px(new_avg)
            pos["trade_id"] = trade_id or old_trade_id
            self._cash = self._q_money(self._cash - (qty * px) - fee)
            return

        if side == "SELL":
            if qty > old_qty:
                raise ValueError(
                    f"PaperAdapter long-only violation: attempted SELL qty={qty} "
                    f"against position qty={old_qty} for {symbol}"
                )

            realized = (px - old_avg) * qty
            self._realized_pnl = self._q_money(self._realized_pnl + realized - fee)
            self._cash = self._q_money(self._cash + (qty * px) - fee)

            new_qty = old_qty - qty
            if new_qty == 0:
                self._last_position_symbol = symbol
                pos["qty"] = Decimal("0")
                pos["avg_entry_px"] = Decimal("0")
                pos["trade_id"] = None
            else:
                pos["qty"] = self._q_qty(new_qty)
                pos["avg_entry_px"] = old_avg
                pos["trade_id"] = trade_id or old_trade_id
            return

        raise ValueError(f"Unsupported side: {side}")

    # -------------------------------------------------------------------------
    # Fill-plan / realism helpers
    # -------------------------------------------------------------------------

    def _resolve_fill_mode_from_tags(self, tags: Dict[str, Any]) -> str:
        if not tags:
            return self._fill_mode
        raw = str(tags.get("paper_fill_mode", self._fill_mode)).strip().lower()
        return raw if raw in self.VALID_FILL_MODES else self._fill_mode

    def _build_fill_plan(
        self,
        *,
        order: OrderState,
        req: OrderRequest,
        quote: Optional[Dict[str, Decimal | int | None]],
    ) -> Dict[str, Any]:
        mode = self._resolve_fill_mode_from_tags(req.tags)
        plan_fill_delay_ms = int(req.tags.get("paper_fill_delay_ms", self._fill_delay_ms) or 0)

        raw_ratio = req.tags.get("paper_partial_fill_ratio", str(self._partial_fill_ratio))
        ratio = self._to_decimal(raw_ratio, "paper_partial_fill_ratio")
        if ratio <= 0 or ratio > 1:
            ratio = self._partial_fill_ratio

        now_ms = self._now_ms()
        plan = {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "mode": mode,
            "fill_delay_ms": plan_fill_delay_ms,
            "partial_fill_ratio": str(ratio),
            "next_fill_ts": now_ms + plan_fill_delay_ms,
            "created_ts": now_ms,
            "updated_ts": now_ms,
            "terminal": False,
        }

        if quote is not None and mode == "immediate":
            plan["next_fill_ts"] = now_ms

        return plan

    def _mark_plan_post_fill(self, order_id: str) -> None:
        plan = self._fill_plans.get(order_id)
        if plan is None:
            return
        updated = self._orders_by_id.get(order_id)
        if updated is None:
            return
        plan["updated_ts"] = self._now_ms()
        if updated.remaining_qty == 0:
            plan["terminal"] = True
        self._save_fill_plans()

    def _process_due_orders(self) -> None:
        if not self._fill_plans:
            return

        open_order_ids = [o.order_id for o in self.get_open_orders()]
        for order_id in open_order_ids:
            order = self._orders_by_id.get(order_id)
            if order is None:
                continue
            quote = self._last_quote_by_symbol.get(order.symbol)
            self._try_fill_or_rest(order_id=order_id, quote=quote)

    def _order_can_fill_now(
        self,
        *,
        order: OrderState,
        quote: Dict[str, Decimal | int | None],
    ) -> bool:
        bid = self._as_decimal(quote["bid"]) if quote.get("bid") is not None else None
        ask = self._as_decimal(quote["ask"]) if quote.get("ask") is not None else None
        last = self._as_decimal(quote["last"]) if quote.get("last") is not None else None

        if order.order_type == "MARKET":
            return True

        if order.order_type == "LIMIT":
            return self._is_limit_marketable(order=order, bid=bid, ask=ask)

        if order.order_type == "STOP":
            return self._stop_triggered(order=order, bid=bid, ask=ask, last=last)

        if order.order_type == "STOP_LIMIT":
            if not self._stop_triggered(order=order, bid=bid, ask=ask, last=last):
                return False
            return self._is_limit_marketable(order=order, bid=bid, ask=ask)

        return False

    def _waiting_event_type(self, *, order: OrderState) -> str:
        if order.order_type in {"STOP", "STOP_LIMIT"}:
            return "ORDER_WAITING_STOP"
        return "ORDER_RESTING"

    def _resolve_fill_px_for_order(self, order: OrderState) -> Decimal:
        quote = self._last_quote_by_symbol.get(order.symbol)
        bid = self._as_decimal(quote["bid"]) if quote and quote.get("bid") is not None else None
        ask = self._as_decimal(quote["ask"]) if quote and quote.get("ask") is not None else None
        last = self._as_decimal(quote["last"]) if quote and quote.get("last") is not None else None

        if order.order_type == "MARKET":
            return self._market_fill_px(order=order, bid=bid, ask=ask, last=last)

        if order.order_type in {"LIMIT", "STOP_LIMIT"}:
            if self._default_manual_fill_px is not None and not self._order_can_fill_now(order=order, quote=quote or {}):
                return self._q_px(self._default_manual_fill_px)
            return self._limit_fill_px(order=order, bid=bid, ask=ask)

        if order.order_type == "STOP":
            return self._market_fill_px(order=order, bid=bid, ask=ask, last=last)

        return self._synthetic_fill_px(order)

    # -------------------------------------------------------------------------
    # Fill/price helpers
    # -------------------------------------------------------------------------

    def _market_fill_px(
        self,
        *,
        order: OrderState,
        bid: Optional[Decimal],
        ask: Optional[Decimal],
        last: Optional[Decimal],
    ) -> Decimal:
        if order.side == "BUY":
            base_px = ask or last or bid
        else:
            base_px = bid or last or ask

        if base_px is None:
            base_px = self._synthetic_fill_px(order)

        slip_mult = self._fee_multiplier(self._slippage_bps, positive=(order.side == "BUY"))
        fill_px = base_px * slip_mult

        # Extra stop-loss slippage: SELL orders tagged as stop-loss exits fill at worse price
        if (
            self._stop_loss_slippage_bps > 0
            and order.side == "SELL"
            and "STOP_LOSS" in str((order.raw or {}).get("tags", {}).get("action_reason", "")).upper()
        ):
            fill_px = fill_px * self._fee_multiplier(self._stop_loss_slippage_bps, positive=False)

        return self._q_px(fill_px)

    def _limit_fill_px(
        self,
        *,
        order: OrderState,
        bid: Optional[Decimal],
        ask: Optional[Decimal],
    ) -> Decimal:
        if order.limit_px is None:
            raise ValueError("LIMIT order missing limit_px")

        if order.side == "BUY":
            px = min(order.limit_px, ask or order.limit_px)
        else:
            px = max(order.limit_px, bid or order.limit_px)

        return self._q_px(px)

    def _synthetic_fill_px(self, order: OrderState) -> Decimal:
        quote = self._last_quote_by_symbol.get(order.symbol)
        if quote:
            last = quote.get("last")
            if last is not None:
                last_px = self._as_decimal(last)
                slip_mult = self._fee_multiplier(self._slippage_bps, positive=(order.side == "BUY"))
                return self._q_px(last_px * slip_mult)

        if self._default_manual_fill_px is not None:
            return self._q_px(self._default_manual_fill_px)

        return self._q_px(Decimal("100.00"))

    def _is_limit_marketable(
        self,
        *,
        order: OrderState,
        bid: Optional[Decimal],
        ask: Optional[Decimal],
    ) -> bool:
        if order.limit_px is None:
            return False

        if order.side == "BUY":
            if ask is None:
                return False
            return ask <= order.limit_px

        if bid is None:
            return False
        return bid >= order.limit_px

    def _stop_triggered(
        self,
        *,
        order: OrderState,
        bid: Optional[Decimal],
        ask: Optional[Decimal],
        last: Optional[Decimal],
    ) -> bool:
        if order.stop_px is None:
            return False

        trigger_px = last
        if trigger_px is None:
            trigger_px = ask if order.side == "BUY" else bid
        if trigger_px is None:
            return False

        if order.side == "BUY":
            return trigger_px >= order.stop_px
        return trigger_px <= order.stop_px

    def _weighted_avg_fill_px(self, order_id: str) -> Optional[Decimal]:
        numer = Decimal("0")
        denom = Decimal("0")

        for fill_id in self._fills_by_order_id.get(order_id, []):
            fill = self._fills_by_id[fill_id]
            numer += fill.qty * fill.price
            denom += fill.qty

        if denom == 0:
            return None
        return self._q_px(numer / denom)

    def _calc_fee(self, *, notional: Decimal) -> Decimal:
        if self._fee_bps <= 0:
            return Decimal("0")
        fee = notional * (self._fee_bps / Decimal("10000"))
        return self._q_money(fee)

    # -------------------------------------------------------------------------
    # Persistence / artifacts
    # -------------------------------------------------------------------------

    def _ensure_artifacts(self) -> None:
        os.makedirs(self._artifact_dir, exist_ok=True)

        self._ensure_csv(self._orders_csv, self.ORDERS_HEADERS)
        self._ensure_csv(self._order_events_csv, self.ORDER_EVENTS_HEADERS)
        self._ensure_csv(self._fills_csv, self.FILLS_HEADERS)
        self._ensure_csv(self._positions_csv, self.POSITIONS_HEADERS)
        self._ensure_csv(self._account_csv, self.ACCOUNT_HEADERS)

        if not os.path.exists(self._id_state_json):
            self._atomic_write_json(
                self._id_state_json,
                {
                    "seq_order": 0,
                    "seq_fill": 0,
                    "seq_trade": 0,
                },
            )

        if not os.path.exists(self._fill_plans_json):
            self._atomic_write_json(self._fill_plans_json, {})

    def _ensure_csv(self, path: str, fieldnames: List[str]) -> None:
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

    def _bootstrap_snapshots_if_empty(self) -> None:
        if self._csv_has_data(self._account_csv):
            return
        self._persist_positions_snapshot(reason="BOOTSTRAP")
        self._persist_account_snapshot(reason="BOOTSTRAP")

    def _persist_order_snapshot(self, order: OrderState, *, event_type: str) -> None:
        row = {
            "ts": order.ts or self._now_ms(),
            "event_type": event_type,
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "order_type": order.order_type,
            "status": order.status,
            "qty": self._fmt_dec(order.qty),
            "filled_qty": self._fmt_dec(order.filled_qty),
            "remaining_qty": self._fmt_dec(order.remaining_qty),
            "avg_fill_px": self._fmt_dec(order.avg_fill_px),
            "limit_px": self._fmt_dec(order.limit_px),
            "stop_px": self._fmt_dec(order.stop_px),
            "adapter": self.name,
            "raw_json": self._json_dumps(order.raw),
        }
        self._append_csv_row(self._orders_csv, self.ORDERS_HEADERS, row)

    def _persist_order_event(self, order: OrderState, *, event_type: str) -> None:
        row = {
            "ts": self._now_ms(),
            "event_type": event_type,
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "status": order.status,
            "filled_qty": self._fmt_dec(order.filled_qty),
            "remaining_qty": self._fmt_dec(order.remaining_qty),
            "adapter": self.name,
            "raw_json": self._json_dumps(order.raw),
        }
        self._append_csv_row(self._order_events_csv, self.ORDER_EVENTS_HEADERS, row)

    def _fill_id_exists_on_disk(self, fill_id: str) -> bool:
        if not os.path.exists(self._fills_csv):
            return False

        with open(self._fills_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if self._safe_str(row.get("fill_id")) == fill_id:
                    return True
        return False

    def _persist_fill(self, fill: FillState) -> None:
        fill_id = self._safe_str(fill.fill_id)
        if not fill_id:
            raise ValueError("fill.fill_id is required")

        self._debug(
            f"[PERSIST_FILL_ENTER] instance_id={self._instance_id} "
            f"fill_id={fill_id} order_id={fill.order_id} "
            f"persisted_known={fill_id in self._persisted_fill_ids}"
        )

        row = {
            "ts": fill.ts or self._now_ms(),
            "fill_id": fill.fill_id,
            "trade_id": fill.trade_id,
            "order_id": fill.order_id,
            "client_order_id": fill.client_order_id,
            "symbol": fill.symbol,
            "side": fill.side,
            "qty": self._fmt_dec(fill.qty),
            "price": self._fmt_dec(fill.price),
            "fee": self._fmt_dec(fill.fee),
            "fee_currency": fill.fee_currency,
            "liquidity": fill.liquidity,
            "adapter": self.name,
            "raw_json": self._json_dumps(fill.raw),
        }

        # line above: row = {
        with self._artifact_lock():
            if fill_id in self._persisted_fill_ids or self._fill_id_exists_on_disk(fill_id):
                self._persisted_fill_ids.add(fill_id)
                self._debug(
                    f"[PERSIST_FILL_SKIP_DUP] instance_id={self._instance_id} "
                    f"fill_id={fill_id}"
                )
                return

            self._persisted_fill_ids.add(fill_id)
            try:
                self._append_csv_row(self._fills_csv, self.FILLS_HEADERS, row)
            except Exception:
                self._persisted_fill_ids.discard(fill_id)
                raise

    def _persist_positions_snapshot(self, *, reason: str, force_symbol: Optional[str] = None) -> None:
        ts = self._now_ms()
        positions = self.get_positions()

        if not positions:
            symbol_out = self._safe_str(force_symbol) or self._safe_str(self._last_position_symbol)
            mark_px = self._get_mark_px(symbol_out) if symbol_out else None
            row = {
                "ts": ts,
                "reason": reason,
                "symbol": symbol_out,
                "qty": "0",
                "avg_entry_px": self._fmt_dec(self._q_px(Decimal("0"))),
                "mark_px": self._fmt_dec(mark_px),
                "unrealized_pnl": self._fmt_dec(self._q_money(Decimal("0"))),
                "realized_pnl": self._fmt_dec(self._q_money(self._realized_pnl)),
                "side": "FLAT",
                "adapter": self.name,
                "raw_json": self._json_dumps(
                    {
                        "last_position_symbol": symbol_out or None,
                        "instance_id": self._instance_id,
                    }
                ),
            }
            self._append_csv_row(self._positions_csv, self.POSITIONS_HEADERS, row)
            return

        for pos in positions:
            row = {
                "ts": ts,
                "reason": reason,
                "symbol": pos.symbol,
                "qty": self._fmt_dec(pos.qty),
                "avg_entry_px": self._fmt_dec(pos.avg_entry_px),
                "mark_px": self._fmt_dec(pos.mark_px),
                "unrealized_pnl": self._fmt_dec(pos.unrealized_pnl),
                "realized_pnl": self._fmt_dec(pos.realized_pnl),
                "side": pos.side,
                "adapter": self.name,
                "raw_json": self._json_dumps(pos.raw),
            }
            self._append_csv_row(self._positions_csv, self.POSITIONS_HEADERS, row)

    def _persist_account_snapshot(self, *, reason: str) -> None:
        acct = self.get_account_state()
        row = {
            "ts": self._now_ms(),
            "reason": reason,
            "equity": self._fmt_dec(acct.equity),
            "cash": self._fmt_dec(acct.cash),
            "buying_power": self._fmt_dec(acct.buying_power),
            "realized_pnl": self._fmt_dec(acct.realized_pnl),
            "unrealized_pnl": self._fmt_dec(acct.unrealized_pnl),
            "currency": acct.currency,
            "adapter": self.name,
            "raw_json": self._json_dumps(acct.raw),
        }
        self._append_csv_row(self._account_csv, self.ACCOUNT_HEADERS, row)

    def _append_csv_row(self, path: str, fieldnames: List[str], row: Dict[str, Any]) -> None:
        self._debug(
            f"[APPEND_CSV_ROW] instance_id={self._instance_id} "
            f"path={os.path.basename(path)} fill_id={row.get('fill_id', '')}"
        )
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writerow({k: row.get(k, "") for k in fieldnames})
            f.flush()
            os.fsync(f.fileno())

    def _load_id_state(self) -> None:
        try:
            with open(self._id_state_json, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            obj = {}

        self._seq_order = int(obj.get("seq_order", 0) or 0)
        self._seq_fill = int(obj.get("seq_fill", 0) or 0)
        self._seq_trade = int(obj.get("seq_trade", 0) or 0)

    def _save_id_state(self) -> None:
        self._atomic_write_json(
            self._id_state_json,
            {
                "seq_order": self._seq_order,
                "seq_fill": self._seq_fill,
                "seq_trade": self._seq_trade,
            },
        )

    def _allocate_next_id(self, *, key: str, prefix: str) -> str:
        # line above: def _allocate_next_id(self, *, key: str, prefix: str) -> str:
        with self._artifact_lock():
            try:
                with open(self._id_state_json, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                obj = {}

            current = int(obj.get(key, 0) or 0) + 1
            obj[key] = current

            self._atomic_write_json(self._id_state_json, obj)

            self._seq_order = int(obj.get("seq_order", 0) or 0)
            self._seq_fill = int(obj.get("seq_fill", 0) or 0)
            self._seq_trade = int(obj.get("seq_trade", 0) or 0)

            return f"{prefix}{current:08d}"

    def _load_fill_plans(self) -> None:
        try:
            with open(self._fill_plans_json, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                self._fill_plans = obj
            else:
                self._fill_plans = {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._fill_plans = {}

    def _save_fill_plans(self) -> None:
        self._atomic_write_json(self._fill_plans_json, self._fill_plans)

    def _load_from_artifacts(self) -> None:
        self._orders_by_id = {}
        self._order_id_by_client_id = {}
        self._fills_by_id = {}
        self._fills_by_order_id = {}
        self._positions = {}
        self._realized_pnl = Decimal("0")
        self._cash = self._q_money(self._starting_cash)
        self._persisted_fill_ids = set()
        self._last_position_symbol = ""
        self._recovery_diagnostics = {
            "source": "canonical_fills",
            "ok": True,
            "warnings": [],
            "contradictions": [],
            "position_snapshot_check": {},
            "account_snapshot_check": {},
        }

        self._load_orders_from_csv()
        self._load_fills_from_csv()
        self._rebuild_state_from_fills()
        self._normalize_orders_after_fill_rebuild()
        self._prune_or_finalize_fill_plans()
        self._reconcile_sequence_counters_from_loaded_artifacts()
        self._compare_canonical_state_to_snapshots()

    def _load_orders_from_csv(self) -> None:
        if not os.path.exists(self._orders_csv):
            return

        with open(self._orders_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                order_id = self._safe_str(row.get("order_id"))
                if not order_id:
                    continue

                client_order_id = self._safe_str(row.get("client_order_id"))
                raw = self._json_loads(row.get("raw_json"))

                limit_px_raw = self._parse_optional_decimal(row.get("limit_px"))
                stop_px_raw = self._parse_optional_decimal(row.get("stop_px"))
                avg_fill_px_raw = self._parse_optional_decimal(row.get("avg_fill_px"))

                order = OrderState(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    symbol=self._safe_str(row.get("symbol")),
                    side=self._safe_str(row.get("side")),
                    qty=self._q_qty(self._parse_decimal_or_zero(row.get("qty"))),
                    order_type=self._safe_str(row.get("order_type")),
                    status=self._safe_str(row.get("status")),
                    limit_px=self._q_px(limit_px_raw) if limit_px_raw is not None else None,
                    stop_px=self._q_px(stop_px_raw) if stop_px_raw is not None else None,
                    filled_qty=self._q_qty(self._parse_decimal_or_zero(row.get("filled_qty"))),
                    remaining_qty=self._q_qty(self._parse_decimal_or_zero(row.get("remaining_qty"))),
                    avg_fill_px=self._q_px(avg_fill_px_raw) if avg_fill_px_raw is not None else None,
                    ts=self._parse_optional_int(row.get("ts")),
                    raw=raw if isinstance(raw, dict) else {},
                )
                self._orders_by_id[order_id] = order
                if client_order_id:
                    self._order_id_by_client_id[client_order_id] = order_id
                self._fills_by_order_id.setdefault(order_id, [])

    def _load_fills_from_csv(self) -> None:
        if not os.path.exists(self._fills_csv):
            return

        with open(self._fills_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fill_id = self._safe_str(row.get("fill_id"))
                if not fill_id:
                    continue

                self._persisted_fill_ids.add(fill_id)

                if fill_id in self._fills_by_id:
                    self._recovery_diagnostics["ok"] = False
                    self._recovery_diagnostics["contradictions"].append(
                        {
                            "type": "duplicate_fill_id_in_fills_csv",
                            "fill_id": fill_id,
                        }
                    )
                    continue

                fill = FillState(
                    fill_id=fill_id,
                    trade_id=self._safe_str(row.get("trade_id")) or None,
                    order_id=self._safe_str(row.get("order_id")),
                    client_order_id=self._safe_str(row.get("client_order_id")),
                    symbol=self._safe_str(row.get("symbol")),
                    side=self._safe_str(row.get("side")),
                    qty=self._q_qty(self._parse_decimal_or_zero(row.get("qty"))),
                    price=self._q_px(self._parse_decimal_or_zero(row.get("price"))),
                    fee=self._q_money(self._parse_decimal_or_zero(row.get("fee"))),
                    fee_currency=self._safe_str(row.get("fee_currency")) or self._currency,
                    liquidity=self._safe_str(row.get("liquidity")) or None,
                    ts=self._parse_optional_int(row.get("ts")),
                    raw=self._json_loads(row.get("raw_json")),
                )
                self._fills_by_id[fill_id] = fill
                self._fills_by_order_id.setdefault(fill.order_id, [])
                if fill_id not in self._fills_by_order_id[fill.order_id]:
                    self._fills_by_order_id[fill.order_id].append(fill_id)
                if fill.symbol:
                    self._last_position_symbol = fill.symbol

    def _rebuild_state_from_fills(self) -> None:
        self._positions = {}
        self._realized_pnl = Decimal("0")
        self._cash = self._q_money(self._starting_cash)

        fills = list(self._fills_by_id.values())
        fills.sort(key=lambda x: ((x.ts or 0), x.fill_id))

        for fill in fills:
            self._apply_position_fill(
                symbol=fill.symbol,
                side=fill.side,
                qty=fill.qty,
                px=fill.price,
                fee=fill.fee,
                trade_id=fill.trade_id,
            )

    def _normalize_orders_after_fill_rebuild(self) -> None:
        for order_id, order in list(self._orders_by_id.items()):
            fill_ids = self._fills_by_order_id.get(order_id, [])
            filled_qty = Decimal("0")

            for fill_id in fill_ids:
                fill = self._fills_by_id.get(fill_id)
                if fill is None:
                    continue
                filled_qty += fill.qty

            filled_qty = self._q_qty(filled_qty)
            remaining_qty = self._q_qty(order.qty - filled_qty)
            if remaining_qty < 0:
                self._recovery_diagnostics["ok"] = False
                self._recovery_diagnostics["contradictions"].append(
                    {
                        "type": "order_overfilled",
                        "order_id": order_id,
                        "qty": str(order.qty),
                        "filled_qty": str(filled_qty),
                    }
                )
                remaining_qty = Decimal("0")

            avg_fill_px = self._weighted_avg_fill_px(order_id)
            normalized_status = order.status

            if filled_qty == 0:
                if order.status in self.TERMINAL_ORDER_STATUSES:
                    normalized_status = order.status
                else:
                    normalized_status = "ACKED"
            elif remaining_qty == 0:
                normalized_status = "FILLED"
            else:
                normalized_status = "PARTIALLY_FILLED"

            self._orders_by_id[order_id] = replace(
                order,
                status=normalized_status,
                filled_qty=filled_qty,
                remaining_qty=remaining_qty,
                avg_fill_px=avg_fill_px,
            )

    def _prune_or_finalize_fill_plans(self) -> None:
        changed = False

        for order_id in list(self._fill_plans.keys()):
            order = self._orders_by_id.get(order_id)
            if order is None:
                self._fill_plans.pop(order_id, None)
                changed = True
                continue

            plan = self._fill_plans[order_id]
            if order.status in self.TERMINAL_ORDER_STATUSES:
                if not plan.get("terminal"):
                    plan["terminal"] = True
                    plan["updated_ts"] = self._now_ms()
                    changed = True
            else:
                if "terminal" not in plan:
                    plan["terminal"] = False
                    changed = True

        if changed:
            self._save_fill_plans()

    def _compare_canonical_state_to_snapshots(self) -> None:
        latest_positions = self._load_latest_position_snapshot_by_symbol()
        latest_account = self._load_latest_account_snapshot()

        canonical_positions = {
            symbol: {
                "qty": self._fmt_dec(self._as_decimal(pos.get("qty", Decimal("0")))),
                "avg_entry_px": self._fmt_dec(self._as_decimal(pos.get("avg_entry_px", Decimal("0")))),
                "trade_id": self._safe_str(pos.get("trade_id")) or None,
            }
            for symbol, pos in self._positions.items()
            if self._as_decimal(pos.get("qty", Decimal("0"))) != 0
        }

        position_check: Dict[str, Any] = {"ok": True, "symbols": {}}
        all_symbols = sorted(set(canonical_positions.keys()) | set(latest_positions.keys()))

        for symbol in all_symbols:
            c = canonical_positions.get(symbol)
            s = latest_positions.get(symbol)

            snapshot_qty = self._parse_decimal_or_zero(s.get("qty")) if s is not None else Decimal("0")
            snapshot_is_flat = s is not None and snapshot_qty == 0

            match = (
                (c is None and (s is None or snapshot_is_flat))
                or (
                    c is not None
                    and s is not None
                    and c.get("qty") == s.get("qty")
                    and c.get("avg_entry_px") == s.get("avg_entry_px")
                )
            )

            position_check["symbols"][symbol] = {
                "canonical": c,
                "snapshot": s,
                "match": match,
            }
            if not match:
                position_check["ok"] = False
                self._recovery_diagnostics["warnings"].append(
                    {
                        "type": "position_snapshot_mismatch",
                        "symbol": symbol,
                        "canonical": c,
                        "snapshot": s,
                    }
                )

        self._recovery_diagnostics["position_snapshot_check"] = position_check

        acct_check: Dict[str, Any] = {"ok": True}
        if latest_account:
            canonical_acct = self.get_account_state()
            canonical_cash = self._fmt_dec(canonical_acct.cash)
            canonical_realized = self._fmt_dec(canonical_acct.realized_pnl)

            snapshot_cash = self._safe_str(latest_account.get("cash"))
            snapshot_realized = self._safe_str(latest_account.get("realized_pnl"))

            acct_check = {
                "ok": canonical_cash == snapshot_cash and canonical_realized == snapshot_realized,
                "canonical": {
                    "cash": canonical_cash,
                    "realized_pnl": canonical_realized,
                },
                "snapshot": {
                    "cash": snapshot_cash,
                    "realized_pnl": snapshot_realized,
                },
            }
            if not acct_check["ok"]:
                self._recovery_diagnostics["warnings"].append(
                    {
                        "type": "account_snapshot_mismatch",
                        "canonical": acct_check["canonical"],
                        "snapshot": acct_check["snapshot"],
                    }
                )

        self._recovery_diagnostics["account_snapshot_check"] = acct_check

    def _load_latest_position_snapshot_by_symbol(self) -> Dict[str, Dict[str, str]]:
        latest: Dict[str, Tuple[int, Dict[str, str]]] = {}

        if not os.path.exists(self._positions_csv):
            return {}

        with open(self._positions_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = self._safe_str(row.get("symbol"))
                ts = self._parse_optional_int(row.get("ts")) or 0
                qty = self._safe_str(row.get("qty")).strip()

                if not symbol:
                    continue

                if not qty or self._parse_decimal_or_zero(qty) == 0:
                    candidate = {
                        "qty": "0",
                        "avg_entry_px": self._safe_str(row.get("avg_entry_px")) or "0",
                        "trade_id": None,
                    }
                else:
                    raw = self._json_loads(row.get("raw_json"))
                    trade_id_val = None
                    if isinstance(raw, dict):
                        trade_id_val = self._safe_str(raw.get("trade_id")) or None
                    candidate = {
                        "qty": qty,
                        "avg_entry_px": self._safe_str(row.get("avg_entry_px")),
                        "trade_id": trade_id_val,
                    }

                prev = latest.get(symbol)
                if prev is None or ts >= prev[0]:
                    latest[symbol] = (ts, candidate)

        return {symbol: payload for symbol, (_, payload) in latest.items()}

    def _load_latest_account_snapshot(self) -> Dict[str, str]:
        latest_ts = -1
        latest: Dict[str, str] = {}

        if not os.path.exists(self._account_csv):
            return latest

        with open(self._account_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = self._parse_optional_int(row.get("ts")) or 0
                if ts >= latest_ts:
                    latest_ts = ts
                    latest = {
                        "cash": self._safe_str(row.get("cash")),
                        "realized_pnl": self._safe_str(row.get("realized_pnl")),
                    }

        return latest

    def _reconcile_sequence_counters_from_loaded_artifacts(self) -> None:
        max_order = self._seq_order
        max_fill = self._seq_fill
        max_trade = self._seq_trade

        for order_id in self._orders_by_id.keys():
            max_order = max(max_order, self._extract_numeric_suffix(order_id, prefix="PO-"))

        for fill in self._fills_by_id.values():
            max_fill = max(max_fill, self._extract_numeric_suffix(fill.fill_id, prefix="PF-"))
            trade_id = self._safe_str(fill.trade_id)
            if trade_id:
                max_trade = max(max_trade, self._extract_numeric_suffix(trade_id, prefix="PT-"))

        changed = (
            max_order != self._seq_order
            or max_fill != self._seq_fill
            or max_trade != self._seq_trade
        )

        self._seq_order = max_order
        self._seq_fill = max_fill
        self._seq_trade = max_trade

        if changed:
            self._save_id_state()

    def _atomic_write_json(self, path: str, payload: Dict[str, Any]) -> None:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_paper_adapter_",
            suffix=".json",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # -------------------------------------------------------------------------
    # Internal bookkeeping / ID / lookup helpers
    # -------------------------------------------------------------------------

    def _resolve_order_id(
        self,
        *,
        order_id: Optional[str],
        client_order_id: Optional[str],
    ) -> Optional[str]:
        if order_id:
            return order_id if order_id in self._orders_by_id else None
        if client_order_id:
            return self._order_id_by_client_id.get(client_order_id)
        return None

    def _set_order_status_if_needed(self, order_id: str, status: str, *, event_type: str) -> None:
        order = self._orders_by_id[order_id]
        if order.status != status:
            updated = replace(order, status=status, ts=self._now_ms())
            self._orders_by_id[order_id] = updated
            self._persist_order_snapshot(updated, event_type=event_type)
            self._persist_order_event(updated, event_type=event_type)

    def _get_mark_px(self, symbol: str) -> Optional[Decimal]:
        quote = self._last_quote_by_symbol.get(symbol)
        if not quote:
            return None
        last = quote.get("last")
        if last is not None:
            return self._as_decimal(last)
        bid = quote.get("bid")
        ask = quote.get("ask")
        if bid is not None and ask is not None:
            return self._q_px((self._as_decimal(bid) + self._as_decimal(ask)) / Decimal("2"))
        if bid is not None:
            return self._as_decimal(bid)
        if ask is not None:
            return self._as_decimal(ask)
        return None

    def _quote_raw(self, symbol: str) -> Dict[str, Any]:
        q = self._last_quote_by_symbol.get(symbol)
        if not q:
            return {}
        return {
            "bid": str(q["bid"]) if q.get("bid") is not None else None,
            "ask": str(q["ask"]) if q.get("ask") is not None else None,
            "last": str(q["last"]) if q.get("last") is not None else None,
            "ts": q.get("ts"),
        }

    def _resolve_trade_id_for_fill(self, *, order: OrderState, fill_qty: Decimal) -> str:
        pos = self._positions.get(order.symbol, {})
        current_qty = self._as_decimal(pos.get("qty", Decimal("0")))
        current_trade_id = self._safe_str(pos.get("trade_id")) or None

        if order.side == "BUY":
            if current_qty > 0 and current_trade_id:
                return current_trade_id
            return self._next_trade_id()

        if order.side == "SELL":
            if current_qty <= 0 or not current_trade_id:
                raise ValueError(
                    f"Cannot assign SELL trade_id without open long position for {order.symbol}"
                )
            if fill_qty > current_qty:
                raise ValueError(
                    f"SELL fill_qty {fill_qty} exceeds current position qty {current_qty} for {order.symbol}"
                )
            return current_trade_id

        raise ValueError(f"Unsupported side: {order.side}")

    def _next_order_id(self) -> str:
        return self._allocate_next_id(key="seq_order", prefix="PO-")

    def _next_fill_id(self) -> str:
        return self._allocate_next_id(key="seq_fill", prefix="PF-")

    def _next_trade_id(self) -> str:
        return self._allocate_next_id(key="seq_trade", prefix="PT-")

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _to_decimal(value: Decimal | str | None, field_name: str) -> Decimal:
        if value is None:
            raise ValueError(f"{field_name} cannot be None")
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise ValueError(f"Invalid decimal for {field_name}: {value!r}") from exc

    @staticmethod
    def _as_decimal(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _q_qty(self, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("qty cannot be None")
        return Decimal(str(value)).quantize(self._qty_quant, rounding=ROUND_DOWN)

    def _q_px(self, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("price cannot be None")
        return Decimal(str(value)).quantize(self._px_quant, rounding=ROUND_DOWN)

    def _q_money(self, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("money value cannot be None")
        return Decimal(str(value)).quantize(self._money_quant, rounding=ROUND_DOWN)

    @staticmethod
    def _fee_multiplier(bps: Decimal, *, positive: bool) -> Decimal:
        delta = bps / Decimal("10000")
        if positive:
            return Decimal("1") + delta
        return Decimal("1") - delta

    @staticmethod
    def _fmt_dec(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, Decimal):
            return str(value)
        return str(value)

    @staticmethod
    def _safe_str(value: Any) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _json_dumps(value: Any) -> str:
        try:
            return json.dumps(value, sort_keys=True)
        except Exception:
            return "{}"

    @staticmethod
    def _json_loads(value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        s = str(value).strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            return int(s)
        except Exception:
            return None

    @staticmethod
    def _parse_optional_decimal(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            return Decimal(s)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _parse_decimal_or_zero(value: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        s = str(value).strip()
        if not s:
            return Decimal("0")
        try:
            return Decimal(s)
        except (InvalidOperation, ValueError):
            return Decimal("0")

    @staticmethod
    def _csv_has_data(path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
            return len(rows) > 1
        except OSError:
            return False

    @staticmethod
    def _extract_numeric_suffix(value: str, prefix: str) -> int:
        s = str(value or "")
        if not s.startswith(prefix):
            return 0
        m = re.search(r"(\d+)$", s)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except Exception:
            return 0


if __name__ == "__main__":
    adapter = PaperAdapter(
        starting_cash="1000",
        fee_bps="5",
        slippage_bps="2",
        artifact_dir="ops/logs",
        fill_mode="partial",
        fill_delay_ms=1500,
        partial_fill_ratio="0.50",
        money_precision="0.00000001",
        debug_instrumentation=True,
    )

    adapter.update_market("ETH-USD", bid="2999.50", ask="3000.50", last="3000.00")

    order = adapter.place_order(
        OrderRequest(
            symbol="ETH-USD",
            side="BUY",
            qty=Decimal("0.10"),
            order_type="MARKET",
            client_order_id=f"cid-{uuid.uuid4().hex[:12]}",
            tags={
                "paper_fill_mode": "partial",
                "paper_fill_delay_ms": 1500,
                "paper_partial_fill_ratio": "0.50",
            },
        )
    )

    print("ORDER 1:", order)
    print("OPEN ORDERS:", adapter.get_open_orders())
    print("FILLS T0:", adapter.fetch_fills())

    time.sleep(2)
    print("FILLS T1:", adapter.fetch_fills())
    print("OPEN ORDERS T1:", adapter.get_open_orders())

    time.sleep(2)
    print("FILLS T2:", adapter.fetch_fills())
    print("OPEN ORDERS T2:", adapter.get_open_orders())

    print("RECOVERY DIAGNOSTICS:", adapter.get_recovery_diagnostics())

    exit_order = adapter.place_order(
        OrderRequest(
            symbol="ETH-USD",
            side="SELL",
            qty=Decimal("0.10"),
            order_type="MARKET",
            client_order_id=f"cid-{uuid.uuid4().hex[:12]}",
            tags={"paper_fill_mode": "manual"},
        )
    )

    print("ORDER 2:", exit_order)
    print("OPEN ORDERS BEFORE MANUAL EXIT:", adapter.get_open_orders())

    adapter.trigger_manual_fill(client_order_id=exit_order.client_order_id)

    fills = adapter.fetch_fills()
    if fills:
        adapter.replay_fill(fill_id=fills[-1].fill_id)
        print("DUP REPLAY FETCH:", adapter.fetch_fills())

    print("ACCOUNT:", adapter.get_account_state())
    print("POSITIONS:", adapter.get_positions())
    print("FILLS FINAL:", adapter.fetch_fills())