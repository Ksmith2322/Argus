"""
VIX Mean-Reversion Paper Runner
================================
Lightweight wrapper around VixMeanReversion (forge/macro_strategies.py).

Checks every hour during market hours. When VIX > 30, logs BUY SPY signal.
When VIX < 20 while in position, logs EXIT signal.

Usage:
    python -m forge.vix_revert_runner --loop       # continuous (hourly)
    python -m forge.vix_revert_runner --check      # one-shot evaluation
    python -m forge.vix_revert_runner --status      # show current state
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Required: pip install yfinance")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_FORGE = Path(__file__).resolve().parent
LOG_DIR = _FORGE / "logs" / "vix_revert"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HEARTBEAT_JSON = LOG_DIR / "heartbeat.json"
SIGNALS_CSV = LOG_DIR / "signals.csv"
STATE_JSON = LOG_DIR / "state.json"
REGIME_PATH = _FORGE / "macro_regime.json"

LOOP_INTERVAL_S = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("vix_revert")
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
# VIX thresholds (mirrors VixMeanReversion class)
# ---------------------------------------------------------------------------
VIX_ENTRY = 30.0
VIX_EXIT = 20.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_heartbeat(status: str = "ok", extra: dict | None = None) -> None:
    hb = {
        "system": "forge.vix_revert",
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
    return {"in_position": False, "entry_date": None, "entry_vix": None}


def _save_state(state: dict) -> None:
    STATE_JSON.write_text(json.dumps(state, indent=2, default=str))


def _load_regime_vix() -> float | None:
    """Try to read VIX from Atlas regime file."""
    if REGIME_PATH.exists():
        try:
            data = json.loads(REGIME_PATH.read_text())
            v = data.get("raw_inputs", {}).get("vix")
            if v is not None:
                return float(v)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    return None


def _fetch_vix() -> float | None:
    """Get current VIX: prefer Atlas regime, fallback to yfinance."""
    vix = _load_regime_vix()
    if vix is not None:
        log.debug(f"VIX from regime file: {vix:.2f}")
        return vix
    try:
        data = yf.download("^VIX", period="5d", progress=False)
        if data is not None and not data.empty:
            close = data["Close"]
            if hasattr(close, "iloc"):
                val = float(close.iloc[-1])
                log.debug(f"VIX from yfinance: {val:.2f}")
                return val
    except Exception as e:
        log.warning(f"yfinance VIX fetch failed: {e}")
    return None


def _is_market_hours() -> bool:
    """Check if US equity market is roughly open (9:30-16:00 ET, Mon-Fri)."""
    from datetime import timedelta
    now_utc = datetime.now(tz=timezone.utc)
    # Rough ET offset (not DST-aware, but close enough for hourly checks)
    et_hour = (now_utc.hour - 5) % 24  # EST approximation
    weekday = now_utc.weekday()  # 0=Mon
    if weekday >= 5:
        return False
    return 9 <= et_hour <= 16


def _append_signal(action: str, vix: float, detail: str) -> None:
    """Append a row to signals.csv."""
    header = ["timestamp", "action", "ticker", "vix", "detail"]
    write_header = not SIGNALS_CSV.exists()
    with open(SIGNALS_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow([
            datetime.now(tz=timezone.utc).isoformat(),
            action,
            "SPY",
            f"{vix:.2f}",
            detail,
        ])
    log.info(f"Signal written: {action} SPY  VIX={vix:.2f}  {detail}")


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate_once() -> str:
    """Run a single VIX check. Returns action taken."""
    state = _load_state()
    vix = _fetch_vix()

    if vix is None:
        log.warning("Could not fetch VIX — skipping this cycle")
        _write_heartbeat("warn", {"reason": "no_vix_data"})
        return "NO_DATA"

    action = "NO_SIGNAL"

    if not state["in_position"]:
        if vix >= VIX_ENTRY:
            action = "BUY"
            state["in_position"] = True
            state["entry_date"] = datetime.now(tz=timezone.utc).isoformat()
            state["entry_vix"] = round(vix, 2)
            _append_signal("BUY", vix, f"VIX {vix:.1f} >= {VIX_ENTRY} — buy the panic")
            log.info(f"BUY SPY — VIX={vix:.2f} >= {VIX_ENTRY}")
        else:
            log.info(f"No signal — VIX={vix:.2f} < {VIX_ENTRY}")
    else:
        if vix < VIX_EXIT:
            action = "EXIT"
            _append_signal("EXIT", vix, f"VIX {vix:.1f} < {VIX_EXIT} — mean reverted")
            log.info(f"EXIT SPY — VIX={vix:.2f} < {VIX_EXIT}")
            state["in_position"] = False
            state["entry_date"] = None
            state["entry_vix"] = None
        else:
            action = "HOLD"
            log.info(f"HOLD — in position, VIX={vix:.2f} (exit below {VIX_EXIT})")

    _save_state(state)
    _write_heartbeat("ok", {"vix": round(vix, 2), "action": action,
                            "in_position": state["in_position"]})
    return action


def show_status() -> None:
    """Print current runner status."""
    state = _load_state()
    vix = _fetch_vix()

    print()
    print("=" * 50)
    print("  VIX Mean-Reversion Runner Status")
    print("=" * 50)
    print(f"  In position:  {state['in_position']}")
    if state["in_position"]:
        print(f"  Entry date:   {state.get('entry_date', '?')}")
        print(f"  Entry VIX:    {state.get('entry_vix', '?')}")
    print(f"  Current VIX:  {vix if vix else 'unavailable'}")
    print(f"  Entry thresh: >= {VIX_ENTRY}")
    print(f"  Exit thresh:  < {VIX_EXIT}")

    if HEARTBEAT_JSON.exists():
        hb = json.loads(HEARTBEAT_JSON.read_text())
        print(f"  Last beat:    {hb.get('ts', '?')}")
        print(f"  Status:       {hb.get('status', '?')}")

    if SIGNALS_CSV.exists():
        with open(SIGNALS_CSV) as f:
            lines = f.readlines()
        n = max(0, len(lines) - 1)
        print(f"  Total signals: {n}")
    print("=" * 50)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="VIX Mean-Reversion Paper Runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--loop", action="store_true", help="Continuous loop (hourly)")
    group.add_argument("--check", action="store_true", help="One-shot evaluation")
    group.add_argument("--status", action="store_true", help="Show current state")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.check:
        result = evaluate_once()
        print(f"Result: {result}")
        return

    # --loop
    log.info("VIX mean-reversion runner starting (loop mode, interval=%ds)", LOOP_INTERVAL_S)
    _write_heartbeat("starting")

    while True:
        try:
            if _is_market_hours():
                evaluate_once()
            else:
                log.debug("Market closed — sleeping")
                _write_heartbeat("ok", {"note": "market_closed"})
        except Exception as e:
            log.exception(f"Cycle error: {e}")
            _write_heartbeat("error", {"error": str(e)})

        time.sleep(LOOP_INTERVAL_S)


if __name__ == "__main__":
    main()
