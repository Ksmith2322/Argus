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
# 2026-05-12: replaced custom FileHandler with setup_logging() so
# helio.signal_executor + helio.ibkr_execution errors are visible in
# forge/logs/vix_revert/runner.log. See project_2026_05_12_capital_ladder_session
# memory for the 4-layer silent-gate cascade this prevents.
from forge.logging_setup import setup_logging
log = setup_logging("vix_revert")

# ---------------------------------------------------------------------------
# VIX thresholds (mirrors VixMeanReversion class)
# ---------------------------------------------------------------------------
VIX_ENTRY = 30.0
VIX_EXIT = 20.0

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
sys.path.insert(0, str(_FORGE.parent))
from helio import ibkr_execution as ibkr  # noqa: E402
IBKR_CLIENT_ID = 113
_SIGNAL_ONLY_MODE = False
# Event-driven sizing: VIX > 30 is a panic event. Use 50% anchor for the SPY long.
NOTIONAL_FRACTION = 0.5

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

    # IBKR connection (per-cycle, brief)
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
        if not state["in_position"]:
            if vix >= VIX_ENTRY:
                # Submit BUY SPY (no bracket — exit triggered by VIX < 20)
                entry_px = None
                shares = 0
                execution_venue = "signal_only"
                if ib is not None:
                    contract = ibkr.make_contract("SPY", "etf")
                    try:
                        ib.qualifyContracts(contract)
                        existing = ibkr.query_position(ib, contract)
                        if existing != 0:
                            log.warning("BROKER_HAS_POSITION: SPY qty=%s, skipping vix_revert entry", existing)
                            return "SKIP_HAS_POS"
                        try:
                            from helio.fleet_sizing import get_sizing_anchor_usd, max_notional_usd
                            anchor = float(get_sizing_anchor_usd())
                        except Exception:
                            anchor = 10000.0
                        target_notional = min(anchor * NOTIONAL_FRACTION, max_notional_usd("etf"))
                        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="1 D",
                                                      barSizeSetting="1 hour", whatToShow="TRADES", useRTH=True)
                        plan_entry = float(bars[-1].close) if bars else 500.0
                        shares = max(1, int(target_notional / max(plan_entry, 1e-6)))
                        from ib_insync import MarketOrder
                        order = MarketOrder("BUY", shares)
                        trade = ib.placeOrder(contract, order)
                        fill = ibkr._wait_for_fill(ib, trade, timeout_s=15.0)
                        if fill.filled:
                            entry_px = fill.fill_price
                            execution_venue = "ibkr_paper"
                            log.info(f"VIX_REVERT ENTRY FILLED: BUY {shares} SPY @ {entry_px:.2f}")
                        else:
                            log.error("VIX_REVERT ENTRY FAILED: %s", fill.reject_reason)
                    except Exception as exc:
                        log.error("VIX_REVERT ENTRY EXCEPTION: %s", exc, exc_info=True)
                action = "BUY"
                state["in_position"] = True
                state["entry_date"] = datetime.now(tz=timezone.utc).isoformat()
                state["entry_vix"] = round(vix, 2)
                state["entry_px"] = entry_px
                state["position_size"] = shares
                state["execution_venue"] = execution_venue
                _append_signal("BUY", vix, f"VIX {vix:.1f} >= {VIX_ENTRY} — buy the panic ({execution_venue} {shares} sh @ {entry_px})")
                log.info(f"BUY SPY — VIX={vix:.2f} >= {VIX_ENTRY}")
            else:
                log.info(f"No signal — VIX={vix:.2f} < {VIX_ENTRY}")
        else:
            if vix < VIX_EXIT:
                exit_px = None
                pnl_usd = None
                if ib is not None and state.get("execution_venue") == "ibkr_paper":
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
                            entry_px = state.get("entry_px") or exit_px
                            pnl_usd = (exit_px - entry_px) * broker_qty
                            log.info(f"VIX_REVERT EXIT FILLED @ {exit_px:.2f}, pnl=${pnl_usd:.2f}")
                action = "EXIT"
                _append_signal("EXIT", vix, f"VIX {vix:.1f} < {VIX_EXIT} — mean reverted (exit_px={exit_px} pnl=${pnl_usd})")
                log.info(f"EXIT SPY — VIX={vix:.2f} < {VIX_EXIT}")
                state["in_position"] = False
                state["entry_date"] = None
                state["entry_vix"] = None
                state["entry_px"] = None
                state["position_size"] = 0
                state["execution_venue"] = None
            else:
                action = "HOLD"
                log.info(f"HOLD — in position, VIX={vix:.2f} (exit below {VIX_EXIT})")

        _save_state(state)
        _write_heartbeat("ok", {"vix": round(vix, 2), "action": action,
                                "in_position": state["in_position"]})
        return action
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


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
