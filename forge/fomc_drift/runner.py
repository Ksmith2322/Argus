"""forge.fomc_drift.runner — Live-check daemon for the FOMC pre-announcement drift.

Edge (per Lucca & Moench NY Fed 2015): ~80% of SPX equity premium accrues in
the 24h pre-FOMC announcement window. Backtest: PF 1.58 over 215 trades
(2000-2026). See `forge/fomc_drift.py` for the backtest + FOMC_DATES list.

This runner is a once-daily check (not a --loop eval every N minutes) because:
  - FOMC meetings happen ~8 times/year
  - Entry fires at prior day's close (end of UTC 19:00 trading day)
  - Exit fires on announcement day ~2pm ET (18:00 UTC during DST, 19:00 STD)

Writes signals.csv + heartbeat.json so dashboard + operational_maturity pick
it up. Reuses live broker equity via helio.fleet_sizing (no hardcoded anchor).
Paper mode only for now — switching to live = change --dry-run off.

Usage:
    python -m forge.fomc_drift.runner --loop               # daily daemon mode
    python -m forge.fomc_drift.runner --check              # one-shot eval
    python -m forge.fomc_drift.runner --check --verbose    # show reasoning
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Reuse FOMC_DATES from the backtest file. The original `forge/fomc_drift.py`
# was renamed to `forge/fomc_drift_backtest.py` on 2026-04-23 when this
# package was created (module-vs-package naming conflict).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from forge.fomc_drift_backtest import FOMC_DATES  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "fomc_drift"
SIGNALS_CSV = LOG_DIR / "signals.csv"
TRADES_CSV = LOG_DIR / "trades.csv"
STATE_JSON = LOG_DIR / "state.json"
HEARTBEAT_JSON = LOG_DIR / "heartbeat.json"

# Parse FOMC_DATES once into a set of date objects for fast lookup
_FOMC_DATES_SET = frozenset(datetime.strptime(d, "%Y-%m-%d").date() for d in FOMC_DATES)

LOOP_INTERVAL_S = 3600  # check once per hour (cheap; only acts at specific hours)
RISK_PCT = 0.005  # unproven tier — conservative first-pass

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr  # noqa: E402
IBKR_CLIENT_ID = 110
_SIGNAL_ONLY_MODE = False
# Event-driven sizing (no stops): use risk_pct × 100 as fraction of anchor.
# 0.005 × 100 = 0.5 → 50% of anchor in SPY for the 24hr FOMC hold.
# Capped at etf notional cap (1.0× anchor).
NOTIONAL_FRACTION_MULTIPLIER = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] fomc_drift | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("fomc_drift.runner")


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {"open_trade": None, "trade_count": 0}
    return {"open_trade": None, "trade_count": 0}


def _save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_JSON.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(mode: str, position: str = "FLAT", open_trade: dict | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "system": "fomc_drift",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "position": position,
        "next_fomc": _next_fomc_from(datetime.now(timezone.utc).date()),
        "open_trade": open_trade,
    }
    HEARTBEAT_JSON.write_text(json.dumps(hb, indent=2, default=str), encoding="utf-8")


def _append_signal(action: str, reason: str, extra: dict | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not SIGNALS_CSV.exists()
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "reason": reason,
    }
    if extra:
        row.update({k: str(v) for k, v in extra.items()})
    with SIGNALS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _append_trade(trade: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not TRADES_CSV.exists()
    fieldnames = ["entry_ts", "exit_ts", "entry_px", "exit_px", "return_pct", "pnl_usd",
                  "position_size", "risk_usd", "fomc_date"]
    row = {k: trade.get(k, "") for k in fieldnames}
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _next_fomc_from(today) -> str | None:
    upcoming = sorted(d for d in _FOMC_DATES_SET if d >= today)
    return upcoming[0].isoformat() if upcoming else None


def _is_day_before_fomc(today) -> tuple[bool, str | None]:
    """Check if today is T-1 to an FOMC (trading-day awareness)."""
    tomorrow = today + timedelta(days=1)
    if tomorrow in _FOMC_DATES_SET:
        return True, tomorrow.isoformat()
    # Friday → Monday FOMC (weekend wrap)
    if today.weekday() == 4:  # Friday
        for offset in (2, 3):  # check Mon and Tue
            candidate = today + timedelta(days=offset)
            if candidate in _FOMC_DATES_SET:
                return True, candidate.isoformat()
    return False, None


def _broker_anchor() -> float | None:
    """Resolve live broker equity. Returns None if unavailable so caller can
    skip the evaluation (aligns with 2026-04-23 no-fallback architecture)."""
    try:
        from helio.fleet_sizing import get_sizing_anchor_usd, BrokerEquityUnavailableError
        try:
            return float(get_sizing_anchor_usd())
        except BrokerEquityUnavailableError as e:
            log.warning("broker equity unavailable; skipping eval this cycle: %s", e)
            return None
    except Exception as e:
        log.error("fleet_sizing import failed: %s", e)
        return None


def check_and_act(verbose: bool = False) -> dict:
    """One evaluation cycle. Returns action dict."""
    now = datetime.now(timezone.utc)
    today = now.date()
    state = _load_state()
    result: dict = {"ts": now.isoformat(), "action": "NONE", "reason": ""}

    open_trade = state.get("open_trade")
    is_fomc_today = today in _FOMC_DATES_SET

    # IBKR connection (per-cycle, brief). Falls through to signal-only on failure.
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
        if open_trade:
            # We have an open position — exit on FOMC announcement day.
            if is_fomc_today:
                result["action"] = "EXIT"
                result["reason"] = f"FOMC announcement day {today.isoformat()}, 2pm ET exit"

                exit_px = None
                pnl_usd = None
                if ib is not None and open_trade.get("execution_venue") == "ibkr_paper":
                    contract = ibkr.make_contract("SPY", "etf")
                    try:
                        ib.qualifyContracts(contract)
                        broker_qty = ibkr.query_position(ib, contract)
                    except Exception:
                        broker_qty = 0
                    if broker_qty > 0:
                        fill = ibkr.close_position_market(
                            ib, contract, direction="long", size=broker_qty,
                        )
                        if fill.filled:
                            exit_px = fill.fill_price
                            entry_px = open_trade.get("entry_px") or exit_px
                            pnl_usd = (exit_px - entry_px) * broker_qty
                            log.info("FOMC EXIT FILLED @ %.2f, pnl=$%.2f", exit_px, pnl_usd)
                _append_signal("EXIT", result["reason"], extra={
                    "entry_ts": open_trade.get("entry_ts"),
                    "entry_px": open_trade.get("entry_px"),
                    "exit_px": exit_px,
                    "pnl_usd": pnl_usd,
                    "fomc_date": today.isoformat(),
                })
                if exit_px is not None:
                    _append_trade({
                        "entry_ts": open_trade.get("entry_ts"),
                        "exit_ts": now.isoformat(),
                        "entry_px": open_trade.get("entry_px"),
                        "exit_px": exit_px,
                        "return_pct": ((exit_px / open_trade.get("entry_px", exit_px)) - 1) * 100 if open_trade.get("entry_px") else 0,
                        "pnl_usd": pnl_usd or 0,
                        "position_size": open_trade.get("position_size"),
                        "risk_usd": open_trade.get("risk_usd_at_entry"),
                        "fomc_date": today.isoformat(),
                    })
                state["open_trade"] = None
                state["trade_count"] = state.get("trade_count", 0) + 1
                _save_state(state)
                _write_heartbeat("ibkr_paper", "FLAT", None)
            else:
                result["action"] = "HOLD"
                result["reason"] = f"open trade from {open_trade.get('entry_ts')}, waiting for FOMC {open_trade.get('fomc_date')}"
                _write_heartbeat("ibkr_paper", "LONG_SPY", open_trade)
            return result

        # No open trade — check if today is T-1 to FOMC (entry condition)
        t_minus_1, fomc_date = _is_day_before_fomc(today)
        if t_minus_1 and is_pre_close_hour(now):
            anchor = _broker_anchor()
            if anchor is None:
                result["action"] = "SKIP"
                result["reason"] = "broker equity unavailable"
                _write_heartbeat("ibkr_paper", "FLAT", None)
                return result
            risk_usd = anchor * RISK_PCT
            # Event-driven sizing: 50% of anchor in SPY (capped at etf cap).
            target_notional = anchor * RISK_PCT * NOTIONAL_FRACTION_MULTIPLIER

            entry_px = None
            position_size = 0
            execution_venue = "signal_only"
            if ib is not None:
                contract = ibkr.make_contract("SPY", "etf")
                try:
                    ib.qualifyContracts(contract)
                    existing = ibkr.query_position(ib, contract)
                    if existing != 0:
                        log.warning("BROKER_HAS_POSITION: SPY qty=%s, skipping fomc entry", existing)
                        return {"ts": now.isoformat(), "action": "SKIP", "reason": "broker has SPY"}
                    # Get a quote-ish price by reading historical
                    bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="1 D",
                                                  barSizeSetting="1 hour", whatToShow="TRADES", useRTH=True)
                    if bars:
                        plan_entry = float(bars[-1].close)
                    else:
                        plan_entry = 500.0  # rough fallback; SPY ≈ $5xx
                    try:
                        from helio.fleet_sizing import max_notional_usd
                        cap = max_notional_usd("etf")
                        target_notional = min(target_notional, cap)
                    except Exception:
                        pass
                    shares = max(1, int(target_notional / max(plan_entry, 1e-6)))
                    # Submit market BUY (no bracket — this strategy holds for ~24hrs and
                    # exits on calendar event, not stop/target)
                    from ib_insync import MarketOrder
                    order = MarketOrder("BUY", shares)
                    trade = ib.placeOrder(contract, order)
                    fill = ibkr._wait_for_fill(ib, trade, timeout_s=15.0)
                    if fill.filled:
                        entry_px = fill.fill_price
                        position_size = shares
                        execution_venue = "ibkr_paper"
                        log.info("FOMC ENTRY FILLED: BUY %d SPY @ %.2f", shares, entry_px)
                    else:
                        log.error("FOMC ENTRY FAILED: %s", fill.reject_reason)
                except Exception as exc:
                    log.error("FOMC ENTRY EXCEPTION: %s", exc, exc_info=True)

            result["action"] = "ENTRY_LONG_SPY"
            result["reason"] = (f"T-1 to FOMC {fomc_date}, "
                                f"{execution_venue} entry @ {entry_px} ({position_size} sh, "
                                f"target notional ${target_notional:.0f})")
            _append_signal("ENTRY_LONG_SPY", result["reason"], extra={
                "fomc_date": fomc_date,
                "anchor_usd": anchor,
                "risk_usd": risk_usd,
                "entry_px": entry_px,
                "position_size": position_size,
                "execution_venue": execution_venue,
            })
            state["open_trade"] = {
                "entry_ts": now.isoformat(),
                "entry_px": entry_px,
                "position_size": position_size,
                "fomc_date": fomc_date,
                "anchor_usd_at_entry": anchor,
                "risk_usd_at_entry": risk_usd,
                "execution_venue": execution_venue,
            }
            _save_state(state)
            _write_heartbeat(execution_venue, "LONG_SPY", state["open_trade"])
        else:
            next_fomc = _next_fomc_from(today)
            result["action"] = "WAITING"
            result["reason"] = f"next FOMC: {next_fomc or 'none scheduled'}"
            _write_heartbeat("ibkr_paper", "FLAT", None)

        return result
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def is_pre_close_hour(now: datetime) -> bool:
    """Entry fires during the US-market close window: 19:00-20:30 UTC
    (3-4:30pm ET during DST). Conservative — catches both DST/STD."""
    return 19 <= now.hour < 21 or (now.hour == 18 and now.minute >= 30)


def main_loop() -> int:
    log.info("fomc_drift.runner starting (loop mode, %ds interval)", LOOP_INTERVAL_S)
    _write_heartbeat("live_signal_only", "FLAT", None)
    cycle = 0
    while True:
        cycle += 1
        try:
            result = check_and_act(verbose=False)
            log.info("cycle %d: %s — %s", cycle, result["action"], result["reason"])
        except Exception as e:
            log.error("cycle %d FAILED: %s", cycle, e)
        time.sleep(LOOP_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Run continuous daily-check daemon")
    parser.add_argument("--check", action="store_true", help="One-shot evaluation")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="Skip IBKR submission — log signals only")
    args = parser.parse_args(argv)
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.loop:
        return main_loop()
    if args.check:
        result = check_and_act(verbose=args.verbose)
        print(json.dumps(result, indent=2))
        return 0

    # Default: one-shot
    result = check_and_act(verbose=args.verbose)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
