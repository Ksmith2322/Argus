"""Shared IBKR execution helper for wake-and-sleep runners (forge/*, apollo, etc.).

Designed for strategies that evaluate on a schedule (hourly, daily, event-driven)
rather than argus's persistent event-loop runner. Connect → submit → verify →
disconnect per cycle. Between cycles, IBKR holds any open position and its
protective stop/target brackets.

Target: IBKR paper account on port 7497 (override via IBKR_PORT env).

Why a shared module: converting 19 strategies from signal-only to real execution
means the same connection, contract-building, bracket-order, and reconciliation
logic written 19 times. This keeps the strategy runners focused on the signal
logic and delegates execution plumbing here. Argus's persistent-loop path stays
separate — argus has tick-level needs a wake-and-sleep runner can't match.

2026-04-24: extracted from planned gld_pm_long conversion.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ib_insync import (
    IB,
    Contract,
    Forex,
    Future,
    LimitOrder,
    MarketOrder,
    Stock,
    StopOrder,
)

log = logging.getLogger(__name__)

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "7497"))  # 7497 = paper, 7496 = live


@dataclass
class FillResult:
    """Outcome of submitting a market order + waiting briefly for fill."""
    filled: bool
    fill_price: Optional[float] = None
    fill_ts: Optional[str] = None
    order_id: Optional[str] = None
    reject_reason: Optional[str] = None


@dataclass
class BracketResult:
    """Outcome of submit_bracket — entry fill + protective order IDs."""
    entry: FillResult
    stop_order_id: Optional[str] = None
    target_order_id: Optional[str] = None


class IBKRExecutionError(Exception):
    """Raised when IBKR execution fails in a way the caller should surface."""


def connect(client_id: int, timeout: int = 15) -> IB:
    """Connect to TWS/gateway. Caller must call disconnect() when done.

    Each runner must use a unique client_id (forge range: 100-199).
    """
    ib = IB()
    try:
        log.info(f"Connecting to IBKR {IBKR_HOST}:{IBKR_PORT} client_id={client_id}")
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=timeout)
        return ib
    except Exception as exc:
        raise IBKRExecutionError(f"connect failed: {exc}") from exc


def disconnect(ib: IB) -> None:
    """Disconnect cleanly. Safe to call even if already disconnected."""
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception as exc:
        log.warning(f"disconnect raised (ignored): {exc}")


def make_contract(symbol: str, instrument_type: str) -> Contract:
    """Build a qualified IBKR contract. Caller should ib.qualifyContracts() it.

    instrument_type:
      "stock" / "etf"   - SMART/USD routing (GLD, SPY, UVXY, GDX, etc.)
      "fx" / "forex"    - IDEALPRO cash FX (GBPUSD, USDJPY, etc.)
      "future"          - CME/GLOBEX (NQ, ES)
      "micro_future"    - micro contracts (MNQ, MES, MYM, M2K)
    """
    t = instrument_type.lower()
    if t in ("stock", "etf"):
        return Stock(symbol, "SMART", "USD")
    if t in ("fx", "forex"):
        # IB FX uses concatenated pair, e.g. "EURUSD" or "USDJPY"
        return Forex(symbol)
    if t == "future":
        # Front-month continuous — caller should qualify to pin month
        return Future(symbol, exchange="CME")
    if t == "micro_future":
        return Future(symbol, exchange="CME")
    raise IBKRExecutionError(f"unknown instrument_type: {instrument_type!r}")


def _wait_for_fill(ib: IB, trade, timeout_s: float = 10.0) -> FillResult:
    """Block up to timeout_s for a market order to fill. Returns FillResult."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ib.sleep(0.5)
        if trade.isDone():
            break
    order_id = str(getattr(trade.order, "orderId", "")) or None
    if trade.fills:
        f = trade.fills[-1]
        return FillResult(
            filled=True,
            fill_price=float(f.execution.price),
            fill_ts=f.time.isoformat() if hasattr(f.time, "isoformat") else str(f.time),
            order_id=order_id,
        )
    status = getattr(trade.orderStatus, "status", "")
    reject = getattr(trade.orderStatus, "whyHeld", "") or status
    return FillResult(filled=False, order_id=order_id, reject_reason=reject or "timeout")


def submit_bracket(
    ib: IB,
    contract: Contract,
    direction: str,   # "long" or "short"
    size: float,
    stop_px: float,
    target_px: float,
    account: str = "",
    price_decimals: int = 5,
    entry_fill_timeout_s: float = 15.0,
) -> BracketResult:
    """Submit market entry + protective stop + target limit.

    Waits briefly for the entry fill so we can return a real fill price.
    Stop and target are submitted AFTER the entry fills (IBKR needs the
    position to exist before it accepts the brackets as reducing orders).

    Returns BracketResult. Check .entry.filled before trusting stop/target IDs.
    """
    if direction not in ("long", "short"):
        raise IBKRExecutionError(f"bad direction: {direction!r}")
    if size <= 0:
        raise IBKRExecutionError(f"size must be positive, got {size}")

    entry_action = "BUY" if direction == "long" else "SELL"
    close_action = "SELL" if direction == "long" else "BUY"

    # 1. Entry market order
    entry_order = MarketOrder(entry_action, abs(size))
    if account:
        entry_order.account = account
    entry_trade = ib.placeOrder(contract, entry_order)
    entry_fill = _wait_for_fill(ib, entry_trade, timeout_s=entry_fill_timeout_s)

    if not entry_fill.filled:
        log.error(
            f"ENTRY NOT FILLED: {entry_action} {size} {contract.symbol} "
            f"reason={entry_fill.reject_reason}"
        )
        return BracketResult(entry=entry_fill)

    log.info(
        f"ENTRY FILLED: {entry_action} {size} {contract.symbol} @ {entry_fill.fill_price:.5f} "
        f"orderId={entry_fill.order_id}"
    )

    # 2. Protective stop (STP)
    stop_order = StopOrder(close_action, abs(size), round(stop_px, price_decimals))
    if account:
        stop_order.account = account
    stop_trade = ib.placeOrder(contract, stop_order)
    stop_id = str(getattr(stop_trade.order, "orderId", "")) or None

    # 3. Target limit (LMT)
    target_order = LimitOrder(close_action, abs(size), round(target_px, price_decimals))
    if account:
        target_order.account = account
    target_trade = ib.placeOrder(contract, target_order)
    target_id = str(getattr(target_trade.order, "orderId", "")) or None

    log.info(
        f"BRACKETS SUBMITTED: stop_id={stop_id} @ {stop_px:.5f}  "
        f"target_id={target_id} @ {target_px:.5f}"
    )
    # Give IBKR a moment to acknowledge bracket orders before we return
    ib.sleep(1.0)
    return BracketResult(entry=entry_fill, stop_order_id=stop_id, target_order_id=target_id)


def query_position(ib: IB, contract: Contract) -> float:
    """Current broker position for this contract. Positive=long, negative=short, 0=flat.

    Compares by conId if the contract is qualified, else by symbol.
    """
    positions = ib.positions()
    target_conid = getattr(contract, "conId", None)
    for p in positions:
        pc_conid = getattr(p.contract, "conId", None)
        if target_conid and pc_conid and pc_conid == target_conid:
            return float(p.position)
        if p.contract.symbol == contract.symbol and p.contract.secType == contract.secType:
            return float(p.position)
    return 0.0


def cancel_order_by_id(ib: IB, order_id: Optional[str]) -> bool:
    """Cancel an open order by its IBKR order_id. Returns True if cancel sent."""
    if not order_id:
        return True
    for trade in ib.openTrades():
        if str(getattr(trade.order, "orderId", "")) == order_id:
            ib.cancelOrder(trade.order)
            ib.sleep(0.5)
            return True
    return False


def close_position_market(
    ib: IB,
    contract: Contract,
    direction: str,
    size: float,
    stop_order_id: Optional[str] = None,
    target_order_id: Optional[str] = None,
    account: str = "",
    timeout_s: float = 10.0,
) -> FillResult:
    """Cancel any outstanding brackets, then submit market close.

    direction: the direction of the OPEN position ("long" → submit SELL to close).
    Safe to call even if brackets already got filled — cancel fails silently.
    """
    cancel_order_by_id(ib, stop_order_id)
    cancel_order_by_id(ib, target_order_id)
    close_action = "SELL" if direction == "long" else "BUY"
    order = MarketOrder(close_action, abs(size))
    if account:
        order.account = account
    trade = ib.placeOrder(contract, order)
    fill = _wait_for_fill(ib, trade, timeout_s=timeout_s)
    if fill.filled:
        log.info(
            f"CLOSE FILLED: {close_action} {size} {contract.symbol} @ {fill.fill_price:.5f}"
        )
    else:
        log.error(
            f"CLOSE NOT FILLED: {close_action} {size} {contract.symbol} "
            f"reason={fill.reject_reason}"
        )
    return fill


def check_bracket_filled(
    ib: IB,
    contract: Contract,
    stop_order_id: Optional[str],
    target_order_id: Optional[str],
) -> Optional[dict]:
    """If a bracket order filled since we last checked, return details.

    Returns {"reason": "stop"|"target", "fill_price": float, "fill_ts": str}
    or None if neither filled yet. Uses fills() which returns completed executions.
    """
    if not stop_order_id and not target_order_id:
        return None

    # Check open trades first — if still open, nothing filled
    still_open = {str(getattr(t.order, "orderId", "")) for t in ib.openTrades()}
    stop_open = stop_order_id and stop_order_id in still_open
    target_open = target_order_id and target_order_id in still_open

    if stop_open and target_open:
        return None  # both brackets still pending

    # One side closed — look for its fill
    for trade in ib.trades():
        oid = str(getattr(trade.order, "orderId", ""))
        if oid == stop_order_id and trade.fills and not stop_open:
            f = trade.fills[-1]
            return {
                "reason": "stop",
                "fill_price": float(f.execution.price),
                "fill_ts": f.time.isoformat() if hasattr(f.time, "isoformat") else str(f.time),
                "order_id": oid,
            }
        if oid == target_order_id and trade.fills and not target_open:
            f = trade.fills[-1]
            return {
                "reason": "target",
                "fill_price": float(f.execution.price),
                "fill_ts": f.time.isoformat() if hasattr(f.time, "isoformat") else str(f.time),
                "order_id": oid,
            }
    return None
