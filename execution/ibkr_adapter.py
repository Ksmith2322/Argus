#!/usr/bin/env python3
"""execution/ibkr_adapter.py — IBKR Client Portal Web API execution adapter.

Implements ExecutionAdapter for live forex trading via the IBKR Client Portal
Web API (local Java gateway at https://localhost:5000).

Forex position sizing notes:
- IBKR forex quantities are in units of base currency (EUR for EURUSD).
- Engine sends qty already calculated from USD_PER_TRADE / price.
- Round to nearest integer (IBKR minimum 1 unit; practical minimum ~1000
  for Ideal FX routing to get best spreads).
- qty_precision in config handles rounding upstream; adapter rounds to int
  as a safety net.

Fill lifecycle:
- place_order() submits a supported order type, handles the confirmation reply loop
  if IBKR prompts for one, and returns an OrderState immediately.
- fetch_fills() polls GET /iserver/account/trades and returns FillState list.
- Fills are persisted to artifact_dir/fills.csv in PaperAdapter-compatible
  format so recovery logic works unchanged.
- Account snapshots are written to artifact_dir/account.csv.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from execution.adapter import (
    AccountState,
    CancelResult,
    ExecutionAdapter,
    FillState,
    OrderRequest,
    OrderState,
    PositionState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV schema constants (compatible with PaperAdapter for recovery)
# ---------------------------------------------------------------------------

_FILLS_HEADERS = [
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

_ACCOUNT_HEADERS = [
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


def _ensure_csv(path: str, headers: List[str]) -> None:
    """Create CSV file with header row if it does not already exist."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)


def _append_csv(path: str, headers: List[str], row: Dict[str, Any]) -> None:
    """Append one row to a CSV file, creating header if missing."""
    _ensure_csv(path, headers)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writerow(row)


def _to_dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x).strip())
    except Exception:
        return Decimal(default)


def _build_ibkr_order_payload(req: OrderRequest, conid: int, qty_int: int) -> Dict[str, Any]:
    """Translate canonical order intent into IBKR Client Portal fields."""
    order_type_map = {
        "MARKET": "MKT",
        "LIMIT": "LMT",
        "STOP": "STP",
        "STOP_LIMIT": "STP LMT",
    }
    ibkr_order_type = order_type_map[req.order_type]
    payload: Dict[str, Any] = {
        "conid": conid,
        "orderType": ibkr_order_type,
        "side": req.side.upper(),
        "quantity": qty_int,
        "tif": req.time_in_force or "GTC",
        "cOID": req.client_order_id,
    }

    if req.limit_px is not None:
        payload["price"] = str(req.limit_px)
    if req.stop_px is not None:
        payload["auxPrice"] = str(req.stop_px)

    return payload


# ---------------------------------------------------------------------------
# IBKRAdapter
# ---------------------------------------------------------------------------


class IBKRAdapter(ExecutionAdapter):
    """Live execution adapter for IBKR forex via Client Portal Web API.

    Unlike PaperAdapter this does NOT simulate fills in-process. Fills are
    polled from the venue via fetch_fills() and persisted to fills.csv so that
    the existing recovery machinery in runner_live.py works unchanged.
    """

    def __init__(
        self,
        client: Any,  # IBKRClient — typed as Any to avoid circular imports
        account_id: str,
        artifact_dir: str = "ops/logs",
        currency: str = "USD",
    ) -> None:
        """
        Args:
            client: IBKRClient instance (from feed_ibkr)
            account_id: IBKR account ID (e.g. "U1234567")
            artifact_dir: directory for fills.csv and account.csv
            currency: account base currency (default "USD")
        """
        self._client = client
        self._account_id = account_id
        self._artifact_dir = artifact_dir
        self._currency = currency

        self._fills_csv = os.path.join(artifact_dir, "fills.csv")
        self._account_csv = os.path.join(artifact_dir, "account.csv")

        os.makedirs(artifact_dir, exist_ok=True)
        _ensure_csv(self._fills_csv, _FILLS_HEADERS)
        _ensure_csv(self._account_csv, _ACCOUNT_HEADERS)

        # Track fill IDs seen this session to support idempotent polling
        self._seen_fill_ids: set = set()

    # ------------------------------------------------------------------
    # ExecutionAdapter contract
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "IBKR"

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(self, req: OrderRequest) -> OrderState:
        """Submit a supported order to IBKR via Client Portal API.

        Handles the IBKR confirmation reply loop automatically:
        if the response contains a reply ID, we POST /iserver/reply/{id}
        with {"confirmed": true} to proceed.

        Returns OrderState immediately after acknowledgement. The actual fill
        will appear via fetch_fills() polling.
        """
        self.validate_order_request(req)

        # Resolve conid for the symbol
        conid = self._client.get_conid(req.symbol)
        if conid is None:
            raise ValueError(f"IBKRAdapter: cannot resolve conid for symbol={req.symbol!r}")

        # Forex qty must be integer units of base currency
        qty_int = max(1, int(Decimal(str(req.qty)).to_integral_value()))

        order_body = {"orders": [_build_ibkr_order_payload(req, conid, qty_int)]}

        try:
            path = f"/v1/api/iserver/account/{self._account_id}/orders"
            resp = self._client._post(path, body=order_body, timeout=15.0)
        except Exception as exc:
            logger.error("[IBKR] place_order failed for %s: %s", req.symbol, exc)
            raise

        # Handle confirmation reply loop
        resp = self._handle_reply_loop(resp)

        # Parse response
        order_id = ""
        order_status = "NEW"

        if isinstance(resp, list) and resp:
            item = resp[0]
            order_id = str(item.get("order_id", item.get("orderId", "")))
            order_status = str(item.get("order_status", item.get("orderStatus", "NEW"))).upper()
        elif isinstance(resp, dict):
            order_id = str(resp.get("order_id", resp.get("orderId", "")))
            order_status = str(resp.get("order_status", resp.get("orderStatus", "NEW"))).upper()

        if not order_id:
            order_id = f"ibkr-{int(time.time() * 1000)}"

        # Normalize status to canonical set
        status_map = {
            "SUBMITTED": "NEW",
            "PRESUBMITTED": "NEW",
            "FILLED": "FILLED",
            "CANCELLED": "CANCELED",
            "CANCELED": "CANCELED",
            "REJECTED": "REJECTED",
        }
        canonical_status = status_map.get(order_status, "NEW")

        order_state = OrderState(
            order_id=order_id,
            client_order_id=req.client_order_id or "",
            symbol=req.symbol,
            side=req.side,
            qty=Decimal(str(qty_int)),
            order_type=req.order_type,
            status=canonical_status,
            limit_px=req.limit_px,
            stop_px=req.stop_px,
            filled_qty=Decimal("0"),
            remaining_qty=Decimal(str(qty_int)),
            avg_fill_px=None,
            ts=int(time.time()),
            raw={"ibkr_request": order_body, "ibkr_response": resp},
        )

        logger.info(
            "[IBKR] place_order OK: %s %s %s qty=%d order_id=%s status=%s",
            req.side, req.symbol, req.order_type, qty_int, order_id, canonical_status,
        )

        return order_state

    def _handle_reply_loop(self, resp: Any, max_rounds: int = 3) -> Any:
        """Auto-confirm IBKR reply prompts.

        IBKR sometimes requires order confirmation before routing. The first
        POST /orders response contains {"id": "<replyId>"} instead of fill data.
        We POST /iserver/reply/{replyId} with confirmed=true to proceed.
        """
        for _ in range(max_rounds):
            if not isinstance(resp, list):
                resp = [resp] if isinstance(resp, dict) else []
            if not resp:
                break
            first = resp[0] if resp else {}
            reply_id = first.get("id") if isinstance(first, dict) else None
            if not reply_id:
                break
            try:
                logger.debug("[IBKR] Confirming order reply id=%s", reply_id)
                resp = self._client._post(
                    f"/v1/api/iserver/reply/{reply_id}",
                    body={"confirmed": True},
                    timeout=10.0,
                )
            except Exception as exc:
                logger.warning("[IBKR] reply confirmation failed: %s", exc)
                break
        return resp

    # ------------------------------------------------------------------
    # Fill polling
    # ------------------------------------------------------------------

    def fetch_fills(self, since_ts: Optional[int] = None) -> List[FillState]:
        """Poll GET /iserver/account/trades and return new FillState objects.

        Each fill is persisted to fills.csv on first seen. The returned list
        may include fills already returned in a prior call — callers must
        deduplicate using fill_id.
        """
        try:
            trades = self._client._get("/v1/api/iserver/account/trades", timeout=15.0)
        except Exception as exc:
            logger.warning("[IBKR] fetch_fills failed: %s", exc)
            return []

        if not isinstance(trades, list):
            return []

        fills: List[FillState] = []
        for t in trades:
            if not isinstance(t, dict):
                continue

            # Use execution_id or trade_id as fill_id
            fill_id = str(t.get("execution_id") or t.get("execId") or t.get("trade_id") or "")
            if not fill_id:
                continue

            order_id = str(t.get("order_ref") or t.get("orderId") or t.get("order_id") or "")
            client_order_id = str(t.get("cOID") or t.get("client_order_id") or "")
            symbol = str(t.get("symbol") or t.get("conidex") or "")
            side = str(t.get("side") or "BUY").upper()
            if side not in ("BUY", "SELL"):
                side = "BUY"
            qty = _to_dec(t.get("size") or t.get("qty") or t.get("quantity"), "0")
            price = _to_dec(t.get("price") or t.get("avg_price") or t.get("px"), "0")
            commission = _to_dec(t.get("commission") or t.get("comm"), "0")
            trade_ts = t.get("trade_time_r") or t.get("trade_time") or t.get("ts")
            try:
                ts_epoch = int(trade_ts) if trade_ts else int(time.time())
                # IBKR sometimes gives ms timestamps
                if ts_epoch > 2_000_000_000_000:
                    ts_epoch = ts_epoch // 1000
            except Exception:
                ts_epoch = int(time.time())

            # Filter by since_ts if provided
            if since_ts is not None and ts_epoch < since_ts:
                continue

            fill = FillState(
                fill_id=fill_id,
                order_id=order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                fee=commission,
                fee_currency=self._currency,
                liquidity="TAKER",
                ts=ts_epoch,
                trade_id=fill_id,
                raw=t,
            )

            fills.append(fill)

            # Persist new fills to fills.csv
            if fill_id not in self._seen_fill_ids:
                self._seen_fill_ids.add(fill_id)
                self._persist_fill(fill)

        return fills

    def _persist_fill(self, fill: FillState) -> None:
        """Write one fill to fills.csv (PaperAdapter-compatible format)."""
        try:
            row = {
                "ts": fill.ts or int(time.time()),
                "fill_id": fill.fill_id,
                "trade_id": fill.trade_id or fill.fill_id,
                "order_id": fill.order_id,
                "client_order_id": fill.client_order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": str(fill.qty),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "fee_currency": fill.fee_currency,
                "liquidity": fill.liquidity or "TAKER",
                "adapter": self.name,
                "raw_json": json.dumps(fill.raw) if fill.raw else "{}",
            }
            _append_csv(self._fills_csv, _FILLS_HEADERS, row)
        except Exception as exc:
            logger.warning("[IBKR] Failed to persist fill %s: %s", fill.fill_id, exc)

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        """GET /portfolio/{accountId}/ledger and return AccountState."""
        try:
            data = self._client._get(
                f"/v1/api/portfolio/{self._account_id}/ledger",
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("[IBKR] get_account_state failed: %s", exc)
            return AccountState(
                equity=Decimal("0"),
                cash=Decimal("0"),
                buying_power=Decimal("0"),
                currency=self._currency,
            )

        # Ledger response is a dict keyed by currency, e.g. {"USD": {...}, "BASE": {...}}
        if not isinstance(data, dict):
            return AccountState(
                equity=Decimal("0"),
                cash=Decimal("0"),
                buying_power=Decimal("0"),
                currency=self._currency,
            )

        # Try to find the account's base currency section
        section = data.get(self._currency) or data.get("BASE") or {}
        if not section and data:
            # fallback: use first key
            section = next(iter(data.values()), {})

        cash = _to_dec(
            section.get("cashbalance") or section.get("cash") or section.get("totalcashvalue"), "0"
        )
        net_liq = _to_dec(
            section.get("netliquidationvalue") or section.get("netliquidation") or section.get("equity"), "0"
        )
        if net_liq <= 0:
            net_liq = cash
        buying_power = _to_dec(
            section.get("buyingpower") or section.get("buying_power"), "0"
        )
        if buying_power <= 0:
            buying_power = cash
        realized_pnl = _to_dec(section.get("realizedpnl") or section.get("realized_pnl"), "0")
        unrealized_pnl = _to_dec(
            section.get("unrealizedpnl") or section.get("unrealized_pnl"), "0"
        )

        acct = AccountState(
            equity=net_liq,
            cash=cash,
            buying_power=buying_power,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            currency=self._currency,
            raw=data,
        )

        self._persist_account_snapshot(acct, reason="poll")
        return acct

    def _persist_account_snapshot(self, acct: AccountState, reason: str = "poll") -> None:
        """Append one account snapshot row to account.csv."""
        try:
            row = {
                "ts": int(time.time()),
                "reason": reason,
                "equity": str(acct.equity),
                "cash": str(acct.cash),
                "buying_power": str(acct.buying_power),
                "realized_pnl": str(acct.realized_pnl),
                "unrealized_pnl": str(acct.unrealized_pnl),
                "currency": acct.currency,
                "adapter": self.name,
                "raw_json": json.dumps(acct.raw) if acct.raw else "{}",
            }
            _append_csv(self._account_csv, _ACCOUNT_HEADERS, row)
        except Exception as exc:
            logger.warning("[IBKR] Failed to persist account snapshot: %s", exc)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> List[PositionState]:
        """GET /portfolio/{accountId}/positions/0 and return PositionState list."""
        try:
            data = self._client._get(
                f"/v1/api/portfolio/{self._account_id}/positions/0",
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("[IBKR] get_positions failed: %s", exc)
            return []

        if not isinstance(data, list):
            return []

        positions: List[PositionState] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            qty = _to_dec(item.get("position") or item.get("qty"), "0")
            if qty == 0:
                continue
            symbol = str(item.get("contractDesc") or item.get("symbol") or item.get("ticker") or "")
            avg_entry = _to_dec(item.get("avgCost") or item.get("avg_cost") or item.get("avgPrice"), "0")
            mark_px = _to_dec(item.get("mktPrice") or item.get("mark_price"), "0") or None
            unrealized_pnl = _to_dec(item.get("unrealizedPnl") or item.get("unrealized_pnl"), "0")
            realized_pnl = _to_dec(item.get("realizedPnl") or item.get("realized_pnl"), "0")
            side = "LONG" if qty > 0 else "SHORT"

            positions.append(
                PositionState(
                    symbol=symbol,
                    qty=qty,
                    avg_entry_px=avg_entry,
                    mark_px=mark_px,
                    unrealized_pnl=unrealized_pnl,
                    realized_pnl=realized_pnl,
                    side=side,
                    raw=item,
                )
            )

        return positions

    # ------------------------------------------------------------------
    # Open orders
    # ------------------------------------------------------------------

    def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderState]:
        """GET /iserver/account/orders and return open OrderState list."""
        try:
            data = self._client._get(
                "/v1/api/iserver/account/orders",
                params={"filters": "inactive"},
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("[IBKR] get_open_orders failed: %s", exc)
            return []

        orders_raw = []
        if isinstance(data, dict):
            orders_raw = data.get("orders", [])
        elif isinstance(data, list):
            orders_raw = data

        result: List[OrderState] = []
        for item in orders_raw:
            if not isinstance(item, dict):
                continue

            order_id = str(item.get("orderId") or item.get("order_id") or "")
            client_order_id = str(item.get("cOID") or item.get("client_order_id") or "")
            sym = str(item.get("ticker") or item.get("symbol") or item.get("contractDesc") or "")

            if symbol and sym and sym.upper() != symbol.upper():
                continue

            side = str(item.get("side") or "BUY").upper()
            status = str(item.get("status") or item.get("orderStatus") or "NEW").upper()
            qty = _to_dec(item.get("totalSize") or item.get("qty") or item.get("quantity"), "0")
            filled = _to_dec(item.get("filledQuantity") or item.get("filled_qty"), "0")
            remaining = max(Decimal("0"), qty - filled)
            order_type = str(item.get("orderType") or item.get("order_type") or "MKT").upper()

            result.append(
                OrderState(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    symbol=sym,
                    side=side,
                    qty=qty,
                    order_type=order_type,
                    status=status,
                    filled_qty=filled,
                    remaining_qty=remaining,
                    ts=int(time.time()),
                    raw=item,
                )
            )

        return result

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def cancel(
        self,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> CancelResult:
        """DELETE /iserver/account/{acctId}/order/{orderId}."""
        if not order_id and not client_order_id:
            return CancelResult(
                ok=False,
                order_id="",
                client_order_id=client_order_id,
                status="REJECTED",
                message="order_id or client_order_id required",
            )

        oid = order_id or ""
        if not oid:
            # Try to resolve from open orders by client_order_id
            for o in self.get_open_orders():
                if o.client_order_id == client_order_id:
                    oid = o.order_id
                    break

        if not oid:
            return CancelResult(
                ok=False,
                order_id=oid,
                client_order_id=client_order_id,
                status="NOT_FOUND",
                message="Could not resolve order_id for cancellation",
            )

        try:
            resp = self._client._delete(
                f"/v1/api/iserver/account/{self._account_id}/order/{oid}",
                timeout=10.0,
            )
            ok = True
            msg = str(resp) if resp else "OK"
        except Exception as exc:
            ok = False
            msg = str(exc)
            logger.warning("[IBKR] cancel(%s) failed: %s", oid, exc)

        return CancelResult(
            ok=ok,
            order_id=oid,
            client_order_id=client_order_id,
            status="CANCELED" if ok else "REJECTED",
            message=msg,
            raw={"response": str(msg)},
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self) -> bool:
        """POST /tickle — keeps gateway session alive and checks auth."""
        return self._client.tickle()
