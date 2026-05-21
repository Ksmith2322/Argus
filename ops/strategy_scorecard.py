"""Daily fleet scorecard — one GREEN/YELLOW/RED card per strategy.

Auto-discovers strategies from argus_flow/configs/allocation_factors.json
(any strategy with allocation > 0). For each, computes the verdict
using helio.strategy_scorecard with default artifact paths.

Examples:
  python -m ops.strategy_scorecard
  python -m ops.strategy_scorecard --json
  python -m ops.strategy_scorecard --strategy forge_gld_pm_long

Exit codes:
  0 = all GREEN
  1 = at least one YELLOW or RED
  2 = configuration or artifact path missing
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOCATIONS_PATH = REPO / "argus_flow" / "configs" / "allocation_factors.json"
FILLS_PATH = REPO / "argus_flow" / "data" / "canonical_fills.jsonl"
TRACE_DIR = REPO / "argus_flow" / "logs" / "traces"
HEARTBEAT_DIR = REPO / "forge" / "logs"

from helio.strategy_scorecard import score_fleet, score_strategy


def _load_allocations() -> list[tuple[str, float]]:
    """Return list of (strategy, allocation) from allocation_factors.json.
    Filters to strategies with allocation > 0 by default."""
    if not ALLOCATIONS_PATH.exists():
        return []
    try:
        data = json.loads(ALLOCATIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    factors = data.get("factors") or data  # support either schema
    out: list[tuple[str, float]] = []
    if isinstance(factors, dict):
        for strategy, val in factors.items():
            try:
                allocation = float(val) if not isinstance(val, dict) else float(val.get("factor", 0))
            except (TypeError, ValueError):
                allocation = 0.0
            if allocation > 0:
                out.append((strategy, allocation))
    return out


def _status_color(status: str) -> str:
    return {"GREEN": "+", "YELLOW": "~", "RED": "!"}.get(status, "?")


def _print_human(scores):
    if not scores:
        print("no allocated strategies found")
        return
    name_w = max(len(s.strategy) for s in scores)
    name_w = max(name_w, len("strategy"))

    print(f"  {' ':1}  {'strategy':<{name_w}}  {'status':<7}  reason")
    print(f"  {'-':1}  {'-' * name_w}  {'-' * 7}  {'-' * 40}")
    for s in scores:
        mark = _status_color(s.status)
        reason = s.reason or "all checks passed"
        print(f"  {mark:1}  {s.strategy:<{name_w}}  {s.status:<7}  {reason}")
    print()
    by_status = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for s in scores:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    print(f"totals: {by_status.get('GREEN', 0)} GREEN, "
          f"{by_status.get('YELLOW', 0)} YELLOW, "
          f"{by_status.get('RED', 0)} RED")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Daily per-strategy fleet scorecard")
    p.add_argument("--strategy", default=None,
                   help="score a single strategy by name (skips allocation discovery)")
    p.add_argument("--allocation", type=float, default=0.5,
                   help="when --strategy is used, assume this allocation (default 0.5)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of cards")
    args = p.parse_args(argv)

    if args.strategy:
        # Single strategy scoring path
        t_path = None
        if TRACE_DIR.exists():
            from helio.strategy_scorecard import _today_iso
            candidate = TRACE_DIR / f"{args.strategy}_{_today_iso()}.jsonl"
            if candidate.exists():
                t_path = candidate
        hb_path = None
        if HEARTBEAT_DIR.exists():
            candidate = HEARTBEAT_DIR / args.strategy / "heartbeat.json"
            if candidate.exists():
                hb_path = candidate
        score = score_strategy(
            strategy=args.strategy, allocation=args.allocation,
            fills_path=FILLS_PATH if FILLS_PATH.exists() else None,
            trace_path=t_path, heartbeat_path=hb_path,
        )
        scores = [score]
    else:
        allocations = _load_allocations()
        if not allocations:
            print(f"no allocations found in {ALLOCATIONS_PATH}", file=sys.stderr)
            return 2
        scores = score_fleet(
            allocations,
            fills_path=FILLS_PATH if FILLS_PATH.exists() else None,
            trace_dir=TRACE_DIR if TRACE_DIR.exists() else None,
            heartbeat_dir=HEARTBEAT_DIR if HEARTBEAT_DIR.exists() else None,
        )

    if args.json:
        print(json.dumps({"scores": [asdict(s) for s in scores]},
                          indent=2, default=str))
    else:
        _print_human(scores)

    return 0 if all(s.status == "GREEN" for s in scores) else 1


if __name__ == "__main__":
    sys.exit(main())
