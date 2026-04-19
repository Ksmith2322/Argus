"""Kill-rule watchdog — enforces strategy-card kill rules automatically.

Strategy cards define kill rules in human-readable YAML. This module encodes
those rules in evaluable form and checks them nightly. When a rule triggers,
the strategy is flagged for the operator's kill discipline review — the
watchdog does NOT auto-kill; it alerts.

Per blueprint §18.9 governance: without enforcement, strategy cards are
aspirational. This module makes them operational.

Usage:
    python -m helio.kill_watchdog
    python -m helio.kill_watchdog --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from helio.fleet_sizing import compute_strategy_stats, get_sizing_anchor_usd  # noqa: E402

OUT_PATH = _REPO / "argus_flow" / "logs" / "kill_watchdog_report.json"
HISTORY_PATH = _REPO / "argus_flow" / "logs" / "kill_watchdog_history.jsonl"
DISCORD_FAILURES_LOG = _REPO / "argus_flow" / "logs" / "discord_failures.jsonl"


# Per-strategy kill rules encoded from the markdown cards in
# research/strategy_cards/. Each rule returns (triggered: bool, reason: str).
# The rules are deliberately conservative: false negatives (missing a kill)
# are worse than false positives (over-flagging), so the thresholds match
# the "clear evidence of broken edge" bar from the cards.
def _universal_rules(label: str, stats: dict, ctx: dict) -> list[dict]:
    """Rules that apply to every strategy regardless of card."""
    hits: list[dict] = []
    trades = stats.get("trades", 0)
    pf = stats.get("profit_factor", 0.0)
    pnl = stats.get("pnl_usd", 0.0)

    # Rule: 30+ valid trades with PF < 1.0 — canonical kill candidate.
    if trades >= 30 and 0 < pf < 1.0:
        hits.append({
            "rule": "pf_below_1_on_30plus_trades",
            "severity": "kill_candidate",
            "detail": f"{trades} trades, PF={pf:.2f} (below 1.0 threshold)",
        })

    # Rule: 20+ trades AND cumulative PnL < -2% of anchor — drawdown severe.
    anchor = ctx.get("anchor_usd", 10000)
    if trades >= 20 and pnl < -0.02 * anchor:
        hits.append({
            "rule": "drawdown_over_2pct_anchor_on_20plus",
            "severity": "kill_candidate",
            "detail": f"pnl=${pnl:.2f} ({pnl/anchor*100:.2f}% of anchor) on {trades} trades",
        })

    return hits


def _strategy_specific_rules(label: str, stats: dict, ctx: dict) -> list[dict]:
    """Rules copied from each strategy card's kill_rules section."""
    hits: list[dict] = []
    trades = stats.get("trades", 0)
    pf = stats.get("profit_factor", 0.0)

    if label == "forge_gdx_gld":
        # Card: "20+ live trades with PF < 1.0 after costs"
        if trades >= 20 and 0 < pf < 1.0:
            hits.append({
                "rule": "card:gdx_gld_pf_below_1_on_20plus",
                "severity": "kill_candidate",
                "detail": f"card rule triggered ({trades} trades, PF={pf:.2f})",
            })

    elif label == "forge_gld_pm_long":
        # Card: "30+ live trades with PF < 1.0 after costs"
        if trades >= 30 and 0 < pf < 1.0:
            hits.append({
                "rule": "card:gld_pm_long_pf_below_1_on_30plus",
                "severity": "kill_candidate",
                "detail": f"card rule triggered ({trades} trades, PF={pf:.2f})",
            })

    elif label == "forge_wick_gbpusd":
        # Card: "10+ live trades with PF < 1.0"
        if trades >= 10 and 0 < pf < 1.0:
            hits.append({
                "rule": "card:wick_gbpusd_pf_below_1_on_10plus",
                "severity": "kill_candidate",
                "detail": f"card rule triggered ({trades} trades, PF={pf:.2f})",
            })

    elif label.startswith("argus_"):
        # Argus pairs: promotion_gate_v2 already flags these. Add a hard kill
        # only if the pair has 30+ VALID trades and PF < 0.9 (worse than pf_below_1
        # universal rule). This is the "kill, don't just flag" threshold for Argus.
        if trades >= 30 and 0 < pf < 0.9:
            hits.append({
                "rule": f"argus_kill:{label}_pf_below_0.9_on_30plus",
                "severity": "kill_candidate",
                "detail": f"{trades} trades, PF={pf:.2f} (below 0.9 — worse than noise)",
            })

    return hits


def _signal_drift_rules(label: str) -> list[dict]:
    """Check signal_frequency_history.jsonl for sustained drift. A rule
    triggers if the strategy has been flagged 'severe' (<20% of replay) for
    3+ consecutive days."""
    hits: list[dict] = []
    hist_path = _REPO / "argus_flow" / "logs" / "signal_frequency_history.jsonl"
    if not hist_path.exists():
        return hits
    try:
        lines = [json.loads(l) for l in hist_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return hits
    if not lines:
        return hits
    # Look at last 3 rows; trigger if all 3 show 'severe' for this label
    severe_streak = 0
    for row in lines[-3:]:
        per = row.get("per_strategy", {}).get(label)
        if per and per.get("severity") == "severe":
            severe_streak += 1
        else:
            severe_streak = 0
    if severe_streak >= 3:
        hits.append({
            "rule": "signal_drift_severe_3_consecutive_days",
            "severity": "drift_warning",
            "detail": f"{label} flagged severe for {severe_streak} consecutive tracker runs",
        })
    return hits


def evaluate_strategy(label: str, ctx: dict) -> dict:
    stats = compute_strategy_stats(label)
    triggered = []
    triggered += _universal_rules(label, stats, ctx)
    triggered += _strategy_specific_rules(label, stats, ctx)
    triggered += _signal_drift_rules(label)

    severities = [h["severity"] for h in triggered]
    status = "OK"
    if any(s == "kill_candidate" for s in severities):
        status = "KILL_CANDIDATE"
    elif any(s == "drift_warning" for s in severities):
        status = "DRIFT_WARNING"

    return {
        "strategy": label,
        "status": status,
        "stats": stats,
        "triggered_rules": triggered,
    }


# Strategies monitored by the watchdog.
WATCHED = [
    "argus_usdjpy",
    "argus_gbpusd",
    "argus_cadjpy",
    "forge_gld_pm_long",
    "forge_wick_gbpusd",
    "forge_nq_overnight",
    "forge_jpy_pm_short",
    "forge_gdx_gld",
]


def build_report() -> dict:
    anchor = get_sizing_anchor_usd()
    ctx = {"anchor_usd": anchor}
    results = [evaluate_strategy(label, ctx) for label in WATCHED]
    kill_candidates = [r["strategy"] for r in results if r["status"] == "KILL_CANDIDATE"]
    drift_warnings = [r["strategy"] for r in results if r["status"] == "DRIFT_WARNING"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor_at_time_usd": anchor,
        "strategies": results,
        "summary": {
            "kill_candidates": kill_candidates,
            "drift_warnings": drift_warnings,
            "total_evaluated": len(results),
        },
    }


def _notify_discord_if_hits(report: dict) -> None:
    sm = report["summary"]
    hits = sm["kill_candidates"] + sm["drift_warnings"]
    if not hits:
        return
    # Build a compact Discord message
    lines = ["**KILL WATCHDOG ALERT**"]
    for r in report["strategies"]:
        if r["status"] != "OK":
            lines.append(f"- **{r['strategy']}** [{r['status']}]")
            for h in r["triggered_rules"]:
                lines.append(f"    - {h['rule']}: {h['detail']}")
    msg = "\n".join(lines)
    try:
        from helio.fleet_monitor import send_discord
        send_discord(msg, system="kill_watchdog")
    except Exception as e:
        # Log but don't fail the watchdog cycle
        DISCORD_FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DISCORD_FAILURES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "reason": f"kill_watchdog_alert_failed: {type(e).__name__}",
                "msg_prefix": msg[:120],
            }) + "\n")


def _append_history(report: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": report["generated_at"],
        "anchor_at_time_usd": report["anchor_at_time_usd"],
        "kill_candidates": report["summary"]["kill_candidates"],
        "drift_warnings": report["summary"]["drift_warnings"],
    }
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _print_table(report: dict) -> None:
    print(f"Kill-rule watchdog — anchor ${report['anchor_at_time_usd']:,.0f}")
    print(f"Generated: {report['generated_at']}")
    print()
    print(f"{'Strategy':24s} {'Status':18s} {'Trades':>7s} {'PF':>6s}  Triggered rules")
    print("-" * 100)
    for r in report["strategies"]:
        s = r["stats"]
        pf_str = f"{s['profit_factor']:.2f}" if s["trades"] > 0 else "—"
        rules_str = ""
        if r["triggered_rules"]:
            rules_str = "; ".join(h["rule"] for h in r["triggered_rules"])
        print(f"  {r['strategy']:22s} {r['status']:18s} {s['trades']:>7d} {pf_str:>6s}  {rules_str}")
    print()
    sm = report["summary"]
    if sm["kill_candidates"]:
        print(f"  KILL CANDIDATES: {', '.join(sm['kill_candidates'])}")
    if sm["drift_warnings"]:
        print(f"  DRIFT WARNINGS: {', '.join(sm['drift_warnings'])}")
    if not sm["kill_candidates"] and not sm["drift_warnings"]:
        print("  All strategies pass kill-rule checks. No action needed.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-discord", action="store_true", help="Skip Discord alert (useful for dry runs)")
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()

    report = build_report()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2))
    if not args.no_history:
        _append_history(report)
    if not args.no_discord:
        _notify_discord_if_hits(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
        print(f"\nWrote: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
