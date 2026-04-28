"""Signal-driven IBKR executor for runners that emit signals as dicts.

Use case: a runner scans the market periodically and emits "BUY/SELL" signals
with entry/stop/target levels. This module turns those signals into managed
positions on the IBKR paper account, then keeps state across cycles so the
next call can detect bracket fills and time-stops.

Wraps `helio.ibkr_execution` with a higher-level state machine designed for
runners that don't track positions themselves (mamba/tori/cuebanks/apollo/hermes/
titan/ares — all signal-only by original design).

State shape:
  {
    "open_trades": {
      "<symbol>": {
        "direction": "long"|"short",
        "size": int,
        "entry_px": float,
        "stop_px": float,
        "target_px": float,
        "entry_ts": str,
        "stop_order_id": str|None,
        "target_order_id": str|None,
        "execution_venue": "ibkr_paper"|"signal_only",
        "instrument_type": str,
        "max_hold_bars": int|None,
        "bars_held": int,
      }
    },
    "trade_count": int,
  }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from helio import ibkr_execution as ibkr

log = logging.getLogger(__name__)


@dataclass
class SignalEntry:
    """Input: a single entry signal to be submitted as a real order."""
    symbol: str
    direction: str       # "long" or "short"
    size: float          # shares / contracts / fx units
    stop_px: float
    target_px: float
    instrument_type: str = "etf"   # "etf" / "stock" / "fx" / "future" / "micro_future"
    price_decimals: int = 2
    max_hold_bars: Optional[int] = None  # None = no time stop


def submit_signal(state: dict, ib, signal: SignalEntry) -> bool:
    """Submit a signal as a real order. Updates state["open_trades"][symbol].

    Returns True if entry filled successfully, False otherwise.
    Caller is responsible for state persistence after this call.
    """
    state.setdefault("open_trades", {})
    if signal.symbol in state["open_trades"]:
        log.warning("signal %s skipped — already have an open trade for %s",
                    signal.direction, signal.symbol)
        return False
    if signal.size <= 0:
        log.warning("signal %s skipped — non-positive size %s", signal.symbol, signal.size)
        return False

    entry_px = None
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None:
        contract = ibkr.make_contract(signal.symbol, signal.instrument_type)
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: %s qty=%s, skipping signal", signal.symbol, existing)
                return False
            result = ibkr.submit_bracket(
                ib, contract, direction=signal.direction, size=signal.size,
                stop_px=signal.stop_px, target_px=signal.target_px,
                price_decimals=signal.price_decimals,
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED %s: %s", signal.symbol, result.entry.reject_reason)
                return False
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            execution_venue = "ibkr_paper"
            log.info("REAL_ENTRY %s %s %s @ %.5f size=%s stop=%.5f target=%.5f",
                     execution_venue, signal.direction, signal.symbol, entry_px, signal.size,
                     signal.stop_px, signal.target_px)
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION %s: %s", signal.symbol, exc, exc_info=True)
            return False
    else:
        # Signal-only: record the planned values
        entry_px = (signal.stop_px + signal.target_px) / 2  # midpoint as placeholder

    state["open_trades"][signal.symbol] = {
        "direction": signal.direction,
        "size": signal.size,
        "entry_px": entry_px,
        "stop_px": signal.stop_px,
        "target_px": signal.target_px,
        "entry_ts": datetime.now(timezone.utc).isoformat(),
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
        "execution_venue": execution_venue,
        "instrument_type": signal.instrument_type,
        "price_decimals": signal.price_decimals,
        "max_hold_bars": signal.max_hold_bars,
        "bars_held": 0,
    }
    return True


def check_open_positions(state: dict, ib) -> list[dict]:
    """For each open trade in state, check if its bracket filled or time-stop.

    Mutates state — closes filled trades and increments bars_held on others.
    Returns list of closed-trade summaries: {symbol, fill_price, fill_ts,
    reason, pnl_usd}.
    """
    closed: list[dict] = []
    open_trades = state.get("open_trades", {}) or {}

    for symbol, ot in list(open_trades.items()):
        execution_venue = ot.get("execution_venue", "signal_only")
        if execution_venue != "ibkr_paper" or ib is None:
            # Signal-only path: increment bars and close on time-stop only
            ot["bars_held"] = ot.get("bars_held", 0) + 1
            if ot.get("max_hold_bars") and ot["bars_held"] >= ot["max_hold_bars"]:
                # No real fill — mark as time-closed using midpoint estimate
                exit_px = ot.get("entry_px") or 0
                closed.append({
                    "symbol": symbol, "fill_price": exit_px,
                    "fill_ts": datetime.now(timezone.utc).isoformat(),
                    "reason": "time", "pnl_usd": 0,
                    "size": ot.get("size", 0), "direction": ot.get("direction"),
                    "entry_px": ot.get("entry_px"), "entry_ts": ot.get("entry_ts"),
                })
                del open_trades[symbol]
            continue

        # Real-execution path: query IBKR for bracket fills
        contract = ibkr.make_contract(symbol, ot["instrument_type"])
        try:
            ib.qualifyContracts(contract)
            outcome = ibkr.check_bracket_filled(
                ib, contract, ot.get("stop_order_id"), ot.get("target_order_id"),
                entry_direction=ot.get("direction"),
                entry_size=ot.get("position_size") or ot.get("size"),
                stop_px=ot.get("stop_px"),
                target_px=ot.get("target_px"),
            )
        except Exception as exc:
            log.error("bracket check failed %s: %s", symbol, exc)
            outcome = None

        if outcome is not None:
            # Bracket filled — record close
            entry_px = ot["entry_px"]
            exit_px = outcome["fill_price"]
            size = ot["size"]
            pnl_usd = (exit_px - entry_px) * size if ot["direction"] == "long" else (entry_px - exit_px) * size
            closed.append({
                "symbol": symbol, "fill_price": exit_px,
                "fill_ts": outcome["fill_ts"], "reason": outcome["reason"],
                "pnl_usd": pnl_usd, "size": size, "direction": ot["direction"],
                "entry_px": entry_px, "entry_ts": ot.get("entry_ts"),
            })
            del open_trades[symbol]
            continue

        # Still open — bump bars + check time stop
        ot["bars_held"] = ot.get("bars_held", 0) + 1
        if ot.get("max_hold_bars") and ot["bars_held"] >= ot["max_hold_bars"]:
            try:
                broker_qty = ibkr.query_position(ib, contract)
            except Exception:
                broker_qty = 0
            if broker_qty != 0:
                fill = ibkr.close_position_market(
                    ib, contract, direction=ot["direction"], size=abs(broker_qty),
                    stop_order_id=ot.get("stop_order_id"),
                    target_order_id=ot.get("target_order_id"),
                )
                if fill.filled:
                    entry_px = ot["entry_px"]
                    exit_px = fill.fill_price
                    pnl_usd = (exit_px - entry_px) * ot["size"] if ot["direction"] == "long" else (entry_px - exit_px) * ot["size"]
                    closed.append({
                        "symbol": symbol, "fill_price": exit_px,
                        "fill_ts": fill.fill_ts, "reason": "time",
                        "pnl_usd": pnl_usd, "size": ot["size"],
                        "direction": ot["direction"],
                        "entry_px": entry_px, "entry_ts": ot.get("entry_ts"),
                    })
                    del open_trades[symbol]
                else:
                    log.error("TIME_STOP close failed %s: %s", symbol, fill.reject_reason)
            else:
                # Broker shows flat, brackets must have already filled; record cleanly
                closed.append({
                    "symbol": symbol, "fill_price": ot["entry_px"],
                    "fill_ts": datetime.now(timezone.utc).isoformat(),
                    "reason": "reconcile_flat", "pnl_usd": 0,
                    "size": ot["size"], "direction": ot["direction"],
                    "entry_px": ot["entry_px"], "entry_ts": ot.get("entry_ts"),
                })
                del open_trades[symbol]

    return closed
