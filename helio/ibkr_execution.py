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
    """Returns (halted, reason) from the reconciled halt-state reader."""
    from helio.halt_state import is_fleet_halted as _is_fleet_halted

    return _is_fleet_halted()


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

# Futures contract multipliers — USD per index point. Used to convert
# `size × price` (which would equal index_value * contracts) into actual
# USD notional. Stocks/ETFs/FX get multiplier=1.0 (default).
#
# E-minis: ES=$50/pt, NQ=$20/pt, YM=$5/pt, RTY=$50/pt (full size)
# Micros:  MES=$5/pt, MNQ=$2/pt, MYM=$0.50/pt, M2K=$5/pt
# Rates:   ZN/ZF/ZT use $1000/pt notional but trade in 1/32 ticks; treated
#          conservatively at 1.0 here since strategies trading them are
#          rare. Refine if a runner needs precision on them.
_FUTURES_MULTIPLIER_USD_PER_POINT: dict[str, float] = {
    "MYM": 0.50,
    "MES": 5.00,
    "MNQ": 2.00,
    "M2K": 5.00,
    "YM":  5.00,
    "ES":  50.00,
    "NQ":  20.00,
    "RTY": 50.00,
}


def _futures_multiplier(symbol: str) -> float:
    """Return USD-per-index-point multiplier for a futures symbol, or 1.0 for
    non-futures (stocks, ETFs, FX). 1.0 means ``size × price`` already equals
    USD notional, which is correct for stocks/ETFs/FX."""
    return _FUTURES_MULTIPLIER_USD_PER_POINT.get((symbol or "").upper(), 1.0)


# OVERSIZED_ORDER hard-sanity threshold as multiple of NetLiq anchor, by
# asset class. Values are calibrated to allow legitimate sizing per
# fleet_sizing.json's notional_caps_by_asset_class with ~25% buffer,
# while still catching phantom-oversized orders (the 1,400-share QQQ /
# 417-contract NQ pattern this guard was added to catch on 2026-05-07).
#
# Stocks/ETFs cap=0.3× → guard at 0.5× (67% headroom).
# FX cap=1.0× → guard at 1.5× (50% headroom).
# Futures cap=2.0× → guard at 2.5× (25% headroom — futures are intrinsically
#                   large in notional but bounded by margin).
_OVERSIZE_THRESHOLD_X = {
    "stock":          0.5,
    "etf":            0.5,
    "leveraged_etf":  0.5,
    "fx":             1.5,
    "micro_future":   2.5,
    "future":         2.5,
}


def _classify_symbol(symbol: str) -> str:
    """Map symbol to the asset class key used by _OVERSIZE_THRESHOLD_X."""
    sym = (symbol or "").upper()
    if sym in _FUTURES_MULTIPLIER_USD_PER_POINT:
        # Micro contracts (multiplier < 10) treated as micro_future, full
        # contracts (>= 10) treated as future. Threshold is the same either
        # way for now; split kept for future divergence.
        return "micro_future" if _FUTURES_MULTIPLIER_USD_PER_POINT[sym] < 10 else "future"
    if len(sym) == 6 and sym[:3].isalpha() and sym[3:].isalpha():
        # 6-char alpha = FX pair (USDJPY, GBPUSD, etc.)
        return "fx"
    # Default everything else (stocks, ETFs, unknown) to stock threshold.
    return "stock"


def _oversize_threshold_usd(symbol: str, anchor_usd: float) -> float:
    """USD threshold above which a single order is considered phantom-oversized
    for this symbol. Pulls per-asset-class multiplier from
    _OVERSIZE_THRESHOLD_X."""
    factor = _OVERSIZE_THRESHOLD_X.get(_classify_symbol(symbol), 0.5)
    return factor * float(anchor_usd)


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
        # Include exception type + repr so empty-message failures (TimeoutError,
        # asyncio CancelledError, etc.) still surface a diagnosable trace.
        # Without this, mamba/cuebanks logged "connect failed: connect failed:"
        # for hours with no clue why — see 2026-05-08 investigation.
        msg = f"connect failed: {type(exc).__name__}: {exc!r} (host={IBKR_HOST} port={IBKR_PORT} client_id={client_id})"
        raise IBKRExecutionError(msg) from exc


def disconnect(ib: IB) -> None:
    """Disconnect cleanly. Safe to call even if already disconnected."""
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception as exc:
        log.warning(f"disconnect raised (ignored): {exc}")


def connect_with_retry(client_id: int, timeout: int = 15,
                        max_attempts: int = 5, backoff_s: float = 60.0) -> IB:
    """Connect to TWS with retry. Addresses the post-TWS-restart client_id
    slot pattern (TWS Error 326): after TWS is restarted, the client_id
    slots from the previous TWS session stay "in use" for 1-5 minutes
    before clearing. The single-attempt `connect()` fails during that
    window, and wake-and-sleep runners with 1-4hr cycles then go silent
    until the next cycle.

    This helper:
      - Retries up to `max_attempts` times with `backoff_s` between
        attempts (default: 5 × 60s = 5min total wait, which covers the
        typical TWS-slot-clear window)
      - Logs each attempt + the reason for failure (caught from improved
        `connect()` diagnostics)
      - Returns the connected IB on success, raises IBKRExecutionError
        with all attempt history on final failure

    Use this when:
      - A runner is in a periodic-wake loop and shouldn't go silent for
        hours after a transient connect failure
      - Right after a TWS restart when slot recovery is expected

    Don't use this when:
      - The caller has its own outer retry loop (gdx_gld has retry-forever
        via `_connect_with_backoff` — that's the right shape for it)
      - The failure should propagate immediately (one-shot scripts)
    """
    last_exc: Exception | None = None
    attempt_log: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            return connect(client_id, timeout=timeout)
        except IBKRExecutionError as exc:
            last_exc = exc
            attempt_log.append(f"attempt {attempt}/{max_attempts}: {exc}")
            if attempt < max_attempts:
                log.warning(
                    f"connect_with_retry: attempt {attempt}/{max_attempts} "
                    f"failed for client_id={client_id}, retrying in {backoff_s}s"
                )
                time.sleep(backoff_s)
    # Exhausted all attempts
    history = "; ".join(attempt_log)
    raise IBKRExecutionError(
        f"connect_with_retry exhausted {max_attempts} attempts for client_id={client_id}: {history}"
    ) from last_exc


def recover_pending_at_startup(ib: IB, strategy: str) -> list[dict]:
    """Forge-runner startup helper. Resolves any orders this strategy submitted
    in a prior process whose fills landed during the gap.

    Call once after connect(), before submitting any new orders. Returns the
    resolved entries (caller logs/ingests them as adopted).

    Safe to call every wake-cycle — only resolves entries belonging to the
    given strategy and only if broker has a matching position.
    """
    try:
        from helio.pending_fills import reconcile_pending
        resolved = reconcile_pending(ib, strategy=strategy)
        for r in resolved:
            log.warning(
                f"PENDING_FILL_RESOLVED: strategy={r.get('strategy')} "
                f"symbol={r.get('symbol')} dir={r.get('direction')} "
                f"size={r.get('size')} broker_qty={r.get('broker_qty_observed')} "
                f"order_id={r.get('order_id')} (filled during prior-session gap)"
            )
        return resolved
    except Exception as exc:
        log.warning(f"recover_pending_at_startup failed (non-fatal): {exc}")
        return []


def make_contract(symbol: str, instrument_type: str) -> Contract:
    """Build a qualified IBKR contract. Caller should ib.qualifyContracts() it.

    instrument_type:
      "stock" / "etf"   - SMART/USD routing (GLD, SPY, UVXY, GDX, etc.)
      "fx" / "forex"    - IDEALPRO cash FX (GBPUSD, USDJPY, etc.)
      "future"          - exchange-specific routing (NQ/ES on CME, YM/RTY on CBOT)
      "micro_future"    - micros (MNQ/MES on CME, MYM/M2K on CBOT)

    Exchange routing (2026-05-16 fix): CME Group splits products across two
    exchanges. NQ/MNQ (Nasdaq-100) and ES/MES (S&P-500) route to CME; YM/MYM
    (Dow) and RTY/M2K (Russell-2000) route to CBOT. Sending a Dow/Russell
    contract with exchange="CME" causes a SILENT order Cancellation from TWS
    (no errorCode, no whyHeld message). This bug kept tori/cuebanks/mamba at
    0 fills across 22+ days of MYM signals — they detected, submitted, and
    got silently rejected with no diagnostic trail.
    """
    t = instrument_type.lower()
    if t in ("stock", "etf"):
        return Stock(symbol, "SMART", "USD")
    if t in ("fx", "forex"):
        # IB FX uses concatenated pair, e.g. "EURUSD" or "USDJPY"
        return Forex(symbol)
    if t in ("future", "micro_future"):
        sym_u = symbol.upper()
        # CBOT-listed: Dow + Russell 2000 (both full-size and micros)
        if sym_u in {"YM", "MYM", "RTY", "M2K"}:
            return Future(symbol, exchange="CBOT", currency="USD")
        # Default CME for everything else (NQ/MNQ, ES/MES, etc.)
        return Future(symbol, exchange="CME", currency="USD")
    raise IBKRExecutionError(f"unknown instrument_type: {instrument_type!r}")


def qualify_front_month_future(ib: IB, symbol: str, exchange: str = None,
                                 min_days_to_expiry: int = 7) -> Contract:
    """Resolve the front-month futures contract for a symbol.

    Background (2026-05-18): `ib.qualifyContracts()` silently fails on
    multi-expiry futures contracts when given just `Future(symbol, exchange)`
    with no expiry hint. ib_insync sees multiple ContractDetails returned
    (Jun/Sep/Dec quarterly contracts), can't disambiguate, and returns the
    contract un-mutated. The resulting order has empty lastTradeDateOrContractMonth
    + empty localSymbol + conId=0, which TWS rejects with Error 321
    "Please enter a local symbol or an expiry". Bug discovered when
    cuebanks/mamba/tori MYM orders cancelled on CBOT first thing 2026-05-18.

    This helper enumerates all matching contracts via `reqContractDetails`,
    filters out expiries within `min_days_to_expiry` (avoid roll-week
    drama), sorts by expiry, and returns the front qualified contract.

    Returns a fully-populated Contract (conId, localSymbol, expiry all set).
    Raises IBKRExecutionError if no contracts found.
    """
    from datetime import datetime, timedelta
    sym_u = symbol.upper()
    if exchange is None:
        # Default routing: CBOT for Dow/Russell, CME for everything else
        exchange = "CBOT" if sym_u in {"YM", "MYM", "RTY", "M2K"} else "CME"
    probe = Future(symbol, exchange=exchange, currency="USD")
    try:
        details = ib.reqContractDetails(probe)
    except Exception as exc:
        raise IBKRExecutionError(
            f"reqContractDetails failed for {symbol}@{exchange}: {exc}"
        ) from exc
    if not details:
        raise IBKRExecutionError(
            f"no contract details returned for {symbol}@{exchange}"
        )
    # Filter to expiries that aren't about to roll
    today = datetime.now()
    candidates = []
    for d in details:
        c = d.contract
        ltd = getattr(c, "lastTradeDateOrContractMonth", "")
        if not ltd:
            continue
        # Format: "YYYYMMDD" or "YYYYMM"
        try:
            if len(ltd) >= 8:
                exp = datetime.strptime(ltd[:8], "%Y%m%d")
            elif len(ltd) >= 6:
                # Month-only — assume end of month
                exp = datetime.strptime(ltd[:6] + "28", "%Y%m%d")
            else:
                continue
        except ValueError:
            continue
        days_out = (exp - today).days
        if days_out < min_days_to_expiry:
            continue
        candidates.append((exp, c))
    if not candidates:
        raise IBKRExecutionError(
            f"no front-month {symbol}@{exchange} contracts >={min_days_to_expiry}d out "
            f"(found {len(details)} total)"
        )
    candidates.sort(key=lambda x: x[0])
    front = candidates[0][1]
    log.info(
        f"qualify_front_month_future({symbol}@{exchange}): "
        f"selected {front.localSymbol} expiry={front.lastTradeDateOrContractMonth} "
        f"conId={front.conId}"
    )
    return front


def _dump_unfilled_forensics(trade, order_id: Optional[str]) -> None:
    """Persist a full forensic record of an unfilled order to disk.

    Background (2026-05-16): the MYM contract-routing bug (cuebanks/mamba/tori
    silently dead 22+ days) lived undetected because `_wait_for_fill`'s log
    only surfaced `reject_reason=Cancelled` with no error code or whyHeld.
    Even with the 5/13 trade.log capture (last 3 entries), TWS produced no
    useful message — the order just came back Cancelled with empty diagnostics.

    To catch the NEXT silent-failure class faster, dump every field of the
    trade object that might explain the failure to a JSONL forensics log.
    Each unfilled order gets one line. Grep this file when a strategy goes
    quiet — the answer is in here even if the live logger missed it.
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        repo = _Path(__file__).resolve().parents[1]
        forensics = repo / "argus_flow" / "logs" / "unfilled_orders.jsonl"
        forensics.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
        }
        # Contract details
        c = getattr(trade, "contract", None)
        if c is not None:
            record["contract"] = {
                "symbol": getattr(c, "symbol", None),
                "secType": getattr(c, "secType", None),
                "exchange": getattr(c, "exchange", None),
                "primaryExchange": getattr(c, "primaryExchange", None),
                "currency": getattr(c, "currency", None),
                "localSymbol": getattr(c, "localSymbol", None),
                "lastTradeDateOrContractMonth": getattr(c, "lastTradeDateOrContractMonth", None),
                "conId": getattr(c, "conId", None),
            }
        # Order details
        o = getattr(trade, "order", None)
        if o is not None:
            record["order"] = {
                "action": getattr(o, "action", None),
                "orderType": getattr(o, "orderType", None),
                "totalQuantity": getattr(o, "totalQuantity", None),
                "lmtPrice": getattr(o, "lmtPrice", None),
                "auxPrice": getattr(o, "auxPrice", None),
                "tif": getattr(o, "tif", None),
                "outsideRth": getattr(o, "outsideRth", None),
                "account": getattr(o, "account", None),
            }
        # All orderStatus fields (every one TWS sets)
        os_obj = getattr(trade, "orderStatus", None)
        if os_obj is not None:
            record["orderStatus"] = {
                "status": getattr(os_obj, "status", None),
                "filled": getattr(os_obj, "filled", None),
                "remaining": getattr(os_obj, "remaining", None),
                "avgFillPrice": getattr(os_obj, "avgFillPrice", None),
                "permId": getattr(os_obj, "permId", None),
                "parentId": getattr(os_obj, "parentId", None),
                "lastFillPrice": getattr(os_obj, "lastFillPrice", None),
                "clientId": getattr(os_obj, "clientId", None),
                "whyHeld": getattr(os_obj, "whyHeld", None),
                "mktCapPrice": getattr(os_obj, "mktCapPrice", None),
            }
        # FULL trade.log (every entry, not just last 3)
        log_entries = []
        for entry in (getattr(trade, "log", None) or []):
            log_entries.append({
                "time": entry.time.isoformat() if hasattr(getattr(entry, "time", None), "isoformat") else str(getattr(entry, "time", "")),
                "status": getattr(entry, "status", None),
                "message": str(getattr(entry, "message", "") or "")[:200],
                "errorCode": int(getattr(entry, "errorCode", 0) or 0),
            })
        record["log"] = log_entries

        with open(forensics, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, default=str) + "\n")
    except Exception as exc:
        # Forensics dump must never break the order path
        log.warning(f"_dump_unfilled_forensics failed (non-fatal): {exc}")


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

    # 2026-05-13: capture the last few TradeLogEntry messages so the
    # actual TWS error (e.g. "Error 10349: Order TIF was set to DAY based
    # on order preset") surfaces in the reject_reason instead of just
    # "Cancelled". Same pattern as the silent-gate logging fix —
    # diagnose before fixing.
    try:
        log_msgs: list[str] = []
        for entry in (getattr(trade, "log", None) or [])[-3:]:
            err_code = int(getattr(entry, "errorCode", 0) or 0)
            msg = str(getattr(entry, "message", "") or "").strip()
            if err_code or msg:
                log_msgs.append(f"err={err_code}:{msg[:120]}" if err_code else msg[:140])
        if log_msgs:
            reject = f"{reject} | {' / '.join(log_msgs)}"
    except Exception:
        pass

    # 2026-05-16: dump full forensic record so the NEXT silent-failure class
    # surfaces in minutes, not weeks. The MYM bug was invisible because the
    # live logger only had the last 3 trade.log entries — and TWS produced
    # no useful messages. This writes everything to a JSONL file for grep.
    _dump_unfilled_forensics(trade, order_id)

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
    strategy_label: Optional[str] = None,
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
    # Accept either case ('long'/'short' or 'LONG'/'SHORT'). cuebanks/mamba/tori
    # all emit uppercase from their signal-detection layer; multi_orb/fomc_drift
    # emit lowercase. Pre-2026-05-12 the uppercase callers were silently failing
    # with IBKRExecutionError caught + swallowed inside signal_executor's
    # try/except, with the error message going to a logger that wasn't attached
    # to those runners' file handlers (see forge/logging_setup.py).
    direction = (direction or "").lower()
    if direction not in ("long", "short"):
        raise IBKRExecutionError(f"bad direction: {direction!r}")
    if size <= 0:
        raise IBKRExecutionError(f"size must be positive, got {size}")

    # Real-money boundary — no-op for paper connections. For real connections,
    # validates strategy is allowlisted, allowlist is internally consistent,
    # and notional is within cap. Any violation rejects the order before any
    # broker round-trip. See helio/real_money.py and project_real_money_boundary.md.
    try:
        from helio.real_money import (
            enforce_real_money_boundary,
            AccountBoundaryViolationError,
        )
        est_notional_for_boundary = None
        if est_entry_px is not None and est_entry_px > 0:
            est_notional_for_boundary = float(size) * float(est_entry_px)
        enforce_real_money_boundary(
            ib,
            strategy_label=strategy_label or "unknown",
            notional_usd=est_notional_for_boundary,
        )
    except AccountBoundaryViolationError as exc:
        log.error(
            f"REAL_MONEY_BOUNDARY: refusing {direction} {size} {contract.symbol} "
            f"strategy={strategy_label} — {exc}"
        )
        return BracketResult(entry=FillResult(
            filled=False,
            reject_reason=f"real_money_boundary:{str(exc)[:80]}",
        ))

    # Killed-strategy runtime invariant (Codex audit 2026-05-18 X6).
    # The static-config test guarantees killed strategies have factor=0.0
    # and no_restart=True. This is the runtime fail-safe: even if the
    # config sync got out of step (or an operator hand-started a killed
    # runner), the executor refuses entry. Belt-and-suspenders against
    # resurrecting a thesis-exhausted strategy.
    if strategy_label:
        try:
            from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
            if strategy_label in KILLED_STRATEGY_CUTOFFS:
                cutoff = KILLED_STRATEGY_CUTOFFS[strategy_label]
                log.error(
                    f"KILLED_STRATEGY: refusing entry {direction} {size} "
                    f"{contract.symbol} — {strategy_label} was killed on {cutoff}. "
                    f"Remove from KILLED_STRATEGY_CUTOFFS only with operator approval."
                )
                return BracketResult(entry=FillResult(
                    filled=False,
                    reject_reason=f"killed_strategy:{strategy_label}:{cutoff}",
                ))
        except ImportError:
            # If the kill registry can't be loaded, fail closed.
            log.error(f"KILL_REGISTRY_UNREADABLE: refusing entry {strategy_label}")
            return BracketResult(entry=FillResult(
                filled=False,
                reject_reason="kill_registry_unreadable",
            ))

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
        # Fail closed: a query failure here means we cannot tell whether
        # the broker already has a position. Refusing avoids accidental
        # doubling up. Previously this logged and continued, which let
        # entries through when the broker query itself was the bug
        # (Codex audit 2026-05-18 X5 — guards must fail closed).
        log.error(f"pre-entry broker query failed, REFUSING entry: {exc}")
        return BracketResult(entry=FillResult(
            filled=False,
            reject_reason=f"broker_query_failed:{type(exc).__name__}",
        ))

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
        # 2026-05-12: futures contracts have a multiplier (MYM=$0.50/pt,
        # MNQ=$2/pt, etc.). Without it, `size × price` for 2 MYM at index
        # value 49,862 reports $99K notional when the actual USD exposure
        # is $49K. That false-positive triggered the 50%-of-NetLiq guard
        # and silently dropped every cuebanks signal. For stocks/ETFs/FX,
        # multiplier=1.0 and the math is unchanged.
        est_notional = float(size) * float(est_entry_px) * _futures_multiplier(contract.symbol)

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
            oversize_threshold = _oversize_threshold_usd(contract.symbol, anchor)
            if anchor > 0 and est_notional > oversize_threshold:
                threshold_pct = (oversize_threshold / anchor) * 100.0 if anchor > 0 else 0.0
                log.error(
                    f"OVERSIZED_ORDER_REJECTED: {direction} {size} "
                    f"{contract.symbol} notional=${est_notional:,.0f} > "
                    f"{threshold_pct:.0f}% of NetLiq=${anchor:,.0f} "
                    f"({est_notional/anchor*100:.0f}%). "
                    f"Sizing-layer likely buggy; refusing to submit."
                )
                return BracketResult(entry=FillResult(
                    filled=False,
                    reject_reason=f"oversized_order:{est_notional:.0f}>{oversize_threshold:.0f}"
                ))
        except Exception as exc:
            # Fail closed: if the size-cap check itself raises (e.g., anchor
            # reader broken), we cannot evaluate whether this is a phantom
            # oversized order. Refuse rather than ship a potentially huge
            # entry. Codex audit 2026-05-18 X5 — guards must fail closed.
            log.error(f"hard size cap check failed, REFUSING entry: {exc}")
            return BracketResult(entry=FillResult(
                filled=False,
                reject_reason=f"size_cap_check_failed:{type(exc).__name__}",
            ))

        try:
            from helio.cluster_exposure import would_breach_cluster_cap
            breach = would_breach_cluster_cap(
                contract.symbol, direction, est_notional,
                strategy_label=strategy_label,
            )
            if breach:
                log.warning(
                    f"CLUSTER_CAP_BREACH: {breach} would exceed cap on "
                    f"{direction} {size} {contract.symbol} (~${est_notional:,.0f} notional). "
                    f"Refusing entry."
                )
                return BracketResult(entry=FillResult(filled=False, reject_reason=f"cluster_cap_breach:{breach}"))
        except Exception as exc:
            # Fail closed: cluster-cap exception means the exposure ledger
            # is unreadable. Without it we cannot tell whether this trade
            # would push the FX_USD / EQUITY_BETA / METALS / etc. clusters
            # past their caps. Refuse rather than risk a cluster-blowup.
            # Codex audit 2026-05-18 X5 — guards must fail closed.
            log.error(f"cluster cap check failed, REFUSING entry: {exc}")
            return BracketResult(entry=FillResult(
                filled=False,
                reject_reason=f"cluster_cap_check_failed:{type(exc).__name__}",
            ))

    entry_action = "BUY" if direction == "long" else "SELL"
    close_action = "SELL" if direction == "long" else "BUY"

    # 1. Entry market order
    entry_order = MarketOrder(entry_action, abs(size))
    if account:
        entry_order.account = account
    entry_trade = ib.placeOrder(contract, entry_order)

    # 2026-05-07 audit: persistent pending-fills queue. Write the order
    # to disk BEFORE waiting for fill so it survives any crash during the
    # fill-wait window. Cleared after confirmed fill below.
    _pending_order_id = str(getattr(entry_trade.order, "orderId", "") or "")
    try:
        from helio.pending_fills import write_pending
        if _pending_order_id and est_entry_px is not None:
            write_pending(
                client_id=int(getattr(ib, "client", None) and ib.client.clientId or 0),
                order_id=_pending_order_id,
                strategy=strategy_label or "unknown",
                symbol=contract.symbol,
                direction=direction,
                size=float(abs(size)),
                est_entry_px=float(est_entry_px),
                stop_px=float(stop_px),
                target_px=float(target_px),
            )
    except Exception as exc:
        log.warning(f"pending_fills write failed (non-fatal): {exc}")

    entry_fill = _wait_for_fill(ib, entry_trade, timeout_s=entry_fill_timeout_s)

    if not entry_fill.filled:
        log.error(
            f"ENTRY NOT FILLED: {entry_action} {size} {contract.symbol} "
            f"reason={entry_fill.reject_reason}"
        )
        # Clear the pending entry — broker rejected/cancelled, no orphan possible
        try:
            from helio.pending_fills import clear_pending
            if _pending_order_id:
                clear_pending(order_id=_pending_order_id)
        except Exception:
            pass
        return BracketResult(entry=entry_fill)

    # Fill confirmed — clear pending
    try:
        from helio.pending_fills import clear_pending
        if _pending_order_id:
            clear_pending(order_id=_pending_order_id)
    except Exception:
        pass

    log.info(
        f"ENTRY FILLED: {entry_action} {size} {contract.symbol} @ {entry_fill.fill_price:.5f} "
        f"orderId={entry_fill.order_id}"
    )

    # Dual-write an ENTRY row to canonical_fills so the fleet ledger captures
    # entries at submission time, not only on EXIT. Without this, post-mortem
    # reconciliation cannot distinguish "took the entry, exit is still open"
    # from "never entered" — and orphan detection has no entry-side anchor.
    # Also stamps lineage_id so the row can be attributed by intent (Codex
    # X3+X7) rather than only by symbol. Wrapped in try/except so a
    # canonical-log failure cannot break a live trade.
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill, make_lineage_id
        from datetime import datetime, timezone
        # session_id heuristic: use the IB client id + process pid so the
        # lineage namespace flips on every runner restart. Entry-order-id
        # comes from the just-filled order.
        try:
            import os as _os
            client_id = getattr(getattr(ib, "client", None), "clientId", None)
            session_id = f"c{client_id}p{_os.getpid()}" if client_id is not None else f"p{_os.getpid()}"
        except Exception:
            session_id = None
        lineage = make_lineage_id(
            strategy=strategy_label or "unknown",
            session_id=session_id,
            entry_order_id=str(entry_fill.order_id) if entry_fill.order_id else None,
        )
        write_fill_typed(Fill(
            strategy=strategy_label or "unknown",
            symbol=contract.symbol,
            direction=direction,
            side="ENTRY",
            entry_ts=datetime.now(timezone.utc).isoformat(),
            exit_ts=None,
            entry_px=float(entry_fill.fill_price),
            exit_px=None,
            size=float(abs(size)),
            risk_usd=None,
            pnl_usd=None,
            exit_reason=None,
            lineage_id=lineage,
        ))
    except Exception as exc:
        log.warning(f"canonical_fills ENTRY write failed (non-fatal): {exc}")

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


def _recover_close_fill_from_executions(
    ib: IB,
    contract: Contract,
    entry_direction: str,
    after_ts: Optional[float] = None,
) -> Optional[FillResult]:
    """Look up the most recent closing fill via reqExecutions.

    Used when the synchronous market-close wait times out but the order may
    actually have filled — TWS can report `Cancelled` while a separate fill
    still made it through, or the wait window expired while the fill was
    in-flight. Without this lookup, the runner records exit_px=None and the
    trade silently disappears from trades.csv (the gating `if exit_px is not
    None` filter drops it). Same recovery pattern as check_bracket_filled.

    Returns None if no matching execution found. Otherwise a FillResult with
    filled=True and fill_price/fill_ts populated from the broker's record.

    after_ts: optional unix timestamp; only fills newer than this count
    (defends against picking up the entry fill as the close).
    """
    ef = ExecutionFilter()
    ef.symbol = contract.symbol
    ef.secType = getattr(contract, "secType", "") or ""
    try:
        fills = ib.reqExecutions(ef)
    except Exception as exc:
        log.warning(f"reqExecutions failed for {contract.symbol}: {exc}")
        return None
    if not fills:
        return None
    close_side = "SLD" if entry_direction == "long" else "BOT"
    candidates = [f for f in fills if f.execution.side == close_side]
    if after_ts is not None:
        def _ts(f) -> float:
            t = f.time
            try:
                return t.timestamp() if hasattr(t, "timestamp") else 0.0
            except Exception:
                return 0.0
        candidates = [f for f in candidates if _ts(f) >= after_ts]
    if not candidates:
        return None
    latest = max(candidates, key=lambda f: f.time)
    return FillResult(
        filled=True,
        fill_price=float(latest.execution.price),
        fill_ts=latest.time.isoformat() if hasattr(latest.time, "isoformat") else str(latest.time),
        order_id=str(getattr(latest.execution, "orderId", "")) or None,
    )


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
    submit_ts = time.time()
    trade = ib.placeOrder(contract, order)
    fill = _wait_for_fill(ib, trade, timeout_s=timeout_s)
    if fill.filled:
        log.info(
            f"CLOSE FILLED: {close_action} {size} {contract.symbol} @ {fill.fill_price:.5f}"
        )
        return fill

    # 2026-05-13: _wait_for_fill timed out or reported Cancelled — try to
    # recover the actual fill from reqExecutions. TWS can report Cancelled
    # while a fill still goes through (timing race), and the wait window
    # can expire mid-fill. Without this fallback fomc_drift / tom_international
    # silently lose trades (audit S7).
    recovered = _recover_close_fill_from_executions(
        ib, contract, entry_direction=direction, after_ts=submit_ts - 5.0,
    )
    if recovered is not None and recovered.filled:
        log.warning(
            f"CLOSE RECOVERED via reqExecutions: {close_action} {size} "
            f"{contract.symbol} @ {recovered.fill_price:.5f} "
            f"(wait reported: {fill.reject_reason})"
        )
        return recovered

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
