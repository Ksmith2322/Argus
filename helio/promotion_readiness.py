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
from helio.strategy_confidence import load_confidence_artifact  # noqa: E402

# Shortlist from blueprint §18.6. `artifact_label` maps the shortlist label
# to the matching strategy_confidence artifact name — used to consult the
# artifact's kill-or-rework disposition before any promotion advance. None
# means "no artifact exists for this strategy yet".
SHORTLIST = [
    {"label": "forge_gdx_gld",        "card": "research/strategy_cards/forge_gdx_gld.md",          "artifact_label": None},
    {"label": "forge_gld_pm_long",    "card": "research/strategy_cards/forge_gld_pm_long.md",      "artifact_label": None},
    {"label": "forge_wick_gbpusd",    "card": "research/strategy_cards/forge_wick_gbpusd.md",      "artifact_label": None},
    {"label": "apollo_earnings_drift","card": "research/strategy_cards/apollo_earnings_drift.md",  "artifact_label": "apollo"},
    # Argus pairs — included to show they're below even the 30-trade gate
    {"label": "argus_usdjpy",         "card": None,                                                 "artifact_label": None},
    {"label": "argus_gbpusd",         "card": None,                                                 "artifact_label": None},
    {"label": "argus_cadjpy",         "card": None,                                                 "artifact_label": None},
]

# Disposition statuses that block promotion advancement. A strategy with
# any of these MUST NOT be promoted, regardless of stats — the operator
# has ruled that the computed evidence is negative or misleading.
BLOCKING_DISPOSITION_STATUSES = frozenset({"kill", "shelve", "scope_down"})

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


def _check_disposition(artifact_label: str | None) -> dict | None:
    """Return disposition info if the artifact carries a blocking
    kill/shelve/scope_down ruling, else None. Non-blocking statuses
    (promote_candidate, paper_only, research_only) return None — the gate
    doesn't care about them; they're informational only."""
    if not artifact_label:
        return None
    art = load_confidence_artifact(artifact_label)
    if art is None or art.disposition is None:
        return None
    if art.disposition.status in BLOCKING_DISPOSITION_STATUSES:
        return {
            "status": art.disposition.status,
            "reason": art.disposition.reason,
            "decided_at": art.disposition.decided_at,
            "next_review_date": art.disposition.next_review_date,
            "artifact_label": artifact_label,
        }
    return None


def evaluate_strategy(spec: dict) -> dict:
    label = spec["label"]
    stats = compute_strategy_stats(label)
    card_path = (_REPO / spec["card"]) if spec.get("card") else None
    has_card = bool(card_path and card_path.exists())

    review_ok, review_blockers = _evaluate_gate(stats, REVIEW_GATE)
    canonical_ok, canonical_blockers = _evaluate_gate(stats, CANONICAL_GATE)

    # Disposition check trumps the stat-based gates. A kill/shelve/scope_down
    # ruling means the operator has ruled the evidence negative regardless of
    # what the live stats happen to show.
    blocking_disposition = _check_disposition(spec.get("artifact_label"))

    # Determine near-term action per blueprint §18.10
    if blocking_disposition is not None:
        action = (
            f"BLOCKED_BY_DISPOSITION ({blocking_disposition['status']}) — "
            f"do not promote; see artifact {blocking_disposition['artifact_label']}.json"
        )
    elif canonical_ok and has_card:
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
        "blocking_disposition": blocking_disposition,
    }


def build_report() -> dict:
    anchor = get_sizing_anchor_usd()
    results = [evaluate_strategy(spec) for spec in SHORTLIST]
    promotion_eligible = [r["strategy"] for r in results if r["next_action"] == "PROMOTION_ELIGIBLE"]
    review_passed = [r["strategy"] for r in results if r["next_action"].startswith("REVIEW_GATE_PASSED")]
    kill_candidates = [r["strategy"] for r in results if r["next_action"].startswith("KILL_CANDIDATE")]
    disposition_blocked = [r["strategy"] for r in results if r["next_action"].startswith("BLOCKED_BY_DISPOSITION")]

    # Scan every artifact for a disposition — surfaces strategies that are
    # NOT on the shortlist but still carry a kill/shelve/scope_down ruling
    # (e.g. mamba, sector_rot, vix_revert). This makes the report the
    # single place to see every active promotion block.
    all_dispositions = []
    try:
        from helio.strategy_confidence import ARTIFACT_DIR
        for p in sorted(ARTIFACT_DIR.glob("*.json")):
            label = p.stem
            art = load_confidence_artifact(label)
            if art is None or art.disposition is None:
                continue
            all_dispositions.append({
                "artifact_label": label,
                "status": art.disposition.status,
                "reason": art.disposition.reason,
                "decided_at": art.disposition.decided_at,
                "next_review_date": art.disposition.next_review_date,
            })
    except Exception:
        pass

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor_usd": anchor,
        "review_gate": REVIEW_GATE,
        "canonical_gate": CANONICAL_GATE,
        "strategies": results,
        "all_dispositions": all_dispositions,
        "summary": {
            "promotion_eligible": promotion_eligible,
            "review_gate_passed": review_passed,
            "kill_candidates": kill_candidates,
            "disposition_blocked": disposition_blocked,
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
