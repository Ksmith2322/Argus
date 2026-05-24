"""forge.tom_spy — turn-of-month effect on SPY, live runner.

Daily-cadence runner. Wakes once per day during the US session window,
checks whether today is the TOM entry or exit day, submits a market
order if appropriate. No protective brackets — TOM is a calendar-based
trade with a deterministic exit date, not a price-stop-based trade.

ENTRY: at close of the 4th-to-last trading day of the month, BUY SPY
       sized to (anchor × allocation_factor × per_trade_fraction).
EXIT:  at close of the 3rd trading day of the next month, SELL.

Modes:
    --check       Print what action today would be (no trade)
    --evaluate    One cycle (entry or exit check)
    --loop        Continuous: wake daily 20 min before US close, evaluate

Allocation fail-closed: refuses to submit if allocation_factor=0 or read
fails. Logs an [ALLOC-GATE] event with reason.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from forge.logging_setup import setup_logging
from helio.fleet_sizing import (
    get_allocation_factor,
    get_sizing_anchor_usd,
    max_notional_usd,
)
from helio import ibkr_execution as ibkr

STRATEGY_LABEL = "forge_tom_spy"
IBKR_CLIENT_ID = 123   # next free in the forge range
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "tom_spy"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"

log = setup_logging("tom_spy")

NY_TZ = ZoneInfo("America/New_York")

PARAMS = {
    "version": "v1_20260524",
    "ticker": "SPY",
    "instrument_type": "etf",
    # Entry: close of 4th-to-last trading day of month.
    # Exit:  close of 3rd trading day of next month.
    "entry_offset": 4,
    "exit_offset": 3,
    # Per-trade capital fraction (of anchor × allocation_factor).
    # TOM holds ~7 days; full anchor allocation is safe.
    "per_trade_fraction": 1.0,
    # Eval cadence: 20 min before US equity close → 19:40 UTC during
    # standard time (15:40 ET); 20:40 UTC during DST.
    "eval_hour_utc": 19,
    "eval_minute_utc": 40,
}


# ── State / heartbeat / trade CSV ─────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "open_trade": None,   # None or {entry_ts, entry_px, qty, ...}
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


TRADE_FIELDS = [
    "ts", "side", "ticker", "px", "qty",
    "pnl_usd", "pnl_pct", "session_id", "config_version",
]


def _ensure_trade_csv() -> None:
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in TRADE_FIELDS}
        )


def _write_heartbeat(state: dict, action: str) -> None:
    try:
        from helio.strategy_common import git_sha as _git_sha
        git_sha = _git_sha(REPO)
    except Exception:
        git_sha = "unknown"
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "tom_spy",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_action": action,
        "version": PARAMS["version"],
        "git_sha": git_sha,
    }, indent=2, default=str))


# ── Calendar logic ────────────────────────────────────────────────────

def _us_trading_days_in_month(year: int, month: int) -> list[date]:
    """All weekdays in (year, month). Approximation: ignores US holidays.
    For TOM purposes, missing 1-2 holidays per year shifts the entry/exit
    by one day in those months — caught by the broker rejecting the order
    if it lands on a market-closed day."""
    days: list[date] = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def is_tom_entry_day(today: date) -> bool:
    """True iff today is the Nth-to-last weekday of its month."""
    days = _us_trading_days_in_month(today.year, today.month)
    if len(days) <= PARAMS["entry_offset"]:
        return False
    target = days[-(PARAMS["entry_offset"] + 1)]
    return today == target


def is_tom_exit_day(today: date) -> bool:
    """True iff today is the Mth weekday of its month (1-indexed via
    PARAMS['exit_offset'] = 3 → 3rd weekday)."""
    days = _us_trading_days_in_month(today.year, today.month)
    if len(days) < PARAMS["exit_offset"]:
        return False
    target = days[PARAMS["exit_offset"] - 1]
    return today == target


# ── Execution helpers ─────────────────────────────────────────────────

def _submit_market(ib, contract, action: str, qty: int, *, est_px: float,
                    timeout_s: float = 30.0):
    """Thin wrapper around helio.ibkr_execution.submit_market_with_boundary.
    Kept as a per-strategy shim so call sites stay short. The actual
    real-money boundary + halt + flatten + market-open checks live in
    the central helper (static-safety-invariant compliant)."""
    fill = ibkr.submit_market_with_boundary(
        ib, contract, action, qty,
        strategy_label=STRATEGY_LABEL,
        est_px=est_px,
        timeout_s=timeout_s,
    )
    if fill is None or not fill.filled:
        return None
    return fill


def _write_canonical(side: str, ticker: str, qty: int, fill_px: float,
                      *, entry_px: float | None = None,
                      entry_ts: str | None = None,
                      pnl_usd: float | None = None) -> None:
    try:
        from helio.canonical_fills import write_fill
        write_fill(
            strategy=STRATEGY_LABEL,
            symbol=ticker,
            direction="long",
            side=side,
            entry_ts=entry_ts or (datetime.now(timezone.utc).isoformat()
                                    if side == "ENTRY" else None),
            exit_ts=(datetime.now(timezone.utc).isoformat()
                      if side == "EXIT" else None),
            entry_px=entry_px if entry_px is not None else fill_px,
            exit_px=fill_px if side == "EXIT" else None,
            size=int(qty),
            pnl_usd=(round(pnl_usd, 2) if pnl_usd is not None else None),
        )
    except Exception as exc:
        log.warning("canonical %s write failed (non-fatal): %s", side, exc)


# ── Core: one evaluation cycle ────────────────────────────────────────

def evaluate_once(force_action: str | None = None) -> dict:
    """One cycle. Returns a summary dict.

    force_action="entry"/"exit"/None — None means use today's calendar.
    Useful for backfill/testing."""
    summary: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "noop",
        "reason": "",
    }
    state = _load_state()
    today = datetime.now(NY_TZ).date()
    is_entry = (force_action == "entry") or (force_action is None
                                                and is_tom_entry_day(today))
    is_exit = (force_action == "exit") or (force_action is None
                                              and is_tom_exit_day(today))

    if not (is_entry or is_exit):
        summary["reason"] = (f"not a TOM action day "
                              f"(today={today.isoformat()})")
        _write_heartbeat(state, action="noop")
        log.info(summary["reason"])
        return summary

    if is_entry and state.get("open_trade"):
        summary["action"] = "skip_entry_position_already_open"
        summary["reason"] = "state.open_trade is not None"
        log.warning("ENTRY day but position already open — skipping")
        _write_heartbeat(state, action=summary["action"])
        return summary
    if is_exit and not state.get("open_trade"):
        summary["action"] = "skip_exit_no_position"
        summary["reason"] = "state.open_trade is None"
        log.warning("EXIT day but no position to close")
        _write_heartbeat(state, action=summary["action"])
        return summary

    # Allocation gate
    try:
        alloc = float(get_allocation_factor(STRATEGY_LABEL))
    except Exception as exc:
        log.error("allocation_factor read failed: %s — refusing trade", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_read_failed:{exc}"
        _write_heartbeat(state, action=summary["action"])
        return summary
    if alloc <= 0.0:
        log.info("[ALLOC-GATE] allocation_factor=%.3f — strategy deallocated", alloc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_factor={alloc}"
        _write_heartbeat(state, action=summary["action"])
        return summary

    if _SIGNAL_ONLY_MODE:
        action_word = "ENTRY" if is_entry else "EXIT"
        log.info("SIGNAL-ONLY: would %s SPY", action_word)
        summary["action"] = f"signal_only_{action_word.lower()}"
        summary["reason"] = "signal_only_mode"
        _write_heartbeat(state, action=summary["action"])
        return summary

    # Connect to IBKR
    try:
        ib = ibkr.connect_with_retry(IBKR_CLIENT_ID, max_attempts=3)
    except Exception as exc:
        log.error("IBKR connect failed: %s — skipping cycle", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"ibkr_connect:{exc}"
        _write_heartbeat(state, action=summary["action"])
        return summary

    try:
        contract = ibkr.make_contract(PARAMS["ticker"], PARAMS["instrument_type"])
        try:
            ib.qualifyContracts(contract)
        except Exception as exc:
            log.error("qualifyContracts(%s) failed: %s", PARAMS["ticker"], exc)
            summary["action"] = "blocked"
            summary["reason"] = f"qualify_failed:{exc}"
            return summary

        # Get latest price for sizing + safety
        try:
            ticker_data = ib.reqMktData(contract, snapshot=True)
            ib.sleep(2.0)
            est_px = float(ticker_data.last or ticker_data.close or 0.0)
        except Exception:
            est_px = 0.0

        if is_entry:
            anchor = get_sizing_anchor_usd()
            capital = anchor * alloc * PARAMS["per_trade_fraction"]
            cap_usd = max_notional_usd("etf", strategy_label=STRATEGY_LABEL) or 0.0
            if cap_usd > 0:
                capital = min(capital, cap_usd)
            if est_px <= 0:
                log.error("no price data for sizing — aborting entry")
                summary["action"] = "blocked"
                summary["reason"] = "no_price"
                return summary
            qty = int(capital // est_px)
            if qty < 1:
                log.warning("computed qty<1 (capital=%.2f / est_px=%.2f) — abort",
                              capital, est_px)
                summary["action"] = "blocked"
                summary["reason"] = "size_zero"
                return summary
            # Guard against doubling-up
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION %s qty=%.0f — abort", PARAMS["ticker"], existing)
                summary["action"] = "blocked"
                summary["reason"] = "broker_has_position"
                return summary
            fill = _submit_market(ib, contract, "BUY", qty, est_px=est_px)
            if fill is None:
                summary["action"] = "entry_failed"
                return summary
            state["open_trade"] = {
                "entry_ts": datetime.now(timezone.utc).isoformat(),
                "entry_px": float(fill.fill_price),
                "qty": int(qty),
                "alloc_factor": alloc,
            }
            state["trade_count"] = int(state.get("trade_count", 0)) + 1
            _append_trade({
                "ts": state["open_trade"]["entry_ts"],
                "side": "ENTRY",
                "ticker": PARAMS["ticker"],
                "px": round(fill.fill_price, 4),
                "qty": qty,
                "pnl_usd": "",
                "pnl_pct": "",
                "session_id": state.get("session_id", ""),
                "config_version": PARAMS["version"],
            })
            _write_canonical("ENTRY", PARAMS["ticker"], qty, fill.fill_price)
            summary["action"] = "entered"
            summary["entry_px"] = fill.fill_price
            summary["qty"] = qty

        elif is_exit:
            ot = state.get("open_trade") or {}
            qty = int(ot.get("qty") or 0)
            if qty <= 0:
                log.warning("EXIT but state.open_trade.qty <= 0")
                summary["action"] = "skip_exit_zero_qty"
                return summary
            fill = _submit_market(ib, contract, "SELL", qty, est_px=est_px or 1.0)
            if fill is None:
                summary["action"] = "exit_failed"
                return summary
            entry_px = float(ot.get("entry_px") or 0.0)
            pnl_usd = (fill.fill_price - entry_px) * qty
            pnl_pct = ((fill.fill_price / entry_px - 1.0) * 100.0) \
                if entry_px > 0 else 0.0
            _append_trade({
                "ts": datetime.now(timezone.utc).isoformat(),
                "side": "EXIT",
                "ticker": PARAMS["ticker"],
                "px": round(fill.fill_price, 4),
                "qty": qty,
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 4),
                "session_id": state.get("session_id", ""),
                "config_version": PARAMS["version"],
            })
            _write_canonical("EXIT", PARAMS["ticker"], qty, fill.fill_price,
                              entry_px=entry_px,
                              entry_ts=ot.get("entry_ts"),
                              pnl_usd=pnl_usd)
            state["open_trade"] = None
            state["trade_count"] = int(state.get("trade_count", 0)) + 1
            summary["action"] = "exited"
            summary["exit_px"] = fill.fill_price
            summary["pnl_usd"] = pnl_usd
            summary["pnl_pct"] = pnl_pct

        _save_state(state)
        _write_heartbeat(state, action=summary["action"])
        return summary
    finally:
        try:
            ibkr.disconnect(ib)
        except Exception:
            pass


# ── Loop mode ────────────────────────────────────────────────────────

def loop_mode() -> None:
    log.info("tom_spy --loop started: daily wake at %02d:%02d UTC",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=PARAMS["eval_hour_utc"],
                                 minute=PARAMS["eval_minute_utc"],
                                 second=15, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            sleep_s = max(5.0, (fire - now).total_seconds())
            log.info("tom_spy next wake at %s UTC (sleep %.0fs)",
                     fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as exc:
                log.error("evaluate_once failed: %s", exc, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("tom_spy loop stopped by user")
            return


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                          help="Print what today's action would be (no trade)")
    parser.add_argument("--evaluate", action="store_true",
                          help="Single cycle: entry/exit/noop based on today's calendar")
    parser.add_argument("--loop", action="store_true",
                          help="Daemon: daily wake at eval_hour_utc:eval_minute_utc")
    parser.add_argument("--force-entry", action="store_true",
                          help="With --evaluate: bypass calendar check; treat as entry day")
    parser.add_argument("--force-exit", action="store_true",
                          help="With --evaluate: bypass calendar check; treat as exit day")
    parser.add_argument("--signal-only", action="store_true",
                          help="Skip IBKR submission; log what would have happened")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)
    if _SIGNAL_ONLY_MODE:
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.check:
        today = datetime.now(NY_TZ).date()
        print(json.dumps({
            "today_ny": today.isoformat(),
            "is_tom_entry_day": is_tom_entry_day(today),
            "is_tom_exit_day": is_tom_exit_day(today),
            "entry_offset": PARAMS["entry_offset"],
            "exit_offset": PARAMS["exit_offset"],
            "version": PARAMS["version"],
        }, indent=2, default=str))
        return 0

    if args.evaluate:
        force = ("entry" if args.force_entry
                  else "exit" if args.force_exit
                  else None)
        summary = evaluate_once(force_action=force)
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.loop:
        loop_mode()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
