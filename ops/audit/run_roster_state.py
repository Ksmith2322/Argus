"""Comprehensive roster-state audit — answers "what's the actual state
of every strategy in the codebase right now?"

For each strategy that appears in ANY of (allocation_factors.json,
KILLED_STRATEGY_CUTOFFS, ACTIVE_ROSTER, or has a forge/<name>/runner.py),
classifies as one of:
  ACTIVE                — allocation_factor > 0 AND in ACTIVE_ROSTER
  KILLED                — in KILLED_STRATEGY_CUTOFFS
  PENDING_OPT_IN        — has a runner + 0.0 allocation but NOT killed
                          (operator-decision candidates like tom_spy, nov_spy)
  LIMBO                 — allocation = 0.0 but NOT in kill registry and
                          NOT a pending candidate (this should be empty)
  ABANDONED             — has a runner.py but nowhere referenced in
                          allocation_factors or kill registry

Output: human-readable table by default; --json for machine.

Run regularly during the "build + test" phase to catch drift between
the kill registry, allocation_factors.json, ACTIVE_ROSTER, and the
actual runner files on disk.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]


def _runner_dirs() -> set[str]:
    """All strategy names that have a forge/<name>/runner.py file."""
    out: set[str] = set()
    forge = REPO / "forge"
    if not forge.exists():
        return out
    for sub in forge.iterdir():
        if sub.is_dir() and (sub / "runner.py").exists():
            out.add(f"forge_{sub.name}")
    return out


def classify_all() -> list[dict]:
    factors_path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
    factors = (json.loads(factors_path.read_text(encoding="utf-8"))
                .get("factors", {}))
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
    except Exception:
        ACTIVE_ROSTER = set()
    runners = _runner_dirs()

    every_strategy = (set(factors) | set(KILLED_STRATEGY_CUTOFFS)
                        | set(ACTIVE_ROSTER) | runners)

    rows: list[dict] = []
    for s in sorted(every_strategy):
        f = factors.get(s)
        in_kill_registry = s in KILLED_STRATEGY_CUTOFFS
        in_active_roster = s in ACTIVE_ROSTER
        has_runner = s in runners

        # Classification rules:
        if in_active_roster and f and f > 0:
            verdict = "ACTIVE"
        elif in_kill_registry:
            verdict = "KILLED"
        elif (has_runner and f == 0.0
                and s not in KILLED_STRATEGY_CUTOFFS
                and not in_active_roster):
            verdict = "PENDING_OPT_IN"
        elif f == 0.0 and s not in KILLED_STRATEGY_CUTOFFS:
            verdict = "LIMBO"
        elif has_runner and s not in factors:
            verdict = "ABANDONED"
        else:
            verdict = "UNKNOWN"

        rows.append({
            "strategy": s,
            "verdict": verdict,
            "allocation_factor": f,
            "in_kill_registry": in_kill_registry,
            "in_active_roster": in_active_roster,
            "has_runner": has_runner,
            "kill_date": KILLED_STRATEGY_CUTOFFS.get(s, ""),
        })
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--filter", default=None,
                          help="Only show rows with this verdict "
                               "(ACTIVE, KILLED, PENDING_OPT_IN, LIMBO, ABANDONED)")
    args = parser.parse_args(argv)

    rows = classify_all()
    if args.filter:
        rows = [r for r in rows if r["verdict"] == args.filter.upper()]

    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "rows": rows,
        }, indent=2, default=str))
        return 0

    # Verdict counts
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print("=" * 78)
    print(f"ROSTER STATE AUDIT — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)
    print("Counts:")
    for v in ("ACTIVE", "KILLED", "PENDING_OPT_IN", "LIMBO",
                "ABANDONED", "UNKNOWN"):
        n = counts.get(v, 0)
        flag = " !" if v in ("LIMBO", "ABANDONED", "UNKNOWN") and n > 0 else ""
        print(f"  {v:<16} {n:>3}{flag}")
    print()
    print(f"{'strategy':<35} {'verdict':<16} {'alloc':>6}  "
          f"{'runner?':>7} {'kill_date':<12}")
    print("-" * 78)
    for r in rows:
        alloc = (f"{r['allocation_factor']}" if r["allocation_factor"] is not None
                   else "(none)")
        runner = "YES" if r["has_runner"] else "no"
        print(f"{r['strategy']:<35} {r['verdict']:<16} {alloc:>6}  "
              f"{runner:>7} {r['kill_date']:<12}")

    print()
    if counts.get("LIMBO", 0) > 0 or counts.get("ABANDONED", 0) > 0:
        print("[!] LIMBO or ABANDONED strategies found. Each should either:")
        print("    - Be added to KILLED_STRATEGY_CUTOFFS in helio/roi_filter.py")
        print("    - Be flagged as a PENDING_OPT_IN candidate (allocation 0.0)")
        print("    - Or have its runner.py deleted if truly abandoned")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
