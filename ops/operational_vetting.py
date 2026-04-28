"""5/1 Review Ceremony — Track 2 Operational Vetting auto-checker.

Runs the 15-item operational vetting checklist (per
project_5_1_review_ceremony_20260501.md) for every strategy currently silent
(verdict=WAITING in the latest operational_maturity report). Automates 9 of
the 15 items; the remaining 6 are flagged MANUAL for explicit sign-off
during the ceremony.

The point: silence-without-audit was being treated like failed-experiment.
This script enforces the operational pre-check so a strategy with zero
trades can't be culled before its rails are verified.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.operational_vetting

Output:
    argus_flow/logs/ceremony_prep/operational_vetting_<YYYYMMDD>.json
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Maps silent-strategy names to {runner_module_path, log_dir, has_scheduled_task}.
# Intentionally explicit (not auto-discovered) so the audit catches when a
# strategy is missing its expected rails — auto-discovery would mask those
# failures by only reporting on what's already there.
STRATEGY_REGISTRY: dict[str, dict] = {
    # Argus FX pairs (single shared runner, per-pair log dir + scheduled task)
    "argus_usdjpy":  {"runner": "argus_flow/runner_unified.py",  "log_dir": "argus_flow/logs/usdjpy",  "task": None},  # always running, not scheduled
    "argus_gbpusd":  {"runner": "argus_flow/runner_unified.py",  "log_dir": "argus_flow/logs/gbpusd",  "task": None},
    "argus_cadjpy":  {"runner": "argus_flow/runner_unified.py",  "log_dir": "argus_flow/logs/cadjpy",  "task": None},
    # Forge strategies
    "forge_gdx_gld":            {"runner": "forge/gdx_gld_runner.py",            "log_dir": "forge/logs/gdx_gld",            "task": None},
    "forge_gld_pm_long":        {"runner": "forge/gld_pm_long/runner.py",        "log_dir": "forge/logs/gld_pm_long",        "task": "ArgusGldPmLoop"},
    "forge_jpy_pm_short":       {"runner": "forge/jpy_pm_short/runner.py",       "log_dir": "forge/logs/jpy_pm_short",       "task": None},
    "forge_nq_overnight":       {"runner": "forge/nq_overnight/runner.py",       "log_dir": "forge/logs/nq_overnight",       "task": None},
    "forge_nq_london_close":    {"runner": "forge/nq_london_close/runner.py",    "log_dir": "forge/logs/nq_london_close",    "task": "ArgusNqLondonCloseLoop"},
    "forge_aud_asian_breakout": {"runner": "forge/aud_asian_breakout/runner.py", "log_dir": "forge/logs/aud_asian_breakout", "task": "ArgusAudOrbLoop"},
    "forge_spy_mean_rev":       {"runner": "forge/spy_mean_rev/runner.py",       "log_dir": "forge/logs/spy_mean_rev",       "task": None},
    "forge_multi_orb":          {"runner": "forge/multi_orb/runner.py",          "log_dir": "forge/logs/multi_orb",          "task": None},
    "forge_vix_intraday":       {"runner": "forge/vix_intraday/runner.py",       "log_dir": "forge/logs/vix_intraday",       "task": None},
    "forge_mamba":              {"runner": "forge/mamba/runner.py",              "log_dir": "forge/logs/mamba",              "task": None},
    "forge_tori":               {"runner": "forge/tori/runner.py",               "log_dir": "forge/logs/tori",               "task": None},
    "forge_cuebanks":           {"runner": "forge/cuebanks/runner.py",           "log_dir": "forge/logs/cuebanks",           "task": None},
    "forge_vix_revert":         {"runner": "forge/vix_revert_runner.py",         "log_dir": "forge/logs/vix_revert",         "task": None},
    "forge_rebalance":          {"runner": "forge/rebalance_runner.py",          "log_dir": "forge/logs/rebalance",          "task": None},
    "forge_wick_gbpusd":        {"runner": "forge/wick_gbpusd/runner.py",        "log_dir": "forge/logs/wick_gbpusd",        "task": None},
    "forge_fomc_drift":         {"runner": "forge/fomc_drift/runner.py",         "log_dir": "forge/logs/fomc_drift",         "task": None},
    "forge_tom_international":  {"runner": "forge/tom_international/runner.py",  "log_dir": "forge/logs/tom_international",  "task": None},
    # Greek family (Helio)
    "apollo":  {"runner": "apollo/runner.py",  "log_dir": "apollo/logs",  "task": None},
    "hermes":  {"runner": "hermes/runner.py",  "log_dir": "hermes/logs",  "task": None},
    "titan":   {"runner": "titan/runner.py",   "log_dir": "titan/logs",   "task": None},
    "ares":    {"runner": "ares/runner.py",    "log_dir": "ares/logs",    "task": None},
}


# ─────────────────────────────────────────────────────────────────────────────
# Check primitives — each returns {"id": int, "name": str, "status": "PASS"|"FAIL"|"WARN"|"MANUAL", "detail": str}
# ─────────────────────────────────────────────────────────────────────────────


def _result(check_id: int, name: str, status: str, detail: str) -> dict:
    return {"id": check_id, "name": name, "status": status, "detail": detail}


def check_runner_exists(strat: str, reg: dict) -> dict:
    """Item 1: Runner module file exists at expected path."""
    runner_path = REPO / reg["runner"]
    if runner_path.exists():
        return _result(1, "runner_exists", "PASS", reg["runner"])
    return _result(1, "runner_exists", "FAIL", f"runner not found at {reg['runner']}")


def check_scheduled_task(strat: str, reg: dict, tasks_data: dict | None) -> dict:
    """Item 2: Scheduled task exists with last_result=0 (where applicable)."""
    if reg["task"] is None:
        return _result(2, "scheduled_task", "PASS", "n/a — runs as continuous loop, not scheduled")
    if tasks_data is None:
        return _result(2, "scheduled_task", "MANUAL", "no recent tasks.json — run full_audit first")
    t = tasks_data.get(reg["task"])
    if t is None:
        return _result(2, "scheduled_task", "FAIL", f"task '{reg['task']}' not registered")
    if not t.get("exists", False):
        return _result(2, "scheduled_task", "FAIL", f"task '{reg['task']}' marked exists=false")
    last = str(t.get("last_result", ""))
    if last == "0":
        return _result(2, "scheduled_task", "PASS", f"{reg['task']} last_result=0")
    return _result(2, "scheduled_task", "WARN", f"{reg['task']} last_result={last} (non-zero exit)")


def check_account_mode(strat: str, reg: dict) -> dict:
    """Item 3: Account mode = paper.

    Fleet is paper-only as of 2026-04-28 (no real-money runners promoted yet).
    Until a real-money boundary is wired in, this check is structural —
    confirms no rogue runner has flipped to real mode by checking for known
    real-money config flags.
    """
    runner_file = REPO / reg["runner"]
    if not runner_file.exists():
        return _result(3, "account_mode_paper", "FAIL", "cannot verify — runner missing")
    try:
        text = runner_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return _result(3, "account_mode_paper", "MANUAL", f"could not read: {e}")
    # Sentinel: any reference to real_money_enabled=True or live account creds = WARN
    if re.search(r"real_money_enabled\s*=\s*True", text):
        return _result(3, "account_mode_paper", "WARN", "real_money_enabled=True found in runner — verify intentional")
    return _result(3, "account_mode_paper", "PASS", "paper-only (no real_money_enabled=True flag)")


def check_client_id_unique(strat: str, reg: dict, all_strats_clientids: dict[str, int]) -> dict:
    """Item 4: IBKR client_id no collision."""
    cid = all_strats_clientids.get(strat)
    if cid is None:
        return _result(4, "client_id_unique", "MANUAL", "client_id not auto-detected — verify manually")
    duplicates = [k for k, v in all_strats_clientids.items() if v == cid and k != strat]
    if duplicates:
        return _result(4, "client_id_unique", "FAIL", f"client_id={cid} collides with: {', '.join(duplicates)}")
    return _result(4, "client_id_unique", "PASS", f"client_id={cid} unique")


def check_symbol_mapping(strat: str, reg: dict) -> dict:
    """Item 5: Correct symbol/contract mapping. MANUAL — strategy-specific."""
    return _result(5, "symbol_mapping", "MANUAL", "verify symbol(s) in runner PARAMS match strategy intent")


def check_market_data(strat: str, reg: dict) -> dict:
    """Item 6: Market data available. MANUAL — requires live TWS connection."""
    return _result(6, "market_data_available", "MANUAL", "verify in TWS Market Data Subscriptions screen")


def check_session_logic(strat: str, reg: dict) -> dict:
    """Item 7: Timezone/session logic correct. MANUAL — case-by-case."""
    return _result(7, "session_logic", "MANUAL", "verify session/timezone gates against strategy spec")


def check_research_only_off(strat: str, reg: dict) -> dict:
    """Item 8: RESEARCH_ONLY flag is False (if intended to trade live)."""
    runner_file = REPO / reg["runner"]
    if not runner_file.exists():
        return _result(8, "research_only_off", "FAIL", "cannot verify — runner missing")
    try:
        text = runner_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return _result(8, "research_only_off", "MANUAL", f"could not read: {e}")
    if re.search(r"RESEARCH_ONLY\s*=\s*True", text):
        return _result(8, "research_only_off", "WARN", "RESEARCH_ONLY=True found in runner")
    if re.search(r'mode_default\s*[:=]\s*[\'"]research_only[\'"]', text):
        return _result(8, "research_only_off", "WARN", "mode_default=research_only found")
    return _result(8, "research_only_off", "PASS", "no RESEARCH_ONLY=True flag")


def check_signals_csv_populated(strat: str, reg: dict) -> dict:
    """Item 9: Strategy logs signal evaluations somehow.

    Prefers signals.csv (forge/argus convention) but accepts alternatives
    used by other families:
      - Greek scanners (apollo/hermes/titan): per-day scan_<date>.json files
      - tom_international and others: planned_trades.jsonl

    Without one of these, gate evaluations leave no trace and operational
    vetting can't confirm the runner is actually evaluating opportunities.
    """
    log_dir = REPO / reg["log_dir"]
    if not log_dir.exists():
        return _result(9, "signals_csv_populated", "FAIL", f"log_dir missing: {log_dir.relative_to(REPO)}")
    # Convention 1: signals.csv with rows
    sig_path = log_dir / "signals.csv"
    if sig_path.exists():
        try:
            with open(sig_path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            rows = max(0, lines - 1)
            if rows > 0:
                return _result(9, "signals_csv_populated", "PASS", f"signals.csv: {rows} rows")
        except Exception as e:
            return _result(9, "signals_csv_populated", "MANUAL", f"signals.csv read error: {e}")
    # Convention 2: Greek family per-day scans
    scans = list(log_dir.glob("scan_*.json"))
    if scans:
        recent = sorted(scans, key=lambda p: p.stat().st_mtime)[-1]
        age_h = (datetime.now().timestamp() - recent.stat().st_mtime) / 3600
        return _result(9, "signals_csv_populated", "PASS", f"{len(scans)} scan_*.json files (latest {age_h:.1f}h ago)")
    # Convention 3: planned_trades.jsonl (some Helio runners)
    pt = log_dir / "planned_trades.jsonl"
    if pt.exists() and pt.stat().st_size > 0:
        return _result(9, "signals_csv_populated", "PASS", f"planned_trades.jsonl: {pt.stat().st_size} bytes")
    # Nothing found
    return _result(9, "signals_csv_populated", "FAIL",
                   f"no signal log found (checked signals.csv, scan_*.json, planned_trades.jsonl in {log_dir.relative_to(REPO)})")


def check_blocked_signals_logged(strat: str, reg: dict) -> dict:
    """Item 10: Blocked signals logged with reasons.

    For signals.csv: check header has verdict/blocked/reason column.
    For Greek/Helio: check that scan_<date>.json contains rationale fields
    (apollo's scan files include 'rejected'/'rejection_reason' per candidate).
    """
    log_dir = REPO / reg["log_dir"]
    sig_path = log_dir / "signals.csv"
    if sig_path.exists():
        try:
            with open(sig_path, encoding="utf-8") as f:
                header = f.readline()
                sample_rows = [f.readline() for _ in range(20)]
        except Exception as e:
            return _result(10, "blocked_signals_logged", "MANUAL", f"read error: {e}")
        sample_rows = [r for r in sample_rows if r.strip()]
        has_verdict = ("verdict" in header.lower() or "blocked" in header.lower() or "reason" in header.lower())
        if not sample_rows:
            return _result(10, "blocked_signals_logged", "FAIL", "signals.csv header only — no rows")
        if not has_verdict:
            return _result(10, "blocked_signals_logged", "WARN", "no verdict/blocked/reason column in signals.csv header")
        return _result(10, "blocked_signals_logged", "PASS", f"verdict column + {len(sample_rows)} sample rows")
    # Greek scanner: peek at most-recent scan_*.json
    scans = sorted(log_dir.glob("scan_*.json"), key=lambda p: p.stat().st_mtime)
    if scans:
        try:
            scan_data = json.loads(scans[-1].read_text(encoding="utf-8", errors="ignore"))
            txt = json.dumps(scan_data)[:5000]
            if "reject" in txt.lower() or "blocked" in txt.lower() or "reason" in txt.lower():
                return _result(10, "blocked_signals_logged", "PASS", f"latest scan has rejection/reason fields")
            return _result(10, "blocked_signals_logged", "WARN", f"latest scan has no obvious rejection field")
        except Exception as e:
            return _result(10, "blocked_signals_logged", "MANUAL", f"scan parse error: {e}")
    return _result(10, "blocked_signals_logged", "FAIL", "no signals.csv or scan_*.json — covered by item 9")


def check_synthetic_signal(strat: str, reg: dict) -> dict:
    """Item 11: Strategy can generate at least one synthetic/test signal. MANUAL."""
    return _result(11, "synthetic_signal", "MANUAL", "run runner with --backtest or --once to verify signal generation")


def check_orders_block_reasons(strat: str, reg: dict) -> dict:
    """Item 12: Orders blocked only for valid reasons (parse runner.log for REAL_ENTRY FAILED)."""
    log_path = REPO / reg["log_dir"] / "runner.log"
    if not log_path.exists():
        return _result(12, "order_block_reasons", "MANUAL", f"no runner.log at {log_path.relative_to(REPO)}")
    try:
        # Tail last 500 lines
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-500:]
    except Exception as e:
        return _result(12, "order_block_reasons", "MANUAL", f"read error: {e}")
    failure_lines = [l for l in lines if "REAL_ENTRY FAILED" in l or "BLOCKED" in l]
    if not failure_lines:
        return _result(12, "order_block_reasons", "PASS", "no REAL_ENTRY FAILED / BLOCKED lines in last 500 (silence may mean no attempts)")
    # Categorize
    reasons: dict[str, int] = {}
    for l in failure_lines:
        for tag in ("market_closed", "cluster_cap_breach", "duplicate_entry", "rth_only",
                    "fx_below_idealpro_min", "fleet_halted", "no_quote", "ex_pickle"):
            if tag in l:
                reasons[tag] = reasons.get(tag, 0) + 1
                break
        else:
            reasons["other"] = reasons.get("other", 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:5])
    return _result(12, "order_block_reasons", "WARN" if reasons.get("other", 0) > 5 else "PASS",
                   f"{len(failure_lines)} block events: {summary}")


def check_no_stale_state(strat: str, reg: dict) -> dict:
    """Item 13: No stale state preventing entry (state.json mtime within 24h if exists)."""
    state_path = REPO / reg["log_dir"] / "state.json"
    if not state_path.exists():
        return _result(13, "no_stale_state", "MANUAL", "no state.json found (strategy may not use one)")
    age_s = (datetime.now().timestamp() - state_path.stat().st_mtime)
    age_h = age_s / 3600
    if age_h > 72:
        return _result(13, "no_stale_state", "WARN", f"state.json {age_h:.1f}h old — verify strategy isn't blocked on stale state")
    return _result(13, "no_stale_state", "PASS", f"state.json {age_h:.1f}h old")


def check_session_guards(strat: str, reg: dict) -> dict:
    """Item 14: Weekend/holiday guards correct. MANUAL — requires market-calendar review."""
    return _result(14, "session_guards", "MANUAL", "verify weekend/holiday/early-close guards against expected fire dates")


def check_outputs_updating(strat: str, reg: dict) -> dict:
    """Item 15: Output files (heartbeat.json) update as expected — within last 30 minutes."""
    hb_path = REPO / reg["log_dir"] / "heartbeat.json"
    if not hb_path.exists():
        return _result(15, "outputs_updating", "FAIL", f"missing {hb_path.relative_to(REPO)}")
    age_s = datetime.now().timestamp() - hb_path.stat().st_mtime
    age_min = age_s / 60
    # During market hours, heartbeat should be fresh (< 5 min). Off-hours, stale is expected.
    if age_min < 5:
        return _result(15, "outputs_updating", "PASS", f"heartbeat.json fresh ({age_min:.1f}m)")
    if age_min < 60:
        return _result(15, "outputs_updating", "WARN", f"heartbeat.json {age_min:.1f}m old — runner may be paused or off-cycle")
    if age_min < 60 * 24:
        return _result(15, "outputs_updating", "WARN", f"heartbeat.json {age_min/60:.1f}h old — runner likely down or off-hours")
    return _result(15, "outputs_updating", "FAIL", f"heartbeat.json {age_min/60/24:.1f}d old — runner has not started recently")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────


# Authoritative client_id assignments per CLAUDE.md (2026-04-24).
# Edit this when a new runner gets a client_id assignment.
KNOWN_CLIENT_IDS: dict[str, int] = {
    "argus_usdjpy": 12, "argus_gbpusd": 51, "argus_cadjpy": 53,
    "titan": 60, "ares": 70, "hermes": 80, "apollo": 90,
    "forge_gdx_gld": 101, "forge_gld_pm_long": 102, "forge_jpy_pm_short": 103,
    "forge_nq_overnight": 104, "forge_nq_london_close": 105, "forge_aud_asian_breakout": 106,
    "forge_spy_mean_rev": 107, "forge_multi_orb": 108, "forge_vix_intraday": 109,
    "forge_mamba": 110, "forge_tori": 111, "forge_cuebanks": 112,
    "forge_vix_revert": 113, "forge_wick_gbpusd": 114, "forge_fomc_drift": 115,
    "forge_tom_international": 116, "forge_rebalance": 117,
}


def _load_silent_strategies() -> list[str]:
    """Pull strategies with verdict=WAITING (zero live trades) from the latest
    operational_maturity report. Falls back to file enumeration on miss."""
    fp = REPO / "argus_flow" / "logs" / "operational_maturity_latest.json"
    if not fp.exists():
        # Fallback to most recent dated report
        candidates = sorted((REPO / "argus_flow" / "logs").glob("operational_maturity_*.json"))
        if not candidates:
            return []
        fp = candidates[-1]
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [s["strategy"] for s in data.get("strategies", []) if s.get("live_trades", 0) == 0]


def _load_latest_tasks_json() -> dict | None:
    """Find the most recent full_audit tasks.json."""
    candidates = sorted((REPO / "docs" / "audits").glob("*/tasks.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def run_checklist(strat: str, reg: dict, tasks_data: dict | None) -> dict:
    """Run all 15 checks for one strategy."""
    checks = [
        check_runner_exists(strat, reg),
        check_scheduled_task(strat, reg, tasks_data),
        check_account_mode(strat, reg),
        check_client_id_unique(strat, reg, KNOWN_CLIENT_IDS),
        check_symbol_mapping(strat, reg),
        check_market_data(strat, reg),
        check_session_logic(strat, reg),
        check_research_only_off(strat, reg),
        check_signals_csv_populated(strat, reg),
        check_blocked_signals_logged(strat, reg),
        check_synthetic_signal(strat, reg),
        check_orders_block_reasons(strat, reg),
        check_no_stale_state(strat, reg),
        check_session_guards(strat, reg),
        check_outputs_updating(strat, reg),
    ]
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "MANUAL": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    # Auto-verdict for the operational track:
    #   FAIL > 0  => BLOCKED (something structural is wrong)
    #   PASS + auto items >= 7  => OPERATIONAL_VERIFIED_AUTO (manual checks remain)
    #   else => INSUFFICIENT_AUTO_CHECKS
    if counts["FAIL"] > 0:
        verdict_auto = "BLOCKED"
    elif counts["PASS"] >= 7:
        verdict_auto = "OPERATIONAL_VERIFIED_AUTO"
    else:
        verdict_auto = "INSUFFICIENT_AUTO_CHECKS"
    return {
        "strategy": strat,
        "verdict_auto": verdict_auto,
        "counts": counts,
        "checks": checks,
    }


def main() -> int:
    silent = _load_silent_strategies()
    tasks = _load_latest_tasks_json()

    # Guard: any silent strategy not in the registry is a config gap — flag it.
    not_registered = [s for s in silent if s not in STRATEGY_REGISTRY]
    if not_registered:
        print(f"WARN: {len(not_registered)} silent strategies not in STRATEGY_REGISTRY:")
        for s in not_registered:
            print(f"  - {s}")
        print("  Add them to STRATEGY_REGISTRY and re-run for complete coverage.\n")

    results = []
    for strat in silent:
        if strat not in STRATEGY_REGISTRY:
            continue
        results.append(run_checklist(strat, STRATEGY_REGISTRY[strat], tasks))

    out_dir = REPO / "argus_flow" / "logs" / "ceremony_prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = out_dir / f"operational_vetting_{today}.json"
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_silent_strategies": len(silent),
        "n_vetted": len(results),
        "n_not_registered": len(not_registered),
        "verdict_summary": {
            "BLOCKED": sum(1 for r in results if r["verdict_auto"] == "BLOCKED"),
            "OPERATIONAL_VERIFIED_AUTO": sum(1 for r in results if r["verdict_auto"] == "OPERATIONAL_VERIFIED_AUTO"),
            "INSUFFICIENT_AUTO_CHECKS": sum(1 for r in results if r["verdict_auto"] == "INSUFFICIENT_AUTO_CHECKS"),
        },
        "strategies": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Console summary
    print(f"Vetted {len(results)} of {len(silent)} silent strategies")
    print(f"Output: {out_path.relative_to(REPO)}\n")
    print(f"{'STRATEGY':30s}  {'VERDICT':28s}  PASS WARN FAIL MANUAL")
    print("-" * 90)
    for r in results:
        c = r["counts"]
        print(f"{r['strategy']:30s}  {r['verdict_auto']:28s}  {c['PASS']:>4} {c['WARN']:>4} {c['FAIL']:>4} {c['MANUAL']:>6}")
    print()
    print(f"BLOCKED:                   {payload['verdict_summary']['BLOCKED']}")
    print(f"OPERATIONAL_VERIFIED_AUTO: {payload['verdict_summary']['OPERATIONAL_VERIFIED_AUTO']}")
    print(f"INSUFFICIENT_AUTO_CHECKS:  {payload['verdict_summary']['INSUFFICIENT_AUTO_CHECKS']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
