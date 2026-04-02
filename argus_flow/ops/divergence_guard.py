"""Replay-Live Divergence Guard — compare live paper results against replay expectations.

Required before Paper→Live promotion. Flags WATCH/KILL when live diverges from replay.

Usage:
    python -m argus_flow.ops.divergence_guard
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import STAGE_PAPER, STAGE_QUARANTINE, STAGE_REAL, STAGE_WATCHER, runners_for_stages

REPO = Path(__file__).resolve().parents[2]

# Stage-aware thresholds: early stages need more data before KILL escalation.
# Prevents false KILLs on immature cohorts (alert fatigue / operator desensitization).
STAGE_ESCALATION_RULES = {
    STAGE_WATCHER: {
        "min_days_for_kill": 7,
        "min_signals_for_kill": 50,
        "min_trades_for_wr_kill": 20,
        "max_verdict": "WATCH",  # cap at WATCH until minimums met
    },
    STAGE_PAPER: {
        "min_days_for_kill": 14,
        "min_signals_for_kill": 60,
        "min_trades_for_wr_kill": 20,
        "max_verdict": "WATCH",  # cap at WATCH until minimums met
    },
    STAGE_REAL: {
        "min_days_for_kill": 0,
        "min_signals_for_kill": 0,
        "min_trades_for_wr_kill": 10,
        "max_verdict": "KILL",  # no cap for real capital
    },
    STAGE_QUARANTINE: {
        "min_days_for_kill": 0,
        "min_signals_for_kill": 0,
        "min_trades_for_wr_kill": 10,
        "max_verdict": "KILL",
    },
}


def governed_runners() -> list[dict]:
    """Return managed runners that should be held to replay/live divergence checks."""
    runners: list[dict] = []
    for runner in runners_for_stages({STAGE_WATCHER, STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE}):
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


def _load_signals(log_dir: Path) -> list[dict]:
    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return []
    with open(sig_file, "r") as f:
        return list(csv.DictReader(f))


def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r") as f:
        return list(csv.DictReader(f))


def _compute_days(rows: list[dict]) -> float:
    if len(rows) < 2:
        return 0.0
    try:
        first = datetime.fromisoformat(rows[0]["ts"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(rows[-1]["ts"].replace("Z", "+00:00"))
        return max((last - first).total_seconds() / 86400, 0.001)
    except Exception:
        return 0.0


def check_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    cfg_path = REPO / runner["config"]

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "status": "NO_DATA",
        "verdict": "NO_DATA",
        "reason": "",
        "flags": [],
        "kill_flags": [],
        "watch_flags": [],
        "metrics": {},
        "current_stage": runner.get("current_stage", ""),
        "live": bool(runner.get("live", False)),
    }

    # Load replay expectations
    if not cfg_path.exists():
        result["flags"].append("CONFIG_MISSING")
        result["reason"] = "CONFIG_MISSING"
        return result
    cfg = json.loads(cfg_path.read_text())
    rexp = cfg.get("replay_expectations", {})
    result["replay"] = rexp

    signals = _load_signals(log_dir)
    trades = _load_trades(log_dir)

    if not signals:
        # Distinguish QUIET (healthy watcher with no signals yet) from broken
        stage = runner.get("current_stage", "")
        if stage in (STAGE_WATCHER,):
            result["status"] = "QUIET"
            result["verdict"] = "QUIET"
            result["reason"] = "watcher has no signals yet (normal for new/quiet observer)"
        else:
            result["status"] = "NO_SIGNALS"
            result["verdict"] = "NO_SIGNALS"
            result["reason"] = "signals.csv missing or empty"
        return result

    days = _compute_days(signals)
    entries = [s for s in signals if s.get("action") == "ENTRY"]

    # Signals per day
    sigs_per_day = len(entries) / days if days > 0 else 0
    replay_spd = rexp.get("signals_per_day", 0)
    spd_range = rexp.get("signals_per_day_range", [0, 999])

    result["metrics"]["days_observed"] = round(days, 2)
    result["metrics"]["signals_total"] = len(signals)
    result["metrics"]["entries_total"] = len(entries)
    result["metrics"]["signals_per_day"] = round(sigs_per_day, 1)
    result["metrics"]["replay_signals_per_day"] = replay_spd

    # Signal frequency check
    if replay_spd > 0 and days >= 1:
        ratio = sigs_per_day / replay_spd if replay_spd > 0 else 0
        result["metrics"]["signal_freq_ratio"] = round(ratio, 2)
        if ratio < 0.5 or ratio > 2.0:
            result["flags"].append(f"SIGNAL_FREQ_KILL: {sigs_per_day:.1f}/day vs replay {replay_spd}/day (ratio {ratio:.2f})")
        elif ratio < 0.7 or ratio > 1.5:
            result["flags"].append(f"SIGNAL_FREQ_WATCH: {sigs_per_day:.1f}/day vs replay {replay_spd}/day (ratio {ratio:.2f})")

    # Trade metrics
    if trades:
        pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
        pnls = [float(t.get(pnl_field, 0)) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        live_wr = len(wins) / len(pnls) if pnls else 0
        replay_wr = rexp.get("win_rate", 0)

        result["metrics"]["closed_trades"] = len(trades)
        result["metrics"]["live_win_rate"] = round(live_wr, 3)
        result["metrics"]["replay_win_rate"] = replay_wr
        result["metrics"]["total_pnl"] = round(sum(pnls), 2)

        # Win rate check
        if len(trades) >= 10:
            wr_delta = abs(live_wr - replay_wr)
            result["metrics"]["win_rate_delta"] = round(wr_delta, 3)
            if wr_delta > 0.20:
                result["flags"].append(f"WIN_RATE_KILL: live {live_wr:.1%} vs replay {replay_wr:.1%} (delta {wr_delta:.1%})")
            elif wr_delta > 0.15:
                result["flags"].append(f"WIN_RATE_WATCH: live {live_wr:.1%} vs replay {replay_wr:.1%} (delta {wr_delta:.1%})")

        # Stop/target/timeout rates
        outcomes = [t.get("exit_reason", "") for t in trades]
        stop_rate = sum(1 for o in outcomes if o == "stop") / len(outcomes) if outcomes else 0
        target_rate = sum(1 for o in outcomes if o == "target") / len(outcomes) if outcomes else 0
        timeout_rate = sum(1 for o in outcomes if o == "timeout") / len(outcomes) if outcomes else 0

        result["metrics"]["stop_rate"] = round(stop_rate, 3)
        result["metrics"]["target_rate"] = round(target_rate, 3)
        result["metrics"]["timeout_rate"] = round(timeout_rate, 3)

        if timeout_rate > 0.60 and len(trades) >= 15:
            result["flags"].append(f"TIMEOUT_KILL: {timeout_rate:.1%} > 60% threshold")
        if stop_rate > 2 * target_rate and target_rate > 0 and len(trades) >= 15:
            result["flags"].append(f"STOP_RATIO_WATCH: stop {stop_rate:.1%} > 2x target {target_rate:.1%}")

    # Determine overall status
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
    elif len(trades) >= 5:
        result["status"] = "PASS"
        result["reason"] = "within replay/live divergence guardrails"
    else:
        result["status"] = "COLLECTING"
        result["reason"] = "insufficient closed trades for divergence verdict"

    # Stage-aware escalation cap: prevent KILL on immature cohorts
    stage = runner.get("current_stage", "")
    rules = STAGE_ESCALATION_RULES.get(stage, {})
    if rules and result["status"] == "KILL":
        max_verdict = rules.get("max_verdict", "KILL")
        min_days = rules.get("min_days_for_kill", 0)
        min_signals = rules.get("min_signals_for_kill", 0)
        min_trades = rules.get("min_trades_for_wr_kill", 0)

        days_ok = days >= min_days
        signals_ok = len(entries) >= min_signals
        trades_ok = len(trades) >= min_trades

        if not (days_ok and signals_ok and trades_ok):
            capped = max_verdict
            cap_reasons = []
            if not days_ok:
                cap_reasons.append(f"days={days:.1f}<{min_days}")
            if not signals_ok:
                cap_reasons.append(f"signals={len(entries)}<{min_signals}")
            if not trades_ok:
                cap_reasons.append(f"trades={len(trades)}<{min_trades}")
            result["status"] = capped
            result["reason"] = f"KILL capped to {capped} ({stage}): {', '.join(cap_reasons)} | " + result["reason"]
            result["stage_capped"] = True

    result["verdict"] = result["status"]

    return result


def main():
    print("=" * 65)
    print(f"  Replay-Live Divergence Guard — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 65)

    results = []
    runners = governed_runners()
    for runner in runners:
        r = check_runner(runner)
        results.append(r)

        status_colors = {"PASS": "32", "WATCH": "33", "KILL": "31", "COLLECTING": "36", "NO_DATA": "90", "NO_SIGNALS": "90"}
        color = status_colors.get(r["status"], "0")
        print(f"\n  {r['name']:>10s}: \033[{color}m{r['status']}\033[0m")

        m = r.get("metrics", {})
        if m:
            print(f"    Days: {m.get('days_observed', 0):.1f} | Entries: {m.get('entries_total', 0)} | Trades: {m.get('closed_trades', 0)}")
            if m.get("signals_per_day"):
                print(f"    Signals/day: {m['signals_per_day']:.1f} (replay: {m.get('replay_signals_per_day', '?')})")
            if "live_win_rate" in m:
                print(f"    Win rate: {m['live_win_rate']:.1%} (replay: {m.get('replay_win_rate', 0):.1%})")

        for flag in r.get("flags", []):
            fc = "31" if "KILL" in flag else "33"
            print(f"    \033[{fc}m! {flag}\033[0m")

    # Save report
    out_path = REPO / "argus_flow" / "logs" / "divergence_report.json"
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
