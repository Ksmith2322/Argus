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

INSTRUMENTS = ["EEM", "EWJ", "VGK"]  # SPY excluded — it's the control in backtest, no alpha there
LOOP_INTERVAL_S = 3600
RISK_PCT = 0.005  # unproven tier

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

    # Exit leg: T+3 of current month, close any open trades
    if _is_tom_exit_day(today) and open_trades and _pre_close_hour(now):
        exited = []
        for instrument, trade in list(open_trades.items()):
            _append_signal("EXIT", instrument,
                           f"TOM exit day {today.isoformat()}, T+3 close",
                           extra={"entry_ts": trade.get("entry_ts")})
            exited.append(instrument)
        for inst in exited:
            open_trades.pop(inst, None)
        state["open_trades"] = open_trades
        state["trade_count"] = state.get("trade_count", 0) + len(exited)
        _save_state(state)
        result["action"] = "EXIT"
        result["reason"] = f"exited {len(exited)} positions on TOM T+3"
        _write_heartbeat("live_signal_only", open_trades)
        return result

    # Entry leg: T-2 of current month, open positions in all instruments
    if _is_tom_entry_day(today) and not open_trades and _pre_close_hour(now):
        anchor = _broker_anchor()
        if anchor is None:
            result["action"] = "SKIP"
            result["reason"] = "broker equity unavailable"
            _write_heartbeat("live_signal_only", None)
            return result
        # Risk across instruments — split across 3
        per_instrument_risk = (anchor * RISK_PCT) / len(INSTRUMENTS)
        for instrument in INSTRUMENTS:
            _append_signal("ENTRY_LONG", instrument,
                           f"TOM entry day {today.isoformat()}, T-2 close",
                           extra={"anchor_usd": anchor, "risk_usd": per_instrument_risk})
            open_trades[instrument] = {
                "entry_ts": now.isoformat(),
                "anchor_usd_at_entry": anchor,
                "risk_usd_at_entry": per_instrument_risk,
            }
        state["open_trades"] = open_trades
        _save_state(state)
        result["action"] = "ENTRY"
        result["reason"] = f"entered {len(INSTRUMENTS)} positions on TOM T-2 (anchor=${anchor:.0f}, risk=${per_instrument_risk:.2f} per)"
        _write_heartbeat("live_signal_only", open_trades)
        return result

    # Idle cases
    next_entry = _next_tom_entry(today)
    if open_trades:
        result["action"] = "HOLD"
        result["reason"] = f"{len(open_trades)} open positions, waiting for T+3 exit"
    else:
        result["action"] = "WAITING"
        result["reason"] = f"next TOM entry: {next_entry or 'none scheduled'}"
    _write_heartbeat("live_signal_only", open_trades if open_trades else None)
    return result


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
    args = parser.parse_args(argv)

    if args.loop:
        return main_loop()
    r = check_and_act()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
