"""forge.gld_jan_hold -- GLD January effect, live runner.

Calendar-based runner. Wakes daily; on the first weekday of
January, BUYS GLD at close. On the last weekday of January, SELLS
at close. One trade per year.

Backtest: 20y on GLD, n=20, PF=4.23, CI lower 1.46, WR=70%,
avg +2.84% per trade. Top single-month survivor for gold in the
2026-05-26 extension matrix sweep
(ops/reports/system_audit/extension_matrix_sweep.md). Indian
wedding-season + post-year-end portfolio rebalancing flows are
the documented drivers. Orthogonal to equity + bond strategies.

Modes:
    --check       Print today's intended action (no trade)
    --evaluate    One cycle (entry / exit / noop)
    --loop        Daemon: wake daily ~20 min before US close
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

from forge.logging_setup import setup_logging
from helio.fleet_sizing import (
    get_allocation_factor,
    get_sizing_anchor_usd,
    max_notional_usd,
)
from helio import ibkr_execution as ibkr

STRATEGY_LABEL = "forge_gld_jan_hold"
IBKR_CLIENT_ID = 134
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "gld_jan_hold"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"

log = setup_logging("gld_jan_hold")

NY_TZ = ZoneInfo("America/New_York")

PARAMS = {
    "version": "v1_20260526",
    "ticker": "GLD",
    "instrument_type": "etf",
    "target_month": 1,            # January
    "per_trade_fraction": 1.0,
    "eval_hour_utc": 19,
    "eval_minute_utc": 50,         # offset from ief_jul_hold / nov_spy / tom_spy
}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "open_trade": None,
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
        "system": "gld_jan_hold",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_action": action,
        "version": PARAMS["version"],
        "git_sha": git_sha,
    }, indent=2, default=str))


def _us_weekdays_in_month(year: int, month: int) -> list[date]:
    days: list[date] = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def is_entry_day(today: date) -> bool:
    if today.month != PARAMS["target_month"]:
        return False
    weekdays = _us_weekdays_in_month(today.year, PARAMS["target_month"])
    return bool(weekdays) and today == weekdays[0]


def is_exit_day(today: date) -> bool:
    if today.month != PARAMS["target_month"]:
        return False
    weekdays = _us_weekdays_in_month(today.year, PARAMS["target_month"])
    return bool(weekdays) and today == weekdays[-1]


def _submit_market(ib, contract, action: str, qty: int, *, est_px: float,
                    timeout_s: float = 30.0):
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


def evaluate_once(force_action: str | None = None) -> dict:
    summary: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "noop",
        "reason": "",
    }
    state = _load_state()
    today = datetime.now(NY_TZ).date()
    is_entry = (force_action == "entry") or (force_action is None
                                                and is_entry_day(today))
    is_exit = (force_action == "exit") or (force_action is None
                                              and is_exit_day(today))

    if not (is_entry or is_exit):
        summary["reason"] = (f"not a gld_jan action day "
                              f"(today={today.isoformat()})")
        _write_heartbeat(state, action="noop")
        log.info(summary["reason"])
        return summary

    if is_entry and state.get("open_trade"):
        summary["action"] = "skip_entry_position_already_open"
        summary["reason"] = "state.open_trade is not None"
        log.warning("ENTRY day but position already open -- skipping")
        _write_heartbeat(state, action=summary["action"])
        return summary
    if is_exit and not state.get("open_trade"):
        summary["action"] = "skip_exit_no_position"
        summary["reason"] = "state.open_trade is None"
        log.warning("EXIT day but no position to close")
        _write_heartbeat(state, action=summary["action"])
        return summary

    try:
        alloc = float(get_allocation_factor(STRATEGY_LABEL))
    except Exception as exc:
        log.error("allocation_factor read failed: %s -- refusing trade", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_read_failed:{exc}"
        _write_heartbeat(state, action=summary["action"])
        return summary
    if alloc <= 0.0:
        log.info("[ALLOC-GATE] allocation_factor=%.3f -- deallocated", alloc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_factor={alloc}"
        _write_heartbeat(state, action=summary["action"])
        return summary

    if _SIGNAL_ONLY_MODE:
        action_word = "ENTRY" if is_entry else "EXIT"
        log.info("SIGNAL-ONLY: would %s GLD", action_word)
        summary["action"] = f"signal_only_{action_word.lower()}"
        summary["reason"] = "signal_only_mode"
        _write_heartbeat(state, action=summary["action"])
        return summary

    try:
        ib = ibkr.connect_with_retry(IBKR_CLIENT_ID, max_attempts=3)
    except Exception as exc:
        log.error("IBKR connect failed: %s -- skipping cycle", exc)
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
                log.error("no price for sizing -- aborting entry")
                summary["action"] = "blocked"
                summary["reason"] = "no_price"
                return summary
            qty = int(capital // est_px)
            if qty < 1:
                log.warning("qty<1 (capital=%.2f / est_px=%.2f)", capital, est_px)
                summary["action"] = "blocked"
                summary["reason"] = "size_zero"
                return summary
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION %s qty=%.0f -- abort",
                              PARAMS["ticker"], existing)
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
                "ts": state["open_trade"]["entry_ts"], "side": "ENTRY",
                "ticker": PARAMS["ticker"], "px": round(fill.fill_price, 4),
                "qty": qty, "pnl_usd": "", "pnl_pct": "",
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
                "ts": datetime.now(timezone.utc).isoformat(), "side": "EXIT",
                "ticker": PARAMS["ticker"], "px": round(fill.fill_price, 4),
                "qty": qty, "pnl_usd": round(pnl_usd, 2),
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


def loop_mode() -> None:
    log.info("gld_jan_hold --loop started: daily wake at %02d:%02d UTC",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    try:
        _write_heartbeat(_load_state(), action="boot")
    except Exception as exc:
        log.warning("boot heartbeat write failed: %s", exc)
    while True:
        try:
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=PARAMS["eval_hour_utc"],
                                 minute=PARAMS["eval_minute_utc"],
                                 second=15, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            sleep_s = max(5.0, (fire - now).total_seconds())
            log.info("gld_jan_hold next wake at %s UTC (sleep %.0fs)",
                     fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as exc:
                log.error("evaluate_once failed: %s", exc, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("gld_jan_hold loop stopped by user")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--force-entry", action="store_true")
    parser.add_argument("--force-exit", action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)
    if _SIGNAL_ONLY_MODE:
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.check:
        today = datetime.now(NY_TZ).date()
        print(json.dumps({
            "today_ny": today.isoformat(),
            "target_month": PARAMS["target_month"],
            "is_entry_day": is_entry_day(today),
            "is_exit_day": is_exit_day(today),
            "version": PARAMS["version"],
        }, indent=2, default=str))
        return 0

    if args.evaluate or args.loop:
        from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
        try:
            with acquire_runner_lock(STRATEGY_LABEL):
                if args.evaluate:
                    force = ("entry" if args.force_entry
                              else "exit" if args.force_exit
                              else None)
                    summary = evaluate_once(force_action=force)
                    print(json.dumps(summary, indent=2, default=str))
                    return 0
                loop_mode()
                return 0
        except RunnerAlreadyRunning as exc:
            log.error("REFUSING_DUPLICATE_RUNNER: %s", exc)
            return 75

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
