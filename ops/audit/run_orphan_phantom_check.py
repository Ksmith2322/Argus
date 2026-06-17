"""Detect phantom positions held by killed strategies.

A "phantom" is a stale entry in a strategy's `state.json` (open_trade)
that survived the strategy being killed. The cluster_exposure module
reads these state files to compute portfolio cap consumption, so a
phantom inflates apparent cluster usage AND can block fresh entries
from active strategies that share the symbol.

This audit scans:
  forge/logs/<strategy>/state.json     — runner state with open_trade
  forge/logs/<strategy>/heartbeat.json — runtime metadata

For any strategy with allocation_factor=0 OR in KILLED_STRATEGY_CUTOFFS,
if state.open_trade or state.current_picks contains non-empty data, the
script flags it as a phantom + suggests the safe remediation step.

DOES NOT MUTATE STATE. Read-only audit; the actual state.json clear
is an operator action because it discards a record (no broker reconciliation
done in this script — operator is responsible for verifying broker is
actually flat before clearing).

USAGE:
    python -m ops.audit.run_orphan_phantom_check
    python -m ops.audit.run_orphan_phantom_check --json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _load_killed_strategies() -> set[str]:
    """All strategies that should NOT have any open position."""
    out: set[str] = set()
    try:
        from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
        out.update(KILLED_STRATEGY_CUTOFFS.keys())
    except Exception:
        pass
    # Add allocation_factor=0 strategies (broader than kill registry —
    # includes strategies in research-only state)
    try:
        path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for s, f in (cfg.get("factors") or {}).items():
            try:
                if float(f) == 0.0:
                    out.add(s)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


def _scan_state(strategy: str) -> Optional[dict]:
    """Return the state.json content for a forge strategy, or None."""
    short = strategy.replace("forge_", "")
    state_path = REPO / "forge" / "logs" / short / "state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _has_phantom(state: dict) -> tuple[bool, str]:
    """Inspect a state dict for non-empty position artifacts. Returns
    (has_phantom, description)."""
    if not isinstance(state, dict):
        return False, ""
    # Single-position runners use 'open_trade'
    ot = state.get("open_trade")
    if isinstance(ot, dict) and ot:
        return True, (f"open_trade @ {ot.get('entry_px', '?')} × "
                       f"{ot.get('position_size') or ot.get('size') or '?'} "
                       f"(entry_ts={ot.get('entry_ts', '?')})")
    # Multi-position runners use 'current_picks' (xs_momentum) or 'open_trades'
    cp = state.get("current_picks")
    if isinstance(cp, dict) and cp:
        return True, f"current_picks: {list(cp.keys())}"
    ots = state.get("open_trades")
    if isinstance(ots, dict) and ots:
        return True, f"open_trades: {list(ots.keys())}"
    op = state.get("open_positions")
    if isinstance(op, dict) and op:
        return True, f"open_positions: {list(op.keys())}"
    return False, ""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                          help="Emit JSON instead of human-readable text")
    parser.add_argument("--save", action="store_true",
                          help="Also write report to ops/reports/system_audit/")
    args = parser.parse_args(argv)

    killed = sorted(_load_killed_strategies())

    phantoms: list[dict] = []
    clean: list[str] = []
    for strategy in killed:
        state = _scan_state(strategy)
        if state is None:
            continue
        has, desc = _has_phantom(state)
        if has:
            phantoms.append({
                "strategy": strategy,
                "state_path": str(REPO / "forge" / "logs"
                                    / strategy.replace("forge_", "")
                                    / "state.json"),
                "phantom": desc,
            })
        else:
            clean.append(strategy)

    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_killed_strategies_scanned": len(killed),
            "n_phantoms_found": len(phantoms),
            "phantoms": phantoms,
            "clean_strategies_with_state_files": clean,
        }, indent=2, default=str))
    else:
        print("=" * 78)
        print("ORPHAN PHANTOM POSITION CHECK")
        print("=" * 78)
        print(f"Killed/deallocated strategies scanned: {len(killed)}")
        print()
        if not phantoms:
            print("  No phantom positions found.")
        else:
            print(f"  {len(phantoms)} phantom position(s) found:\n")
            for ph in phantoms:
                print(f"  [!] {ph['strategy']}")
                print(f"      {ph['phantom']}")
                print(f"      file: {ph['state_path']}")
                print()
            print("REMEDIATION (operator):")
            print()
            print("  1. CONFIRM the broker is actually flat for the symbol(s).")
            print("     `python -m ops.broker_status` (or check TWS Positions tab)")
            print()
            print("  2. If broker is flat, the state is stale. Clear with:")
            print("     python -c \"import json,sys;p=sys.argv[1];")
            print("       s=json.load(open(p));s['open_trade']=None;")
            print("       json.dump(s,open(p,'w'),indent=2)\" <state_path>")
            print()
            print("  3. If broker has the position, USE emergency_close.py")
            print("     to actually flatten it. DO NOT clear state without")
            print("     a corresponding broker action — that creates an")
            print("     unmanaged position.")
            print()
            print("  4. Re-run capacity_stress to confirm cluster cap freed:")
            print("     python -m ops.audit.run_capacity_stress")

    if args.save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = OUT_DIR / f"orphan_phantom_check_{ts}.json"
        out_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_killed_strategies_scanned": len(killed),
            "n_phantoms_found": len(phantoms),
            "phantoms": phantoms,
            "clean_strategies_with_state_files": clean,
        }, indent=2, default=str), encoding="utf-8")
        if not args.json:
            print(f"\nJSON: {out_path}")

    return 0 if not phantoms else 1


if __name__ == "__main__":
    raise SystemExit(main())
