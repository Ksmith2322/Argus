"""Kill Discipline Checker — automated enforcement of kill rules.

Checks each FX pair against kill criteria from the roadmap.
Flags KILL/WATCH/PASS for each pair.

Usage:
    python -m argus_flow.ops.kill_discipline
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import STAGE_PAPER, STAGE_QUARANTINE, STAGE_REAL, runners_for_stages

REPO = Path(__file__).resolve().parents[2]

# Kill thresholds
CONSECUTIVE_NEGATIVE_WEEKS = 3
MIN_TRADES_PER_WEEK = 15
MAX_DRAWDOWN_MULT = 3.0  # 3x model drawdown = kill


def governed_runners() -> list[dict]:
    """Return managed paper/live runners covered by kill discipline."""
    runners: list[dict] = []
    for runner in runners_for_stages({STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE}):
        runners.append(
            {
                "name": runner["name"],
                "symbol": runner["symbol"],
                "log_dir": runner["log_dir"],
                "config": runner["config_path"],
                "unit": runner["unit"],
                "current_stage": runner["current_stage"],
                "live": runner["live"],
            }
        )
    runners.sort(key=lambda item: (item.get("current_stage", ""), item["name"]))
    return runners


def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r") as f:
        return list(csv.DictReader(f))


def _load_replay_expectations(config_path: Path) -> dict:
    """Load replay expectations from config."""
    if config_path.exists():
        return json.loads(config_path.read_text()).get("replay_expectations", {})
    return {}


def check_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    trades = _load_trades(log_dir)
    cfg_path = REPO / runner["config"]
    rexp = _load_replay_expectations(cfg_path)

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "status": "PASS",
        "verdict": "PASS",
        "reason": "",
        "flags": [],
        "kill_flags": [],
        "watch_flags": [],
        "metrics": {},
        "current_stage": runner.get("current_stage", ""),
        "live": bool(runner.get("live", False)),
    }

    # Filter to valid trades only
    valid_trades = [t for t in trades if t.get("experiment_valid", "").lower() == "true"]

    if not valid_trades:
        result["status"] = "COLLECTING"
        result["verdict"] = "COLLECTING"
        result["reason"] = "no valid trades yet"
        result["metrics"]["valid_trades"] = 0
        return result

    result["metrics"]["valid_trades"] = len(valid_trades)

    pnl_field = "pnl_pips" if "pnl_pips" in valid_trades[0] else "pnl_pts"
    pnls = [float(t.get(pnl_field, 0)) for t in valid_trades]

    # ── Check 1: Weekly P&L (3 consecutive negative weeks) ──
    # Group trades by ISO week
    weekly_pnl: dict[str, dict] = {}
    for t in valid_trades:
        try:
            ts = datetime.fromisoformat(t["ts"].replace("Z", "+00:00"))
            week_key = ts.strftime("%Y-W%W")
            weekly_pnl.setdefault(week_key, {"pnl": 0.0, "count": 0})
            weekly_pnl[week_key]["pnl"] += float(t.get(pnl_field, 0))
            weekly_pnl[week_key]["count"] += 1
        except Exception:
            pass

    if weekly_pnl:
        sorted_weeks = sorted(weekly_pnl.keys())
        result["metrics"]["weeks_observed"] = len(sorted_weeks)

        # Check for 3 consecutive negative weeks with >= 15 trades each
        consec_neg = 0
        max_consec_neg = 0
        for week in sorted_weeks:
            w = weekly_pnl[week]
            if w["pnl"] < 0 and w["count"] >= MIN_TRADES_PER_WEEK:
                consec_neg += 1
                max_consec_neg = max(max_consec_neg, consec_neg)
            else:
                consec_neg = 0

        result["metrics"]["max_consecutive_negative_weeks"] = max_consec_neg
        if max_consec_neg >= CONSECUTIVE_NEGATIVE_WEEKS:
            result["flags"].append(
                f"KILL_CONSECUTIVE_WEEKS: {max_consec_neg} consecutive negative weeks "
                f"(>={MIN_TRADES_PER_WEEK} trades each)"
            )

    # ── Check 2: Win rate vs replay (execution delta > 25%) ──
    if rexp and len(valid_trades) >= 10:
        live_wr = sum(1 for p in pnls if p > 0) / len(pnls)
        replay_wr = rexp.get("win_rate", 0)
        if replay_wr > 0:
            wr_delta = abs(live_wr - replay_wr) / replay_wr
            result["metrics"]["win_rate_delta_pct"] = round(wr_delta * 100, 1)
            if wr_delta > 0.25:
                result["flags"].append(
                    f"KILL_EXECUTION_DELTA: WR {live_wr:.1%} vs replay {replay_wr:.1%} "
                    f"(delta {wr_delta:.0%} > 25%)"
                )
            elif wr_delta > 0.15:
                result["flags"].append(
                    f"WATCH_EXECUTION_DELTA: WR {live_wr:.1%} vs replay {replay_wr:.1%} "
                    f"(delta {wr_delta:.0%})"
                )

    # ── Check 3: Expectancy vs replay (execution delta > 25%) ──
    if rexp and len(valid_trades) >= 10:
        live_exp = sum(pnls) / len(pnls)
        replay_exp = rexp.get("exp_pips_per_trade", 0)
        result["metrics"]["live_expectancy"] = round(live_exp, 3)
        result["metrics"]["replay_expectancy"] = replay_exp
        if replay_exp > 0:
            exp_delta = (replay_exp - live_exp) / replay_exp
            if exp_delta > 0.25:
                result["flags"].append(
                    f"WATCH_EXPECTANCY_DELTA: live {live_exp:.2f} vs replay "
                    f"{replay_exp:.2f} ({exp_delta:.0%} degradation)"
                )

    # ── Check 4: Drawdown ──
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    result["metrics"]["max_drawdown_pips"] = round(max_dd, 2)
    result["metrics"]["total_pnl"] = round(sum(pnls), 2)

    # Model drawdown: derived from config stop_pips if available,
    # otherwise fallback to conservative 10-pip baseline.
    # Rationale: expected max DD ~ 3-5 consecutive stop-outs at full stop distance.
    default_model_dd_used = True
    model_dd = 10.0  # fallback
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            stop_pips = cfg.get("risk", {}).get("stop_pips", 0)
            if stop_pips > 0:
                # Model DD = 3 consecutive stops (conservative baseline)
                model_dd = stop_pips * 3
                default_model_dd_used = False
        except Exception:
            pass
    result["metrics"]["model_drawdown_pips"] = model_dd
    result["metrics"]["default_model_dd_used"] = default_model_dd_used

    if max_dd > model_dd * MAX_DRAWDOWN_MULT:
        result["flags"].append(
            f"KILL_DRAWDOWN: {max_dd:.1f} pips > {MAX_DRAWDOWN_MULT}x model ({model_dd:.1f})"
            + (" [fallback baseline]" if default_model_dd_used else "")
        )
    elif max_dd > model_dd * 1.5:
        result["flags"].append(
            f"WATCH_DRAWDOWN: {max_dd:.1f} pips > 1.5x model ({model_dd:.1f})"
            + (" [fallback baseline]" if default_model_dd_used else "")
        )

    # ── Determine status ──
    kills = [f for f in result["flags"] if "KILL" in f]
    watches = [f for f in result["flags"] if "WATCH" in f]
    result["kill_flags"] = kills
    result["watch_flags"] = watches

    if kills:
        result["status"] = "KILL"
        result["reason"] = " | ".join(kills[:3])
    elif watches:
        result["status"] = "WATCH"
        result["reason"] = " | ".join(watches[:3])
    else:
        result["status"] = "PASS"
        result["reason"] = "within kill-discipline guardrails"

    result["verdict"] = result["status"]

    return result


def main():
    print("=" * 65)
    print(f"  Kill Discipline Check — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 65)

    results = []
    runners = governed_runners()
    for runner in runners:
        r = check_runner(runner)
        results.append(r)

        status_colors = {"PASS": "32", "WATCH": "33", "KILL": "31", "COLLECTING": "36"}
        color = status_colors.get(r["status"], "0")
        print(f"\n  {r['name']:>10s}: \033[{color}m{r['status']}\033[0m")

        m = r.get("metrics", {})
        if m.get("valid_trades"):
            print(
                f"    Trades: {m['valid_trades']} | "
                f"PnL: {m.get('total_pnl', 0):+.1f} pips | "
                f"MaxDD: {m.get('max_drawdown_pips', 0):.1f} pips"
            )
            if m.get("weeks_observed"):
                print(
                    f"    Weeks: {m['weeks_observed']} | "
                    f"Max consec neg: {m.get('max_consecutive_negative_weeks', 0)}"
                )

        for flag in r.get("flags", []):
            fc = "31" if "KILL" in flag else "33"
            print(f"    \033[{fc}m! {flag}\033[0m")

    # Save report
    out_path = REPO / "argus_flow" / "logs" / "kill_discipline_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "stages": [STAGE_PAPER, STAGE_REAL],
            "runner_count": len(runners),
        },
        "runners": results,
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
