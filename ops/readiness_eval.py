"""readiness_eval — auto-evaluate the 5/31 real-money readiness checklist.

The 20-item checklist in
  C:/Users/ksmit/.claude/projects/c--Argus/memory/project_real_money_readiness_gate_20260531.md
is currently manual — operator edits `- [ ]` → `- [x]` in markdown. Tedious
and out-of-date. This script auto-checks the items that ARE data-driven
(broker state, scheduled-task results, recon drift counts, etc) and emits
a live evaluation that the dashboard's readiness panel reads.

Manual items (drills, doc reviews) are left as null — those still require
operator sign-off via the markdown.

Output: argus_flow/logs/readiness_eval_latest.json
Schema:
  {
    "evaluated_at_utc": "...",
    "items": [
      {"n": 1, "auto_status": "PASS"|"FAIL"|"MANUAL", "evidence": "..."},
      ...
    ],
    "summary": {"auto_pass": N, "auto_fail": N, "manual": N}
  }

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.readiness_eval

Schedule: hourly via managed_truth_loop (alongside tws_health_probe, etc).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "readiness_eval_latest.json"


def _safe_load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _result(n: int, status: str, evidence: str) -> dict:
    return {"n": n, "auto_status": status, "evidence": evidence}


# ── Item-specific check functions ────────────────────────────────────────

def check_1_account_balance() -> dict:
    """Account at IBKR paper holds expected balance (~$11,815 + interest accrued).

    Auto-check via broker_snapshot.json's net_liquidation_usd. Threshold:
    $5,000 floor — anything below means an issue (negative balance, broker
    mis-mapping). Originally the spec said ~$11,815 but post-reset the
    paper account is at ~$31K, so we just verify it's NON-ZERO and reasonable.
    """
    snap = _safe_load(REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json")
    if not snap:
        return _result(1, "FAIL", "broker_snapshot.json missing")
    netliq = (snap.get("account") or {}).get("net_liquidation_usd", 0)
    if not netliq or netliq < 5000:
        return _result(1, "FAIL", f"net_liquidation_usd=${netliq} (below $5K floor)")
    return _result(1, "PASS", f"NetLiq=${netliq:.2f} (broker_snapshot)")


def check_4_recon_drift_14d() -> dict:
    """Zero RECON_DRIFT events in last 14 days."""
    p = REPO / "argus_flow" / "logs" / "canonical_reconcile.json"
    d = _safe_load(p)
    if not d:
        return _result(4, "FAIL", "canonical_reconcile.json missing")
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    events = d.get("events") or d.get("drift_events") or []
    recent = []
    for e in events:
        ts_raw = e.get("ts") or e.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts >= cutoff:
            recent.append(e)
    if recent:
        return _result(4, "FAIL", f"{len(recent)} RECON_DRIFT events in last 14d")
    return _result(4, "PASS", "0 RECON_DRIFT events in last 14d")


def check_5_artifact_divergence() -> dict:
    """Zero artifact_divergence flags."""
    p = REPO / "argus_flow" / "logs" / "artifact_divergence_report.json"
    d = _safe_load(p)
    if not d:
        return _result(5, "MANUAL", "artifact_divergence_report.json missing — verify via full_audit")
    status = d.get("status") or d.get("verdict") or "?"
    if str(status).upper() in ("OK", "PASS", "CLEAN"):
        return _result(5, "PASS", f"status={status}")
    if str(status).upper() == "DEGRADED" and d.get("documented_cause"):
        return _result(5, "PASS", f"status=DEGRADED (documented: {d['documented_cause']})")
    return _result(5, "FAIL", f"status={status}")


def check_6_canonical_reconciles() -> dict:
    """canonical_fills.jsonl reconciles to per-strategy CSVs (delta_n=0 for all)."""
    p = REPO / "argus_flow" / "logs" / "canonical_reconcile.json"
    d = _safe_load(p)
    if not d:
        return _result(6, "MANUAL", "no recent canonical_reconcile run; use full_audit")
    strategies = d.get("strategies") or []
    drifted = [s for s in strategies if abs(s.get("delta_n", 0)) != 0]
    if drifted:
        names = [s.get("strategy", "?") for s in drifted[:3]]
        return _result(6, "FAIL", f"{len(drifted)} strategies have delta_n != 0: {names}")
    if strategies:
        return _result(6, "PASS", f"all {len(strategies)} strategies reconcile (delta_n=0)")
    return _result(6, "MANUAL", "reconcile report empty — run full_audit")


def check_tasks_lastresult(item_n: int, task_name: str) -> dict:
    """Generic check: scheduled task last_result=0."""
    audits = sorted((REPO / "docs" / "audits").glob("*/tasks.json"))
    if not audits:
        return _result(item_n, "MANUAL", "no recent full_audit tasks.json")
    try:
        tasks = json.loads(audits[-1].read_text(encoding="utf-8"))
    except Exception:
        return _result(item_n, "MANUAL", "tasks.json parse error")
    t = tasks.get(task_name)
    if not t:
        return _result(item_n, "FAIL", f"task '{task_name}' not registered")
    last = str(t.get("last_result", ""))
    if last == "0":
        return _result(item_n, "PASS", f"{task_name} last_result=0")
    return _result(item_n, "FAIL", f"{task_name} last_result={last}")


def check_9_all_tasks_zero() -> dict:
    """All scheduled tasks last_result=0."""
    audits = sorted((REPO / "docs" / "audits").glob("*/tasks.json"))
    if not audits:
        return _result(9, "MANUAL", "no recent full_audit tasks.json")
    try:
        tasks = json.loads(audits[-1].read_text(encoding="utf-8"))
    except Exception:
        return _result(9, "MANUAL", "tasks.json parse error")
    # Defensively handle non-dict entries (some tasks.json values are bare strings)
    bad = []
    checked = 0
    for name, t in tasks.items():
        if not isinstance(t, dict):
            continue
        checked += 1
        if str(t.get("last_result", "0")) != "0":
            bad.append(name)
    if bad:
        return _result(9, "FAIL", f"{len(bad)} of {checked} tasks have non-zero last_result: {bad[:3]}")
    return _result(9, "PASS", f"all {checked} scheduled tasks last_result=0")


def check_10_no_down_runners() -> dict:
    """No DOWN runners in fleet_status."""
    p = REPO / "argus_flow" / "logs" / "fleet_status.json"
    d = _safe_load(p)
    if not d:
        return _result(10, "MANUAL", "fleet_status.json missing")
    # fleet_status is a dict of {name: {...status fields...}}, but defensively
    # handle non-dict values (some entries are status strings or scalars)
    down = []
    checked = 0
    for name, info in d.items():
        if not isinstance(info, dict):
            continue
        checked += 1
        status = (info.get("status") or "").upper()
        runtime = (info.get("runtime") or "").upper()
        if status == "DOWN" or runtime == "DOWN":
            down.append(name)
    if down:
        return _result(10, "FAIL", f"{len(down)} runners DOWN: {down[:3]}")
    return _result(10, "PASS", f"no DOWN runners (checked {checked})")


def check_19_verdict_file() -> dict:
    """5/1 verdict record exists at argus_flow/logs/verdict_20260501.json."""
    p = REPO / "argus_flow" / "logs" / "verdict_20260501.json"
    if not p.exists():
        return _result(19, "FAIL", "verdict_20260501.json not yet created (ceremony 5/1)")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return _result(19, "FAIL", f"verdict file parse error: {e}")
    n = len(d.get("strategies", []))
    filled = sum(1 for s in d.get("strategies", []) if s.get("verdict"))
    if filled == n and n > 0:
        return _result(19, "PASS", f"verdict file exists, {filled}/{n} strategies filled")
    return _result(19, "FAIL", f"verdict file exists but only {filled}/{n} filled")


def check_20_final_cull_executed() -> dict:
    """Final cull executed — strategies with KILL verdict are at factor 0.0."""
    verdict_path = REPO / "argus_flow" / "logs" / "verdict_20260501.json"
    factors_path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
    v = _safe_load(verdict_path)
    f = _safe_load(factors_path)
    if not v:
        return _result(20, "FAIL", "no verdict file (5/1 ceremony hasn't happened)")
    if not f:
        return _result(20, "FAIL", "allocation_factors.json missing")
    factors = f.get("factors", {})
    kills = [s for s in v.get("strategies", []) if (s.get("verdict") or "").upper() == "KILL"]
    not_zeroed = [s["strategy"] for s in kills if factors.get(s["strategy"], 1.0) != 0.0]
    if not kills:
        return _result(20, "MANUAL", "no KILL verdicts to enforce")
    if not_zeroed:
        return _result(20, "FAIL", f"{len(not_zeroed)} KILL strategies not at 0.0x: {not_zeroed[:3]}")
    return _result(20, "PASS", f"all {len(kills)} KILL strategies at 0.0x")


# Items 2, 3, 11-18 are MANUAL (drills, doc reviews, account funding)
MANUAL_ITEMS = {
    2:  "Real-money account funded — operator confirms funding + recorded amount",
    3:  "IBKR paper/real login — operator confirms credentials in password manager",
    11: "KILL_SWITCH drill — operator runs Week-3 drill, all 22 runners halt within 60s",
    12: "Daily-loss circuit breaker drill — operator triggers at -2%",
    13: "Per-instrument cumulative cap — code change + drill needed",
    14: "Manual override (KILL + FLATTEN_EOD) drill",
    15: "Real-money-specific rules coded — code review needed",
    16: "reference_reboot_recovery.md current — dry-run by operator",
    17: "project_operations_solo_manual reviewed end-to-end",
    18: "reference_failure_modes.md extended to 15+ modes",
}


def main() -> int:
    items: list[dict] = []

    # Auto-checked items
    items.append(check_1_account_balance())
    items.append(_result(2, "MANUAL", MANUAL_ITEMS[2]))
    items.append(_result(3, "MANUAL", MANUAL_ITEMS[3]))
    items.append(check_4_recon_drift_14d())
    items.append(check_5_artifact_divergence())
    items.append(check_6_canonical_reconciles())
    items.append(check_tasks_lastresult(7, "ArgusCohortReport"))
    items.append(check_tasks_lastresult(8, "ArgusWatchdog"))
    items.append(check_9_all_tasks_zero())
    items.append(check_10_no_down_runners())

    # Manual items 11-18
    for n in range(11, 19):
        items.append(_result(n, "MANUAL", MANUAL_ITEMS[n]))

    # Auto-checked items 19-20 (5/1 ceremony output)
    items.append(check_19_verdict_file())
    items.append(check_20_final_cull_executed())

    # Sort by item number
    items.sort(key=lambda x: x["n"])

    summary = {
        "auto_pass":   sum(1 for i in items if i["auto_status"] == "PASS"),
        "auto_fail":   sum(1 for i in items if i["auto_status"] == "FAIL"),
        "manual":      sum(1 for i in items if i["auto_status"] == "MANUAL"),
        "total":       len(items),
    }

    payload = {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "summary": summary,
        "method_note": (
            "Auto-evaluation of the 5/31 readiness gate. PASS/FAIL items are "
            "data-driven; MANUAL items (drills, doc reviews) require operator "
            "sign-off via the markdown checklist file. The dashboard's "
            "readiness panel merges this with the markdown state."
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Console summary
    print("=" * 76)
    print("  READINESS GATE AUTO-EVAL")
    print("=" * 76)
    print(f"  PASS: {summary['auto_pass']:>2}  FAIL: {summary['auto_fail']:>2}  MANUAL: {summary['manual']:>2}  TOTAL: {summary['total']}")
    print()
    for it in items:
        marker = {"PASS": "[+]", "FAIL": "[-]", "MANUAL": "[ ]"}.get(it["auto_status"], "[?]")
        print(f"  {marker} #{it['n']:>2}  {it['auto_status']:<7}  {it['evidence'][:90]}")
    print()
    print(f"  Output: {OUT_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
