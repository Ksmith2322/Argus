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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("rebalance_runner")
log.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
log.addHandler(_console)

_file_handler = logging.FileHandler(LOG_DIR / "runner.log")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
log.addHandler(_file_handler)

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
    """Run a single rebalance scan. Returns list of signals generated."""
    state = _load_state()
    signals = _scan_active_windows()
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    new_signals = []

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

    for sig in signals:
        ticker = sig["ticker"]
        action_type = sig["action"]     # ADD or DELETE
        direction = sig["direction"]    # LONG or SHORT
        status = sig["status"]          # ACTIVE or POST-EFFECTIVE

        # Only signal additions (BUY) in ACTIVE window, not post-effective
        if action_type == "ADD" and status == "ACTIVE":
            if ticker not in already_signaled:
                detail = (
                    f"S&P500 ADD — announce={sig['announce_date']} "
                    f"effective={sig['effective_date']} "
                    f"window={sig['window_pct_elapsed']:.0f}% elapsed"
                )
                _append_signal("BUY", ticker, detail)
                new_signals.append(sig)
                log.info(f"BUY {ticker} — index rebalance addition")
            else:
                log.debug(f"Already signaled {ticker} — skipping")

    state["last_scan_date"] = today_str
    state["active_windows"] = len(signals)
    _save_state(state)

    _write_heartbeat("ok", {
        "active_windows": len(signals),
        "new_signals": len(new_signals),
        "last_scan": today_str,
    })

    if not signals:
        log.info("No active rebalance windows found")
    else:
        log.info(f"Found {len(signals)} active windows, {len(new_signals)} new signals")

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
    args = parser.parse_args()

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
