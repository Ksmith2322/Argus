"""Phase 22B — Risk Oversight Agent.
Cross-strategy portfolio risk monitor. Sits above all runners.

Usage:
    python -m argus_flow.ops.risk_oversight
    python -m argus_flow.ops.risk_oversight --watch
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.broker_truth import load_runner_broker_state

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"

# ---------------------------------------------------------------------------
# Fleet definition — Class A active cohort
# ---------------------------------------------------------------------------
RUNNERS = [
    {"name": "EUR/USD", "symbol": "eurusd", "log_dir": LOGS / "eurusd"},
    {"name": "GBP/USD", "symbol": "gbpusd", "log_dir": LOGS / "gbpusd"},
    {"name": "EUR/JPY", "symbol": "eurjpy", "log_dir": LOGS / "eurjpy"},
]

# Correlation groups (mirrors correlation_guard.py)
CORRELATION_GROUPS = {
    "USD": ["eurusd", "gbpusd"],
    "JPY": ["eurjpy", "gbpjpy", "audjpy", "cadjpy", "usdjpy"],
}

# ---------------------------------------------------------------------------
# Risk thresholds
# ---------------------------------------------------------------------------
MAX_SIMULTANEOUS_POSITIONS = 3
MAX_CORRELATED_POSITIONS = 1  # per group
FLEET_DRAWDOWN_PAUSE_PIPS = 50.0
INVALID_TRADE_RATE_THRESHOLD = 0.15  # 15%
BROKER_STATE_FRESH_S = 600
MAX_TOTAL_OPEN_RISK_PCT = 0.05


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_state(log_dir: Path) -> dict | None:
    """Read state.json for a pair."""
    return _load_json(log_dir / "state.json")


def _read_runtime_truth(runner: dict) -> dict:
    """Return the best available runtime truth for a runner."""
    broker_state = load_runner_broker_state(runner["log_dir"], max_age_s=BROKER_STATE_FRESH_S)
    if broker_state:
        broker = broker_state.get("broker", {})
        runner_state = broker_state.get("runner", {})
        account = broker_state.get("account", {})
        return {
            "status": broker.get("position", "FLAT"),
            "source": "broker_state",
            "open_risk_usd": float(runner_state.get("open_risk_usd", 0.0) or 0.0),
            "unrealized_pnl_usd": float(runner_state.get("unrealized_pnl_usd", 0.0) or 0.0),
            "account_equity_usd": float(account.get("net_liquidation_usd", 0.0) or 0.0),
            "reconciliation": broker_state.get("reconciliation", {}).get("result", ""),
            "broker_connected": bool(broker_state.get("broker_connected", False)),
        }

    state = _read_state(runner["log_dir"])
    if state is None:
        return {
            "status": "UNKNOWN",
            "source": "missing",
            "open_risk_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "account_equity_usd": 0.0,
            "reconciliation": "",
            "broker_connected": False,
        }

    return {
        "status": str(state.get("position", state.get("status", "FLAT"))).upper(),
        "source": "state",
        "open_risk_usd": float(state.get("entry_risk_usd", 0.0) or 0.0),
        "unrealized_pnl_usd": float(state.get("pnl_usd", 0.0) or 0.0),
        "account_equity_usd": 0.0,
        "reconciliation": "",
        "broker_connected": False,
    }


def _read_positions() -> tuple[list[dict], dict]:
    """Return list of pairs with open positions plus fleet broker-truth stats."""
    open_positions = []
    fleet_open_risk_usd = 0.0
    fleet_unrealized_pnl_usd = 0.0
    account_equity_usd = 0.0
    truth_sources = set()
    for r in RUNNERS:
        truth = _read_runtime_truth(r)
        status = str(truth.get("status", "FLAT")).upper()
        truth_sources.add(truth.get("source", "unknown"))
        if truth.get("account_equity_usd", 0.0) > 0:
            account_equity_usd = max(account_equity_usd, truth["account_equity_usd"])
        if status not in ("FLAT", "NONE", ""):
            fleet_open_risk_usd += float(truth.get("open_risk_usd", 0.0) or 0.0)
            fleet_unrealized_pnl_usd += float(truth.get("unrealized_pnl_usd", 0.0) or 0.0)
            open_positions.append({
                "pair": r["name"],
                "symbol": r["symbol"],
                "status": status,
                "source": truth.get("source", "unknown"),
                "reconciliation": truth.get("reconciliation", ""),
            })
    meta = {
        "fleet_open_risk_usd": round(fleet_open_risk_usd, 2),
        "fleet_unrealized_pnl_usd": round(fleet_unrealized_pnl_usd, 2),
        "account_equity_usd": round(account_equity_usd, 2),
        "truth_sources": sorted(truth_sources),
    }
    return open_positions, meta


# ---------------------------------------------------------------------------
# Risk checks
# ---------------------------------------------------------------------------

def _check_correlation_exposure(open_positions: list[dict]) -> dict:
    """Check if correlated positions exceed limits."""
    open_symbols = {p["symbol"] for p in open_positions}
    alerts = []
    group_counts = {}

    for group_name, members in CORRELATION_GROUPS.items():
        active = [s for s in members if s in open_symbols]
        group_counts[group_name] = len(active)
        if len(active) > MAX_CORRELATED_POSITIONS:
            alerts.append(
                f"{group_name}: {len(active)} correlated positions "
                f"({', '.join(active)}) exceeds limit of {MAX_CORRELATED_POSITIONS}"
            )

    return {"groups": group_counts, "alerts": alerts}


def _check_fleet_drawdown() -> float:
    """Sum drawdown across all pairs from their state files."""
    total_dd = 0.0
    for r in RUNNERS:
        state = _read_state(r["log_dir"])
        if state is None:
            continue
        dd = state.get("drawdown_pips", 0.0)
        if isinstance(dd, (int, float)):
            total_dd += abs(dd)
    return total_dd


def _check_kill_discipline() -> list[dict]:
    """Read kill_discipline_report.json for KILL-status pairs."""
    report = _load_json(LOGS / "kill_discipline_report.json")
    if not report:
        return []
    kills = []
    for pair_report in report.get("pairs", []):
        if pair_report.get("verdict") == "KILL":
            kills.append(pair_report)
    return kills


def _check_divergence() -> dict | None:
    """Read divergence_report.json."""
    return _load_json(LOGS / "divergence_report.json")


def _check_artifact_divergence() -> dict | None:
    """Read artifact_divergence_report.json."""
    return _load_json(LOGS / "artifact_divergence_report.json")


def _check_position_monitor() -> dict | None:
    """Read position_monitor.json for broker reconciliation alerts."""
    return _load_json(LOGS / "position_monitor.json")


def _check_invalid_trade_rate() -> float:
    """Combined invalid trade rate across all pairs from daily report."""
    report = None
    # Find latest daily report
    for f in sorted(LOGS.glob("daily_report_*.json"), reverse=True):
        report = _load_json(f)
        if report:
            break

    if not report:
        return 0.0

    total_trades = 0
    total_invalid = 0
    for pair_data in report.get("pairs", []):
        total_trades += pair_data.get("total_trades", 0)
        total_invalid += pair_data.get("invalid_trades", 0)

    if total_trades == 0:
        return 0.0
    return total_invalid / total_trades


# ---------------------------------------------------------------------------
# Main assessment
# ---------------------------------------------------------------------------

def assess_risk() -> dict:
    """Run all risk checks and produce a consolidated report."""
    now = datetime.now(timezone.utc).isoformat()

    # Positions
    open_positions, broker_meta = _read_positions()
    position_count = len(open_positions)

    # Correlation
    corr = _check_correlation_exposure(open_positions)

    # Fleet drawdown
    fleet_dd = _check_fleet_drawdown()

    # Kill discipline
    kills = _check_kill_discipline()

    # Divergence
    divergence = _check_divergence()
    artifact_div = _check_artifact_divergence()
    position_monitor = _check_position_monitor()

    # Invalid trade rate
    invalid_rate = _check_invalid_trade_rate()

    # Per-pair status
    per_pair = {}
    for r in RUNNERS:
        truth = _read_runtime_truth(r)
        status = str(truth.get("status", "FLAT")).upper()
        per_pair[r["symbol"]] = {
            "name": r["name"],
            "status": status,
            "source": truth.get("source", "unknown"),
            "has_state": _read_state(r["log_dir"]) is not None,
            "reconciliation": truth.get("reconciliation", ""),
        }

    # ---------------------------------------------------------------------------
    # Build recommendations and determine risk level
    # ---------------------------------------------------------------------------
    recommendations = []
    risk_level = "GREEN"

    # Position limit
    if position_count > MAX_SIMULTANEOUS_POSITIONS:
        recommendations.append(
            f"REDUCE_POSITIONS: {position_count} open (max {MAX_SIMULTANEOUS_POSITIONS})"
        )
        risk_level = "RED"

    # Correlation alerts
    if corr["alerts"]:
        for alert in corr["alerts"]:
            recommendations.append(f"CORRELATED_EXPOSURE: {alert}")
        if risk_level != "RED":
            risk_level = "YELLOW"

    # Fleet drawdown
    if fleet_dd >= FLEET_DRAWDOWN_PAUSE_PIPS:
        recommendations.append(
            f"PAUSE_ALL: fleet drawdown {fleet_dd:.1f} pips >= {FLEET_DRAWDOWN_PAUSE_PIPS} pip threshold"
        )
        risk_level = "RED"
    elif fleet_dd >= FLEET_DRAWDOWN_PAUSE_PIPS * 0.7:
        if risk_level == "GREEN":
            risk_level = "YELLOW"

    # Kill discipline
    for k in kills:
        recommendations.append(f"KILL_PAIR: {k.get('name', k.get('symbol', '?'))}")
        risk_level = "RED"

    # Divergence
    if divergence:
        div_status = divergence.get("status", "")
        if div_status == "DIVERGENT":
            recommendations.append("INVESTIGATE: divergence_report shows DIVERGENT status")
            if risk_level != "RED":
                risk_level = "YELLOW"

    if artifact_div:
        art_status = artifact_div.get("status", "")
        if art_status == "DIVERGENT":
            recommendations.append("INVESTIGATE: artifact_divergence_report shows DIVERGENT status")
            if risk_level != "RED":
                risk_level = "YELLOW"

    # Invalid trade rate
    if invalid_rate > INVALID_TRADE_RATE_THRESHOLD:
        recommendations.append(
            f"PAUSE_COHORT: combined invalid trade rate {invalid_rate:.1%} > {INVALID_TRADE_RATE_THRESHOLD:.0%}"
        )
        risk_level = "RED"

    account_equity_usd = broker_meta.get("account_equity_usd", 0.0)
    fleet_open_risk_usd = broker_meta.get("fleet_open_risk_usd", 0.0)
    open_risk_pct = (fleet_open_risk_usd / account_equity_usd) if account_equity_usd > 0 else 0.0
    if account_equity_usd > 0 and open_risk_pct > MAX_TOTAL_OPEN_RISK_PCT:
        recommendations.append(
            f"REDUCE_RISK: open risk {open_risk_pct:.1%} > {MAX_TOTAL_OPEN_RISK_PCT:.0%} budget"
        )
        risk_level = "RED"
    elif account_equity_usd > 0 and open_risk_pct > MAX_TOTAL_OPEN_RISK_PCT * 0.8 and risk_level == "GREEN":
        risk_level = "YELLOW"

    if position_monitor:
        alerts = position_monitor.get("alerts", [])
        if position_monitor.get("has_critical"):
            recommendations.append("BROKER_RECONCILIATION: position_monitor reports CRITICAL mismatch")
            risk_level = "RED"
        elif alerts and risk_level == "GREEN":
            recommendations.append("BROKER_RECONCILIATION: position_monitor has active alerts")
            risk_level = "YELLOW"

    report = {
        "timestamp": now,
        "level": risk_level,
        "status": risk_level,
        "metric": "fleet_risk",
        "message": "; ".join(recommendations[:3]) if recommendations else "no action required",
        "fleet_positions": {
            "count": position_count,
            "list": [p["pair"] for p in open_positions],
        },
        "broker_truth": {
            "account_equity_usd": account_equity_usd,
            "fleet_open_risk_usd": fleet_open_risk_usd,
            "fleet_open_risk_pct": round(open_risk_pct, 4),
            "fleet_unrealized_pnl_usd": broker_meta.get("fleet_unrealized_pnl_usd", 0.0),
            "sources": broker_meta.get("truth_sources", []),
        },
        "correlation_exposure": corr,
        "fleet_drawdown_pips": round(fleet_dd, 2),
        "invalid_trade_rate": round(invalid_rate, 4),
        "risk_level": risk_level,
        "recommendations": recommendations,
        "per_pair_status": per_pair,
    }

    return report


def print_report(report: dict) -> None:
    """Print a human-readable summary."""
    rl = report["risk_level"]
    color = {"GREEN": "\033[92m", "YELLOW": "\033[93m", "RED": "\033[91m"}.get(rl, "")
    reset = "\033[0m"

    print(f"\n{'='*60}")
    print(f"  RISK OVERSIGHT REPORT  —  {report['timestamp']}")
    print(f"{'='*60}")
    print(f"  Risk Level:       {color}{rl}{reset}")
    print(f"  Open Positions:   {report['fleet_positions']['count']}/{MAX_SIMULTANEOUS_POSITIONS}")
    if report["fleet_positions"]["list"]:
        print(f"                    {', '.join(report['fleet_positions']['list'])}")
    print(f"  Fleet Drawdown:   {report['fleet_drawdown_pips']:.1f} pips")
    print(f"  Invalid Rate:     {report['invalid_trade_rate']:.1%}")
    broker_truth = report.get("broker_truth", {})
    if broker_truth.get("account_equity_usd", 0) > 0:
        print(f"  Open Risk:        ${broker_truth['fleet_open_risk_usd']:.2f} ({broker_truth['fleet_open_risk_pct']:.1%})")
        print(f"  Equity:           ${broker_truth['account_equity_usd']:.2f}")
    if broker_truth.get("sources"):
        print(f"  Truth Source:     {', '.join(broker_truth['sources'])}")

    # Correlation
    corr = report["correlation_exposure"]
    if corr["groups"]:
        print(f"\n  Correlation Groups:")
        for group, count in corr["groups"].items():
            marker = " !" if count > MAX_CORRELATED_POSITIONS else ""
            print(f"    {group}: {count} active{marker}")

    # Per-pair
    print(f"\n  Per-Pair Status:")
    for sym, info in report["per_pair_status"].items():
        print(f"    {info['name']:10s}  {info['status']}")

    # Recommendations
    if report["recommendations"]:
        print(f"\n  Recommendations:")
        for rec in report["recommendations"]:
            print(f"    -> {rec}")
    else:
        print(f"\n  No action required.")

    print(f"{'='*60}\n")


def save_report(report: dict) -> Path:
    """Save report to logs directory."""
    out = LOGS / "risk_oversight_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return out


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def watch_loop(interval: int = 60) -> None:
    """Continuous monitoring loop. Alerts on RED transitions."""
    prev_level = None
    print(f"Risk oversight watch mode — checking every {interval}s. Ctrl+C to stop.")

    while True:
        try:
            report = assess_risk()
            print_report(report)
            save_report(report)

            # Alert on transition to RED
            if report["risk_level"] == "RED" and prev_level != "RED":
                try:
                    sys.path.insert(0, str(REPO))
                    from argus_flow.ops.discord_alerts import send_discord
                    recs = "\n".join(f"- {r}" for r in report["recommendations"])
                    msg = (
                        f"**RISK OVERSIGHT: RED ALERT**\n"
                        f"Fleet drawdown: {report['fleet_drawdown_pips']:.1f} pips\n"
                        f"Positions: {report['fleet_positions']['count']}\n"
                        f"Recommendations:\n{recs}"
                    )
                    send_discord(msg)
                except Exception as e:
                    print(f"  Discord alert failed: {e}")

            prev_level = report["risk_level"]
            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nWatch mode stopped.")
            break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 22B — Risk Oversight Agent")
    parser.add_argument("--watch", action="store_true", help="Continuous monitoring every 60s")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds")
    args = parser.parse_args()

    if args.watch:
        watch_loop(interval=args.interval)
    else:
        report = assess_risk()
        print_report(report)
        path = save_report(report)
        print(f"Report saved to {path}")


if __name__ == "__main__":
    main()
