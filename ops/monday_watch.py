#!/usr/bin/env python3
"""ops/monday_watch.py — Quick fleet health snapshot.

Run anytime to get a fast 'what's the bot doing right now?' view.
Designed for the post-weekend Monday morning check but useful any day.

Usage:
    python -m ops.monday_watch
    python -m ops.monday_watch --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS_ARGUS = REPO / "argus_flow" / "logs"
LOGS_FORGE = REPO / "forge" / "logs"

# Heartbeat freshness tiers (minutes)
FRESH_MAX = 30
SLEEP_OK_MAX = 75  # most hourly-cycle runners
LONG_CYCLE_MAX = 240  # 4hr cycle is normal


def _heartbeat_age_min(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 60


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def heartbeat_table() -> tuple[list[tuple[str, float, str]], list[str]]:
    """Scan all heartbeat files. Returns (rows, concerns)."""
    rows = []
    concerns = []
    for d in (LOGS_ARGUS, LOGS_FORGE):
        for hb_path in d.glob("*/heartbeat.json"):
            if "_archive" in str(hb_path):
                continue
            name = hb_path.parent.name
            age = _heartbeat_age_min(hb_path)
            if age is None:
                continue
            if age > LONG_CYCLE_MAX:
                # Killed strategies are expected to be stale — filter them
                killed = {"multi_orb", "spy_mean_rev", "vix_intraday", "nq_london_close"}
                if name in killed:
                    continue
                concerns.append(f"{name}: heartbeat {age:.0f}min stale (>4hr)")
            rows.append((name, age, _classify_age(age)))
    rows.sort(key=lambda r: r[1])
    return rows, concerns


def _classify_age(age_min: float) -> str:
    if age_min < FRESH_MAX:
        return "FRESH"
    if age_min < SLEEP_OK_MAX:
        return "SLEEP-OK"
    if age_min < LONG_CYCLE_MAX:
        return "4HR-CYCLE"
    return "STALE"


def broker_state() -> dict:
    """Pull broker_truth + open positions."""
    out = {"equity": None, "connected": False, "positions": []}
    ro = _load_json(LOGS_ARGUS / "risk_oversight_report.json")
    if ro:
        bt = ro.get("broker_truth", {})
        out["equity"] = bt.get("account_equity_usd")
    snap = _load_json(LOGS_ARGUS / "_broker" / "broker_snapshot.json")
    if snap:
        out["connected"] = bool(snap.get("broker_connected"))
        positions = snap.get("positions", {})
        for sym, p in positions.items():
            if p.get("qty"):
                out["positions"].append({
                    "symbol": sym,
                    "qty": p["qty"],
                    "direction": p["direction"],
                })
    return out


def halt_state() -> dict:
    """Pull fleet halt state. The actual halt logic uses
    `_risk/broker_drift_state.json:tripped` (not HALT.flag). Surface that
    here so the watch reflects what the runners actually see."""
    out = {"halted": False, "reasons": [], "drift_pct": None,
           "drift_sustained_min": None, "halt_flag_present": False,
           "flatten_flag_present": False}
    halt_flag = LOGS_ARGUS / "HALT.flag"
    flatten_flag = LOGS_ARGUS / "FLATTEN_EOD.flag"
    drift_path = LOGS_ARGUS / "_risk" / "broker_drift_state.json"
    if halt_flag.exists():
        out["halt_flag_present"] = True
        out["halted"] = True
        try:
            out["reasons"].append(f"HALT.flag: {halt_flag.read_text(encoding='utf-8').strip()[:120]}")
        except Exception:
            out["reasons"].append("HALT.flag present")
    if flatten_flag.exists():
        out["flatten_flag_present"] = True
        out["halted"] = True
        out["reasons"].append("FLATTEN_EOD.flag present")
    drift = _load_json(drift_path)
    if drift and drift.get("tripped"):
        out["halted"] = True
        out["drift_pct"] = drift.get("divergence_pct")
        out["drift_sustained_min"] = drift.get("sustained_minutes")
        out["reasons"].append(
            f"broker drift tripped: divergence={drift.get('divergence_pct')}% "
            f"sustained={drift.get('sustained_minutes')}min"
        )
    return out


def today_fills() -> list[dict]:
    """Pull canonical fills from today (local 00:00 onward)."""
    fills_path = LOGS_ARGUS / "canonical_fills.jsonl"
    if not fills_path.exists():
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for line in open(fills_path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("side") != "EXIT":
            continue
        if d.get("ts", "")[:10] != today:
            continue
        out.append(d)
    return out


def recent_unfilled_forensics(hours: int = 24) -> list[dict]:
    """Pull recent unfilled-order forensics entries."""
    path = LOGS_ARGUS / "unfilled_orders.jsonl"
    if not path.exists():
        return []
    cutoff = time.time() - hours * 3600
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
            ts_str = d.get("ts", "")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.timestamp() < cutoff:
                continue
            out.append(d)
        except Exception:
            continue
    return out


def recent_errors_per_runner(lookback_lines: int = 100) -> dict[str, list[str]]:
    """Tail each runner.log and find recent errors. Returns {strategy: [error lines]}."""
    errors = defaultdict(list)
    err_patterns = ["[ERROR]", "FAILED", "TimeoutError", "broker drift", "HALT", "CRITICAL"]
    today = datetime.now().strftime("%Y-%m-%d")

    for d in (LOGS_ARGUS, LOGS_FORGE):
        for log_path in d.glob("*/runner.log"):
            if "_archive" in str(log_path):
                continue
            name = log_path.parent.name
            try:
                with open(log_path, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()[-lookback_lines:]
            except Exception:
                continue
            for line in lines:
                # Only include errors from today
                if today not in line:
                    continue
                if any(p in line for p in err_patterns):
                    errors[name].append(line.strip()[:140])
    return dict(errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"MONDAY WATCH  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')})")
    print(f"{'='*70}")

    # 1. Broker state
    b = broker_state()
    eq = b['equity']
    if eq is None:
        eq_str = "(N/A)"
    else:
        try:
            eq_str = f"${float(eq):,.2f}"
        except (TypeError, ValueError):
            eq_str = str(eq)
    print(f"\n[BROKER]  connected={b['connected']}  equity={eq_str}")
    if b["positions"]:
        print(f"          open positions ({len(b['positions'])}):")
        for p in b["positions"]:
            print(f"            {p['symbol']:10s} qty={p['qty']:>9.0f} {p['direction']}")
    else:
        print(f"          open positions: 0")

    # 1b. Halt state — what the runners are actually seeing
    h = halt_state()
    print(f"\n[FLEET HALT]  halted={h['halted']}")
    if h["halted"]:
        for r in h["reasons"]:
            print(f"  ! {r}")
    else:
        print(f"  none")

    # 2. Today's fills
    fills = today_fills()
    total = sum(float(f.get("pnl_usd", 0) or 0) for f in fills)
    print(f"\n[FILLS today]  count={len(fills)}  realized PnL=${total:+.2f}")
    for f in fills:
        pnl = float(f.get("pnl_usd", 0) or 0)
        marker = "+" if pnl > 0 else "-" if pnl < 0 else "0"
        print(f"  {marker} {f['ts'][:19]}  {f.get('strategy','?'):30s} {f.get('symbol','?'):8s} ${pnl:>+8.2f}")

    # 3. Heartbeats
    rows, concerns = heartbeat_table()
    print(f"\n[HEARTBEATS]  ({len(rows)} active runners)")
    if args.verbose:
        for name, age, cat in rows:
            print(f"  {age:>7.1f} min  {cat:10s}  {name}")
    else:
        # Show only FRESH count + STALE list
        fresh_n = sum(1 for _, _, c in rows if c == "FRESH")
        sleep_n = sum(1 for _, _, c in rows if c == "SLEEP-OK")
        four_hr_n = sum(1 for _, _, c in rows if c == "4HR-CYCLE")
        stale_n = sum(1 for _, _, c in rows if c == "STALE")
        print(f"  FRESH: {fresh_n}  SLEEP-OK: {sleep_n}  4HR-CYCLE: {four_hr_n}  STALE: {stale_n}")
        stale = [(n, a) for n, a, c in rows if c == "STALE"]
        for n, a in stale:
            print(f"  STALE  {a:>7.1f}min  {n}")

    # 4. Errors today
    errs = recent_errors_per_runner()
    print(f"\n[ERRORS today]  ({len(errs)} runners with errors)")
    for name, lines in sorted(errs.items()):
        print(f"  {name} ({len(lines)} errors)")
        if args.verbose:
            for line in lines[-3:]:
                print(f"    {line}")
        else:
            # Show only the most recent
            if lines:
                print(f"    {lines[-1][:120]}")

    # 5. Unfilled forensics
    unfilled = recent_unfilled_forensics(hours=24)
    print(f"\n[UNFILLED ORDERS (24h)]  count={len(unfilled)}")
    for u in unfilled[-5:]:
        c = u.get("contract", {})
        os_ = u.get("orderStatus", {})
        print(f"  {u.get('ts','?')[:19]}  {c.get('symbol','?'):8s} on {c.get('exchange','?'):6s}  status={os_.get('status','?')}  whyHeld='{os_.get('whyHeld','')}'")

    # 6. Concerns summary
    print(f"\n[CONCERNS]")
    if concerns:
        for c in concerns:
            print(f"  ! {c}")
    else:
        print(f"  none")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
