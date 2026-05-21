"""Off-hours synthetic-signal injector — paper only, on-demand activity.

Fires tiny brackets on a paper-test instrument (default EURUSD, NOT in
the survivor roster) so the operator can stress-test the execution path
without waiting for real market signals. Each cycle exercises the full
submit → fill → exit lifecycle and writes every event to a dedicated
JSONL, with race detectors comparing local state to broker state.

Why this exists: the operational audit on 2026-05-20 caught 15+ bugs
that had been firing during live trading since 2026-05-12. Most surfaced
only when a real fill arrived. Real fills are rare; synthetic fills on a
test symbol are cheap.

Safety contract — all four locks must pass at module import:
  1. IBKR_PORT == "7497"
  2. helio.real_money.REAL_MONEY_ENABLED is False (module constant)
  3. os.environ["REAL_MONEY_ENABLED"] is unset/false
  4. os.environ["STRESS_INJECT_OK"] == "1"  (explicit operator opt-in)

If any gate fails, module import raises RuntimeError. There is no
override flag. This is deliberate — easy bypass = the next 5/19.

Usage:
  $env:STRESS_INJECT_OK="1"
  $env:IBKR_PORT="7497"
  python -m ops.stress_injector --single
  python -m ops.stress_injector --loop 10 --cooldown 60
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
LOG_PATH = REPO / "argus_flow" / "logs" / "stress_injector.jsonl"

# Test-instrument allowlist — anything NOT in production survivor roster.
# EURUSD is paper-only, not in argus FX (USDJPY/GBPUSD/CADJPY), not in any
# forge survivor (which trade equities/futures). Stress-fills here don't
# poison real-strategy attribution.
TEST_INSTRUMENTS = {
    "EURUSD": {"base": "EUR", "quote": "USD", "lot": 1000, "pip": 0.0001},
    "AUDUSD": {"base": "AUD", "quote": "USD", "lot": 1000, "pip": 0.0001},
}

STRESS_CLIENT_ID = 250  # 250-299 reserved for stress harness


# ─── safety locks (executed at import) ────────────────────────────────────

def _enforce_safety_locks() -> None:
    failures = []
    if os.environ.get("IBKR_PORT") != "7497":
        failures.append(f"IBKR_PORT={os.environ.get('IBKR_PORT')!r} (need '7497')")
    rm_env = os.environ.get("REAL_MONEY_ENABLED", "").strip().lower()
    if rm_env in ("1", "true", "yes"):
        failures.append(f"REAL_MONEY_ENABLED env={rm_env!r}")
    if os.environ.get("STRESS_INJECT_OK") != "1":
        failures.append("STRESS_INJECT_OK != '1' (explicit operator opt-in required)")
    try:
        from helio import real_money
        if real_money.REAL_MONEY_ENABLED:
            failures.append("helio.real_money.REAL_MONEY_ENABLED is True")
    except Exception as e:
        failures.append(f"helio.real_money import failed: {e}")
    if failures:
        raise RuntimeError(
            "stress_injector REFUSING TO LOAD — safety locks failed:\n  - "
            + "\n  - ".join(failures)
        )


# Locks run on import only when invoked as a module — never on test import
if __name__ == "__main__" or os.environ.get("STRESS_INJECTOR_ENFORCE_AT_IMPORT") == "1":
    _enforce_safety_locks()


# ─── event log ────────────────────────────────────────────────────────────

@dataclass
class Event:
    ts: str
    cycle_id: str
    kind: str
    payload: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(event: Event) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), default=str) + "\n")


# ─── one cycle ────────────────────────────────────────────────────────────

CHAOS_MODES = {
    "none": "no chaos",
    "cancel_during_fill": "submit then cancel within 200ms — exercises the fill-during-cancel race",
    "double_submit": "submit the same order twice — exercises duplicate-order handling",
    "disconnect_after_submit": "disconnect immediately after placeOrder, reconnect to check state recovery",
}


def run_one_cycle(symbol: str, qty: int, side: str | None = None,
                  chaos: str = "none") -> dict:
    """Submit one bracket, watch the lifecycle, log every event.

    chaos selects an optional fault to inject — see CHAOS_MODES.

    Returns a result dict with: cycle_id, outcome, races_detected, durations.
    """
    if chaos not in CHAOS_MODES:
        raise ValueError(f"unknown chaos mode {chaos!r}; choose from {sorted(CHAOS_MODES)}")
    from ib_insync import IB, Forex, LimitOrder, MarketOrder

    if symbol not in TEST_INSTRUMENTS:
        raise ValueError(f"{symbol} not in TEST_INSTRUMENTS — "
                         f"choose from {sorted(TEST_INSTRUMENTS)}")
    cycle_id = uuid.uuid4().hex[:12]
    side = side or random.choice(["BUY", "SELL"])
    spec = TEST_INSTRUMENTS[symbol]

    _log_event(Event(_now(), cycle_id, "CYCLE_START", {"symbol": symbol, "side": side, "qty": qty}))

    ib = IB()
    races_detected: list[str] = []
    t0 = time.time()

    def _result(outcome: str, **extra: object) -> dict:
        """Centralized result builder — guarantees elapsed_s in every
        return path. 2026-05-21 BUGFIX: early-return paths (NO_QUOTE,
        RECONNECT_FAILED, ENTRY_TIMEOUT, ORPHAN_START) previously omitted
        elapsed_s and caused run_loop to KeyError. Always include it now."""
        out = {
            "cycle_id": cycle_id,
            "outcome": outcome,
            "races_detected": list(races_detected),
            "elapsed_s": time.time() - t0,
        }
        out.update(extra)
        return out

    try:
        ib.connect("127.0.0.1", 7497, clientId=STRESS_CLIENT_ID, timeout=10)
        _log_event(Event(_now(), cycle_id, "CONNECTED", {"client_id": STRESS_CLIENT_ID}))

        contract = Forex(symbol)
        ib.qualifyContracts(contract)

        # 2026-05-21 BUGFIX: defensive pre-cycle orphan check. Before each
        # cycle, verify the test instrument is flat. The 4h run on
        # 2026-05-21 accumulated up to 9000 long / 8000 short of EURUSD
        # because the force-close path silently failed with Error 10349
        # (MarketOrder + TIF=DAY rejected on FX) AND because the chaos
        # cancel race produced a real unexpected position that compounded
        # cycle over cycle. Refusing to enter a cycle with an existing
        # orphan stops the bleeding.
        existing = _read_broker_position(ib, symbol)
        if abs(existing) > 0.5:
            _log_event(Event(_now(), cycle_id, "ORPHAN_START",
                             {"existing_position": existing}))
            races_detected.append("PRE_CYCLE_ORPHAN")
            # Try to clean up before bailing out
            _flatten_position(ib, contract, symbol, existing, cycle_id)
            return _result("ORPHAN_START_REFUSED")

        # Get current mid
        ticker = ib.reqTickers(contract)[0]
        mid = (ticker.bid + ticker.ask) / 2 if (ticker.bid and ticker.ask) else float(ticker.marketPrice() or 0)
        if mid <= 0:
            _log_event(Event(_now(), cycle_id, "NO_QUOTE", {"ticker": str(ticker)}))
            return _result("NO_QUOTE")

        # Wide-LMT entry at touch; 5 pip stop + 5 pip target on each side
        pip = spec["pip"]
        if side == "BUY":
            entry_px = round(mid + 2 * pip, 5)  # cross spread a bit so we fill
            stop_px = round(mid - 5 * pip, 5)
            tgt_px = round(mid + 5 * pip, 5)
        else:
            entry_px = round(mid - 2 * pip, 5)
            stop_px = round(mid + 5 * pip, 5)
            tgt_px = round(mid - 5 * pip, 5)

        entry = LimitOrder(side, qty, entry_px, tif="DAY", outsideRth=True)
        _log_event(Event(_now(), cycle_id, "ENTRY_SUBMIT",
                         {"mid": mid, "entry_px": entry_px, "stop_px": stop_px, "tgt_px": tgt_px}))

        trade = ib.placeOrder(contract, entry)
        _log_event(Event(_now(), cycle_id, "PLACED", {"chaos": chaos}))

        # Chaos hook: immediately after placeOrder
        if chaos == "double_submit":
            trade2 = ib.placeOrder(contract, entry)
            _log_event(Event(_now(), cycle_id, "CHAOS_DOUBLE_SUBMIT",
                             {"order_id_1": trade.order.orderId,
                              "order_id_2": trade2.order.orderId}))
        elif chaos == "cancel_during_fill":
            ib.sleep(0.2)
            ib.cancelOrder(entry)
            _log_event(Event(_now(), cycle_id, "CHAOS_CANCEL_DURING_FILL",
                             {"delay_ms": 200}))
        elif chaos == "disconnect_after_submit":
            ib.sleep(0.1)
            ib.disconnect()
            _log_event(Event(_now(), cycle_id, "CHAOS_DISCONNECT", {}))
            ib.sleep(2)
            try:
                ib.connect("127.0.0.1", 7497, clientId=STRESS_CLIENT_ID, timeout=10)
                _log_event(Event(_now(), cycle_id, "RECONNECTED", {}))
            except Exception as e:
                _log_event(Event(_now(), cycle_id, "RECONNECT_FAILED", {"error": str(e)}))
                races_detected.append("RECONNECT_FAILED")
                return _result("RECONNECT_FAILED")

        # Wait for fill, up to 30s
        filled = False
        for _ in range(60):
            ib.sleep(0.5)
            status = trade.orderStatus.status
            if status == "Filled":
                filled = True
                _log_event(Event(_now(), cycle_id, "ENTRY_FILLED",
                                 {"avg_fill": trade.orderStatus.avgFillPrice}))
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                _log_event(Event(_now(), cycle_id, "ENTRY_REJECTED", {"status": status}))
                break

        if not filled:
            # Race detector #1: cancel an order while it might be filling
            ib.cancelOrder(entry)
            ib.sleep(2)
            broker_pos = _read_broker_position(ib, symbol)
            if abs(broker_pos) >= qty * 0.99:
                races_detected.append("ENTRY_FILLED_DURING_CANCEL")
                _log_event(Event(_now(), cycle_id, "RACE_DETECTED",
                                 {"race": "ENTRY_FILLED_DURING_CANCEL", "broker_pos": broker_pos}))
                filled = True
            else:
                _log_event(Event(_now(), cycle_id, "ENTRY_TIMEOUT", {"broker_pos": broker_pos}))
                return _result("ENTRY_TIMEOUT")

        # Now submit the exit OCO bracket and watch lifecycle
        exit_side = "SELL" if side == "BUY" else "BUY"
        stop_order = LimitOrder(exit_side, qty, stop_px, tif="GTC", outsideRth=True)
        tgt_order = LimitOrder(exit_side, qty, tgt_px, tif="GTC", outsideRth=True)
        ib.placeOrder(contract, stop_order)
        ib.placeOrder(contract, tgt_order)
        _log_event(Event(_now(), cycle_id, "BRACKET_SUBMITTED",
                         {"stop_px": stop_px, "tgt_px": tgt_px}))

        # Watch up to 5 minutes for exit
        outcome = "BRACKET_OPEN_AT_TIMEOUT"
        deadline = time.time() + 300
        while time.time() < deadline:
            ib.sleep(2)
            broker_pos = _read_broker_position(ib, symbol)
            if abs(broker_pos) < qty * 0.01:
                outcome = "BRACKET_RESOLVED"
                _log_event(Event(_now(), cycle_id, "BRACKET_RESOLVED", {"broker_pos": broker_pos}))
                break
            # Race detector #2: did both legs fill?
            stop_status = stop_order.orderStatus.status if hasattr(stop_order, "orderStatus") else "?"
            tgt_status = tgt_order.orderStatus.status if hasattr(tgt_order, "orderStatus") else "?"
            if stop_status == "Filled" and tgt_status == "Filled":
                races_detected.append("BOTH_BRACKET_LEGS_FILLED")
                _log_event(Event(_now(), cycle_id, "RACE_DETECTED",
                                 {"race": "BOTH_BRACKET_LEGS_FILLED",
                                  "broker_pos": broker_pos}))

        # Cleanup: if still open, force-close.
        # 2026-05-21 BUGFIX: the original force-close used `MarketOrder` which
        # hits Error 10349 on IDEALPRO FX (TIF=DAY rejected) — same bug class
        # as the entry-path and exit-path fixes. Silent failure here is what
        # caused EURUSD positions to accumulate up to 9000 units over a 4h
        # run. Now uses the same LimitOrder + GTC + outsideRth + wide-buffer
        # pattern that runner_unified._build_exit_order uses for FX exits.
        broker_pos = _read_broker_position(ib, symbol)
        if abs(broker_pos) >= qty * 0.5:
            _flatten_position(ib, contract, symbol, broker_pos, cycle_id)

        elapsed = time.time() - t0
        _log_event(Event(_now(), cycle_id, "CYCLE_END",
                         {"elapsed_s": elapsed, "races": races_detected,
                          "outcome": outcome}))
        return _result(outcome)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _read_broker_position(ib, symbol: str) -> float:
    """Match by base+quote on the Forex contract."""
    spec = TEST_INSTRUMENTS[symbol]
    for p in ib.positions():
        c = p.contract
        if getattr(c, "symbol", "") == spec["base"] and getattr(c, "currency", "") == spec["quote"]:
            return float(p.position)
    return 0.0


def _flatten_position(ib, contract, symbol: str, broker_pos: float, cycle_id: str) -> None:
    """Force-flatten a position using the LimitOrder + GTC + outsideRth pattern
    that bypasses Error 10349. Mirrors runner_unified._build_exit_order's
    FX-safe submission path.

    2026-05-21: bare MarketOrder(side, qty) was silently failing with
    Error 10349 (TIF=DAY rejected on IDEALPRO FX), causing orphan positions
    to compound across cycles. This helper centralizes the safe pattern."""
    from ib_insync import LimitOrder

    if abs(broker_pos) < 0.5:
        return  # already flat

    close_side = "SELL" if broker_pos > 0 else "BUY"
    qty = abs(int(round(broker_pos)))

    # Wide LMT toward fill direction with 5% buffer; broker fills at NBBO
    ticker = ib.reqTickers(contract)[0]
    bid = float(getattr(ticker, "bid", 0) or 0)
    ask = float(getattr(ticker, "ask", 0) or 0)
    ref = (bid + ask) / 2 if (bid and ask) else float(getattr(ticker, "marketPrice", lambda: 1.0)() or 1.0)
    if ref <= 0:
        ref = 1.0  # last-resort

    buffer = 1.05 if close_side == "BUY" else 0.95
    spec = TEST_INSTRUMENTS[symbol]
    decimals = 3 if "JPY" in (spec.get("quote", "") + spec.get("base", "")).upper() else 5
    lmt = round(ref * buffer, decimals)

    order = LimitOrder(close_side, qty, lmt, tif="GTC")
    order.outsideRth = True
    trade = ib.placeOrder(contract, order)

    # Wait up to 8s for fill
    for _ in range(16):
        ib.sleep(0.5)
        if trade.orderStatus.status == "Filled":
            break

    _log_event(Event(_now(), cycle_id, "FORCE_CLOSED", {
        "broker_pos_before": broker_pos,
        "close_side": close_side,
        "qty": qty,
        "lmt": lmt,
        "status": trade.orderStatus.status,
        "filled": trade.orderStatus.filled,
        "avg_fill": trade.orderStatus.avgFillPrice,
    }))


# ─── loop runner ──────────────────────────────────────────────────────────

def run_loop(symbol: str, qty: int, count: int, cooldown_s: int,
             chaos: str = "none") -> list[dict]:
    results = []
    for i in range(count):
        print(f"[stress_injector] cycle {i+1}/{count} (chaos={chaos})")
        try:
            r = run_one_cycle(symbol, qty, chaos=chaos)
            print(f"  {r['outcome']} races={r['races_detected']} elapsed={r['elapsed_s']:.1f}s")
            results.append(r)
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            _log_event(Event(_now(), "n/a", "CYCLE_EXCEPTION", {"error": str(e)}))
            results.append({"outcome": "EXCEPTION", "error": str(e)})
        if i < count - 1:
            time.sleep(cooldown_s)
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Paper-only synthetic signal injector")
    p.add_argument("--symbol", default="EURUSD", choices=sorted(TEST_INSTRUMENTS))
    p.add_argument("--qty", type=int, default=1000)
    p.add_argument("--single", action="store_true", help="one cycle then exit")
    p.add_argument("--loop", type=int, default=0, metavar="N", help="N cycles")
    p.add_argument("--cooldown", type=int, default=60, help="seconds between cycles")
    p.add_argument("--chaos", default="none", choices=sorted(CHAOS_MODES),
                   help="inject a specific fault: " + ", ".join(f"{k}={v}" for k, v in CHAOS_MODES.items()))
    args = p.parse_args(argv)

    if args.single:
        r = run_one_cycle(args.symbol, args.qty, chaos=args.chaos)
        print(json.dumps(r, indent=2))
        return 0 if r["outcome"] in ("BRACKET_RESOLVED",) else 2
    if args.loop > 0:
        rs = run_loop(args.symbol, args.qty, args.loop, args.cooldown, chaos=args.chaos)
        races = sum(len(r.get("races_detected", [])) for r in rs)
        print(f"\nCompleted {len(rs)} cycles, {races} races detected.")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
