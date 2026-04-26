"""forge.tom_international.runner — Live daily-check for Turn-of-Month effect.

Edge: International ETFs (EEM, EWJ, VGK) show persistent turn-of-month premium.
Entry at close of T-2 (2nd-to-last trading day), exit at close of T+3 (3rd
trading day of next month).

Backtest (see `forge/tom_international_backtest.py`):
- 1,249 trades, 2003-2026, PF 1.31, 56% WR, +27.5 bps/trade gross
- At realistic 12 bp round-trip friction: +15.5 bps net = +5.6% annual
- Study: `research/tom_international_slippage_20260423.py`

Runner pattern mirrors forge.fomc_drift.runner — daily hourly-cycle check
that emits ENTRY/EXIT signals on the right calendar days. Uses live broker
anchor (no hardcoded equity).

Usage:
    python -m forge.tom_international.runner --loop     # daemon mode
    python -m forge.tom_international.runner --check    # one-shot eval
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "tom_international"
SIGNALS_CSV = LOG_DIR / "signals.csv"
TRADES_CSV = LOG_DIR / "trades.csv"
STATE_JSON = LOG_DIR / "state.json"
HEARTBEAT_JSON = LOG_DIR / "heartbeat.json"

# 2026-04-26: expanded from [EEM, EWJ, VGK] (3) to add EFA, FXI, INDA (6 total).
# Per-event volume doubles; ~12 events/year = ~6 per month (was 3). SPY still excluded
# (backtest control). Note NOTIONAL_FRACTION_MULTIPLIER below was sized for 3 names —
# halved to 50 so total per-event notional stays ~30% × 6 ≈ 180% of single-instrument
# allocation, similar overall exposure to the prior 3-name × 30% = 90% setup.
INSTRUMENTS = ["EEM", "EWJ", "VGK", "EFA", "FXI", "INDA"]
LOOP_INTERVAL_S = 3600
RISK_PCT = 0.005  # unproven tier

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr  # noqa: E402
IBKR_CLIENT_ID = 111
_SIGNAL_ONLY_MODE = False
# Event-driven sizing: 2026-04-26 halved 100→50 to compensate for instrument expansion 3→6.
NOTIONAL_FRACTION_MULTIPLIER = 50  # × risk_pct = fraction per instrument

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] tom_international | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("tom_international.runner")


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {"open_trades": {}, "trade_count": 0}
    return {"open_trades": {}, "trade_count": 0}


def _save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_JSON.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(mode: str, open_trades: dict | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "system": "tom_international",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "position": "LONG" if open_trades else "FLAT",
        "instruments": INSTRUMENTS,
        "open_trade_count": len(open_trades) if open_trades else 0,
        "next_entry_day": _next_tom_entry(datetime.now(timezone.utc).date()),
    }
    HEARTBEAT_JSON.write_text(json.dumps(hb, indent=2, default=str), encoding="utf-8")


def _append_signal(action: str, instrument: str, reason: str, extra: dict | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not SIGNALS_CSV.exists()
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "instrument": instrument,
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


def _is_trading_day(d: date) -> bool:
    """Conservative trading-day check: Mon-Fri, not obvious holidays. A full
    exchange calendar would be better, but this is close enough for TOM timing
    (which cares about "2nd-to-last" relative to whatever the calendar shows)."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    # Very basic US market holiday skip — New Year's, Christmas (approximate)
    if (d.month, d.day) in {(1, 1), (12, 25)}:
        return False
    return True


def _last_n_trading_days_of_month(year: int, month: int, n: int = 2) -> list[date]:
    """Get the last N trading days of (year, month)."""
    # Walk backwards from the 31st (or month's last day)
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    days = []
    d = last_day
    while len(days) < n and d.month == month:
        if _is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _first_n_trading_days_of_month(year: int, month: int, n: int = 3) -> list[date]:
    """Get the first N trading days of (year, month)."""
    d = date(year, month, 1)
    days = []
    while len(days) < n:
        if _is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _is_tom_entry_day(today: date) -> bool:
    """Entry day = 2nd-to-last trading day of the current month."""
    last2 = _last_n_trading_days_of_month(today.year, today.month, 2)
    return len(last2) >= 2 and today == last2[0]  # first of last-2 = 2nd-to-last


def _is_tom_exit_day(today: date) -> bool:
    """Exit day = 3rd trading day of the current month (covering the T+3 of prior month)."""
    first3 = _first_n_trading_days_of_month(today.year, today.month, 3)
    return len(first3) >= 3 and today == first3[2]


def _next_tom_entry(today: date) -> str | None:
    """Find next entry day within 35 days from today."""
    for offset in range(0, 35):
        d = today + timedelta(days=offset)
        if _is_tom_entry_day(d):
            return d.isoformat()
    return None


def _pre_close_hour(now: datetime) -> bool:
    """US market close window: 19:45-20:15 UTC (DST) or 20:45-21:15 (STD).
    We're generous — 19:30-21:30 UTC covers both."""
    return (19 <= now.hour < 21) or (now.hour == 21 and now.minute < 30) or (now.hour == 19 and now.minute >= 30)


def _broker_anchor() -> float | None:
    try:
        from helio.fleet_sizing import get_sizing_anchor_usd, BrokerEquityUnavailableError
        try:
            return float(get_sizing_anchor_usd())
        except BrokerEquityUnavailableError as e:
            log.warning("broker equity unavailable; skipping this cycle: %s", e)
            return None
    except Exception as e:
        log.error("fleet_sizing import failed: %s", e)
        return None


def check_and_act() -> dict:
    """One evaluation cycle."""
    now = datetime.now(timezone.utc)
    today = now.date()
    state = _load_state()
    result = {"ts": now.isoformat(), "action": "NONE", "reason": ""}

    open_trades = state.get("open_trades", {})

    # Connect to IBKR (per-cycle, brief)
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
        # Exit leg: T+3 of current month, close any open trades
        if _is_tom_exit_day(today) and open_trades and _pre_close_hour(now):
            exited = []
            for instrument, trade in list(open_trades.items()):
                exit_px = None
                pnl_usd = None
                if ib is not None and trade.get("execution_venue") == "ibkr_paper":
                    contract = ibkr.make_contract(instrument, "etf")
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
                            entry_px = trade.get("entry_px") or exit_px
                            pnl_usd = (exit_px - entry_px) * broker_qty
                            log.info("TOM EXIT FILLED %s @ %.2f, pnl=$%.2f", instrument, exit_px, pnl_usd)
                _append_signal("EXIT", instrument,
                               f"TOM exit day {today.isoformat()}, T+3 close",
                               extra={"entry_ts": trade.get("entry_ts"),
                                      "exit_px": exit_px, "pnl_usd": pnl_usd})
                if exit_px is not None:
                    _append_trade({
                        "entry_ts": trade.get("entry_ts"),
                        "exit_ts": now.isoformat(),
                        "entry_px": trade.get("entry_px"),
                        "exit_px": exit_px,
                        "pnl_usd": pnl_usd or 0,
                        "instrument": instrument,
                        "position_size": trade.get("position_size"),
                    })
                exited.append(instrument)
            for inst in exited:
                open_trades.pop(inst, None)
            state["open_trades"] = open_trades
            state["trade_count"] = state.get("trade_count", 0) + len(exited)
            _save_state(state)
            result["action"] = "EXIT"
            result["reason"] = f"exited {len(exited)} positions on TOM T+3"
            _write_heartbeat("ibkr_paper", open_trades)
            return result

        # Entry leg: T-2 of current month, open positions in all instruments
        if _is_tom_entry_day(today) and not open_trades and _pre_close_hour(now):
            anchor = _broker_anchor()
            if anchor is None:
                result["action"] = "SKIP"
                result["reason"] = "broker equity unavailable"
                _write_heartbeat("ibkr_paper", None)
                return result
            per_instrument_risk = (anchor * RISK_PCT) / len(INSTRUMENTS)
            # Event-driven sizing: split (anchor × risk_pct × MULTIPLIER) across instruments.
            # 0.005 × 100 = 50% total → ~17% per instrument across 3 ETFs.
            target_notional_per = (anchor * RISK_PCT * NOTIONAL_FRACTION_MULTIPLIER) / len(INSTRUMENTS)
            try:
                from helio.fleet_sizing import max_notional_usd
                cap_per = max_notional_usd("etf") / len(INSTRUMENTS)
                target_notional_per = min(target_notional_per, cap_per)
            except Exception:
                pass

            from ib_insync import MarketOrder
            for instrument in INSTRUMENTS:
                entry_px = None
                position_size = 0
                execution_venue = "signal_only"
                if ib is not None:
                    contract = ibkr.make_contract(instrument, "etf")
                    try:
                        ib.qualifyContracts(contract)
                        existing = ibkr.query_position(ib, contract)
                        if existing != 0:
                            log.warning("BROKER_HAS_POSITION: %s qty=%s, skipping", instrument, existing)
                            continue
                        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="1 D",
                                                      barSizeSetting="1 hour", whatToShow="TRADES", useRTH=True)
                        plan_entry = float(bars[-1].close) if bars else 50.0
                        shares = max(1, int(target_notional_per / max(plan_entry, 1e-6)))
                        order = MarketOrder("BUY", shares)
                        trade_obj = ib.placeOrder(contract, order)
                        fill = ibkr._wait_for_fill(ib, trade_obj, timeout_s=15.0)
                        if fill.filled:
                            entry_px = fill.fill_price
                            position_size = shares
                            execution_venue = "ibkr_paper"
                            log.info("TOM ENTRY FILLED: BUY %d %s @ %.2f", shares, instrument, entry_px)
                        else:
                            log.error("TOM ENTRY FAILED %s: %s", instrument, fill.reject_reason)
                    except Exception as exc:
                        log.error("TOM ENTRY EXCEPTION %s: %s", instrument, exc, exc_info=True)
                _append_signal("ENTRY_LONG", instrument,
                               f"TOM entry day {today.isoformat()}, T-2 close",
                               extra={"anchor_usd": anchor, "risk_usd": per_instrument_risk,
                                      "entry_px": entry_px, "position_size": position_size,
                                      "execution_venue": execution_venue})
                open_trades[instrument] = {
                    "entry_ts": now.isoformat(),
                    "entry_px": entry_px,
                    "position_size": position_size,
                    "anchor_usd_at_entry": anchor,
                    "risk_usd_at_entry": per_instrument_risk,
                    "execution_venue": execution_venue,
                }
            state["open_trades"] = open_trades
            _save_state(state)
            result["action"] = "ENTRY"
            result["reason"] = f"entered {len(open_trades)} positions on TOM T-2 (anchor=${anchor:.0f})"
            _write_heartbeat("ibkr_paper", open_trades)
            return result

        # Idle cases
        next_entry = _next_tom_entry(today)
        if open_trades:
            result["action"] = "HOLD"
            result["reason"] = f"{len(open_trades)} open positions, waiting for T+3 exit"
        else:
            result["action"] = "WAITING"
            result["reason"] = f"next TOM entry: {next_entry or 'none scheduled'}"
        _write_heartbeat("ibkr_paper", open_trades if open_trades else None)
        return result
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def _append_trade(trade: dict) -> None:
    """Append a closed-trade row to trades.csv."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not TRADES_CSV.exists()
    fieldnames = ["entry_ts", "exit_ts", "entry_px", "exit_px", "pnl_usd",
                  "instrument", "position_size"]
    row = {k: trade.get(k, "") for k in fieldnames}
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main_loop() -> int:
    log.info("tom_international.runner starting (loop mode, %ds interval)", LOOP_INTERVAL_S)
    _write_heartbeat("live_signal_only", None)
    cycle = 0
    while True:
        cycle += 1
        try:
            r = check_and_act()
            log.info("cycle %d: %s — %s", cycle, r["action"], r["reason"])
        except Exception as e:
            log.error("cycle %d FAILED: %s", cycle, e)
        time.sleep(LOOP_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="Skip IBKR submission — log signals only")
    args = parser.parse_args(argv)
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.loop:
        return main_loop()
    r = check_and_act()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
