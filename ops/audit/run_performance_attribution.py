"""Generate portfolio performance attribution and bounded improvement ranges."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else ["metric", "value"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def forward_score_state(forward_rows: list[dict[str, str]]) -> dict[str, Any]:
    resolved = [row for row in forward_rows if row.get("status") == "RESOLVED"]
    pnl = [to_float(row.get("pnl_pips")) for row in resolved]
    positive_review = [
        row for row in read_csv(OUT_DIR / "blocked_opportunity_forward_summary.csv")
        if str(row.get("recommendation", "")).startswith("REVIEW")
    ]
    return {
        "blocked_forward_rows": len(forward_rows),
        "blocked_forward_resolved": len(resolved),
        "blocked_forward_net_pips": round(sum(pnl), 2) if pnl else 0,
        "blocked_forward_expectancy_pips": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "blocked_forward_review_gates": [
            {
                "block_reason": row.get("block_reason", ""),
                "resolved": to_int(row.get("resolved")),
                "expectancy_pips": to_float(row.get("expectancy_pips")),
                "net_pips": to_float(row.get("net_pips")),
            }
            for row in positive_review
        ],
    }


def performance_state(scorecard: list[dict[str, str]], phantom_rows: list[dict[str, str]], forward_rows: list[dict[str, str]]) -> dict[str, Any]:
    active = [row for row in scorecard if row.get("status") == "ACTIVE"]
    killed = [row for row in scorecard if row.get("status") == "KILLED"]
    blocked = [row for row in scorecard if row.get("status") == "BLOCKED"]
    all_post_trades = sum(to_int(row.get("trade_count_post_reset")) for row in scorecard)
    active_trades = sum(to_int(row.get("trade_count_post_reset")) for row in active)
    active_pnl = sum(to_float(row.get("net_pnl")) for row in active)
    killed_pnl = sum(to_float(row.get("net_pnl")) for row in killed)
    blocked_rows = sum(to_int(row.get("blocked_trade_count")) for row in blocked)
    signal_rows = sum(to_int(row.get("signal_or_opportunity_rows")) for row in scorecard)
    phantom_pnl = sum(to_float(row.get("pnl_usd")) for row in phantom_rows)
    active_expectancies = [
        to_float(row.get("expectancy_per_trade"))
        for row in active
        if to_int(row.get("trade_count_post_reset")) > 0
    ]
    state = {
        "active_strategy_count": len(active),
        "killed_strategy_count": len(killed),
        "blocked_strategy_count": len(blocked),
        "post_reset_trade_count_all_strategies": all_post_trades,
        "post_reset_trade_count_active": active_trades,
        "clean_active_net_pnl": round(active_pnl, 2),
        "clean_active_expectancy_per_trade": round(active_pnl / active_trades, 4) if active_trades else None,
        "killed_strategy_clean_net_pnl": round(killed_pnl, 2),
        "blocked_argus_entries": blocked_rows,
        "signal_or_opportunity_rows": signal_rows,
        "phantom_trade_count": len(phantom_rows),
        "phantom_pnl_excluded_from_scoring": round(phantom_pnl, 2),
        "active_expectancy_range": [
            round(min(active_expectancies), 4),
            round(max(active_expectancies), 4),
        ] if active_expectancies else [],
    }
    state.update(forward_score_state(forward_rows))
    return state


def improvement_levers(state: dict[str, Any]) -> list[dict[str, Any]]:
    active_trades = int(state["post_reset_trade_count_active"] or 0)
    active_expectancy = float(state["clean_active_expectancy_per_trade"] or 0.0)
    blocked = int(state["blocked_argus_entries"] or 0)
    phantom_pnl = float(state["phantom_pnl_excluded_from_scoring"] or 0.0)
    return [
        {
            "lever": "Keep phantom trades excluded from all promotion/ROI scoring",
            "class": "truth",
            "current_evidence": f"{state['phantom_trade_count']} phantom rows, {phantom_pnl:.2f} phantom PnL excluded",
            "conservative_impact": "No direct PnL uplift; prevents false allocation based on fake PnL",
            "measured_upside": round(-phantom_pnl, 2) if phantom_pnl < 0 else 0,
            "confidence": "HIGH",
            "required_validation": "Broker fill/order reconciliation remains clean.",
        },
        {
            "lever": "Repair or quarantine weak VIX intraday expectancy",
            "class": "strategy_quality",
            "current_evidence": "21 post-reset trades, PF about 1.08, weak friction-adjusted edge",
            "conservative_impact": "Avoid capital scale-up until PF and drawdown improve",
            "measured_upside": "Unknown; target is drawdown reduction before PnL increase",
            "confidence": "MEDIUM",
            "required_validation": "Exit/MFE and regime split across at least 60 clean post-reset trades.",
        },
        {
            "lever": "Sample GLD PM long and NQ overnight without increasing capital",
            "class": "evidence",
            "current_evidence": "Positive PnL but only 5 and 2 post-reset trades",
            "conservative_impact": "Keeps current positive systems alive without overfitting",
            "measured_upside": round(active_expectancy * max(active_trades, 1), 2),
            "confidence": "LOW_UNTIL_SAMPLE",
            "required_validation": "Minimum 30 clean trades each before promotion; 60 preferred.",
        },
        {
            "lever": "Replay Argus MTF-blocked entries as counterfactuals",
            "class": "throughput",
            "current_evidence": f"{state.get('blocked_forward_resolved', 0)} unique blocked entries resolved; net {state.get('blocked_forward_net_pips', 0)} pips",
            "conservative_impact": "Keep most gates; review only the positive MTF long-not-at-support slice in shadow.",
            "measured_upside": state.get("blocked_forward_review_gates", []),
            "confidence": "MEDIUM for gate preservation, LOW for positive slice until larger sample",
            "required_validation": "Repeat on fresh data; do not loosen live filters without a shadow-only A/B gate.",
        },
        {
            "lever": "Add SPY/QQQ/IWM as research/shadow candidates only",
            "class": "breadth",
            "current_evidence": "Expansion list is tiered; no live promotion allowed before evidence gate",
            "conservative_impact": "Increases research opportunity surface without live risk",
            "measured_upside": "Throughput uplift unknown until shadow fills and backtest queue complete",
            "confidence": "HIGH for safety, UNKNOWN for expectancy",
            "required_validation": "30-60 shadow/paper trades per instrument and clean reconciliation.",
        },
    ]


def write_json_report(state: dict[str, Any], levers: list[dict[str, Any]]) -> None:
    report = {
        "generated_by": "ops/audit/run_performance_attribution.py",
        "state": state,
        "bottom_line": (
            "Argus is not ready for broad capital expansion. It has positive clean active PnL, "
            "but evidence is low sample or weak expectancy. The biggest immediate performance "
            "improvement is truth preservation plus targeted opportunity replay, not looser live filters."
        ),
        "improvement_levers": levers,
        "bounded_answer_to_how_much": {
            "confirmed_pnl_uplift_available_now": 0,
            "confirmed_truth_uplift": f"{state['phantom_pnl_excluded_from_scoring']} phantom PnL removed from promotion math",
            "validated_trade_throughput_upside": f"{len(state.get('blocked_forward_review_gates', []))} blocked gate slice(s) merit more shadow review.",
            "live_profitability_upside": "Still $0 confirmed for live because positive blocked slices need larger sample and shadow-only A/B validation.",
        },
    }
    (OUT_DIR / "overall_performance_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def write_markdown(state: dict[str, Any], levers: list[dict[str, Any]]) -> None:
    lines = [
        "# Overall Performance Summary",
        "",
        "## Current State",
        f"- Clean active post-reset PnL: ${state['clean_active_net_pnl']}",
        f"- Active post-reset closed trades: {state['post_reset_trade_count_active']}",
        f"- Clean active expectancy/trade: ${state['clean_active_expectancy_per_trade']}",
        f"- Blocked Argus entries needing replay: {state['blocked_argus_entries']}",
        f"- Unique blocked entries forward-scored: {state['blocked_forward_resolved']}",
        f"- Blocked-entry replay net: {state['blocked_forward_net_pips']} pips",
        f"- Blocked-entry replay expectancy: {state['blocked_forward_expectancy_pips']} pips",
        f"- Phantom PnL excluded from scoring: ${state['phantom_pnl_excluded_from_scoring']}",
        "",
        "## Honest Read",
        "Argus is profitable on the clean active slice, but not yet robust enough to scale.",
        "The positive systems are low-sample, while VIX has weak profit factor and large drawdown relative to net PnL.",
        "The Argus FX pairs are blocked systems, not failed trading systems, because they have no broker fills.",
        "",
        "## Can Performance Increase, And By How Much?",
        "Confirmed live PnL uplift available now: $0. The audit does not prove a safe live trading change yet.",
        f"Confirmed truth uplift: ${state['phantom_pnl_excluded_from_scoring']} of phantom PnL is excluded from promotion math.",
        f"Blocked-entry replay result: {state['blocked_forward_resolved']} resolved entries, {state['blocked_forward_net_pips']} net pips.",
        "Validated throughput upside: review MTF_LONG_NOT_AT_SUPPORT in shadow only; keep the other gates based on current replay.",
        "Profitability upside is intentionally unclaimed for live until the positive slice survives larger sample and A/B shadow validation.",
        "",
        "## Highest-Leverage Levers",
    ]
    for row in levers:
        lines.append(f"- {row['lever']}: {row['conservative_impact']} Confidence: {row['confidence']}.")
    lines.extend(
        [
            "",
            "## Next Validation Gate",
            "Before any capital increase: reconcile truth, replay MTF-blocked entries, collect 30-60 clean trades per candidate, and keep SPY/QQQ/IWM in research/shadow only.",
        ]
    )
    (OUT_DIR / "overall_performance_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate bounded Argus performance attribution")
    parser.parse_args()
    scorecard = read_csv(OUT_DIR / "strategy_scorecard.csv")
    phantom_rows = read_csv(OUT_DIR / "phantom_trade_annotations.csv")
    forward_rows = read_csv(OUT_DIR / "blocked_opportunity_forward_scores.csv")
    state = performance_state(scorecard, phantom_rows, forward_rows)
    levers = improvement_levers(state)
    write_csv(OUT_DIR / "performance_improvement_levers.csv", levers)
    write_json_report(state, levers)
    write_markdown(state, levers)
    print(f"active_pnl={state['clean_active_net_pnl']} active_trades={state['post_reset_trade_count_active']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
