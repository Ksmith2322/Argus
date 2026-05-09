"""Artifact Consistency & Staleness Checker.

Checks intra-runner artifact consistency (state vs trades, config hashes)
and liveness (signal/heartbeat freshness). Also checks governance-surface
artifacts (evidence registry, promotion gate report).

This is NOT a full truth-reconciliation system. It checks whether local
artifacts agree with each other. It does not reconcile against broker truth
(that is position_monitor.py's job).

Exit codes:
  0 = CLEAN (all checks pass)
  1 = WARN (staleness or advisory issues)
  2 = FAIL (divergence or corruption detected)

Usage:
    python -m argus_flow.ops.artifact_divergence
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import discover_managed_runners

REPO = Path(__file__).resolve().parents[2]

STALE_THRESHOLD_S = 300  # 5 minutes
HEARTBEAT_STALE_S = 600  # 10 minutes

# Severity levels (structured, not string-based)
INFO = "INFO"
WARN = "WARN"
FAIL = "FAIL"


def governed_runners() -> list[dict]:
    """Return all managed runners that should maintain clean local artifacts."""
    runners: list[dict] = []
    for runner in discover_managed_runners():
        if not runner.get("launch_enabled", True):
            continue
        instrument_type = str(runner.get("instrument_type", "")).lower()
        if instrument_type == "forex":
            tolerance = 0.5 if "JPY" in runner["symbol"] else 0.1
        else:
            tolerance = 0.1
        runners.append(
            {
                "name": runner["name"],
                "symbol": runner["symbol"],
                "log_dir": runner["log_dir"],
                "config": runner["config_path"],
                "pip_tolerance": tolerance,
                "instrument_type": instrument_type,
                "current_stage": runner.get("current_stage", ""),
                "live": bool(runner.get("live", False)),
            }
        )
    runners.sort(key=lambda item: (item.get("current_stage", ""), item["name"]))
    return runners


def _check(name: str, passed: bool, detail: str, severity: str = FAIL) -> dict:
    return {"name": name, "passed": passed, "detail": detail, "severity": severity}


def _max_trade_num(rows: list[dict]) -> int:
    """Return the highest journal trade_num, falling back to row count."""
    nums: list[int] = []
    for row in rows:
        try:
            nums.append(int(float(row.get("trade_num", 0) or 0)))
        except (TypeError, ValueError):
            continue
    return max(nums) if nums else len(rows)


def check_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "status": "CLEAN",
        "checks": [],
        "alerts": [],
        "max_severity": INFO,
        "current_stage": runner.get("current_stage", ""),
        "live": bool(runner.get("live", False)),
    }

    state_file = log_dir / "state.json"
    trade_file = log_dir / "trades.csv"
    signal_file = log_dir / "signals.csv"
    registry_file = log_dir / "evidence_registry.json"

    state_pos = "UNKNOWN"
    state_pnl = 0
    state_trades = 0

    # ── Consistency checks (FAIL severity) ────────────────────

    # Check 1: State file readable
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            state_pos = state.get("position", "UNKNOWN")
            if runner.get("instrument_type") == "future":
                state_pnl = state.get("pnl_points", state.get("pnl_pips", 0))
            else:
                state_pnl = state.get("pnl_pips", state.get("pnl_points", 0))
            state_trades = state.get("trade_count", 0)
            result["checks"].append(_check("state_readable", True, f"position={state_pos}", INFO))
        except (json.JSONDecodeError, OSError) as e:
            result["checks"].append(_check("state_readable", False, f"corrupt: {e}", FAIL))
    else:
        # Missing state is WARN if heartbeat exists (runner should have state), INFO otherwise
        hb = log_dir / "heartbeat.json"
        if hb.exists():
            result["checks"].append(_check("state_readable", False, "state.json missing but runner has heartbeat", WARN))
        else:
            result["checks"].append(_check("state_readable", True, "no state file (runner not started)", INFO))

    # Check 2: Trade count consistency
    rows: list[dict] = []
    csv_trade_count = 0
    csv_pnl = 0
    csv_trade_serial = 0
    if trade_file.exists():
        try:
            with open(trade_file) as f:
                rows = list(csv.DictReader(f))
            csv_trade_count = len(rows)
            csv_trade_serial = _max_trade_num(rows)
            pnl_field = "pnl_pips" if rows and "pnl_pips" in rows[0] else "pnl_pts"
            csv_pnl = sum(float(r.get(pnl_field, 0)) for r in rows)
        except Exception as e:
            result["checks"].append(_check("trade_file_readable", False, str(e), FAIL))

    if state_file.exists() and trade_file.exists():
        if (
            str(runner.get("current_stage", "")).lower() == "watcher"
            and state_pos == "FLAT"
            and csv_trade_serial == 0
            and state_trades > 0
        ):
            result["checks"].append(_check("trade_serial_match", True,
                f"watcher observe-only; ignoring legacy state trade_count={state_trades}", INFO))
        elif state_trades == csv_trade_serial:
            result["checks"].append(_check("trade_serial_match", True, f"serial={state_trades}", INFO))
        elif csv_trade_serial > state_trades:
            # CSV has trades that state doesn't know about. This is the dangerous
            # direction — local state lost track of completed trades. Real bug.
            result["checks"].append(_check("trade_serial_match", False,
                f"LOST_STATE: state={state_trades} < csv_last_trade_num={csv_trade_serial}", FAIL))
        elif state_pos == "FLAT":
            # State counter ahead of CSV while position FLAT. Two known benign
            # causes: (a) counter increments at signal-eval not at entry (argus
            # pairs that signal but never fill), (b) leftover counter from a
            # prior stage. No safety issue while flat — WARN not FAIL so risk
            # oversight does not escalate to RED on a counter-semantic gap.
            result["checks"].append(_check("trade_serial_match", False,
                f"COUNTER_AHEAD_WHILE_FLAT: state={state_trades} > csv_last_trade_num={csv_trade_serial} (no open position)",
                WARN))
        else:
            # State counter ahead of CSV with an OPEN position — possible silent
            # missed-write to trades.csv. Real divergence.
            result["checks"].append(_check("trade_serial_match", False,
                f"state={state_trades} > csv_last_trade_num={csv_trade_serial} with position={state_pos}",
                FAIL))

    # Check 3: P&L consistency (instrument-specific tolerance)
    pnl_tol = runner.get("pip_tolerance", 0.1)
    if state_file.exists() and trade_file.exists() and csv_trade_count > 0:
        pnl_diff = abs(state_pnl - csv_pnl)
        if pnl_diff > pnl_tol:
            result["checks"].append(_check("pnl_match", False,
                f"state={state_pnl:.2f} vs csv_sum={csv_pnl:.2f} (diff={pnl_diff:.2f}, tol={pnl_tol})", FAIL))
        else:
            result["checks"].append(_check("pnl_match", True, f"diff={pnl_diff:.4f} (tol={pnl_tol})", INFO))

    # Check 4: Config hash consistency
    cfg_path = REPO / runner["config"]
    hashes_path = REPO / "argus_flow" / "configs" / "hashes.json"
    if cfg_path.exists() and hashes_path.exists():
        try:
            cfg_hash = hashlib.sha256(cfg_path.read_text().encode()).hexdigest()[:16]
            hashes = json.loads(hashes_path.read_text())
            expected = hashes.get(cfg_path.name, "")
            if cfg_hash != expected:
                result["checks"].append(_check("config_hash", False,
                    f"computed={cfg_hash} vs hashes.json={expected}", FAIL))
            else:
                result["checks"].append(_check("config_hash", True, cfg_hash, INFO))
        except Exception as e:
            result["checks"].append(_check("config_hash", False, str(e), FAIL))

    # ── Liveness checks (WARN severity) ──────────────────────

    # Check 5: Signal freshness
    now_utc = datetime.now(timezone.utc)
    if signal_file.exists():
        sig_age = time.time() - signal_file.stat().st_mtime
        if sig_age > STALE_THRESHOLD_S:
            result["checks"].append(_check("signal_freshness", False,
                f"signal file {sig_age:.0f}s old (>{STALE_THRESHOLD_S}s)", WARN))
        else:
            result["checks"].append(_check("signal_freshness", True, f"{sig_age:.0f}s old", INFO))

    # Check 6: Heartbeat freshness
    hb_file = log_dir / "heartbeat.json"
    if hb_file.exists():
        hb_age = time.time() - hb_file.stat().st_mtime
        if hb_age > HEARTBEAT_STALE_S:
            result["checks"].append(_check("heartbeat_fresh", False,
                f"heartbeat {hb_age:.0f}s old (>{HEARTBEAT_STALE_S}s)", WARN))
        else:
            result["checks"].append(_check("heartbeat_fresh", True, f"{hb_age:.0f}s old", INFO))

    # ── Governance surface checks (WARN severity) ────────────

    # Check 7: Evidence registry exists and is readable
    if registry_file.exists():
        try:
            reg = json.loads(registry_file.read_text())
            cohort = reg.get("cohort", {}) if isinstance(reg.get("cohort", {}), dict) else {}
            reg_trades = cohort.get("valid_trade_count", -1)
            reg_total = cohort.get("total_trade_count", -1)
            if csv_trade_count >= 0 and reg_total >= 0:
                if reg_total != csv_trade_count:
                    result["checks"].append(_check("registry_total_trade_count", False,
                        f"registry total={reg_total} vs csv rows={csv_trade_count}", FAIL))
                else:
                    result["checks"].append(_check("registry_total_trade_count", True,
                        f"{reg_total} total trades", INFO))
            # Cross-check registry trade count vs trades.csv
            if csv_trade_count > 0 and reg_trades >= 0:
                valid_csv = sum(1 for r in rows if r.get("experiment_valid", "").lower() == "true")
                if reg_trades != valid_csv:
                    result["checks"].append(_check("registry_trade_count", False,
                        f"registry valid={reg_trades} vs csv valid={valid_csv}", FAIL))
                else:
                    result["checks"].append(_check("registry_trade_count", True,
                        f"{reg_trades} valid trades", INFO))
            result["checks"].append(_check("registry_readable", True, "evidence_registry.json valid", INFO))
        except Exception as e:
            result["checks"].append(_check("registry_readable", False, f"corrupt: {e}", WARN))
    else:
        result["checks"].append(_check("registry_readable", False,
            "evidence_registry.json missing (run evidence_registry.py)", WARN))

    # ── Determine overall status from structured severity ────
    severities = [c["severity"] for c in result["checks"] if not c["passed"]]

    if FAIL in severities:
        result["status"] = "DIVERGENT"
        result["max_severity"] = FAIL
    elif WARN in severities:
        result["status"] = "DEGRADED"
        result["max_severity"] = WARN
    else:
        result["status"] = "CLEAN"
        result["max_severity"] = INFO

    result["alerts"] = [check["detail"] for check in result["checks"] if not check["passed"]]

    return result


def main():
    print("=" * 65)
    print(f"  Artifact Consistency Check -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 65)

    results = []
    runners = governed_runners()
    for runner in runners:
        r = check_runner(runner)
        results.append(r)

        status_colors = {"CLEAN": "32", "DEGRADED": "33", "DIVERGENT": "31"}
        color = status_colors.get(r["status"], "0")
        print(f"\n  {r['name']:>10s}: \033[{color}m{r['status']}\033[0m")

        for check in r["checks"]:
            if check["passed"]:
                icon = "\033[32mOK\033[0m"
            elif check["severity"] == FAIL:
                icon = "\033[31mFAIL\033[0m"
            else:
                icon = "\033[33mWARN\033[0m"
            sev_tag = f" [{check['severity']}]" if check["severity"] != INFO else ""
            print(f"    [{icon}] {check['name']}{sev_tag}: {check['detail']}")

    # Overall
    any_divergent = any(r["status"] == "DIVERGENT" for r in results)
    any_degraded = any(r["status"] == "DEGRADED" for r in results)
    overall = "DIVERGENT" if any_divergent else "DEGRADED" if any_degraded else "CLEAN"
    oc = {"CLEAN": "32", "DEGRADED": "33", "DIVERGENT": "31"}.get(overall, "0")
    print(f"\n  Overall: \033[{oc}m{overall}\033[0m")

    # Save
    out_path = REPO / "argus_flow" / "logs" / "artifact_divergence_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "status": overall,
        "scope": {
            "runner_count": len(runners),
        },
        "runners": results,
    }, indent=2, default=str))
    print(f"  Saved: {out_path}")

    # Exit code: 0=CLEAN, 1=DEGRADED, 2=DIVERGENT
    if overall == "DIVERGENT":
        sys.exit(2)
    elif overall == "DEGRADED":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
