#!/usr/bin/env python3
# execution/adapter.py

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


# ----------------------------
# Canonical execution models
# ----------------------------

@dataclass(frozen=True)
class AccountState:
    """
    Snapshot of account-level state returned by an execution adapter.
    Keep this small and stable. Add fields only when the runtime truly needs them.
    """
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    currency: str = "USD"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionState:
    """
    One open or recently known position on the venue/account.
    qty > 0 typically means long.
    """
    symbol: str
    qty: Decimal
    avg_entry_px: Decimal
    mark_px: Optional[Decimal] = None
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    side: str = "LONG"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRequest:
    """
    Canonical order request passed from engine/runtime into the adapter.

    Notes:
    - qty should already be normalized by the caller for strategy/risk purposes.
    - client_order_id is mandatory for restart safety / dedupe / reconciliation.
    - reduce_only and post_only are optional policy flags the adapter may honor
      if the downstream venue supports them.
    """
    symbol: str
    side: str                     # BUY | SELL
    qty: Decimal
    order_type: str               # MARKET | LIMIT | STOP | STOP_LIMIT ...
    limit_px: Optional[Decimal] = None
    stop_px: Optional[Decimal] = None
    client_order_id: Optional[str] = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    post_only: bool = False
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderState:
    """
    Canonical order state returned by adapter after placement or fetch.
    """
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    order_type: str
    status: str                   # NEW | ACKED | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED
    limit_px: Optional[Decimal] = None
    stop_px: Optional[Decimal] = None
    filled_qty: Decimal = Decimal("0")
    remaining_qty: Decimal = Decimal("0")
    avg_fill_px: Optional[Decimal] = None
    ts: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FillState:
    """
    Canonical fill/trade execution record.

    fill_id must be unique and stable for exactly-once reconciliation.
    """
    fill_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    fee_currency: str = "USD"
    liquidity: Optional[str] = None   # MAKER | TAKER | UNKNOWN
    ts: Optional[int] = None
    trade_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    """
    Canonical cancellation response.
    """
    ok: bool
    order_id: str
    client_order_id: Optional[str] = None
    status: str = "UNKNOWN"
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


# ----------------------------
# Adapter contract
# ----------------------------

class ExecutionAdapter(ABC):
    """
    Abstract execution boundary for Argus.

    Architectural rule:
        Strategy -> Engine -> ExecutionAdapter -> Ledger -> Artifacts

    The adapter is the only layer allowed to translate between Argus order intent
    and venue/broker/exchange semantics.

    Non-negotiable expectations:
    1) client_order_id must be supported for dedupe/restart safety
    2) returned order_id/fill_id values must be stable and deterministic
    3) fetch_fills() must be safe to call repeatedly without creating duplicates
    4) adapter should expose venue truth; caller owns reconciliation logic
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable adapter name, e.g.:
        - 'paper'
        - 'coinbase-sandbox'
        - 'alpaca-paper'
        """
        raise NotImplementedError

    @abstractmethod
    def get_account_state(self) -> AccountState:
        """
        Return the latest account snapshot from venue truth.
        """
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> List[PositionState]:
        """
        Return current open positions known by the venue/account.
        """
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderState]:
        """
        Return open working orders.
        """
        raise NotImplementedError

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderState:
        """
        Submit an order and return canonical order state.

        Required behavior:
        - reject missing client_order_id
        - preserve request identity
        - normalize downstream response into OrderState
        """
        raise NotImplementedError

    @abstractmethod
    def cancel(
        self,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> CancelResult:
        """
        Cancel a working order by venue order_id or client_order_id.

        At least one identifier must be supplied.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_fills(self, since_ts: Optional[int] = None) -> List[FillState]:
        """
        Fetch fills since the given epoch timestamp.

        This must be idempotent from the caller's perspective:
        repeated polling may return already-seen fills, but each fill must carry
        a stable fill_id so the caller can deduplicate exactly once.
        """
        raise NotImplementedError

    @abstractmethod
    def heartbeat(self) -> bool:
        """
        Lightweight health check to determine whether the adapter/venue is reachable.
        """
        raise NotImplementedError

    def reconcile_snapshot(self) -> Dict[str, Any]:
        """
        Optional convenience method for startup/restart reconciliation.

        Default behavior is intentionally generic. Adapters may override for more
        efficient venue-native batch fetches.
        """
        return {
            "adapter": self.name,
            "account": self.get_account_state(),
            "positions": self.get_positions(),
            "open_orders": self.get_open_orders(),
            "fills": self.fetch_fills(since_ts=None),
        }

    def validate_order_request(self, req: OrderRequest) -> None:
        """
        Shared validation helper. Concrete adapters may call this before sending.
        """
        if not req.symbol or not isinstance(req.symbol, str):
            raise ValueError("OrderRequest.symbol is required")

        if req.side not in {"BUY", "SELL"}:
            raise ValueError("OrderRequest.side must be 'BUY' or 'SELL'")

        if req.order_type not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
            raise ValueError(
                "OrderRequest.order_type must be one of: "
                "'MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT'"
            )

        if req.qty <= Decimal("0"):
            raise ValueError("OrderRequest.qty must be > 0")

        if not req.client_order_id:
            raise ValueError("OrderRequest.client_order_id is required")

        if req.order_type in {"LIMIT", "STOP_LIMIT"} and req.limit_px is None:
            raise ValueError("limit_px is required for LIMIT and STOP_LIMIT orders")

        if req.order_type in {"STOP", "STOP_LIMIT"} and req.stop_px is None:
            raise ValueError("stop_px is required for STOP and STOP_LIMIT orders")

    @staticmethod
    def normalize_decimal(value: Any, field_name: str) -> Decimal:
        """
        Small helper for concrete adapters normalizing venue payloads.
        """
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise ValueError(f"Invalid decimal for {field_name}: {value!r}") from exc