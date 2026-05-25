"""ops/auto_promotion.py — operator CLI for the auto-promotion loop.

Symmetric to ops/auto_pause.py but for the OPPOSITE direction.
Auto-pause fires when live evidence says "this strategy is broken;
demote." Auto-promotion fires when live + audit evidence says "this
strategy is doing what we hoped; consider raising its allocation."

Two modes:

1. **Default (recommend)**: snapshot the gate, write recommendations
   to argus_flow/logs/auto_promotion_recommendations.json. No
   allocation changes. Daily-cron mode.

       python -m ops.auto_promotion

2. **--apply**: read the most recent recommendations + propose
   allocation_factor flips. Requires --confirm to actually write.

       python -m ops.auto_promotion --apply              # dry-run
       python -m ops.auto_promotion --apply --confirm    # write

WHY HUMAN-GATED (same as auto_pause)
====================================
Capacity, real-money status, risk policy — the audit can recommend
a promotion, but the operator owns the decision. Especially given
the 5/25 audit finding that the strategy is "tracking factor beta"
rather than generating alpha — every promotion needs operator
context the audit can't see.

Cron suggestion:
  Weekly Sunday at 22:00 UTC. Runs default (no --apply). Operator
  reviews recommendations + decides Monday whether to --apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]
ALLOCATION_FACTORS_PATH = _REPO / "argus_flow" / "configs" / "allocation_factors.json"
RECOMMENDATIONS_PATH = (
    _REPO / "argus_flow" / "logs" / "auto_promotion_recommendations.json"
)
APPLY_EVENTS_PATH = (
    _REPO / "argus_flow" / "logs" / "auto_promotion_apply_events.jsonl"
)


def _load_recommendations() -> dict:
    if not RECOMMENDATIONS_PATH.exists():
        raise FileNotFoundError(
            f"no recommendations file at {RECOMMENDATIONS_PATH}. "
            f"Run `python -m ops.auto_promotion` first."
        )
    return json.loads(RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))


def _load_allocation_factors() -> dict:
    return json.loads(
        ALLOCATION_FACTORS_PATH.read_text(encoding="utf-8")
    )


def _save_allocation_factors(cfg: dict) -> None:
    ALLOCATION_FACTORS_PATH.write_text(
        json.dumps(cfg, indent=2, default=str), encoding="utf-8"
    )


def _append_event(event: dict) -> None:
    APPLY_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(APPLY_EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def snapshot() -> dict:
    """Run the recommendation engine and persist the result."""
    from helio.auto_promotion import compute_recommendations
    result = compute_recommendations()
    RECOMMENDATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOMMENDATIONS_PATH.write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    return result


def apply_recommendations(*, confirm: bool = False,
                          strategies: list[str] | None = None) -> dict:
    """Read the latest recommendations + flip allocations.

    `strategies` filter lets the operator apply only specific
    promotions instead of all-or-nothing.
    """
    recs = _load_recommendations()
    factors_cfg = _load_allocation_factors()
    factors = factors_cfg.get("factors", {})

    candidates = recs.get("recommendations") or []
    if strategies:
        wanted = set(strategies)
        candidates = [c for c in candidates if c["strategy"] in wanted]

    ts = datetime.now(timezone.utc).isoformat()
    applied = []
    skipped = []
    for c in candidates:
        strat = c["strategy"]
        old = float(factors.get(strat, 0.0))
        new = float(c["proposed_alloc"])
        if old == new:
            skipped.append({"strategy": strat,
                            "reason": "already at proposed value"})
            continue
        action = {
            "strategy": strat,
            "old_alloc": old,
            "new_alloc": new,
            "delta": round(new - old, 3),
            "confidence": c.get("confidence"),
            "reasons": c.get("reasons", []),
            "ts": ts,
        }
        if not confirm:
            action["note"] = "DRY_RUN: would flip on --confirm"
            applied.append(action)
            continue
        # Persist
        factors[strat] = new
        _append_event({"event": "promotion_applied", **action})
        applied.append(action)

    if confirm and applied:
        # Bump version + kill_log entry summarizing changes
        factors_cfg["factors"] = factors
        ver = factors_cfg.get("version", "")
        factors_cfg["version"] = (
            f"{ver.split('.v')[0]}.v{int((ver.split('.v')[1] or '0').split('_')[0]) + 1 if '.v' in ver else 'X'}_"
            f"auto_promotion"
        )
        factors_cfg["last_updated"] = ts
        log = factors_cfg.get("_kill_log") or []
        log.append(
            f"{ts[:10]}: AUTO-PROMOTION APPLIED via "
            f"`ops.auto_promotion --apply --confirm`. "
            f"{len(applied)} strategy(s) flipped: "
            + "; ".join(
                f"{a['strategy']} {a['old_alloc']:.2f} -> {a['new_alloc']:.2f}"
                for a in applied
            )
        )
        factors_cfg["_kill_log"] = log
        _save_allocation_factors(factors_cfg)

    return {
        "ts": ts,
        "n_applied": len(applied),
        "n_skipped": len(skipped),
        "confirmed": confirm,
        "applied": applied,
        "skipped": skipped,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply pending recommendations (requires --confirm to "
             "actually write)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Required with --apply to actually flip allocations",
    )
    parser.add_argument(
        "--strategy", action="append", default=[],
        help="Optionally restrict --apply to specific strategies",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of human text")
    args = parser.parse_args(argv)

    if not args.apply:
        # Recommend mode
        result = snapshot()
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0
        print(f"AUTO-PROMOTION recommendation snapshot @ "
              f"{result['generated_at']}")
        print(f"  {result['n_recommendations']} recommendation(s)")
        for r in result.get("recommendations", []):
            print(f"\n  {r['strategy']}: {r['current_alloc']:.2f} -> "
                  f"{r['proposed_alloc']:.2f} "
                  f"(confidence: {r['confidence']})")
            for reason in r.get("reasons", []):
                print(f"    - {reason}")
        print(f"\nTo apply: python -m ops.auto_promotion --apply --confirm")
        return 0

    # Apply mode
    result = apply_recommendations(
        confirm=args.confirm,
        strategies=args.strategy or None,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    mode = "CONFIRMED" if args.confirm else "DRY_RUN"
    print(f"AUTO-PROMOTION --apply ({mode})")
    print(f"  {result['n_applied']} flip(s), "
          f"{result['n_skipped']} skipped")
    for a in result["applied"]:
        print(f"  {a['strategy']}: {a['old_alloc']:.2f} -> "
              f"{a['new_alloc']:.2f} (delta {a['delta']:+.2f})")
    if not args.confirm and result["n_applied"]:
        print(f"\n  This was a DRY RUN. Re-run with --confirm to "
              f"persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
