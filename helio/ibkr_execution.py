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
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Kill-switch flag file — if present, submit_bracket refuses ALL new entries
# (existing positions can still exit via close_position_market). User or admin
# can trigger via POST /api/halt_fleet, or by touching the file directly:
#     touch C:\Argus\repo\argus_flow\logs\HALT.flag
# Resume by deleting the file (or POST /api/resume_fleet).
HALT_FLAG_PATH = Path(__file__).resolve().parents[1] / "argus_flow" / "logs" / "HALT.flag"


def is_fleet_halted() -> tuple[bool, str]:
    """Returns (halted, reason). Reason is the file contents if available."""
    if not HALT_FLAG_PATH.exists():
        return False, ""
    try:
        reason = HALT_FLAG_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        reason = "(unable to read flag file)"
    return True, reason or "no reason provided"


# FLATTEN_EOD flag — when present, runners should:
#   - Refuse all new entries (handled by submit_bracket halt check too)
#   - Force-close any existing positions at next eval boundary (close_position_market
#     allows close even if market closed — this is an emergency-flatten scenario)
# Set by ops/daily_loss_circuit_breaker (auto on -4% loss) OR manually.
# NEVER auto-clears - requires manual review per project_capital_allocator_policy.md.
FLATTEN_FLAG_PATH = Path(__file__).resolve().parents[1] / "argus_flow" / "logs" / "FLATTEN_EOD.flag"


def is_flatten_active() -> tuple[bool, str]:
    """Returns (active, reason). Strategy runners should close any open position
    on next eval cycle when this returns True."""
    if not FLATTEN_FLAG_PATH.exists():
        return False, ""
    try:
        reason = FLATTEN_FLAG_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        reason = "(unable to read flag file)"
    return True, reason or "no reason provided"

from ib_insync import (
    IB,
    Contract,
    ExecutionFilter,
    Forex,
    Future,
    LimitOrder,
    MarketOrder,
    Stock,
    StopOrder,
)

NY_TZ = ZoneInfo("America/New_York")

# IdealPro (IBKR's professional FX market) minimum order size. Smaller orders
# get routed as odd lots with materially worse spreads. Floor enforced in
# submit_bracket() for FX entries.
IDEALPRO_MIN_USD = 25000

# Rough base-currency → USD rates for cross-pair USD-notional estimation.
# Updated periodically; precision isn't critical — the check is a guardrail
# against odd-lot routing, not a tax calculation.
_BASE_TO_USD = {
    "USD": 1.00,
    "EUR": 1.05,
    "GBP": 1.30,
    "CHF": 1.10,
    "CAD": 0.74,
    "AUD": 0.65,
    "NZD": 0.60,
    "JPY": 0.0067,
}


def _fx_usd_notional(symbol: str, size: float, price: float) -> float:
    """Estimate USD notional of an FX position. Returns 0 if symbol unparseable.

    For USD-base pairs (USDJPY, USDCAD): notional = size (base unit IS USD).
    For USD-quote pairs (EURUSD, GBPUSD): notional = size × price.
    For cross pairs (CADJPY, EURJPY): use _BASE_TO_USD rate × size.
    """
    sym = (symbol or "").upper()
    if len(sym) < 6:
        return 0.0
    base, quote = sym[:3], sym[3:6]
    if base == "USD":
        return abs(float(size))
    if quote == "USD":
        return abs(float(size) * float(price))
    rate = _BASE_TO_USD.get(base, 0.5)
    return abs(float(size)) * rate


def is_market_open(contract: Contract, *, now_utc: Optional[datetime] = None) -> bool:
    """Is the market for this contract currently open for native (non-extended) trading?

    Used to guard against after-hours submissions that would either be rejected,
    queue silently for next session open (creating gap risk), or trigger TWS
    "Order outside RTH" warnings.

    Hardcoded windows by secType. Doesn't account for US market holidays —
    a rare false-positive that the broker rejects cleanly is acceptable; the
    much more common case is regular weeknight after-hours.

    - STK: NYSE/NASDAQ RTH 09:30-16:00 ET, weekdays only.
    - CASH (FX): 24/5. Closed Fri 17:00 ET to Sun 17:00 ET.
    - FUT: Sun 18:00 ET through Fri 17:00 ET, with daily 17:00-18:00 ET maintenance break.
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(NY_TZ)
    weekday = now.weekday()  # Mon=0 ... Sun=6
    minute_of_day = now.hour * 60 + now.minute
    sec_type = getattr(contract, "secType", "")

    if sec_type == "STK":
        if weekday >= 5:
            return False
        if minute_of_day < 9 * 60 + 30:
            return False
        if minute_of_day >= 16 * 60:
            return False
        return True

    if sec_type == "CASH":
        if weekday == 4 and now.hour >= 17:
            return False
        if weekday == 5:
            return False
        if weekday == 6 and now.hour < 17:
            return False
        return True

    if sec_type == "FUT":
        if weekday == 4 and now.hour >= 17:
            return False
        if weekday == 5:
            return False
        if weekday == 6 and now.hour < 18:
            return False
        if now.hour == 17:  # daily maintenance break
            return False
        return True

    return True  # unknown secType — defer to broker

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
    est_entry_px: Optional[float] = None,
) -> BracketResult:
    """Submit market entry + protective stop + target limit (true OCO).

    Stop and target share an OCA group so when one fills, the other cancels.
    Refuses to submit if the market for this contract is closed (avoids the
    after-hours queue + Pattern Day Trade reject cascade).

    If est_entry_px is provided, runs a cluster-exposure pre-trade check
    (helio/cluster_exposure.py) and refuses if any per-instrument or per-cluster
    cap would breach.

    Returns BracketResult. Check .entry.filled before trusting stop/target IDs.
    """
    if direction not in ("long", "short"):
        raise IBKRExecutionError(f"bad direction: {direction!r}")
    if size <= 0:
        raise IBKRExecutionError(f"size must be positive, got {size}")

    # Kill-switch — fleet-wide halt. Refuses all new entries; existing positions
    # can still exit via close_position_market.
    halted, halt_reason = is_fleet_halted()
    if halted:
        log.warning(
            f"FLEET_HALTED: refusing entry {direction} {size} {contract.symbol}. "
            f"Reason: {halt_reason}"
        )
        return BracketResult(entry=FillResult(filled=False, reject_reason=f"fleet_halted:{halt_reason[:64]}"))

    # FLATTEN_EOD also blocks new entries (it implies emergency state)
    flatten_active, flatten_reason = is_flatten_active()
    if flatten_active:
        log.warning(
            f"FLATTEN_EOD active: refusing entry {direction} {size} {contract.symbol}. "
            f"Reason: {flatten_reason}"
        )
        return BracketResult(entry=FillResult(filled=False, reject_reason=f"flatten_eod:{flatten_reason[:64]}"))

    if not is_market_open(contract):
        log.warning(
            f"MARKET_CLOSED: refusing entry {direction} {size} {contract.symbol} "
            f"({getattr(contract, 'secType', '?')}) — outside trading hours"
        )
        return BracketResult(entry=FillResult(filled=False, reject_reason="market_closed"))

    # 2026-05-07 audit: pre-entry broker reconciliation.
    # If broker already has a position in this contract that the runner
    # doesn't know about, refuse to enter — submitting another order would
    # double the position. This is the proactive analog of the post-hoc
    # orphan adoption logic in argus_flow/runner_unified.py. Strategies
    # using submit_bracket() don't have argus's reconciliation loop, so
    # they need a check here. The block reason `orphan_at_broker` flags
    # the case for operator review.
    try:
        existing_qty = query_position(ib, contract)
        if existing_qty is not None and abs(float(existing_qty)) > 0:
            log.error(
                f"ORPHAN_AT_BROKER: refusing entry {direction} {size} "
                f"{contract.symbol} — broker already has qty={existing_qty}. "
                f"Strategy state likely diverged from broker. Manual reconcile required."
            )
            return BracketResult(entry=FillResult(
                filled=False,
                reject_reason=f"orphan_at_broker:{existing_qty:.2f}",
            ))
    except Exception as exc:
        # Don't block on a transient query failure — but log it so we know
        log.warning(f"pre-entry broker query failed (allowing trade): {exc}")

    # FX-specific: floor at IdealPro's $25K USD-equivalent minimum lot.
    # Below that, IBKR routes as odd-lot with materially worse spread; we'd
    # rather skip the entry than take a degraded fill.
    if getattr(contract, "secType", "") == "CASH" and est_entry_px is not None and est_entry_px > 0:
        usd_notional = _fx_usd_notional(contract.symbol, float(size), float(est_entry_px))
        if 0 < usd_notional < IDEALPRO_MIN_USD:
            log.warning(
                f"FX_BELOW_IDEALPRO_MIN: refusing {direction} {size} {contract.symbol} "
                f"(~${usd_notional:,.0f} USD-equivalent < ${IDEALPRO_MIN_USD:,} IdealPro min). "
                f"Strategy needs to size up or skip; odd-lot fills are not acceptable."
            )
            return BracketResult(entry=FillResult(filled=False, reject_reason="fx_below_idealpro_min"))

    # Cluster cap pre-trade check (only when caller passes est_entry_px)
    if est_entry_px is not None and est_entry_px > 0:
        est_notional = float(size) * float(est_entry_px)

        # 2026-05-07: hard sanity cap independent of cluster math. Catches
        # sizing-layer bugs that produce phantom oversized orders (the
        # 1,400-share QQQ / 417-contract NQ trades observed in trades.csv
        # but never actually filled at broker). Threshold: any single order
        # > 50% of NetLiq notional is refused outright. This is a circuit
        # breaker — strategies that legitimately need bigger sizing should
        # split the entry across multiple orders.
        try:
            from helio.fleet_sizing import get_sizing_anchor_usd
            anchor = float(get_sizing_anchor_usd())
            if anchor > 0 and est_notional > 0.5 * anchor:
                log.error(
                    f"OVERSIZED_ORDER_REJECTED: {direction} {size} "
                    f"{contract.symbol} notional=${est_notional:,.0f} > "
                    f"50% of NetLiq=${anchor:,.0f} ({est_notional/anchor*100:.0f}%). "
                    f"Sizing-layer likely buggy; refusing to submit."
                )
                return BracketResult(entry=FillResult(
                    filled=False,
                    reject_reason=f"oversized_order:{est_notional:.0f}>{0.5*anchor:.0f}"
                ))
        except Exception as exc:
            log.warning(f"hard size cap check failed (allowing trade): {exc}")

        try:
            from helio.cluster_exposure import would_breach_cluster_cap
            breach = would_breach_cluster_cap(contract.symbol, direction, est_notional)
            if breach:
                log.warning(
                    f"CLUSTER_CAP_BREACH: {breach} would exceed cap on "
                    f"{direction} {size} {contract.symbol} (~${est_notional:,.0f} notional). "
                    f"Refusing entry."
                )
                return BracketResult(entry=FillResult(filled=False, reject_reason=f"cluster_cap_breach:{breach}"))
        except Exception as exc:
            log.warning(f"cluster cap check failed (allowing trade): {exc}")

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

    # OCA group — when one bracket leg fills, the other auto-cancels.
    # Unique per bracket so concurrent brackets on different symbols don't interfere.
    oca_group = f"oca_{contract.symbol}_{entry_fill.order_id}_{int(time.time() * 1000) % 1_000_000}"

    # 2. Protective stop (STP) — OCA leg A
    stop_order = StopOrder(close_action, abs(size), round(stop_px, price_decimals))
    stop_order.ocaGroup = oca_group
    stop_order.ocaType = 1  # 1 = cancel all remaining orders with block
    if account:
        stop_order.account = account
    stop_trade = ib.placeOrder(contract, stop_order)
    stop_id = str(getattr(stop_trade.order, "orderId", "")) or None

    # 3. Target limit (LMT) — OCA leg B
    target_order = LimitOrder(close_action, abs(size), round(target_px, price_decimals))
    target_order.ocaGroup = oca_group
    target_order.ocaType = 1
    if account:
        target_order.account = account
    target_trade = ib.placeOrder(contract, target_order)
    target_id = str(getattr(target_trade.order, "orderId", "")) or None

    log.info(
        f"BRACKETS SUBMITTED: oca={oca_group} stop_id={stop_id} @ {stop_px:.5f}  "
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

    Refuses to submit if market is closed for this contract — UNLESS
    FLATTEN_EOD is active (which is an emergency-close scenario where we want
    to flatten regardless of market hours; the flatten_eod_executor uses
    outsideRth LMTs to handle FX OOH but for this caller path we just allow
    the submit and let IBKR queue or reject as it sees fit).
    """
    flatten_active, _ = is_flatten_active()
    if not flatten_active and not is_market_open(contract):
        log.warning(
            f"MARKET_CLOSED: refusing close {direction} {size} {contract.symbol} "
            f"({getattr(contract, 'secType', '?')}) — outside trading hours"
        )
        return FillResult(filled=False, reject_reason="market_closed")

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
    *,
    entry_direction: Optional[str] = None,
    entry_size: Optional[float] = None,
    stop_px: Optional[float] = None,
    target_px: Optional[float] = None,
) -> Optional[dict]:
    """Detect whether the bracket has fired. Restart-safe.

    Primary signal is broker position: if state thought we had a position but
    broker shows we don't, the bracket fired. We then look up the actual fill
    price via reqExecutions (works across process restarts; orderId-based
    lookup did not, since orderIds are session-scoped to the client_id).

    The new keyword args (entry_direction, entry_size, stop_px, target_px)
    let us classify the exit reason as "stop" vs "target" by comparing the
    fill price against the stored bracket levels. They're optional for
    backward compatibility — without them, reason falls back to "broker_exit".

    Returns {"reason", "fill_price", "fill_ts", "order_id"} or None.
    """
    if not stop_order_id and not target_order_id:
        return None

    # Position-based primary detection
    pos = query_position(ib, contract)
    if entry_direction and entry_size:
        expected = float(entry_size) if entry_direction == "long" else -float(entry_size)
        if abs(pos - expected) < 0.5:  # tolerate float quantities (FX)
            return None  # position unchanged → bracket hasn't fired
    else:
        if abs(pos) > 0:
            return None  # we have *some* position — assume bracket hasn't fired

    # Position has changed (or is now flat). Find the actual exit fill.
    # reqExecutions queries the broker for executions in this session AND
    # historical executions accessible to this client. Survives restart.
    ef = ExecutionFilter()
    ef.symbol = contract.symbol
    ef.secType = getattr(contract, "secType", "") or ""
    try:
        fills = ib.reqExecutions(ef)
    except Exception as exc:
        log.warning(f"reqExecutions failed for {contract.symbol}: {exc}")
        fills = []

    if not fills:
        log.warning(
            f"check_bracket_filled: position changed for {contract.symbol} "
            f"but reqExecutions returned no fills"
        )
        return None

    # The closing fill is the most recent one in the opposite direction of entry.
    # ib_insync Fill.execution.side: "BOT" or "SLD"
    if entry_direction == "long":
        close_side = "SLD"
    elif entry_direction == "short":
        close_side = "BOT"
    else:
        close_side = None

    candidates = [
        f for f in fills
        if (close_side is None or f.execution.side == close_side)
    ]
    if not candidates:
        return None

    latest = max(candidates, key=lambda f: f.time)
    fill_price = float(latest.execution.price)

    # Classify reason from price relative to stored stop/target
    reason = "broker_exit"
    if stop_px is not None and target_px is not None:
        if abs(fill_price - target_px) < abs(fill_price - stop_px):
            reason = "target"
        else:
            reason = "stop"

    return {
        "reason": reason,
        "fill_price": fill_price,
        "fill_ts": latest.time.isoformat() if hasattr(latest.time, "isoformat") else str(latest.time),
        "order_id": str(getattr(latest.execution, "orderId", "")) or None,
    }
