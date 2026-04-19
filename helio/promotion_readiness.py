"""Promotion-readiness summary for the applied strategy shortlist.

Per PROFIT_MAX_SYSTEM_BLUEPRINT §18.10, there are two gates layered on top of
the canonical `promotion_gate_v2`:

  - 30-trade review gate: 30 valid trades + PF >= 1.20 + positive expectancy
    → suspend new strategy families for 30 days, focus on observation.
  - Canonical gate: 60 valid trades + PF >= 1.30 + stable execution
    → promotion-eligible via existing promotion_gate_v2.py.

This module evaluates each shortlist strategy against BOTH gates and prints a
per-strategy status. Intended to be run weekly during monitoring mode:

    python -m helio.promotion_readiness
    python -m helio.promotion_readiness --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from helio.fleet_sizing import compute_strategy_stats, get_sizing_anchor_usd  # noqa: E402

# Shortlist from blueprint §18.6
SHORTLIST = [
    {"label": "forge_gdx_gld",       "card": "research/strategy_cards/forge_gdx_gld.md"},
    {"label": "forge_gld_pm_long",   "card": "research/strategy_cards/forge_gld_pm_long.md"},
    {"label": "forge_wick_gbpusd",   "card": "research/strategy_cards/forge_wick_gbpusd.md"},
    {"label": "apollo_earnings_drift","card": "research/strategy_cards/apollo_earnings_drift.md"},
    # Argus pairs — included to show they're below even the 30-trade gate
    {"label": "argus_usdjpy",        "card": None},
    {"label": "argus_gbpusd",        "card": None},
    {"label": "argus_cadjpy",        "card": None},
]

# Gate thresholds (blueprint §18.10 + canonical promotion_gate_v2)
REVIEW_GATE = {"min_trades": 30, "min_pf": 1.20, "min_expectancy": 0.0}
CANONICAL_GATE = {"min_trades": 60, "min_pf": 1.30, "min_expectancy": 0.0}


def _evaluate_gate(stats: dict, gate: dict) -> tuple[bool, list[str]]:
    """Return (passes, list_of_blockers)."""
    blockers: list[str] = []
    if stats["trades"] < gate["min_trades"]:
        blockers.append(f"need {gate['min_trades']} valid trades, have {stats['trades']}")
    if stats["trades"] > 0:
        if stats["profit_factor"] < gate["min_pf"]:
            blockers.append(f"PF {stats['profit_factor']:.2f} < required {gate['min_pf']}")
        expectancy = stats["pnl_usd"] / stats["trades"] if stats["trades"] else 0
        if expectancy < gate["min_expectancy"]:
            blockers.append(f"expectancy ${expectancy:.2f}/trade < required ${gate['min_expectancy']}")
    return len(blockers) == 0, blockers


def evaluate_strategy(spec: dict) -> dict:
    label = spec["label"]
    stats = compute_strategy_stats(label)
    card_path = (_REPO / spec["card"]) if spec.get("card") else None
    has_card = bool(card_path and card_path.exists())

    review_ok, review_blockers = _evaluate_gate(stats, REVIEW_GATE)
    canonical_ok, canonical_blockers = _evaluate_gate(stats, CANONICAL_GATE)

    # Determine near-term action per blueprint §18.10
    if canonical_ok and has_card:
        action = "PROMOTION_ELIGIBLE"
    elif review_ok and has_card:
        action = "REVIEW_GATE_PASSED — 30-day new-strategy freeze active"
    elif stats["trades"] >= 20 and stats["profit_factor"] > 0 and stats["profit_factor"] < 1.0:
        action = "KILL_CANDIDATE — PF below 1.0 on meaningful sample"
    elif stats["trades"] == 0:
        action = "NO_LIVE_EVIDENCE — keep running or escalate"
    else:
        action = "OBSERVE_MORE"

    expectancy = stats["pnl_usd"] / stats["trades"] if stats["trades"] else 0
    return {
        "strategy": label,
        "has_strategy_card": has_card,
        "stats": stats,
        "expectancy_usd": round(expectancy, 2),
        "review_gate_passed": review_ok,
        "review_gate_blockers": review_blockers,
        "canonical_gate_passed": canonical_ok,
        "canonical_gate_blockers": canonical_blockers,
        "next_action": action,
    }


def build_report() -> dict:
    anchor = get_sizing_anchor_usd()
    results = [evaluate_strategy(spec) for spec in SHORTLIST]
    promotion_eligible = [r["strategy"] for r in results if r["next_action"] == "PROMOTION_ELIGIBLE"]
    review_passed = [r["strategy"] for r in results if r["next_action"].startswith("REVIEW_GATE_PASSED")]
    kill_candidates = [r["strategy"] for r in results if r["next_action"].startswith("KILL_CANDIDATE")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor_usd": anchor,
        "review_gate": REVIEW_GATE,
        "canonical_gate": CANONICAL_GATE,
        "strategies": results,
        "summary": {
            "promotion_eligible": promotion_eligible,
            "review_gate_passed": review_passed,
            "kill_candidates": kill_candidates,
            "total_evaluated": len(results),
        },
    }


def _print_table(report: dict) -> None:
    print(f"Promotion readiness — anchor ${report['anchor_usd']:,.0f}")
    print(f"Generated: {report['generated_at']}")
    print(f"Review gate: {REVIEW_GATE['min_trades']}+ trades, PF >= {REVIEW_GATE['min_pf']}")
    print(f"Canonical gate: {CANONICAL_GATE['min_trades']}+ trades, PF >= {CANONICAL_GATE['min_pf']}")
    print()
    print(f"{'Strategy':28s} {'Card':>4s} {'Trades':>7s} {'PF':>6s} {'Exp/trade':>10s}  Next action")
    print("-" * 110)
    for r in report["strategies"]:
        s = r["stats"]
        card_mark = "yes" if r["has_strategy_card"] else "—"
        pf_str = f"{s['profit_factor']:.2f}" if s["trades"] > 0 else "—"
        print(f"  {r['strategy']:26s} {card_mark:>4s} {s['trades']:>7d} {pf_str:>6s} "
              f"${r['expectancy_usd']:>9.2f}  {r['next_action']}")
    print()
    sm = report["summary"]
    if sm["promotion_eligible"]:
        print(f"  PROMOTION ELIGIBLE: {', '.join(sm['promotion_eligible'])}")
    if sm["review_gate_passed"]:
        print(f"  REVIEW GATE PASSED (freeze new strategies 30d): {', '.join(sm['review_gate_passed'])}")
    if sm["kill_candidates"]:
        print(f"  KILL CANDIDATES: {', '.join(sm['kill_candidates'])}")
    if not any([sm["promotion_eligible"], sm["review_gate_passed"], sm["kill_candidates"]]):
        print("  No strategies at any gate yet. Keep running + accumulate evidence.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    ap.add_argument("--output", type=str, default=None,
                    help="Write report JSON to file (default: argus_flow/logs/promotion_readiness_report.json)")
    args = ap.parse_args()

    report = build_report()

    out_path = Path(args.output) if args.output else (_REPO / "argus_flow" / "logs" / "promotion_readiness_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
        print(f"\nWrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
