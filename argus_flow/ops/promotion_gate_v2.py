"""Promotion gate checker for paper-to-live advancement.

Runs governance checks for each cohort runner and writes a canonical
promotion_gate_report.json under argus_flow/logs.

Promotion is intentionally strict:
- hard failures after a sufficiently large cohort are BLOCKED
- missing required proof stays NOT_READY
- only fully evidenced, fully passing runners reach PROMOTE
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import urlopen

from argus_flow.ops.fleet_registry import DEPLOYMENT_REGISTRY_FILE, STAGE_PAPER, discover_managed_runners

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
CONFIGS = REPO / "argus_flow" / "configs"

PROMOTION_THRESHOLD = 60
PROMOTION_MIN_CALENDAR_DAYS = 14
MODELED_FRICTION_PIPS = 0.3
LIVE_DRAWDOWN_BUFFER_RATIO = 1.25
LIVE_DRAWDOWN_BUFFER_ABS = 2.0
REQUIRED_EVIDENCE_CHECKS = {
    "walk_forward_positive",
    "live_drawdown_vs_walkforward",
    "dashboard_truth",
    # survived_disconnect is advisory — a broker disconnect may never happen
    # naturally during paper trading, so requiring it creates a permanent blocker.
}


class CheckResult(NamedTuple):
    passed: bool
    detail: str
    classification: str = "hard"


def _hard(passed: bool, detail: str) -> CheckResult:
    return CheckResult(bool(passed), detail, "hard")


def _advisory(passed: bool, detail: str) -> CheckResult:
    return CheckResult(bool(passed), detail, "advisory")


def _unevidenced(detail: str) -> CheckResult:
    return CheckResult(False, detail, "unevidenced")


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pnl_field(trades: list[dict]) -> str:
    if trades and "pnl_pips" in trades[0]:
        return "pnl_pips"
    return "pnl_pts"


def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


_DASHBOARD_FLEET_CACHE: dict[str, dict] | None = None


def _load_dashboard_fleet() -> dict[str, dict] | None:
    global _DASHBOARD_FLEET_CACHE
    if _DASHBOARD_FLEET_CACHE is not None:
        return _DASHBOARD_FLEET_CACHE
    try:
        with urlopen("http://127.0.0.1:8080/api/ibkr_fleet", timeout=2.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, TimeoutError):
        return None

    runners = payload.get("runners", []) if isinstance(payload, dict) else []
    fleet: dict[str, dict] = {}
    for runner in runners:
        if not isinstance(runner, dict):
            continue
        symbol = str(runner.get("symbol", "") or "").upper()
        if symbol:
            fleet[symbol] = runner
    _DASHBOARD_FLEET_CACHE = fleet
    return fleet


def _stage_entered_at(symbol: str, config_file: str = "") -> datetime | None:
    report = _load_json(DEPLOYMENT_REGISTRY_FILE)
    if not isinstance(report, dict):
        return None
    for runner in report.get("runners", []):
        if not isinstance(runner, dict):
            continue
        if str(runner.get("symbol", "")).upper() != symbol.upper():
            continue
        if config_file and str(runner.get("config_file", "") or "") != config_file:
            continue
        ts = str(runner.get("stage_entered_at", "") or "").strip()
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _load_replay_expectations(symbol: str) -> dict:
    for cfg_path in CONFIGS.glob("*.json"):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(cfg.get("symbol", "")).upper() == symbol.upper():
            return cfg.get("replay_expectations", {})
    return {}


def _load_walkforward_report(log_dir: Path) -> dict | None:
    return _load_json(log_dir / "walkforward_report.json")


def _max_drawdown(trades: list[dict]) -> float:
    pnl_field = _pnl_field(trades)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += _safe_float(trade.get(pnl_field, 0.0))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 4)


def check_min_valid_trades(valid_trades: list[dict]) -> CheckResult:
    n = len(valid_trades)
    if n >= PROMOTION_THRESHOLD:
        return _hard(True, f"{n} valid trades (>={PROMOTION_THRESHOLD})")
    return _hard(False, f"{n} valid trades (need {PROMOTION_THRESHOLD})")


def check_min_calendar_days(runner: dict) -> CheckResult:
    stage_start = _stage_entered_at(
        str(runner.get("symbol", "") or ""),
        str(runner.get("config_file", "") or ""),
    )
    if stage_start is None:
        return _unevidenced("stage_entered_at missing from deployment registry")
    calendar_days = max((datetime.now(timezone.utc) - stage_start).total_seconds() / 86400.0, 0.0)
    detail = f"{calendar_days:.1f} calendar days in QA (need >= {PROMOTION_MIN_CALENDAR_DAYS})"
    return _hard(calendar_days >= PROMOTION_MIN_CALENDAR_DAYS, detail)


def check_frozen_config_hash(valid_trades: list[dict]) -> CheckResult:
    hashes = {str(t.get("config_hash", "")) for t in valid_trades}
    hashes.discard("")
    if not hashes:
        return _hard(False, "no config_hash field found in valid trades")
    if len(hashes) == 1:
        return _hard(True, f"config_hash consistent: {next(iter(hashes))[:12]}...")
    preview = ", ".join(h[:12] for h in sorted(hashes))
    return _hard(False, f"{len(hashes)} config hashes found: {preview}")


def check_git_sha_consistent(valid_trades: list[dict]) -> CheckResult:
    shas = {str(t.get("git_sha", "")) for t in valid_trades}
    shas.discard("")
    if not shas:
        return _hard(False, "no git_sha field found in valid trades")
    if len(shas) == 1:
        return _hard(True, f"git_sha consistent: {next(iter(shas))}")
    return _hard(False, f"{len(shas)} git SHAs found: {', '.join(sorted(shas))}")


def check_invalid_rate(all_trades: list[dict]) -> CheckResult:
    if not all_trades:
        return _hard(False, "no trades recorded")
    invalid_count = sum(1 for t in all_trades if str(t.get("experiment_valid", "")).lower() != "true")
    rate = invalid_count / len(all_trades)
    if rate < 0.10:
        return _hard(True, f"invalid rate {rate:.1%} ({invalid_count}/{len(all_trades)}) < 10%")
    return _hard(False, f"invalid rate {rate:.1%} ({invalid_count}/{len(all_trades)}) >= 10%")


def check_no_runtime_anomalies(valid_trades: list[dict]) -> CheckResult:
    for trade in valid_trades:
        reason = str(trade.get("invalid_reason", "") or "").strip().lower()
        if reason not in ("", "null", "none"):
            return _hard(False, f"valid trade has invalid_reason set: {trade.get('invalid_reason')}")
    return _hard(True, "no invalid_reason flags inside valid trades")


def check_positive_expectancy(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    if not pnls:
        return _hard(False, "no PnL data in valid trades")

    raw_exp = sum(pnls) / len(pnls)
    if pnl_field == "pnl_pips":
        adjusted = raw_exp - MODELED_FRICTION_PIPS
        detail = (
            f"expectancy {adjusted:+.3f} pips/trade "
            f"(raw {raw_exp:+.3f} - {MODELED_FRICTION_PIPS:.1f} friction)"
        )
    else:
        adjusted = raw_exp
        detail = f"expectancy {adjusted:+.3f} pts/trade (raw, no futures friction model)"

    return _hard(adjusted > 0, detail)


def check_session_concentration(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    total_pnl = sum(_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades)
    if abs(total_pnl) < 1e-9:
        return _hard(True, "total PnL is flat, no session concentration issue")

    def market_session(ts_str: str) -> str:
        try:
            hour = int(ts_str[11:13])
        except (TypeError, ValueError, IndexError):
            return "UNKNOWN"
        if 22 <= hour or hour < 8:
            return "ASIA"
        if 8 <= hour < 13:
            return "LONDON"
        return "NY"

    session_pnl: dict[str, float] = {}
    for trade in valid_trades:
        session = market_session(str(trade.get("ts", "")))
        session_pnl[session] = session_pnl.get(session, 0.0) + _safe_float(trade.get(pnl_field, 0.0))

    for session, pnl in session_pnl.items():
        concentration = abs(pnl) / abs(total_pnl)
        if concentration > 0.40:
            return _hard(
                False,
                f"market session {session} contributes {concentration:.0%} of total PnL ({pnl:+.2f}/{total_pnl:+.2f})",
            )
    max_concentration = max(abs(v) / abs(total_pnl) for v in session_pnl.values()) if session_pnl else 0.0
    return _hard(True, f"max market session concentration {max_concentration:.0%} (<=40%)")


def check_outlier_trade(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    total_pnl = sum(pnls)
    if abs(total_pnl) < 1e-9:
        return _hard(True, "total PnL is flat, no outlier issue")

    for idx, pnl in enumerate(pnls, start=1):
        concentration = abs(pnl) / abs(total_pnl)
        if concentration > 0.25:
            return _hard(
                False,
                f"trade #{idx} contributes {concentration:.0%} of total PnL ({pnl:+.2f}/{total_pnl:+.2f})",
            )
    max_concentration = max(abs(p) / abs(total_pnl) for p in pnls) if pnls else 0.0
    return _hard(True, f"max single-trade concentration {max_concentration:.0%} (<=25%)")


def check_regime_diversity(valid_trades: list[dict]) -> CheckResult:
    regimes: set[str] = set()
    field_found = False
    for t in valid_trades:
        for key in ("regime", "entry_regime"):
            val = str(t.get(key, "") or "").strip()
            if val and val.lower() not in ("", "null", "none"):
                field_found = True
                regimes.add(val)
    if not field_found:
        return _advisory(True, "no regime/entry_regime field in trades — check skipped")
    n = len(regimes)
    if n >= 2:
        return _hard(True, f"{n} regimes observed: {', '.join(sorted(regimes))}")
    return _hard(False, f"only {n} regime(s) observed: {', '.join(sorted(regimes))} (need >=2)")


def check_give_back(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    if not pnls:
        return _hard(False, "no PnL data in valid trades")

    cumulative = 0.0
    peak = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)

    if peak <= 0:
        return _hard(False, f"peak cumulative PnL {peak:+.2f} never positive")

    retention = cumulative / peak
    if retention >= 0.65:
        return _hard(True, f"retained {retention:.0%} of peak PnL (current {cumulative:+.2f}, peak {peak:+.2f})")
    return _hard(
        False,
        f"gave back {1 - retention:.0%} of peak PnL (current {cumulative:+.2f}, peak {peak:+.2f}, threshold 35%)",
    )


def check_consecutive_losses(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    if not pnls:
        return _hard(False, "no PnL data in valid trades")

    max_streak = 0
    current_streak = 0
    for p in pnls:
        if p < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    if max_streak >= 8:
        return _hard(False, f"max consecutive losses: {max_streak} (limit 8)")
    return _hard(True, f"max consecutive losses: {max_streak} (<8)")


def check_profit_factor(valid_trades: list[dict]) -> CheckResult:
    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    if not pnls:
        return _hard(False, "no PnL data in valid trades")

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))

    if gross_loss == 0:
        if gross_profit > 0:
            return _hard(True, "profit factor infinite (no losses)")
        return _hard(False, "no wins and no losses — cannot compute PF")

    pf = gross_profit / gross_loss
    if pf >= 1.10:
        return _hard(True, f"profit factor {pf:.2f} (>= 1.10)")
    return _hard(False, f"profit factor {pf:.2f} (need >= 1.10)")


def check_win_rate_vs_replay(valid_trades: list[dict], replay_expectations: dict) -> CheckResult:
    replay_wr = replay_expectations.get("win_rate")
    if replay_wr is None:
        return _advisory(True, "no replay win rate available to compare")

    pnl_field = _pnl_field(valid_trades)
    pnls = [_safe_float(t.get(pnl_field, 0.0)) for t in valid_trades]
    if not pnls:
        return _hard(False, "no PnL data in valid trades")

    live_wr = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
    delta_pp = abs(live_wr - float(replay_wr)) * 100.0
    detail = f"WR {live_wr:.1%} vs replay {float(replay_wr):.1%} (delta {delta_pp:.1f}pp)"
    return _hard(delta_pp <= 15.0, detail)


def check_signal_frequency(log_dir: Path, replay_expectations: dict) -> CheckResult:
    replay_spd = replay_expectations.get("signals_per_day")
    if replay_spd is None:
        return _advisory(True, "no replay signals/day expectation available")

    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return _hard(False, "signals.csv not found")

    with open(sig_file, "r", encoding="utf-8") as f:
        all_signals = list(csv.DictReader(f))
    if len(all_signals) < 2:
        return _hard(False, f"only {len(all_signals)} signals recorded")

    entry_signals = [row for row in all_signals if row.get("action") == "ENTRY"]
    try:
        first_ts = datetime.fromisoformat(str(all_signals[0]["ts"]).replace("Z", "+00:00"))
        last_ts = datetime.fromisoformat(str(all_signals[-1]["ts"]).replace("Z", "+00:00"))
        days = max((last_ts - first_ts).total_seconds() / 86400.0, 1.0)
        live_spd = len(entry_signals) / days
    except Exception as exc:
        return _hard(False, f"could not parse signal timestamps: {exc}")

    replay_spd = float(replay_spd)
    if replay_spd <= 0:
        return _advisory(True, "replay signals/day <= 0, frequency check skipped")

    ratio = live_spd / replay_spd
    detail = f"signals/day {live_spd:.1f} vs replay {replay_spd:.1f} (ratio {ratio:.2f})"
    return _hard(0.50 <= ratio <= 1.50, detail)


def check_survived_disconnect(all_trades: list[dict]) -> CheckResult:
    session_ids = {str(t.get("session_id", "")) for t in all_trades}
    session_ids.discard("")

    reconnect_trades = [
        t
        for t in all_trades
        if "reconnect" in str(t.get("invalid_reason", "") or "").lower()
    ]
    if reconnect_trades:
        reconnect_sessions = {str(t.get("session_id", "")) for t in reconnect_trades}
        reconnect_sessions.discard("")
        post_sessions = session_ids - reconnect_sessions
        if post_sessions:
            return _hard(
                True,
                f"disconnect survived (reconnect in {len(reconnect_sessions)} session(s), clean sessions after: {len(post_sessions)})",
            )
        return _hard(False, "reconnect observed but no clean post-reconnect session")

    if len(session_ids) >= 2:
        return _advisory(True, f"multiple sessions observed ({len(session_ids)}), but no explicit reconnect artifact")

    return _unevidenced("no disconnect/reconnect evidence observed yet")


def check_dashboard_truth(runner: dict, all_trades: list[dict], valid_trades: list[dict]) -> CheckResult:
    fleet = _load_dashboard_fleet()
    if fleet is None:
        return _unevidenced("dashboard API unavailable")

    symbol = str(runner.get("symbol", "") or "").upper()
    entry = fleet.get(symbol)
    if not isinstance(entry, dict):
        return _hard(False, f"dashboard missing runner {symbol}")

    stage = str(entry.get("current_stage", entry.get("lane", "")) or "").lower()
    if stage != STAGE_PAPER:
        return _hard(False, f"dashboard stage={stage or 'unknown'} expected={STAGE_PAPER}")

    dash_valid = int(entry.get("valid_trades", 0) or 0)
    dash_invalid = int(entry.get("invalid_trades", 0) or 0)
    local_invalid = max(len(all_trades) - len(valid_trades), 0)
    if dash_valid != len(valid_trades) or dash_invalid != local_invalid:
        return _hard(
            False,
            (
                "dashboard trade counts diverge "
                f"(dashboard valid/invalid={dash_valid}/{dash_invalid}, "
                f"local={len(valid_trades)}/{local_invalid})"
            ),
        )

    return _hard(
        True,
        f"dashboard matches local truth: stage={stage} valid={dash_valid} invalid={dash_invalid}",
    )


def check_no_manual_intervention(valid_trades: list[dict]) -> CheckResult:
    for trade in valid_trades:
        reason = str(trade.get("invalid_reason", "") or "").lower()
        if "manual" in reason:
            return _hard(False, f"manual intervention detected: {trade.get('invalid_reason')}")
    return _hard(True, "no manual intervention flags found")


def check_walk_forward_positive(log_dir: Path) -> CheckResult:
    report = _load_walkforward_report(log_dir)
    if not report:
        return _unevidenced("walkforward_report.json not found")

    summary = report.get("summary", {})
    status = str(report.get("status", summary.get("status", "UNKNOWN"))).upper()
    folds_scored = int(summary.get("folds_scored", 0) or 0)
    positive_ratio = _safe_float(summary.get("positive_ratio", 0.0))
    mean_expectancy = _safe_float(summary.get("mean_expectancy", 0.0))
    rationale = str(summary.get("rationale", "") or "")

    baseline_ratio = summary.get("expectancy_vs_baseline_ratio")
    baseline_text = ""
    if baseline_ratio is not None:
        baseline_text = f", baseline ratio {float(baseline_ratio):.2f}x"

    if status == "PASS":
        return _hard(
            True,
            f"walk-forward PASS ({positive_ratio:.0%} positive folds, mean exp {mean_expectancy:+.3f}{baseline_text})",
        )
    if status == "INSUFFICIENT_DATA" or folds_scored == 0:
        detail = rationale or "walk-forward has no scored folds yet"
        return _unevidenced(f"walk-forward not ready: {detail}")

    detail = rationale or f"walk-forward {status}"
    return _hard(False, f"walk-forward {status}: {detail} (mean exp {mean_expectancy:+.3f}{baseline_text})")


def check_live_drawdown_vs_walkforward(valid_trades: list[dict], log_dir: Path) -> CheckResult:
    if len(valid_trades) < PROMOTION_THRESHOLD:
        return _unevidenced(f"live drawdown comparison waits for {PROMOTION_THRESHOLD} valid trades")

    report = _load_walkforward_report(log_dir)
    if not report:
        return _unevidenced("walkforward_report.json not found")

    summary = report.get("summary", {})
    folds_scored = int(summary.get("folds_scored", 0) or 0)
    wf_dd = _safe_float(summary.get("max_fold_drawdown", 0.0))
    if folds_scored <= 0:
        return _unevidenced("walk-forward has no scored folds, cannot compare drawdown")

    live_dd = _max_drawdown(valid_trades)
    if wf_dd <= 0:
        return _hard(live_dd <= 0, f"live max DD {live_dd:.2f} vs walk-forward max DD {wf_dd:.2f}")

    allowed_dd = max((wf_dd * LIVE_DRAWDOWN_BUFFER_RATIO), wf_dd + LIVE_DRAWDOWN_BUFFER_ABS)
    detail = (
        f"live max DD {live_dd:.2f} vs walk-forward {wf_dd:.2f} "
        f"(allowed <= {allowed_dd:.2f})"
    )
    return _hard(live_dd <= allowed_dd, detail)


def evaluate_runner(runner: dict) -> dict:
    log_dir = LOGS / runner["log_dir"]
    all_trades = _load_trades(log_dir)
    valid_trades = [t for t in all_trades if str(t.get("experiment_valid", "")).lower() == "true"]
    replay_expectations = _load_replay_expectations(runner["symbol"])

    checks: dict[str, CheckResult] = {
        "min_valid_trades": check_min_valid_trades(valid_trades),
        "min_calendar_days": check_min_calendar_days(runner),
        "frozen_config_hash": check_frozen_config_hash(valid_trades),
        "git_sha_consistent": check_git_sha_consistent(valid_trades),
        "invalid_rate": check_invalid_rate(all_trades),
        "no_runtime_anomalies": check_no_runtime_anomalies(valid_trades),
        "positive_expectancy": check_positive_expectancy(valid_trades),
        "session_concentration": check_session_concentration(valid_trades),
        "outlier_trade": check_outlier_trade(valid_trades),
        "regime_diversity": check_regime_diversity(valid_trades),
        "give_back": check_give_back(valid_trades),
        "consecutive_losses": check_consecutive_losses(valid_trades),
        "profit_factor": check_profit_factor(valid_trades),
        "win_rate_vs_replay": check_win_rate_vs_replay(valid_trades, replay_expectations),
        "signal_frequency": check_signal_frequency(log_dir, replay_expectations),
        "walk_forward_positive": check_walk_forward_positive(log_dir),
        "live_drawdown_vs_walkforward": check_live_drawdown_vs_walkforward(valid_trades, log_dir),
        "survived_disconnect": check_survived_disconnect(all_trades),
        "dashboard_truth": check_dashboard_truth(runner, all_trades, valid_trades),
        "no_manual_intervention": check_no_manual_intervention(valid_trades),
    }

    hard_checks = {name: result for name, result in checks.items() if result.classification == "hard"}
    hard_blockers = [name for name, result in hard_checks.items() if not result.passed]
    evidence_gaps = [
        name
        for name, result in checks.items()
        if result.classification == "unevidenced" and name in REQUIRED_EVIDENCE_CHECKS
    ]
    advisory_items = [name for name, result in checks.items() if result.classification == "advisory"]
    has_min_trades = checks["min_valid_trades"].passed
    has_min_days = checks["min_calendar_days"].passed

    if hard_blockers:
        verdict = "BLOCKED" if has_min_trades and has_min_days else "NOT_READY"
    elif evidence_gaps or not has_min_trades or not has_min_days:
        verdict = "NOT_READY"
    else:
        verdict = "PROMOTE"

    summary = {
        "hard_checks_total": len(hard_checks),
        "hard_checks_passed": sum(1 for result in hard_checks.values() if result.passed),
        "advisory_checks_total": len(advisory_items),
        "required_evidence_ready": len(evidence_gaps) == 0,
    }

    return {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "verdict": verdict,
        "checks": {
            name: {
                "passed": result.passed,
                "detail": result.detail,
                "classification": result.classification,
            }
            for name, result in checks.items()
        },
        "blockers": hard_blockers,
        "evidence_gaps": evidence_gaps,
        "advisories": advisory_items,
        "total_trades": len(all_trades),
        "valid_trades": len(valid_trades),
        "summary": summary,
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    print("=" * 72)
    print(f"  Promotion Gate - {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 72)

    managed_runners = [
        runner
        for runner in discover_managed_runners()
        if runner["current_stage"] == STAGE_PAPER and not runner["live"]
    ]
    results = [evaluate_runner(runner) for runner in managed_runners]
    verdict_counts = {"PROMOTE": 0, "NOT_READY": 0, "BLOCKED": 0}

    for result in results:
        verdict_counts[result["verdict"]] = verdict_counts.get(result["verdict"], 0) + 1
        verdict_colors = {"PROMOTE": "32", "NOT_READY": "36", "BLOCKED": "31"}
        color = verdict_colors.get(result["verdict"], "0")

        print(
            f"\n  {result['name']:>10s}: \033[{color}m{result['verdict']}\033[0m  "
            f"({result['valid_trades']}/{result['total_trades']} valid trades)"
        )
        print(f"  {'-' * 62}")

        for check_name, check_data in result["checks"].items():
            classification = check_data.get("classification", "hard")
            if check_data["passed"]:
                icon = "\033[32mPASS\033[0m"
            elif classification == "unevidenced":
                icon = "\033[36mWAIT\033[0m"
            elif classification == "advisory":
                icon = "\033[33mADV \033[0m"
            else:
                icon = "\033[31mFAIL\033[0m"

            suffix = f" [{classification}]" if classification != "hard" else ""
            print(f"    [{icon}] {check_name}{suffix}")
            print(f"           {check_data['detail']}")

        if result["blockers"]:
            print(f"    Hard blockers: {', '.join(result['blockers'])}")
        if result["evidence_gaps"]:
            print(f"    Evidence gaps: {', '.join(result['evidence_gaps'])}")

    report = {
        "timestamp": now.isoformat(),
        "status": "OK",
        "summary": {
            "promote": verdict_counts.get("PROMOTE", 0),
            "not_ready": verdict_counts.get("NOT_READY", 0),
            "blocked": verdict_counts.get("BLOCKED", 0),
        },
        "runners": results,
    }

    out_path = LOGS / "promotion_gate_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
