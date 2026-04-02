"""Demotion checker, quarantine resolver, and risk sizing progression.

Checks PROD (real) runners for demotion triggers, resolves quarantined
runners, and recommends risk sizing based on trade milestones.

Usage:
    python -m argus_flow.ops.demotion_check
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import (
    STAGE_REAL,
    discover_managed_runners,
)

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
DEPLOYMENT_REGISTRY_FILE = LOGS / "deployment_registry.json"
DEMOTION_REPORT_FILE = LOGS / "demotion_report.json"

# Stage labels used in this module
STAGE_PROD = STAGE_REAL
STAGE_QUARANTINE = "quarantine"

# Demotion thresholds
EXPECTANCY_DEGRADATION_PCT = 0.20  # 20% worse than full cohort
CONSECUTIVE_NEG_WEEKS_TRIGGER = 3
MIN_TRADES_PER_WEEK = 10
DRAWDOWN_QUARANTINE_MULT = 2.0  # 2x model DD -> QUARANTINE
DRAWDOWN_KILL_MULT = 3.0  # 3x model DD -> KILL
SINGLE_DAY_LOSS_PCT = 0.05  # 5% of model equity
MODEL_EQUITY_DEFAULT = 10_000.0

# No-progress kill: if a pair is break-even after this many trades, prune it
NO_PROGRESS_TRADE_THRESHOLD = 80  # trades in paper/real before checking
NO_PROGRESS_MIN_PF = 1.05  # must show at least marginal edge
NO_PROGRESS_MAX_RESIDENCY_DAYS = 90  # or this many days in stage

# Hard strategy kill clock: if PF < this after threshold trades, strategy is dead
STRATEGY_KILL_PF_THRESHOLD = 1.0  # PF < 1.0 after 60 trades = net negative
STRATEGY_KILL_TRADE_THRESHOLD = 60

# Quarantine resolver thresholds
QUARANTINE_MIN_TRADES = 10
QUARANTINE_MIN_PF = 1.10
QUARANTINE_MAX_CALENDAR_DAYS = 14

# Risk sizing milestones
RISK_TIERS = [
    # (min_trades, min_pf, max_dd_mult, risk_pct)
    (300, 1.35, 2.0, 0.030),
    (200, 1.30, 2.0, 0.020),
    (100, 1.30, None, 0.015),
    (60, 1.20, None, 0.010),
    (30, 1.20, None, 0.0075),
]
RISK_BASE = 0.005  # 0-29 trades
RISK_QUARANTINE = 0.0025  # quarantined runners always 0.25%


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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


def _valid_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if str(t.get("experiment_valid", "")).lower() == "true"]


def _model_drawdown(config_path: Path) -> float:
    """Model DD = stop_pips * 3 (or stop_bps * 3). Fallback 10."""
    cfg = _load_json(config_path)
    if not isinstance(cfg, dict):
        return 10.0
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
    stop_pips = _safe_float(risk.get("stop_pips", 0))
    if stop_pips > 0:
        return stop_pips * 3
    stop_bps = _safe_float(risk.get("stop_bps", 0))
    if stop_bps > 0:
        return stop_bps * 3
    return 10.0


def _model_equity(config_path: Path) -> float:
    """Return model start equity from config, or default."""
    cfg = _load_json(config_path)
    if not isinstance(cfg, dict):
        return MODEL_EQUITY_DEFAULT
    deployment = cfg.get("deployment", {}) if isinstance(cfg.get("deployment"), dict) else {}
    rp = deployment.get("risk_policy", {}) if isinstance(deployment.get("risk_policy"), dict) else {}
    return _safe_float(rp.get("model_start_equity_usd", MODEL_EQUITY_DEFAULT)) or MODEL_EQUITY_DEFAULT


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 4)


def _profit_factor(pnls: list[float]) -> float:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss < 1e-9:
        return float("inf") if gross_win > 0 else 0.0
    return round(gross_win / gross_loss, 4)


# ---------------------------------------------------------------------------
# 1. Demotion checks for PROD runners
# ---------------------------------------------------------------------------

def _check_expectancy_degradation(pnls: list[float]) -> dict:
    """Last-20 expectancy vs full-cohort. 20%+ worse = QUARANTINE."""
    if len(pnls) < 20:
        return {"triggered": False, "detail": f"only {len(pnls)} trades, need 20 for window"}

    full_exp = sum(pnls) / len(pnls) if pnls else 0.0
    last_20_exp = sum(pnls[-20:]) / 20.0

    if full_exp <= 0:
        # If full-cohort is already negative, degradation check not meaningful
        return {"triggered": False, "detail": f"full expectancy {full_exp:+.4f} already non-positive"}

    degradation = (full_exp - last_20_exp) / abs(full_exp)
    triggered = degradation >= EXPECTANCY_DEGRADATION_PCT
    return {
        "triggered": triggered,
        "detail": (
            f"last-20 exp {last_20_exp:+.4f} vs full {full_exp:+.4f} "
            f"({degradation:+.0%} degradation, threshold {EXPECTANCY_DEGRADATION_PCT:.0%})"
        ),
        "action": "QUARANTINE" if triggered else "PASS",
    }


def _check_consecutive_negative_weeks(trades: list[dict], pnl_field: str) -> dict:
    """3 consecutive weeks with negative PnL and >= 10 trades/week."""
    weekly: dict[str, dict] = {}
    for t in trades:
        try:
            ts = datetime.fromisoformat(str(t["ts"]).replace("Z", "+00:00"))
            week_key = ts.strftime("%Y-W%W")
            weekly.setdefault(week_key, {"pnl": 0.0, "count": 0})
            weekly[week_key]["pnl"] += _safe_float(t.get(pnl_field, 0))
            weekly[week_key]["count"] += 1
        except Exception:
            pass

    if not weekly:
        return {"triggered": False, "detail": "no weekly data"}

    sorted_weeks = sorted(weekly.keys())
    consec_neg = 0
    max_consec_neg = 0
    for week in sorted_weeks:
        w = weekly[week]
        if w["pnl"] < 0 and w["count"] >= MIN_TRADES_PER_WEEK:
            consec_neg += 1
            max_consec_neg = max(max_consec_neg, consec_neg)
        else:
            consec_neg = 0

    triggered = max_consec_neg >= CONSECUTIVE_NEG_WEEKS_TRIGGER
    return {
        "triggered": triggered,
        "detail": (
            f"max consecutive negative weeks: {max_consec_neg} "
            f"(threshold {CONSECUTIVE_NEG_WEEKS_TRIGGER}, min {MIN_TRADES_PER_WEEK} trades/week)"
        ),
        "action": "QUARANTINE" if triggered else "PASS",
    }


def _check_drawdown(pnls: list[float], model_dd: float) -> dict:
    """2x model DD = QUARANTINE, 3x = KILL."""
    max_dd = _max_drawdown(pnls)
    if max_dd > model_dd * DRAWDOWN_KILL_MULT:
        return {
            "triggered": True,
            "detail": f"max DD {max_dd:.2f} > {DRAWDOWN_KILL_MULT}x model ({model_dd:.2f})",
            "action": "KILL",
        }
    if max_dd > model_dd * DRAWDOWN_QUARANTINE_MULT:
        return {
            "triggered": True,
            "detail": f"max DD {max_dd:.2f} > {DRAWDOWN_QUARANTINE_MULT}x model ({model_dd:.2f})",
            "action": "QUARANTINE",
        }
    return {
        "triggered": False,
        "detail": f"max DD {max_dd:.2f} within {DRAWDOWN_QUARANTINE_MULT}x model ({model_dd:.2f})",
        "action": "PASS",
    }


def _check_single_day_loss(trades: list[dict], pnl_field: str, model_equity: float) -> dict:
    """Any single day loss > 5% of model equity = QUARANTINE."""
    daily_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        try:
            ts = datetime.fromisoformat(str(t["ts"]).replace("Z", "+00:00"))
            day_key = ts.strftime("%Y-%m-%d")
            daily_pnl[day_key] += _safe_float(t.get(pnl_field, 0))
        except Exception:
            pass

    if not daily_pnl:
        return {"triggered": False, "detail": "no daily data"}

    threshold = model_equity * SINGLE_DAY_LOSS_PCT
    worst_day = min(daily_pnl.values())
    worst_day_key = min(daily_pnl, key=daily_pnl.get)  # type: ignore[arg-type]

    triggered = worst_day < -threshold
    return {
        "triggered": triggered,
        "detail": (
            f"worst day: {worst_day:+.2f} on {worst_day_key} "
            f"(threshold -{threshold:.2f} = {SINGLE_DAY_LOSS_PCT:.0%} of ${model_equity:,.0f})"
        ),
        "action": "QUARANTINE" if triggered else "PASS",
    }


def _check_no_progress(pnls: list[float], runner: dict) -> dict:
    """Kill pairs that show no meaningful edge after sufficient sample.

    Break-even after 80 trades or 90 days = not worth more runway.
    Prevents mediocrity from consuming resources indefinitely.
    """
    n = len(pnls)
    if n < NO_PROGRESS_TRADE_THRESHOLD:
        return {"triggered": False, "detail": f"only {n}/{NO_PROGRESS_TRADE_THRESHOLD} trades, too early"}

    pf = _profit_factor(pnls)
    triggered = pf < NO_PROGRESS_MIN_PF
    return {
        "triggered": triggered,
        "detail": f"PF={pf:.3f} after {n} trades (min={NO_PROGRESS_MIN_PF})",
        "action": "KILL" if triggered else "PASS",
    }


def _check_strategy_kill_clock(pnls: list[float]) -> dict:
    """Hard strategy kill: PF < 1.0 after 60 trades = strategy is net negative.

    Not 'demote and try again' — the hypothesis is dead.
    """
    n = len(pnls)
    if n < STRATEGY_KILL_TRADE_THRESHOLD:
        return {"triggered": False, "detail": f"only {n}/{STRATEGY_KILL_TRADE_THRESHOLD} trades"}

    pf = _profit_factor(pnls)
    triggered = pf < STRATEGY_KILL_PF_THRESHOLD
    return {
        "triggered": triggered,
        "detail": f"PF={pf:.3f} after {n} trades — {'STRATEGY DEAD' if triggered else 'alive'}",
        "action": "KILL" if triggered else "PASS",
    }


def check_demotion(runner: dict) -> dict:
    """Run all demotion checks for a PROD runner. Returns verdict dict."""
    log_dir = REPO / runner["log_dir"]
    config_path = REPO / runner.get("config_path", "")
    all_trades = _load_trades(log_dir)
    valid = _valid_trades(all_trades)

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "stage": STAGE_PROD,
        "verdict": "PASS",
        "reasons": [],
        "checks": {},
        "metrics": {"valid_trades": len(valid), "total_trades": len(all_trades)},
    }

    if not valid:
        result["verdict"] = "COLLECTING"
        result["reasons"].append("no valid trades yet")
        return result

    pnl_f = _pnl_field(valid)
    pnls = [_safe_float(t.get(pnl_f, 0)) for t in valid]
    model_dd = _model_drawdown(config_path)
    model_eq = _model_equity(config_path)

    result["metrics"]["total_pnl"] = round(sum(pnls), 4)
    result["metrics"]["expectancy"] = round(sum(pnls) / len(pnls), 4) if pnls else 0.0
    result["metrics"]["profit_factor"] = _profit_factor(pnls)
    result["metrics"]["max_drawdown"] = _max_drawdown(pnls)
    result["metrics"]["model_drawdown"] = model_dd
    result["metrics"]["model_equity"] = model_eq

    checks = {
        "expectancy_degradation": _check_expectancy_degradation(pnls),
        "consecutive_negative_weeks": _check_consecutive_negative_weeks(valid, pnl_f),
        "drawdown_2x_3x": _check_drawdown(pnls, model_dd),
        "single_day_loss": _check_single_day_loss(valid, pnl_f, model_eq),
        "no_progress": _check_no_progress(pnls, runner),
        "strategy_kill_clock": _check_strategy_kill_clock(pnls),
    }
    result["checks"] = checks

    # Determine worst action across all triggered checks
    worst = "PASS"
    action_rank = {"PASS": 0, "QUARANTINE": 1, "KILL": 2}
    for name, check in checks.items():
        if check.get("triggered"):
            action = check.get("action", "QUARANTINE")
            result["reasons"].append(f"{name}: {check['detail']}")
            if action_rank.get(action, 0) > action_rank.get(worst, 0):
                worst = action

    result["verdict"] = worst
    return result


# ---------------------------------------------------------------------------
# 2. Quarantine resolver
# ---------------------------------------------------------------------------

def resolve_quarantine(runner: dict) -> dict:
    """Evaluate a QUARANTINED runner for reinstatement, demotion, or kill."""
    log_dir = REPO / runner["log_dir"]
    config_path = REPO / runner.get("config_path", "")
    all_trades = _load_trades(log_dir)
    valid = _valid_trades(all_trades)

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "stage": STAGE_QUARANTINE,
        "verdict": "HOLDING",
        "reasons": [],
        "metrics": {"valid_trades": len(valid), "total_trades": len(all_trades)},
    }

    # Determine quarantine start
    quarantine_start_ts = runner.get("quarantine_start_ts", "")
    if not quarantine_start_ts:
        result["reasons"].append("no quarantine_start_ts recorded, using earliest trade ts")
        if valid:
            quarantine_start_ts = valid[0].get("ts", "")

    now = datetime.now(timezone.utc)
    calendar_days = 0
    if quarantine_start_ts:
        try:
            start = datetime.fromisoformat(str(quarantine_start_ts).replace("Z", "+00:00"))
            calendar_days = (now - start).days
        except Exception:
            pass

    result["metrics"]["calendar_days_in_quarantine"] = calendar_days

    # Count trades since quarantine start
    trades_since = []
    if quarantine_start_ts:
        for t in valid:
            try:
                ts = datetime.fromisoformat(str(t["ts"]).replace("Z", "+00:00"))
                start = datetime.fromisoformat(str(quarantine_start_ts).replace("Z", "+00:00"))
                if ts >= start:
                    trades_since.append(t)
            except Exception:
                pass
    else:
        trades_since = list(valid)

    pnl_f = _pnl_field(trades_since) if trades_since else "pnl_pips"
    pnls = [_safe_float(t.get(pnl_f, 0)) for t in trades_since]
    n = len(pnls)

    result["metrics"]["trades_since_quarantine"] = n
    result["metrics"]["pnl_since_quarantine"] = round(sum(pnls), 4) if pnls else 0.0

    # 14-day kill switch (check first, overrides other verdicts)
    if calendar_days >= QUARANTINE_MAX_CALENDAR_DAYS:
        result["verdict"] = "KILL"
        result["reasons"].append(f"{calendar_days} calendar days >= {QUARANTINE_MAX_CALENDAR_DAYS} day limit")
        return result

    if n < QUARANTINE_MIN_TRADES:
        result["verdict"] = "HOLDING"
        result["reasons"].append(f"{n} trades since quarantine, need {QUARANTINE_MIN_TRADES}")
        return result

    # Enough trades to evaluate
    pf = _profit_factor(pnls)
    exp = sum(pnls) / n if n > 0 else 0.0
    model_dd = _model_drawdown(config_path)
    max_dd = _max_drawdown(pnls)
    dd_recovered = max_dd <= model_dd * DRAWDOWN_QUARANTINE_MULT

    result["metrics"]["profit_factor_since_quarantine"] = pf
    result["metrics"]["expectancy_since_quarantine"] = round(exp, 4)
    result["metrics"]["max_dd_since_quarantine"] = max_dd
    result["metrics"]["dd_recovered"] = dd_recovered

    if pf >= QUARANTINE_MIN_PF and exp > 0 and dd_recovered:
        result["verdict"] = "REINSTATE"
        result["reasons"].append(
            f"PF {pf:.2f} >= {QUARANTINE_MIN_PF}, exp {exp:+.4f} > 0, DD recovered"
        )
    else:
        result["verdict"] = "DEMOTE_TO_QA"
        reasons = []
        if pf < QUARANTINE_MIN_PF:
            reasons.append(f"PF {pf:.2f} < {QUARANTINE_MIN_PF}")
        if exp <= 0:
            reasons.append(f"exp {exp:+.4f} <= 0")
        if not dd_recovered:
            reasons.append(f"DD {max_dd:.2f} not recovered (model {model_dd:.2f})")
        result["reasons"].append(f"still negative after {n} trades: {'; '.join(reasons)}")

    return result


# ---------------------------------------------------------------------------
# 3. Risk sizing progression
# ---------------------------------------------------------------------------

def recommend_risk_pct(
    n_trades: int,
    pf: float,
    max_dd: float,
    model_dd: float,
    is_quarantined: bool = False,
) -> float:
    """Return recommended risk_pct based on milestones."""
    if is_quarantined:
        return RISK_QUARANTINE

    for min_trades, min_pf, max_dd_mult, risk_pct in RISK_TIERS:
        if n_trades < min_trades:
            continue
        if pf < min_pf:
            continue
        if max_dd_mult is not None and max_dd > model_dd * max_dd_mult:
            continue
        return risk_pct

    return RISK_BASE


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def _load_deployment_registry(registry_path: Path | None = None) -> dict:
    path = registry_path or DEPLOYMENT_REGISTRY_FILE
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def run(registry_path: str | Path | None = None) -> dict:
    """Run demotion checks, quarantine resolution, and risk sizing.

    Returns the full report dict and writes demotion_report.json.
    """
    now = datetime.now(timezone.utc)
    reg_path = Path(registry_path) if registry_path else None
    registry = _load_deployment_registry(reg_path)
    runners = registry.get("runners", [])

    prod_results: list[dict] = []
    quarantine_results: list[dict] = []

    for runner in runners:
        if not isinstance(runner, dict):
            continue
        stage = str(runner.get("current_stage", "")).lower()

        if stage == STAGE_PROD:
            # Demotion check
            result = check_demotion(runner)

            # Risk sizing
            m = result.get("metrics", {})
            pnls_count = m.get("valid_trades", 0)
            pf = m.get("profit_factor", 0.0)
            max_dd = m.get("max_drawdown", 0.0)
            model_dd = m.get("model_drawdown", 10.0)
            result["recommended_risk_pct"] = recommend_risk_pct(
                pnls_count, pf, max_dd, model_dd, is_quarantined=False,
            )
            prod_results.append(result)

        elif stage == STAGE_QUARANTINE:
            # Quarantine resolution
            result = resolve_quarantine(runner)

            # Quarantined runners always get minimum risk
            result["recommended_risk_pct"] = RISK_QUARANTINE
            quarantine_results.append(result)

    # If no registry runners found, fall back to discovering PROD runners
    if not runners:
        managed = discover_managed_runners()
        for runner in managed:
            if runner["current_stage"] == STAGE_REAL:
                result = check_demotion(runner)
                m = result.get("metrics", {})
                result["recommended_risk_pct"] = recommend_risk_pct(
                    m.get("valid_trades", 0),
                    m.get("profit_factor", 0.0),
                    m.get("max_drawdown", 0.0),
                    m.get("model_drawdown", 10.0),
                    is_quarantined=False,
                )
                prod_results.append(result)

    all_results = prod_results + quarantine_results

    # Summarize
    verdict_counts: dict[str, int] = defaultdict(int)
    for r in all_results:
        verdict_counts[r["verdict"]] += 1

    report = {
        "timestamp": now.isoformat(),
        "summary": {
            "prod_checked": len(prod_results),
            "quarantine_checked": len(quarantine_results),
            "verdicts": dict(verdict_counts),
        },
        "prod_runners": prod_results,
        "quarantine_runners": quarantine_results,
    }

    DEMOTION_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEMOTION_REPORT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    report = run()
    now_str = report.get("timestamp", "")[:16]
    summary = report.get("summary", {})

    print("=" * 65)
    print(f"  Demotion Check - {now_str} UTC")
    print("=" * 65)
    print(
        f"  PROD checked: {summary.get('prod_checked', 0)}  "
        f"QUARANTINE checked: {summary.get('quarantine_checked', 0)}"
    )
    verdicts = summary.get("verdicts", {})
    if verdicts:
        parts = [f"{k}={v}" for k, v in sorted(verdicts.items())]
        print(f"  Verdicts: {', '.join(parts)}")

    status_colors = {"PASS": "32", "QUARANTINE": "33", "KILL": "31", "COLLECTING": "36",
                     "HOLDING": "36", "REINSTATE": "32", "DEMOTE_TO_QA": "33"}

    for section, label in [("prod_runners", "PROD"), ("quarantine_runners", "QUARANTINE")]:
        for r in report.get(section, []):
            color = status_colors.get(r["verdict"], "0")
            risk_str = f"{r.get('recommended_risk_pct', 0) * 100:.2f}%"
            print(
                f"\n  [{label}] {r['name']:>10s}: "
                f"\033[{color}m{r['verdict']}\033[0m  "
                f"(risk: {risk_str})"
            )
            m = r.get("metrics", {})
            if m.get("valid_trades"):
                print(
                    f"    Trades: {m['valid_trades']} | "
                    f"PnL: {m.get('total_pnl', 0):+.2f} | "
                    f"PF: {m.get('profit_factor', 0):.2f} | "
                    f"MaxDD: {m.get('max_drawdown', 0):.2f}"
                )
            for reason in r.get("reasons", []):
                fc = "31" if r["verdict"] in ("KILL", "QUARANTINE") else "33"
                print(f"    \033[{fc}m! {reason}\033[0m")

    print(f"\n  Saved: {DEMOTION_REPORT_FILE}")


if __name__ == "__main__":
    main()
