"""Weekly gate review — operator-friendly markdown summary.

Built 2026-05-23. Reads the live_gate_monitor history + auto_pause
recommendations and renders a one-page markdown report for operator
review. Designed for a cron job that fires Saturday mornings (or any
fixed cadence) to surface the cohort state without requiring the
operator to grep JSON.

OUTPUT:
    argus_flow/logs/weekly_review_YYYYMMDD.md

The report includes:
    1. Headline status (PASS_GATE / WARNING / PAUSE_RECOMMENDED counts)
    2. Per-strategy current verdict + trend over last 7 days
    3. Trade activity (n_trades, last trade timestamp)
    4. Recommendations (from auto_pause)
    5. Recent changes (allocation, baseline, or universe)

CLI:
    python -m helio.weekly_gate_review            # writes today's report
    python -m helio.weekly_gate_review --as-of 2026-05-30

Exit codes:
    0  OK (no urgent action)
    1  WARNING tier alerts
    2  PAUSE_RECOMMENDED tier alerts
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]
LOG_DIR = _REPO / "argus_flow" / "logs"
HISTORY_PATH = LOG_DIR / "cohort_gate_history.jsonl"
RECOMMENDATIONS_PATH = LOG_DIR / "auto_pause_recommendations.json"
BASELINE_PATH = _REPO / "argus_flow" / "configs" / "promotion_gate_baseline.json"
ALLOCATION_PATH = _REPO / "argus_flow" / "configs" / "allocation_factors.json"


def _load_history() -> list[dict]:
    """Load history JSONL. Empty list on missing/invalid."""
    if not HISTORY_PATH.exists():
        return []
    rows = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_recommendations() -> dict:
    if not RECOMMENDATIONS_PATH.exists():
        return {}
    try:
        return json.loads(RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _load_allocations() -> dict:
    if not ALLOCATION_PATH.exists():
        return {}
    return json.loads(ALLOCATION_PATH.read_text(encoding="utf-8"))


def _trend_over_window(
    history: list[dict],
    strategy: str,
    days: int,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Return per-strategy trend stats over the last `days` days."""
    if not history:
        return {"snapshots": 0}
    now_ts = now or datetime.now(timezone.utc)
    cutoff = now_ts - timedelta(days=days)
    recent_rows = []
    for row in history:
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent_rows.append(row)

    pfs = []
    verdict_counts: dict[str, int] = {}
    for row in recent_rows:
        for s in row.get("strategies", []):
            if s.get("strategy") != strategy:
                continue
            verdict = s.get("verdict", "?")
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            pf = s.get("live_pf_30trades")
            if pf is not None:
                pfs.append(float(pf))
            break

    if not pfs:
        return {"snapshots": len(recent_rows), "verdict_counts": verdict_counts}
    pfs_sorted = sorted(pfs)
    median = pfs_sorted[len(pfs_sorted) // 2]
    return {
        "snapshots": len(recent_rows),
        "verdict_counts": verdict_counts,
        "min_pf": min(pfs),
        "max_pf": max(pfs),
        "median_pf": median,
        "latest_pf": pfs[-1],
    }


def render(*, as_of: Optional[datetime] = None) -> str:
    as_of = as_of or datetime.now(timezone.utc)
    history = _load_history()
    recommendations = _load_recommendations()
    baseline = _load_baseline()
    allocations = _load_allocations()

    factors = allocations.get("factors", {})
    active = {k: v for k, v in factors.items() if isinstance(v, (int, float)) and v > 0}

    lines = []
    lines.append(f"# Weekly Gate Review — {as_of.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"Generated: {as_of.isoformat()}")
    lines.append(f"Baseline config: `{baseline.get('version', '?')}`")
    lines.append(f"Allocation config: `{allocations.get('version', '?')}`")
    lines.append("")

    # Section 1: Headline
    tier_counts = recommendations.get("alert_tier_counts", {})
    lines.append("## Headline status")
    lines.append("")
    pause_n = tier_counts.get("PAUSE_RECOMMENDED", 0)
    warn_n = tier_counts.get("WARNING", 0)
    ok_n = tier_counts.get("OK", 0)
    if pause_n > 0:
        lines.append(f"**PAUSE_RECOMMENDED: {pause_n}** — operator action needed.")
    if warn_n > 0:
        lines.append(f"**WARNING: {warn_n}** — monitor closely.")
    if pause_n == 0 and warn_n == 0:
        lines.append(f"**All clear** — {ok_n} strategy(ies) at OK status. No alerts.")
    lines.append("")

    # Section 2: Active cohort
    lines.append("## Active cohort allocations")
    lines.append("")
    lines.append("| Strategy | Allocation | Baseline CI lower | Current verdict |")
    lines.append("|---|---|---|---|")
    latest_snapshot = history[-1] if history else {"strategies": []}
    snap_by_strat = {s["strategy"]: s for s in latest_snapshot.get("strategies", [])}
    for strat, factor in sorted(active.items(), key=lambda x: -x[1]):
        snap = snap_by_strat.get(strat, {})
        verdict = snap.get("verdict", "—")
        baseline_ci = baseline.get("strategies", {}).get(strat, {}).get("ci_95_lower", "—")
        lines.append(f"| `{strat}` | {factor}× | {baseline_ci} | {verdict} |")
    lines.append("")

    # Section 3: Per-strategy trend over last 7 days
    lines.append("## Per-strategy trend (last 7 days)")
    lines.append("")
    lines.append("| Strategy | Snapshots | Verdict counts | PF range | Latest PF |")
    lines.append("|---|---|---|---|---|")
    for strat in sorted(active.keys()):
        trend = _trend_over_window(history, strat, days=7, now=as_of)
        n = trend.get("snapshots", 0)
        vc = trend.get("verdict_counts", {})
        vc_str = ", ".join(f"{v}:{c}" for v, c in sorted(vc.items())) or "—"
        if "min_pf" in trend:
            pf_range = f"{trend['min_pf']:.2f}–{trend['max_pf']:.2f}"
            latest = f"{trend['latest_pf']:.2f}"
        else:
            pf_range = "—"
            latest = "—"
        lines.append(f"| `{strat}` | {n} | {vc_str} | {pf_range} | {latest} |")
    lines.append("")

    # Section 4: Recommendations
    alerts = recommendations.get("alerts", [])
    actionable = [a for a in alerts if a.get("alert_tier") != "OK"]
    lines.append("## Actionable recommendations")
    lines.append("")
    if not actionable:
        lines.append("None. All strategies operating within tolerance.")
    else:
        for a in actionable:
            tag = a["alert_tier"]
            lines.append(f"### `{a['strategy']}` — {tag}")
            lines.append("")
            lines.append(f"- Current verdict: {a['current_verdict']}")
            lines.append(f"- Consecutive days at verdict: {a['consecutive_days_at_verdict']}/{a['threshold_days']}")
            lines.append(f"- Last live PF: {a.get('last_live_pf', '—')}")
            lines.append(f"- Baseline CI lower: {a.get('baseline_ci_lower', '—')}")
            lines.append(f"- Recommendation: {a['recommendation']}")
            lines.append("")

    # Section 5: Recent config changes
    lines.append("## Recent allocation changes")
    lines.append("")
    # Show the most-recent `_2026-MM-DD_change` keys from allocations
    change_keys = sorted([k for k in allocations.keys() if k.startswith("_2026")], reverse=True)[:3]
    if not change_keys:
        lines.append("No recent change entries in allocation_factors.json.")
    else:
        for k in change_keys:
            lines.append(f"- **{k.strip('_')}**: {allocations[k]}")
    lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("Generated by `helio/weekly_gate_review.py`. Source data:")
    lines.append(f"- History: `{HISTORY_PATH.relative_to(_REPO)}` ({len(history)} snapshots)")
    lines.append(f"- Recommendations: `{RECOMMENDATIONS_PATH.relative_to(_REPO)}`")
    lines.append(f"- Baseline: `{BASELINE_PATH.relative_to(_REPO)}`")
    lines.append(f"- Allocations: `{ALLOCATION_PATH.relative_to(_REPO)}`")
    return "\n".join(lines)


def write_report(*, as_of: Optional[datetime] = None,
                  output_dir: Path = LOG_DIR) -> Path:
    as_of = as_of or datetime.now(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"weekly_review_{as_of.strftime('%Y%m%d')}.md"
    path.write_text(render(as_of=as_of), encoding="utf-8")
    return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="ISO date for the report (default: today)")
    parser.add_argument("--stdout", action="store_true",
                        help="Print to stdout instead of writing a file")
    args = parser.parse_args(argv)
    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"FAIL: invalid --as-of: {args.as_of}", file=sys.stderr)
            return 2

    if args.stdout:
        print(render(as_of=as_of))
        return 0

    path = write_report(as_of=as_of)
    # Echo to operator
    print(f"Weekly review written: {path}")
    # Exit code reflects worst tier (matches auto_pause convention)
    recommendations = _load_recommendations()
    counts = recommendations.get("alert_tier_counts", {})
    if counts.get("PAUSE_RECOMMENDED", 0) > 0:
        return 2
    if counts.get("WARNING", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
