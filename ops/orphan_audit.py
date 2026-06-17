#!/usr/bin/env python3
"""ops/orphan_audit.py — flag broker positions not owned by any active strategy.

Background (2026-05-18): killed forge_multi_orb on 5/7 but it left a QQQ 7
position at IBKR that no active strategy owns. The position sat undetected
for 11 days, using buying-power capacity with no thesis behind it.

This script maps each broker position to its expected strategy owner. Any
broker position without an active-strategy owner is flagged as an orphan
that needs manual flatten (or kill-workflow improvement to prevent the next
one).

Usage:
    python -m ops.orphan_audit          # report only
    python -m ops.orphan_audit --json   # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BROKER_SNAPSHOT = REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json"
ALLOCATION_FACTORS = REPO / "argus_flow" / "configs" / "allocation_factors.json"

# Symbol -> [list of active strategies that could own it]
# Update this map when new strategies are added or symbols rotate.
SYMBOL_OWNERS = {
    "GLD": ["forge_gld_pm_long", "forge_gdx_gld"],
    "GDX": ["forge_gdx_gld"],
    "SPY": ["forge_spy_trend_follower", "forge_fomc_drift", "forge_rebalance"],
    "QQQ": [],  # No active strategy trades QQQ as of 2026-05-18
    "UVXY": ["forge_vix_revert"],
    "SVXY": ["forge_vix_revert"],
    "USD.JPY": ["argus_usdjpy", "forge_jpy_pm_short"],
    "GBP.USD": ["argus_gbpusd", "forge_wick_gbpusd"],
    "CAD.JPY": ["argus_cadjpy", "forge_jpy_pm_short"],
    "AUD.USD": ["forge_aud_asian_breakout"],
    "MNQ": ["forge_nq_overnight", "forge_cuebanks", "forge_mamba", "forge_nq_london_close"],
    "MYM": ["forge_cuebanks", "forge_mamba", "forge_tori"],
    "MES": [],
    "M2K": [],
    "YINN": ["forge_tom_international"],
    "EWZ": ["forge_tom_international"],
    "EFA": ["forge_tom_international"],
}

# Strategies that are KILLED (allocation_factor=0). Even if they appear in
# SYMBOL_OWNERS, they shouldn't be considered active owners.
def _killed_strategies() -> set[str]:
    if not ALLOCATION_FACTORS.exists():
        return set()
    try:
        cfg = json.loads(ALLOCATION_FACTORS.read_text(encoding="utf-8"))
    except Exception:
        return set()
    factors = cfg.get("factors", {}) or {}
    return {s for s, f in factors.items() if float(f) == 0.0}


def audit() -> dict:
    """Run the audit. Returns {orphans: [...], ok: [...]}."""
    if not BROKER_SNAPSHOT.exists():
        return {"error": f"broker_snapshot.json not found at {BROKER_SNAPSHOT}"}

    snap = json.loads(BROKER_SNAPSHOT.read_text(encoding="utf-8"))
    positions = snap.get("positions", {})
    killed = _killed_strategies()
    orphans = []
    ok = []

    for sym, p in positions.items():
        qty = float(p.get("qty", 0) or 0)
        if qty == 0:
            continue
        owners = SYMBOL_OWNERS.get(sym, None)
        if owners is None:
            # Unknown symbol — explicit orphan
            orphans.append({
                "symbol": sym,
                "qty": qty,
                "direction": p.get("direction", "?"),
                "avg_cost": p.get("avg_cost", 0),
                "reason": "no SYMBOL_OWNERS entry — update ops/orphan_audit.py",
            })
            continue
        # Filter out killed strategies
        active_owners = [s for s in owners if s not in killed]
        if not active_owners:
            orphans.append({
                "symbol": sym,
                "qty": qty,
                "direction": p.get("direction", "?"),
                "avg_cost": p.get("avg_cost", 0),
                "reason": (f"all possible owners are KILLED: {owners}"
                           if owners else "no strategy owns this symbol"),
            })
        else:
            ok.append({
                "symbol": sym,
                "qty": qty,
                "direction": p.get("direction", "?"),
                "owners": active_owners,
            })

    return {"orphans": orphans, "ok": ok, "killed_strategies": sorted(killed)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = audit()
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    orphans = result["orphans"]
    ok = result["ok"]
    print(f"\n=== Orphan position audit ===")
    print(f"\nOK ({len(ok)} positions with active owners):")
    for o in ok:
        print(f"  {o['symbol']:10s} qty={o['qty']:>9.0f} {o['direction']:5s}  owners: {', '.join(o['owners'])}")

    if orphans:
        print(f"\nORPHANS ({len(orphans)} positions with no active owner):")
        for o in orphans:
            print(f"  {o['symbol']:10s} qty={o['qty']:>9.0f} {o['direction']:5s}  avg=${o['avg_cost']:.4f}")
            print(f"             reason: {o['reason']}")
        print(f"\n  >> Manually flatten these in TWS, or update SYMBOL_OWNERS in ops/orphan_audit.py")
        return 2  # nonzero exit so cron jobs / CI can flag
    else:
        print(f"\nORPHANS: none.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
