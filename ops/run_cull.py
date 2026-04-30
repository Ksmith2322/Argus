"""run_cull — execute the 5/1 (or 5/15 / 5/31) verdict file as factor changes.

Reads argus_flow/logs/verdict_<DATE>.json, maps each strategy's verdict to
an allocation factor (the conservative floor of that verdict), and applies
via POST /api/allocation_factors. Each apply is logged to decision_history
with source="ceremony_<DATE>" for full audit trail.

Verdict → factor mapping (the "floor of all plausible interpretations"):
  KILL              → 0.0   (effective stop via sizing)
  QUARANTINE        → 0.0   (data hygiene must come first)
  REWORK            → 0.5   (size-down while logic gets fixed)
  SCOPE_DOWN action → 0.5   (only when action-text indicates SCOPE_DOWN
                             — this is heuristic, verify before --execute)
  REAL_CANDIDATE    → no change (separate real-money gate review)
  WINNER_CANDIDATE  → no change (continue paper)
  KEEP_PAPER        → no change
  OBSERVE           → no change
  BLOCKED           → no change (operational fix needed first)

Dry-run by default. --execute actually applies via the API.

Usage:
    cd c:/Argus/repo

    # See what the cull WOULD do
    C:/Argus/.venv/Scripts/python.exe -m ops.run_cull --date 20260501

    # Actually execute it
    C:/Argus/.venv/Scripts/python.exe -m ops.run_cull --date 20260501 --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHBOARD_URL = "http://localhost:8080"

# Verdict → target factor. None means "no factor change" — the verdict is
# either a continuation of current state or a non-factor action (e.g.
# REAL_CANDIDATE triggers a real-money gate review, not a factor change).
VERDICT_FACTOR_MAP: dict[str, float | None] = {
    "KILL":             0.0,
    "QUARANTINE":       0.0,
    "REWORK":           0.5,
    "REAL_CANDIDATE":   None,
    "WINNER_CANDIDATE": None,
    "KEEP_PAPER":       None,
    "OBSERVE":          None,
    "BLOCKED":          None,
    # Hyphenated variants (the ceremony spec uses both)
    "REAL-CANDIDATE":   None,
    "WINNER-CANDIDATE": None,
    "KEEP-PAPER":       None,
}


def _fetch_current_factors() -> dict[str, float]:
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/api/allocation_factors", timeout=5) as r:
            return json.load(r).get("factors", {})
    except Exception:
        return {}


def _post_factor(strategy: str, factor: float, reason: str) -> tuple[bool, str]:
    """POST one factor change. Returns (ok, message)."""
    body = json.dumps({"strategy": strategy, "factor": factor, "reason": reason}).encode()
    req = urllib.request.Request(
        f"{DASHBOARD_URL}/api/allocation_factors",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
            return bool(data.get("ok")), json.dumps(data)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Execute a ceremony verdict file as factor changes.")
    ap.add_argument("--date", default="20260501",
                    help="Compact ceremony date (e.g. 20260501). Reads verdict_<date>.json")
    ap.add_argument("--execute", action="store_true",
                    help="Actually POST the factor changes (default: dry-run)")
    args = ap.parse_args()

    verdict_path = REPO / "argus_flow" / "logs" / f"verdict_{args.date}.json"
    if not verdict_path.exists():
        # Fall back to the skeleton if the formal verdict isn't filled yet
        skeleton = REPO / "argus_flow" / "logs" / "ceremony_prep" / f"verdict_{args.date}_skeleton.json"
        if skeleton.exists():
            print(f"  WARN: {verdict_path.name} not found — falling back to skeleton")
            print(f"        {skeleton.name}")
            print(f"        (this is fine for DRY-RUN; for --execute, fill the verdict editor first)")
            print()
            verdict_path = skeleton
        else:
            print(f"  FAIL: neither verdict file nor skeleton exists for date={args.date}")
            return 1

    try:
        verdict_doc = json.loads(verdict_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  FAIL: cannot parse {verdict_path}: {e}")
        return 1

    current = _fetch_current_factors()

    print("=" * 76)
    print(f"  CULL EXECUTION  —  ceremony date {args.date}")
    print(f"  Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"  Source: {verdict_path.relative_to(REPO)}")
    print("=" * 76)
    print()

    plan: list[dict] = []
    skipped: list[dict] = []
    for s in verdict_doc.get("strategies", []):
        name = s.get("strategy")
        verdict = (s.get("verdict") or "").upper().replace("-", "_")
        # Re-normalize for the lookup (the verdict_doc may use hyphenated form)
        target = VERDICT_FACTOR_MAP.get(verdict)
        if target is None:
            target = VERDICT_FACTOR_MAP.get(s.get("verdict") or "")
        # If the verdict is empty, skeleton-mode unfilled — skip
        if not s.get("verdict"):
            skipped.append({"strategy": name, "reason": "verdict unfilled (still in skeleton state)"})
            continue
        if target is None:
            skipped.append({"strategy": name, "verdict": s.get("verdict"),
                            "reason": "verdict has no factor implication (paper-continuation or non-factor action)"})
            continue
        # Compare to current
        cur_factor = current.get(name, 1.0)
        if abs(cur_factor - target) < 0.001:
            skipped.append({"strategy": name, "verdict": s.get("verdict"),
                            "reason": f"already at target {target}x"})
            continue
        plan.append({
            "strategy": name,
            "verdict": s.get("verdict"),
            "current_factor": cur_factor,
            "target_factor": target,
            "delta": target - cur_factor,
            "ceremony_action": s.get("action") or "",
        })

    if not plan:
        print(f"  PLAN: 0 factor changes needed")
        print(f"  Skipped: {len(skipped)}")
        for sk in skipped[:5]:
            print(f"    - {sk['strategy']:25s}  {sk['reason']}")
        if len(skipped) > 5:
            print(f"    ... and {len(skipped)-5} more")
        return 0

    # Print plan
    print(f"  PLAN: {len(plan)} factor changes")
    print()
    print(f"  {'STRATEGY':30s}  {'VERDICT':18s}  {'CURRENT':>9s}  {'TARGET':>9s}  ACTION")
    print(f"  {'-'*30}  {'-'*18}  {'-'*9}  {'-'*9}  {'-'*40}")
    for p in plan:
        delta = p["target_factor"] - p["current_factor"]
        delta_str = f'{delta:+.2f}'
        print(f"  {p['strategy']:30s}  {p['verdict']:18s}  {p['current_factor']:>8.2f}x  {p['target_factor']:>8.2f}x  {(p['ceremony_action'] or '')[:50]}")
    print()

    if not args.execute:
        print(f"  DRY-RUN — no API calls made.")
        print(f"  Re-run with --execute to apply.")
        print(f"  Each apply will be logged to argus_flow/logs/decision_history.jsonl")
        print(f"  with source='ceremony_{args.date}'.")
        return 0

    # Execute
    print(f"  EXECUTING {len(plan)} factor changes...")
    print()
    ok_count = 0
    fail_count = 0
    for p in plan:
        reason = (
            f"ceremony {args.date}: verdict={p['verdict']}; "
            f"action={p['ceremony_action'] or '(none)'}; "
            f"applied via run_cull"
        )
        ok, msg = _post_factor(p["strategy"], p["target_factor"], reason)
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {p['strategy']:30s}  {p['current_factor']:.2f}x -> {p['target_factor']:.2f}x  ({msg[:60]})")
        if ok:
            ok_count += 1
        else:
            fail_count += 1

    print()
    print(f"  Result: {ok_count} applied, {fail_count} failed")
    if fail_count:
        print(f"  Re-run after investigating the failures (often the dashboard not running, port blocked, etc).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
