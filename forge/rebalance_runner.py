"""
S&P 500 Index Rebalance Paper Runner
======================================
Lightweight wrapper around index_rebalance.py scanning logic.

Checks once daily at 9:00 AM EST for active rebalance windows.
If an addition is announced and we're in the announcement-to-effective window,
logs a BUY signal.

Usage:
    python -m forge.rebalance_runner --loop       # continuous (daily at 9 AM ET)
    python -m forge.rebalance_runner --check      # one-shot scan
    python -m forge.rebalance_runner --status      # show current state
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_FORGE = Path(__file__).resolve().parent
LOG_DIR = _FORGE / "logs" / "rebalance"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HEARTBEAT_JSON = LOG_DIR / "heartbeat.json"
SIGNALS_CSV = LOG_DIR / "signals.csv"
STATE_JSON = LOG_DIR / "state.json"

CHECK_HOUR_ET = 9  # 9:00 AM ET
LOOP_INTERVAL_S = 300  # check every 5 min whether it's time for daily scan

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
sys.path.insert(0, str(_FORGE.parent))
from helio import ibkr_execution as ibkr  # noqa: E402
IBKR_CLIENT_ID = 117
_SIGNAL_ONLY_MODE = False
HOLD_DAYS = 5  # event-driven: hold each addition for 5 trading days post-buy

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# 2026-05-12: replaced custom FileHandler with setup_logging() so
# helio.signal_executor + helio.ibkr_execution errors are visible in
# forge/logs/rebalance/runner.log. See project_2026_05_12_capital_ladder_session
# memory for the 4-layer silent-gate cascade this prevents.
from forge.logging_setup import setup_logging
log = setup_logging("rebalance")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_heartbeat(status: str = "ok", extra: dict | None = None) -> None:
    hb = {
        "system": "forge.rebalance",
        "mode": "paper_loop",
        "status": status,
        "pid": os.getpid(),
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
    if extra:
        hb.update(extra)
    HEARTBEAT_JSON.write_text(json.dumps(hb, indent=2))


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_scan_date": None, "active_positions": []}


def _save_state(state: dict) -> None:
    STATE_JSON.write_text(json.dumps(state, indent=2, default=str))


def _et_now() -> datetime:
    """Approximate current Eastern Time (EST, no DST adjustment)."""
    return datetime.now(tz=timezone.utc) - timedelta(hours=5)


def _append_signal(action: str, ticker: str, detail: str) -> None:
    """Append a row to signals.csv."""
    header = ["timestamp", "action", "ticker", "detail"]
    write_header = not SIGNALS_CSV.exists()
    with open(SIGNALS_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow([
            datetime.now(tz=timezone.utc).isoformat(),
            action,
            ticker,
            detail,
        ])
    log.info(f"Signal written: {action} {ticker}  {detail}")


# ---------------------------------------------------------------------------
# Core scan — import from index_rebalance
# ---------------------------------------------------------------------------

def _scan_active_windows() -> list[dict]:
    """Use index_rebalance.get_active_rebalance_signals() to find open windows."""
    try:
        from forge.index_rebalance import get_active_rebalance_signals
        return get_active_rebalance_signals()
    except ImportError:
        log.error("Cannot import forge.index_rebalance — check installation")
        return []


def evaluate_once() -> list[dict]:
    """Run a single rebalance scan. Returns list of signals generated.

    2026-04-24: extended to submit real IBKR orders for new BUY signals
    and exit positions held >= HOLD_DAYS trading days."""
    state = _load_state()
    signals = _scan_active_windows()
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    new_signals = []
    state.setdefault("active_positions", [])

    # Track which tickers we've already signaled (avoid duplicates)
    already_signaled = set()
    if SIGNALS_CSV.exists():
        try:
            with open(SIGNALS_CSV) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("action") == "BUY":
                        already_signaled.add(row.get("ticker", ""))
        except Exception:
            pass
    # Plus tickers we currently hold
    for pos in state.get("active_positions", []):
        already_signaled.add(pos.get("ticker", ""))

    # IBKR connection
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed: %s", exc)
            ib = None

    try:
        # 1. Exit positions held >= HOLD_DAYS days
        from datetime import datetime as _dt
        now = datetime.now(tz=timezone.utc)
        keep_positions = []
        for pos in state.get("active_positions", []):
            try:
                entry_dt = _dt.fromisoformat(pos["entry_ts"])
                days_held = (now - entry_dt).days
            except Exception:
                days_held = 0
            if days_held >= HOLD_DAYS:
                ticker = pos["ticker"]
                exit_px = None
                pnl_usd = None
                if ib is not None and pos.get("execution_venue") == "ibkr_paper":
                    contract = ibkr.make_contract(ticker, "stock")
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
                            pnl_usd = (exit_px - pos.get("entry_px", exit_px)) * broker_qty
                            log.info(f"REBALANCE EXIT {ticker} @ {exit_px:.2f} pnl=${pnl_usd:.2f} (held {days_held}d)")
                _append_signal("EXIT", ticker, f"hold {days_held}d complete (pnl=${pnl_usd})")
            else:
                keep_positions.append(pos)
        state["active_positions"] = keep_positions

        # 2. Submit new entries
        for sig in signals:
            ticker = sig["ticker"]
            action_type = sig["action"]
            status = sig["status"]
            if action_type == "ADD" and status == "ACTIVE" and ticker not in already_signaled:
                # Compute size: ~25% of anchor / N expected positions, capped at etf cap
                entry_px = None
                shares = 0
                execution_venue = "signal_only"
                if ib is not None:
                    contract = ibkr.make_contract(ticker, "stock")
                    try:
                        ib.qualifyContracts(contract)
                        existing = ibkr.query_position(ib, contract)
                        if existing != 0:
                            log.warning(f"BROKER_HAS_POSITION: {ticker} qty={existing}, skipping")
                            continue
                        try:
                            from helio.fleet_sizing import get_sizing_anchor_usd, max_notional_usd
                            anchor = float(get_sizing_anchor_usd())
                        except Exception:
                            anchor = 10000.0
                        # 25% anchor per addition (rebalance bursts can have multiple adds)
                        target_notional = min(anchor * 0.25, max_notional_usd("stock"))
                        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="1 D",
                                                      barSizeSetting="1 hour", whatToShow="TRADES", useRTH=True)
                        plan_entry = float(bars[-1].close) if bars else 100.0
                        shares = max(1, int(target_notional / max(plan_entry, 1e-6)))
                        from ib_insync import MarketOrder
                        order = MarketOrder("BUY", shares)
                        trade = ib.placeOrder(contract, order)
                        fill = ibkr._wait_for_fill(ib, trade, timeout_s=15.0)
                        if fill.filled:
                            entry_px = fill.fill_price
                            execution_venue = "ibkr_paper"
                            log.info(f"REBALANCE ENTRY: BUY {shares} {ticker} @ {entry_px:.2f}")
                        else:
                            log.error(f"REBALANCE ENTRY FAILED {ticker}: {fill.reject_reason}")
                            continue
                    except Exception as exc:
                        log.error(f"REBALANCE ENTRY EXCEPTION {ticker}: {exc}", exc_info=True)
                        continue
                detail = (f"S&P500 ADD — announce={sig['announce_date']} "
                          f"effective={sig['effective_date']} "
                          f"window={sig['window_pct_elapsed']:.0f}% elapsed "
                          f"({execution_venue} {shares} sh @ {entry_px})")
                _append_signal("BUY", ticker, detail)
                new_signals.append(sig)
                state["active_positions"].append({
                    "ticker": ticker,
                    "entry_ts": now.isoformat(),
                    "entry_px": entry_px,
                    "position_size": shares,
                    "execution_venue": execution_venue,
                })
    finally:
        if ib is not None:
            ibkr.disconnect(ib)

    state["last_scan_date"] = today_str
    state["active_windows"] = len(signals)
    _save_state(state)

    _write_heartbeat("ok", {
        "active_windows": len(signals),
        "new_signals": len(new_signals),
        "open_positions": len(state.get("active_positions", [])),
        "last_scan": today_str,
    })

    if not signals:
        log.info("No active rebalance windows found")
    else:
        log.info(f"Found {len(signals)} active windows, {len(new_signals)} new entries, {len(state.get('active_positions', []))} held")

    return new_signals


def show_status() -> None:
    """Print current runner status."""
    state = _load_state()
    signals = _scan_active_windows()

    print()
    print("=" * 60)
    print("  S&P 500 Index Rebalance Runner Status")
    print("=" * 60)
    print(f"  Last scan:       {state.get('last_scan_date', 'never')}")
    print(f"  Active windows:  {len(signals)}")

    if signals:
        print()
        for s in signals:
            print(f"    {s['direction']} {s['ticker']}  "
                  f"({s['action']}, {s['status']}, "
                  f"{s['window_pct_elapsed']:.0f}% elapsed)")

    if HEARTBEAT_JSON.exists():
        hb = json.loads(HEARTBEAT_JSON.read_text())
        print(f"\n  Last heartbeat:  {hb.get('ts', '?')}")
        print(f"  Status:          {hb.get('status', '?')}")

    if SIGNALS_CSV.exists():
        with open(SIGNALS_CSV) as f:
            lines = f.readlines()
        n = max(0, len(lines) - 1)
        print(f"  Total signals:   {n}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="S&P 500 Index Rebalance Paper Runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--loop", action="store_true", help="Continuous loop (daily at 9 AM ET)")
    group.add_argument("--check", action="store_true", help="One-shot scan")
    group.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--signal-only", action="store_true",
                        help="Skip IBKR submission — log signals only")
    args = parser.parse_args()
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.status:
        show_status()
        return

    if args.check:
        new = evaluate_once()
        print(f"New signals: {len(new)}")
        return

    # --loop: run daily scan at 9 AM ET
    log.info("Rebalance runner starting (loop mode, daily at %d:00 ET)", CHECK_HOUR_ET)
    _write_heartbeat("starting")

    last_scan_date = None

    while True:
        try:
            et = _et_now()
            today = et.strftime("%Y-%m-%d")
            is_weekday = et.weekday() < 5

            # Run once per day at/after CHECK_HOUR_ET on weekdays
            if is_weekday and et.hour >= CHECK_HOUR_ET and today != last_scan_date:
                log.info("Daily scan triggered")
                evaluate_once()
                last_scan_date = today
            else:
                _write_heartbeat("ok", {"note": "waiting_for_scan_time",
                                        "last_scan": last_scan_date})
        except Exception as e:
            log.exception(f"Cycle error: {e}")
            _write_heartbeat("error", {"error": str(e)})

        time.sleep(LOOP_INTERVAL_S)


if __name__ == "__main__":
    main()
