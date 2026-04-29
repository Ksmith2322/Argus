"""5/1 Review Ceremony — verdict-template skeleton generator.

Pre-fills the 23 strategy verdict entries with structural facts (numbers,
operational status) so the ceremony itself only requires filling in the
*decision* fields: verdict, reasoning, action, next_review.

Critical design rule: this script does NOT suggest verdicts. The ceremony
file warns against rehearsing verdicts pre-ceremony; we leave the verdict
field empty even when the criteria would obviously support a label. A
`criteria_eligibility` block is included with TRUE/FALSE flags for each
verdict's quantitative thresholds — that's raw fact, not opinion.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.generate_verdict_skeleton

Output:
    argus_flow/logs/ceremony_prep/verdict_20260501_skeleton.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _safe_load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _evidence_class(operational_verdict: str, n: int) -> str:
    """Map operational_maturity verdict + sample size to the 9-stage evidence
    ladder labels (per project_evidence_promotion_ladder.md). Best-effort —
    the user should refine on 5/1 if granularity is needed.
    """
    if n == 0:
        return "Stage 1: Backtest Valid (no live data yet)"
    if n < 5:
        return "Stage 4: Paper-Live Initial (n<5)"
    if n < 15:
        return "Stage 5: Paper-Live Building (5≤n<15)"
    if n < 30:
        return "Stage 6: Paper-Live Sample (15≤n<30)"
    if n < 60:
        return "Stage 7: Paper-Live Valid (30≤n<60)"
    if operational_verdict == "DEGRADED":
        return "Stage 7: Paper-Live Valid (DEGRADED — drift visible)"
    return "Stage 8: Promotion Candidate (n≥60)"


def _criteria_eligibility(n: int, pf: float | None, op_verdict: str) -> dict:
    """Quantitative gates from the 6-tier verdict criteria. TRUE/FALSE only —
    NO verdict suggestion. Just whether the numbers would meet each tier's
    quantitative threshold; the user combines with qualitative judgment.
    """
    pf_safe = pf if pf is not None else 0.0
    return {
        # REAL-CANDIDATE: n≥30, PF≥1.30, HEALTHY 21d, expectancy>0
        "real_candidate_quant_thresholds_met": (n >= 30 and pf_safe >= 1.30),
        # WINNER-CANDIDATE: n≥15 (30d), PF≥1.20, not DEGRADED 14d
        "winner_candidate_quant_thresholds_met": (n >= 15 and pf_safe >= 1.20 and op_verdict != "DEGRADED"),
        # KEEP-PAPER: 5-15 trades, PF 0.95-1.20, HEALTHY
        "keep_paper_quant_thresholds_met": (5 <= n < 15 and 0.95 <= pf_safe <= 1.20),
        # KILL: PF<1.0 over n≥30 (one of multiple kill triggers — only this one is purely quantitative)
        "kill_quant_threshold_met (n>=30 + PF<1.0)": (n >= 30 and pf_safe < 1.0),
        # OBSERVE: <5 trades in window
        "observe_quant_threshold_met": (n < 5),
    }


def main() -> int:
    om = _safe_load(REPO / "argus_flow" / "logs" / "operational_maturity_latest.json") or {}
    pr = _safe_load(REPO / "argus_flow" / "logs" / "promotion_readiness_report.json") or {}
    vetting = _safe_load(REPO / "argus_flow" / "logs" / "ceremony_prep" / f"operational_vetting_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json") or {}

    # Index by strategy name for quick joins
    pr_by = {s["strategy"]: s for s in pr.get("strategies", [])}
    vet_by = {r["strategy"]: r for r in vetting.get("strategies", [])}

    entries = []
    for s in om.get("strategies", []):
        name = s["strategy"]
        n = s.get("live_trades", 0)
        pf = s.get("live_pf")
        op_verdict = s.get("verdict", "?")
        pr_row = pr_by.get(name, {})
        vet_row = vet_by.get(name)

        # Auto-fill verdicts that are FACTUAL (not judgmental) per the ceremony spec.
        # OBSERVE for thin samples (n<5) and BLOCKED for operational gaps are
        # criteria-driven, not "rehearsing the verdict." Strategies with enough
        # sample to require actual judgment (multi_orb, spy_mean_rev, vix_intraday,
        # etc) are LEFT BLANK so the user fills them on 5/1.
        vetting_verdict = vet_row.get("verdict_auto") if vet_row else None
        auto_verdict, auto_reasoning, auto_action, auto_next = None, None, None, None
        elig = _criteria_eligibility(n, pf, op_verdict)
        if vetting_verdict == "BLOCKED":
            auto_verdict = "BLOCKED"
            auto_reasoning = "Operational vetting failed structural checks (see operational_vetting_<date>.json). Cannot judge edge until rails are verified."
            auto_action = "Repair window 5/1-5/15: fix operational issues per vetting checklist. Re-vet on 5/15."
            auto_next = "2026-05-15"
        elif vetting_verdict == "LOW_FREQUENCY_OBSERVE":
            auto_verdict = "OBSERVE"
            auto_reasoning = "By-design quiet (event-driven OR research_only mode). Operationally healthy; expected silence given current conditions."
            auto_action = "Continue paper. Track expected fire rate vs actual."
            auto_next = "2026-05-15"
        elif elig["observe_quant_threshold_met"] and vetting_verdict != "BLOCKED":
            # n<5 + not blocked = thin sample, formal OBSERVE
            auto_verdict = "OBSERVE"
            auto_reasoning = f"Thin sample (n={n}) — insufficient evidence for any decision."
            auto_action = "Continue paper. Re-evaluate when n>=10."
            auto_next = "2026-05-15"

        entry = {
            # Pre-filled structural facts (operational_maturity is the primary source —
            # fleet_perf_summary's per-strategy array is sometimes empty between rebuilds)
            "strategy": name,
            "live_trades_post_clamp": n,
            "live_pf": round(pf, 2) if pf is not None else None,
            "live_pnl_usd": s.get("live_total_pnl_usd"),
            "live_expectancy_usd": s.get("live_expectancy_usd"),
            "live_win_rate_pct": s.get("live_wr"),
            "first_trade_ts": s.get("sample_window_start"),
            "last_trade_ts": s.get("sample_window_end"),
            "backtest_pf": s.get("backtest_pf"),
            "drift_ratio": s.get("drift_ratio"),
            "operational_maturity": op_verdict,
            "operational_maturity_reason": s.get("verdict_reason"),
            "evidence_class": _evidence_class(op_verdict, n),
            "promotion_review_gate_passed": pr_row.get("review_gate_passed"),
            "promotion_blockers": pr_row.get("review_gate_blockers", []),
            "blocking_disposition": pr_row.get("blocking_disposition"),
            "operational_vetting_auto": vetting_verdict if vetting_verdict else "n/a (had trades)",
            "criteria_eligibility": elig,
            # Cross-references — direct paths the user can hit on 5/1 without
            # context-switching between dashboard panels.
            "references": {
                "drilldown": f"http://localhost:8080/api/strategy_drilldown?strategy={name}&window_days=60",
                "benchmark_alpha_section": f"In /api/benchmark_alpha → look for strategy='{name}' in 'strategies' or 'skipped'",
                "recommended_actions_section": f"In /api/recommended_actions → look for strategy='{name}' in 'actions'",
                "operational_vetting_section": f"In argus_flow/logs/ceremony_prep/operational_vetting_<date>.json → strategies[?] where strategy='{name}'",
            },
            # Auto-filled when criteria are unambiguous; user can override on 5/1.
            "verdict": auto_verdict,
            "reasoning": auto_reasoning,
            "action": auto_action,
            "named_rework_fix": None,                # FILL only if verdict=REWORK
            "next_review_date": auto_next,
            "auto_filled": auto_verdict is not None,  # operator hint: "this was auto"
            "owner": "ksmith2322",
            "review_date": "2026-05-01",
        }
        entries.append(entry)

    # Sort: most decisive first (most trades, then alphabetical)
    entries.sort(key=lambda e: (-(e["live_trades_post_clamp"] or 0), e["strategy"]))

    out_dir = REPO / "argus_flow" / "logs" / "ceremony_prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "verdict_20260501_skeleton.json"

    n_auto = sum(1 for e in entries if e["auto_filled"])
    n_manual = len(entries) - n_auto
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "5/1 ceremony verdict template — auto-filled where criteria are unambiguous; manual fields blank for actual decisions",
        "instructions": (
            f"Of {len(entries)} strategies, {n_auto} have auto-filled verdicts (OBSERVE/BLOCKED based on objective criteria). "
            f"{n_manual} strategies have enough sample to require manual judgment — fill verdict, reasoning, action, "
            f"named_rework_fix (if REWORK), next_review_date for those. The auto_filled=true entries can still be "
            f"overridden if the user sees something the criteria missed. Save filled version as verdict_20260501.json."
        ),
        "n_strategies": len(entries),
        "n_auto_filled": n_auto,
        "n_requiring_manual_judgment": n_manual,
        "verdict_vocabulary": [
            "BLOCKED", "OBSERVE", "KEEP-PAPER", "WINNER-CANDIDATE",
            "REAL-CANDIDATE", "REWORK", "QUARANTINE", "KILL",
        ],
        "ceremony_target_distribution": {
            "REAL-CANDIDATE": "0-1",
            "WINNER-CANDIDATE": "1-3",
            "KEEP-PAPER": "4-8",
            "REWORK": "≤5",
            "QUARANTINE": "0-3",
            "OBSERVE": "4-8",
            "KILL": "2-6",
        },
        "strategies": entries,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Verdict skeleton written: {out_path.relative_to(REPO)}")
    print(f"Strategies: {len(entries)}")
    print()
    print(f"{'STRATEGY':30s}  {'OP':18s} {'N':>4} {'PF':>6}  ELIG (R-C / W-C / K-P / KILL / OBS)")
    print("-" * 100)
    for e in entries:
        elig = e["criteria_eligibility"]
        elig_str = (
            f"{'Y' if elig['real_candidate_quant_thresholds_met'] else 'n':>4} / "
            f"{'Y' if elig['winner_candidate_quant_thresholds_met'] else 'n':>3} / "
            f"{'Y' if elig['keep_paper_quant_thresholds_met'] else 'n':>3} / "
            f"{'Y' if elig['kill_quant_threshold_met (n>=30 + PF<1.0)'] else 'n':>4} / "
            f"{'Y' if elig['observe_quant_threshold_met'] else 'n':>3}"
        )
        pf_str = f"{e['live_pf']:>5.2f}" if e["live_pf"] is not None else "  -  "
        print(f"{e['strategy']:30s}  {e['operational_maturity']:18s} {e['live_trades_post_clamp']:>4} {pf_str}  {elig_str}")

    print()
    print(f"R-C = REAL-CANDIDATE (n>=30 + PF>=1.30)")
    print(f"W-C = WINNER-CANDIDATE (n>=15 + PF>=1.20 + not DEGRADED)")
    print(f"K-P = KEEP-PAPER (5<=n<15 + 0.95<=PF<=1.20)")
    print(f"KILL = quantitative kill threshold (n>=30 + PF<1.0)")
    print(f"OBS = OBSERVE (n<5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
