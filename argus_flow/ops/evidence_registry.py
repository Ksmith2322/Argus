"""Canonical Evidence Registry — single source of governance truth per runner.

Generates one evidence_registry.json per validation-lane runner, consolidating
data from trades.csv, state.json, heartbeat.json, and all governance reports.

This replaces the conceptual federation of surfaces with one canonical artifact.

Usage:
    python -m argus_flow.ops.evidence_registry
    python -m argus_flow.ops.evidence_registry --symbol GBPUSD
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
CONFIGS = REPO / "argus_flow" / "configs"

# ─────────────────────────────────────────────────────────────
# Runner definitions
# ─────────────────────────────────────────────────────────────

VALIDATION_RUNNERS = [
    {
        "name": "EUR/USD",
        "symbol": "EURUSD",
        "log_dir": "eurusd",
        "config_file": "eurusd_t4_paper_v1.json",
        "strategy_status": "PAPER-VALIDATING",
    },
    {
        "name": "GBP/USD",
        "symbol": "GBPUSD",
        "log_dir": "gbpusd",
        "config_file": "gbpusd_range_paper_v1.json",
        "strategy_status": "PAPER-VALIDATING",
    },
    {
        "name": "EUR/JPY",
        "symbol": "EURJPY",
        "log_dir": "eurjpy",
        "config_file": "eurjpy_t4_paper_v1.json",
        "strategy_status": "PAPER-VALIDATING",
    },
]

OBSERVATION_RUNNERS = [
    {
        "name": "AUD/USD",
        "symbol": "AUDUSD",
        "log_dir": "audusd",
        "config_file": "audusd_ny_paper_v1.json",
        "strategy_status": "OBSERVATION",
    },
    {
        "name": "USD/JPY",
        "symbol": "USDJPY",
        "log_dir": "usdjpy",
        "config_file": "usdjpy_ny_paper_v1.json",
        "strategy_status": "OBSERVATION",
    },
    {
        "name": "AUD/JPY",
        "symbol": "AUDJPY",
        "log_dir": "audjpy",
        "config_file": "audjpy_t4_paper_v1.json",
        "strategy_status": "OBSERVATION",
    },
    {
        "name": "CAD/JPY",
        "symbol": "CADJPY",
        "log_dir": "cadjpy",
        "config_file": "cadjpy_t4_paper_v1.json",
        "strategy_status": "OBSERVATION",
    },
    {
        "name": "GBP/JPY",
        "symbol": "GBPJPY",
        "log_dir": "gbpjpy",
        "config_file": "gbpjpy_t4_paper_v1.json",
        "strategy_status": "OBSERVATION",
    },
]

ALL_RUNNERS = VALIDATION_RUNNERS + OBSERVATION_RUNNERS

PROMOTION_TARGET = 30


# ─────────────────────────────────────────────────────────────
# Data loaders
# ─────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    """Load a JSON file, return None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_trades(log_dir: Path) -> list[dict]:
    """Load trades.csv, return list of row dicts."""
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_hashes() -> dict:
    """Load configs/hashes.json — config file name → hash."""
    return _load_json(CONFIGS / "hashes.json") or {}


def _find_runner_in_report(report: dict | None, symbol: str) -> dict | None:
    """Find a runner entry in a governance report by symbol or name."""
    if report is None:
        return None
    runners = report.get("runners", [])
    for r in runners:
        if r.get("symbol") == symbol or r.get("name") == symbol:
            return r
        # Some reports use display name
        for rdef in ALL_RUNNERS:
            if rdef["symbol"] == symbol and r.get("name") == rdef["name"]:
                return r
    return None


# ─────────────────────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────────────────────

def _build_cohort(trades: list[dict], runner_def: dict, hashes: dict) -> dict:
    """Build cohort section from trades.csv data."""
    is_validation = runner_def in VALIDATION_RUNNERS

    valid_trades = [t for t in trades if t.get("experiment_valid", "").lower() == "true"]
    invalid_trades = [t for t in trades if t.get("experiment_valid", "").lower() == "false"]
    invalid_reasons: dict[str, int] = Counter()
    for t in invalid_trades:
        reason = t.get("invalid_reason", "unknown") or "unknown"
        invalid_reasons[reason] += 1

    total = len(trades)
    invalid_rate = len(invalid_trades) / total if total > 0 else 0.0

    # Frozen config hash from hashes.json
    config_file = runner_def.get("config_file", "")
    frozen_hash = hashes.get(config_file, None)

    # Git SHA — pick from first trade if available
    frozen_git_sha = None
    for t in valid_trades:
        sha = t.get("git_sha")
        if sha:
            frozen_git_sha = sha
            break

    # Start timestamp — earliest trade
    start_ts = None
    if trades:
        try:
            start_ts = trades[0].get("ts")
        except Exception:
            pass

    return {
        "active": is_validation,
        "start_timestamp": start_ts,
        "frozen_config_hash": frozen_hash,
        "frozen_git_sha": frozen_git_sha,
        "valid_trade_count": len(valid_trades),
        "invalid_trade_count": len(invalid_trades),
        "invalid_reasons": dict(invalid_reasons),
        "total_trade_count": total,
        "invalid_rate": round(invalid_rate, 4),
        "promotion_target": PROMOTION_TARGET,
    }


def _build_performance(trades: list[dict]) -> dict:
    """Build performance section from trades.csv data."""
    valid_trades = [t for t in trades if t.get("experiment_valid", "").lower() == "true"]
    pnls: list[float] = []
    exit_reasons: dict[str, int] = Counter()

    for t in valid_trades:
        try:
            pnl = float(t.get("pnl_pips", 0))
            pnls.append(pnl)
        except (ValueError, TypeError):
            pass
        reason = t.get("exit_reason", "unknown") or "unknown"
        exit_reasons[reason] += 1

    if not pnls:
        return {
            "total_pnl_pips": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy_pips": 0.0,
            "max_drawdown_pips": 0.0,
            "avg_win": 0,
            "avg_loss": 0,
            "exit_reasons": dict(exit_reasons),
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) if pnls else 0.0
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = total_pnl / len(pnls) if pnls else 0.0

    # Max drawdown (peak-to-trough)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    avg_win = round(sum(wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0

    return {
        "total_pnl_pips": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else "inf",
        "expectancy_pips": round(expectancy, 2),
        "max_drawdown_pips": round(max_dd, 2),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "exit_reasons": dict(exit_reasons),
    }


def _build_governance(
    symbol: str,
    kill_report: dict | None,
    divergence_report: dict | None,
    artifact_report: dict | None,
    promotion_report: dict | None,
) -> dict:
    """Build governance section from all governance reports."""
    # Kill discipline
    kill_entry = _find_runner_in_report(kill_report, symbol)
    kill_status = "UNKNOWN"
    kill_flags: list[str] = []
    if kill_entry:
        kill_status = kill_entry.get("status", "UNKNOWN")
        kill_flags = kill_entry.get("flags", [])

    # Divergence
    div_entry = _find_runner_in_report(divergence_report, symbol)
    div_status = "UNKNOWN"
    div_flags: list[str] = []
    if div_entry:
        div_status = div_entry.get("status", "UNKNOWN")
        div_flags = div_entry.get("flags", [])

    # Artifact integrity
    art_entry = _find_runner_in_report(artifact_report, symbol)
    art_status = "UNKNOWN"
    art_alerts: list[str] = []
    if art_entry:
        art_status = art_entry.get("status", "UNKNOWN")
        art_alerts = [c.get("detail", "") for c in art_entry.get("checks", []) if not c.get("passed", True)]
        if not art_alerts:
            art_alerts = art_entry.get("alerts", [])

    # Promotion gate
    promo_entry = _find_runner_in_report(promotion_report, symbol)
    promo_verdict = "UNKNOWN"
    promo_blockers: list[str] = []
    promo_passed = 0
    promo_total = 0
    if promo_entry:
        promo_verdict = promo_entry.get("verdict", "UNKNOWN")
        promo_blockers = promo_entry.get("blockers", [])
        checks = promo_entry.get("checks", {})
        promo_total = len(checks)
        promo_passed = sum(1 for c in checks.values() if isinstance(c, dict) and c.get("passed", False))

    return {
        "kill_discipline_status": kill_status,
        "kill_discipline_flags": kill_flags,
        "divergence_status": div_status,
        "divergence_flags": div_flags,
        "artifact_integrity": art_status,
        "artifact_alerts": art_alerts,
        "promotion_gate_verdict": promo_verdict,
        "promotion_gate_blockers": promo_blockers,
        "promotion_gate_checks_passed": promo_passed,
        "promotion_gate_checks_total": promo_total,
    }


def _build_runtime(log_dir: Path) -> dict:
    """Build runtime section from heartbeat.json and state.json."""
    hb = _load_json(log_dir / "heartbeat.json")
    state = _load_json(log_dir / "state.json")

    runner_alive = False
    heartbeat_age_s: int | None = None
    position = "UNKNOWN"
    session_id: str | None = None
    consecutive_errors = 0
    quarantined = False

    if hb:
        ts_str = hb.get("ts")
        if ts_str:
            try:
                hb_time = datetime.fromisoformat(ts_str)
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                heartbeat_age_s = int(age)
                # Consider alive if heartbeat is < 5 minutes old
                runner_alive = age < 300
            except Exception:
                pass

        position = hb.get("position", "UNKNOWN")
        session_id = hb.get("session_id")
        consecutive_errors = hb.get("consecutive_errors", 0)
        quarantined = hb.get("quarantined", False)

    # Prefer state.json position if available
    if state:
        position = state.get("position", position)

    return {
        "runner_alive": runner_alive,
        "heartbeat_age_s": heartbeat_age_s,
        "position": position,
        "session_id": session_id,
        "consecutive_errors": consecutive_errors,
        "quarantined": quarantined,
    }


def _build_eligibility(cohort: dict, performance: dict, governance: dict) -> dict:
    """Build eligibility section — human-readable promotion status."""
    blockers = governance.get("promotion_gate_blockers", [])
    promo_verdict = governance.get("promotion_gate_verdict", "UNKNOWN")

    promotion_eligible = promo_verdict == "PROMOTE"

    valid = cohort.get("valid_trade_count", 0)
    target = cohort.get("promotion_target", PROMOTION_TARGET)
    remaining = max(0, target - valid)

    if promotion_eligible:
        next_milestone = "Ready for promotion review"
    elif remaining > 0:
        next_milestone = f"{remaining} more valid trade{'s' if remaining != 1 else ''} needed"
    else:
        next_milestone = "Trade count met — clear remaining blockers"

    blockers_summary = ", ".join(blockers) if blockers else "none"

    return {
        "promotion_eligible": promotion_eligible,
        "next_milestone": next_milestone,
        "blockers_summary": blockers_summary,
    }


# ─────────────────────────────────────────────────────────────
# Main registry builder
# ─────────────────────────────────────────────────────────────

def build_registry(runner_def: dict) -> dict:
    """Build the complete evidence registry for one runner."""
    symbol = runner_def["symbol"]
    log_dir = LOGS / runner_def["log_dir"]
    is_validation = runner_def in VALIDATION_RUNNERS
    lane = "validation" if is_validation else "observation"

    # Load data sources
    trades = _load_trades(log_dir)
    hashes = _load_hashes()

    # Load governance reports (global files, we extract per-symbol)
    kill_report = _load_json(LOGS / "kill_discipline_report.json")
    divergence_report = _load_json(LOGS / "divergence_report.json")
    artifact_report = _load_json(LOGS / "artifact_divergence_report.json")
    promotion_report = _load_json(LOGS / "promotion_gate_report.json")

    # Determine blocker (top-level concern if any)
    blocker = None
    kill_entry = _find_runner_in_report(kill_report, symbol)
    if kill_entry and kill_entry.get("status") == "KILL":
        blocker = "KILL_DISCIPLINE"

    # Build sections
    cohort = _build_cohort(trades, runner_def, hashes)
    performance = _build_performance(trades)
    governance = _build_governance(symbol, kill_report, divergence_report, artifact_report, promotion_report)
    runtime = _build_runtime(log_dir)
    eligibility = _build_eligibility(cohort, performance, governance)

    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        "symbol": symbol,
        "name": runner_def["name"],
        "lane": lane,
        "strategy_status": runner_def["strategy_status"],
        "blocker": blocker,
        "cohort": cohort,
        "performance": performance,
        "governance": governance,
        "runtime": runtime,
        "last_governance_review": None,
        "last_updated": now_iso,
        "eligibility": eligibility,
    }


def generate_registry(symbol_filter: str | None = None) -> list[dict]:
    """Generate registries for all runners (or one if symbol_filter is set).

    Returns list of generated registry dicts.
    """
    runners_to_process = ALL_RUNNERS
    if symbol_filter:
        symbol_upper = symbol_filter.upper()
        runners_to_process = [r for r in ALL_RUNNERS if r["symbol"] == symbol_upper]
        if not runners_to_process:
            print(f"ERROR: Unknown symbol '{symbol_filter}'. Available: {[r['symbol'] for r in ALL_RUNNERS]}")
            return []

    results = []
    for runner_def in runners_to_process:
        registry = build_registry(runner_def)

        # Write to per-symbol log dir
        out_dir = LOGS / runner_def["log_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "evidence_registry.json"
        out_path.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")

        results.append(registry)
        _print_summary(registry)

    return results


def _print_summary(reg: dict) -> None:
    """Print a concise console summary for one runner."""
    symbol = reg["symbol"]
    lane = reg["lane"].upper()
    status = reg["strategy_status"]
    cohort = reg["cohort"]
    perf = reg["performance"]
    gov = reg["governance"]
    elig = reg["eligibility"]
    runtime = reg["runtime"]

    alive_str = "ALIVE" if runtime["runner_alive"] else "DEAD"
    position = runtime["position"]
    hb_age = runtime.get("heartbeat_age_s")
    hb_str = f"{hb_age}s" if hb_age is not None else "N/A"

    valid = cohort["valid_trade_count"]
    target = cohort["promotion_target"]
    pnl = perf["total_pnl_pips"]
    wr = perf["win_rate"] * 100

    kill = gov["kill_discipline_status"]
    promo = gov["promotion_gate_verdict"]
    passed = gov["promotion_gate_checks_passed"]
    total = gov["promotion_gate_checks_total"]

    print(f"\n{'=' * 60}")
    print(f"  {symbol} ({reg['name']})  |  {lane}  |  {status}")
    print(f"{'=' * 60}")
    print(f"  Runtime:      {alive_str}  |  {position}  |  heartbeat {hb_str}")
    if runtime["quarantined"]:
        print(f"  ** QUARANTINED **  errors={runtime['consecutive_errors']}")
    print(f"  Cohort:       {valid}/{target} valid trades  |  invalid rate {cohort['invalid_rate']:.1%}")
    print(f"  Performance:  PnL {pnl:+.2f} pips  |  WR {wr:.1f}%  |  PF {perf['profit_factor']}")
    print(f"  Kill:         {kill}  |  Promotion: {promo} ({passed}/{total} checks)")

    if reg["blocker"]:
        print(f"  ** BLOCKER: {reg['blocker']} **")

    print(f"  NEXT MILESTONE: {elig['next_milestone']}")
    if elig["blockers_summary"] != "none":
        print(f"  Blockers: {elig['blockers_summary']}")

    out_path = LOGS / symbol.lower() / "evidence_registry.json"
    # Use forward slashes for display even on Windows, but show relative from repo
    try:
        rel = out_path.relative_to(REPO)
    except ValueError:
        rel = out_path
    print(f"  Written: {rel}")


# ─────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate canonical evidence registries")
    parser.add_argument("--symbol", type=str, default=None, help="Generate for a single symbol (e.g. GBPUSD)")
    args = parser.parse_args()

    results = generate_registry(args.symbol)

    if results:
        print(f"\n{'-' * 60}")
        print(f"  Generated {len(results)} evidence registr{'y' if len(results) == 1 else 'ies'}")
        print(f"{'-' * 60}")


if __name__ == "__main__":
    main()
