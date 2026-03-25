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


def _read_positions() -> list[dict]:
    """Return list of pairs with open positions."""
    open_positions = []
    for r in RUNNERS:
        state = _read_state(r["log_dir"])
        if state is None:
            continue
        pos = state.get("position") or state.get("qty") or state.get("status")
        # Heuristic: treat as open if status is not FLAT/flat/None
        status = str(state.get("status", "FLAT")).upper()
        if status not in ("FLAT", "NONE", ""):
            open_positions.append({
                "pair": r["name"],
                "symbol": r["symbol"],
                "status": status,
                "state": state,
            })
    return open_positions


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
    open_positions = _read_positions()
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

    # Invalid trade rate
    invalid_rate = _check_invalid_trade_rate()

    # Per-pair status
    per_pair = {}
    for r in RUNNERS:
        state = _read_state(r["log_dir"])
        status = "UNKNOWN"
        if state:
            status = str(state.get("status", "FLAT")).upper()
        per_pair[r["symbol"]] = {
            "name": r["name"],
            "status": status,
            "has_state": state is not None,
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

    report = {
        "timestamp": now,
        "fleet_positions": {
            "count": position_count,
            "list": [p["pair"] for p in open_positions],
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
