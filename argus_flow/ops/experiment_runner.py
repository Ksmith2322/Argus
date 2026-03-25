"""Config Experiment Framework -- controlled A/B testing per Phase C rules.

Rules (from roadmap):
- ONE variable per cycle
- Minimum 5 days or 20 signals
- Revert if EV drops > 20% vs baseline
- No simultaneous experiments

Usage:
    python -m argus_flow.ops.experiment_runner --create --name NAME --pair PAIR --param PARAM --baseline VAL --experiment VAL
    python -m argus_flow.ops.experiment_runner --status
    python -m argus_flow.ops.experiment_runner --evaluate --name NAME
    python -m argus_flow.ops.experiment_runner --revert --name NAME
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS_DIR = REPO / "argus_flow" / "experiments"

MIN_DAYS = 5
MIN_SIGNALS = 20
EV_DROP_REVERT_THRESHOLD = 0.20  # 20%


# ---------------------------------------------------------------------------
# Experiment I/O
# ---------------------------------------------------------------------------

def _exp_path(name: str) -> Path:
    return EXPERIMENTS_DIR / f"{name}.json"


def _load_experiment(name: str) -> dict | None:
    p = _exp_path(name)
    if not p.exists():
        return None
    with open(p, "r") as f:
        return json.load(f)


def _save_experiment(exp: dict):
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    p = _exp_path(exp["name"])
    with open(p, "w") as f:
        json.dump(exp, f, indent=2)


def _list_experiments() -> list[dict]:
    """Return all experiment definitions."""
    if not EXPERIMENTS_DIR.exists():
        return []
    exps = []
    for f in sorted(EXPERIMENTS_DIR.glob("*.json")):
        with open(f, "r") as fh:
            exps.append(json.load(fh))
    return exps


def _active_experiments() -> list[dict]:
    """Return experiments with status ACTIVE."""
    return [e for e in _list_experiments() if e.get("status") == "ACTIVE"]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_create(args):
    """Create a new experiment definition."""
    name = args.name
    if _load_experiment(name) is not None:
        print(f"ERROR: Experiment '{name}' already exists.")
        sys.exit(1)

    # Phase C rule: no simultaneous active experiments
    active = _active_experiments()
    if active:
        active_names = [e["name"] for e in active]
        print(f"ERROR: Phase C rule violation — active experiment(s) already running: {active_names}")
        print("       ONE variable per cycle. Complete or revert the active experiment first.")
        sys.exit(1)

    # Parse baseline and experiment values (try numeric)
    baseline_val = _parse_value(args.baseline)
    experiment_val = _parse_value(args.experiment)

    exp = {
        "name": name,
        "pair": args.pair.upper(),
        "parameter": args.param,
        "baseline_value": baseline_val,
        "experiment_value": experiment_val,
        "start_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_days": MIN_DAYS,
        "min_signals": MIN_SIGNALS,
        "status": "ACTIVE",
        "baseline_metrics": {},
        "experiment_metrics": {},
        "evaluation": None,
        "notes": "",
    }

    _save_experiment(exp)
    print(f"Experiment '{name}' created and set to ACTIVE.")
    print(f"  Pair:       {exp['pair']}")
    print(f"  Parameter:  {exp['parameter']}")
    print(f"  Baseline:   {exp['baseline_value']}")
    print(f"  Experiment: {exp['experiment_value']}")
    print(f"  Start:      {exp['start_date']}")
    print(f"  Min window: {MIN_DAYS} days or {MIN_SIGNALS} signals")
    print()
    print("  Next steps:")
    print(f"    1. Apply experiment value ({exp['parameter']} = {exp['experiment_value']}) to live config")
    print(f"    2. Wait for {MIN_DAYS} days or {MIN_SIGNALS} signals")
    print(f"    3. Run: python -m argus_flow.ops.experiment_runner --evaluate --name {name}")


def cmd_status(args):
    """List all experiments and their state."""
    exps = _list_experiments()
    if not exps:
        print("No experiments found.")
        return

    print(f"{'Name':<25} {'Pair':<10} {'Parameter':<25} {'Status':<12} {'Start Date'}")
    print("-" * 90)
    for e in exps:
        print(
            f"{e['name']:<25} {e.get('pair', '?'):<10} {e.get('parameter', '?'):<25} "
            f"{e['status']:<12} {e.get('start_date', '?')}"
        )

    active = [e for e in exps if e["status"] == "ACTIVE"]
    print()
    if active:
        print(f"Active experiments: {len(active)}")
        for a in active:
            days = _days_since(a.get("start_date", ""))
            print(f"  - {a['name']}: {days:.1f} days elapsed "
                  f"({a['parameter']}: {a['baseline_value']} -> {a['experiment_value']})")
    else:
        print("No active experiments. Safe to create a new one.")


def cmd_evaluate(args):
    """Evaluate an experiment: check if enough data, compare metrics."""
    name = args.name
    exp = _load_experiment(name)
    if exp is None:
        print(f"ERROR: Experiment '{name}' not found.")
        sys.exit(1)

    if exp["status"] not in ("ACTIVE",):
        print(f"Experiment '{name}' status is {exp['status']} — nothing to evaluate.")
        return

    days = _days_since(exp.get("start_date", ""))
    print(f"Experiment: {name}")
    print(f"  Parameter: {exp['parameter']}")
    print(f"  Baseline:  {exp['baseline_value']}  ->  Experiment: {exp['experiment_value']}")
    print(f"  Days elapsed: {days:.1f} / {exp['min_days']} required")
    print()

    # Check minimum window
    if days < exp["min_days"]:
        signals = exp.get("experiment_metrics", {}).get("signal_count", 0)
        if signals < exp["min_signals"]:
            print(f"  NOT READY: Need {exp['min_days']} days or {exp['min_signals']} signals.")
            print(f"  Current: {days:.1f} days, {signals} signals.")
            print(f"  Check back in {exp['min_days'] - days:.1f} days.")
            return
        else:
            print(f"  Window met via signal count ({signals} >= {exp['min_signals']}).")

    # Compare metrics
    baseline = exp.get("baseline_metrics", {})
    experiment = exp.get("experiment_metrics", {})

    if not baseline or not experiment:
        print("  Metrics not yet populated.")
        print("  To populate, update the experiment JSON with baseline_metrics and experiment_metrics:")
        print(f"    File: {_exp_path(name)}")
        print()
        print("  Expected metric fields: ev_per_signal, win_rate, profit_factor, signal_count")
        return

    # EV comparison
    baseline_ev = baseline.get("ev_per_signal", 0)
    experiment_ev = experiment.get("ev_per_signal", 0)

    if baseline_ev != 0:
        ev_change = (experiment_ev - baseline_ev) / abs(baseline_ev)
    else:
        ev_change = 0 if experiment_ev == 0 else float("inf")

    print(f"  Baseline EV:   {baseline_ev:.4f}")
    print(f"  Experiment EV: {experiment_ev:.4f}")
    print(f"  EV change:     {ev_change:+.1%}")
    print()

    if ev_change < -EV_DROP_REVERT_THRESHOLD:
        recommendation = "REVERT"
        reason = f"EV dropped {ev_change:+.1%} (threshold: -{EV_DROP_REVERT_THRESHOLD:.0%})"
    elif ev_change > 0:
        recommendation = "KEEP"
        reason = f"EV improved {ev_change:+.1%}"
    else:
        recommendation = "KEEP (marginal)"
        reason = f"EV change {ev_change:+.1%} within tolerance"

    print(f"  Recommendation: {recommendation}")
    print(f"  Reason: {reason}")

    # Save evaluation
    exp["evaluation"] = {
        "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_elapsed": round(days, 1),
        "ev_change_pct": round(ev_change * 100, 2),
        "recommendation": recommendation,
        "reason": reason,
    }
    _save_experiment(exp)

    if recommendation == "REVERT":
        print()
        print(f"  To revert: python -m argus_flow.ops.experiment_runner --revert --name {name}")


def cmd_revert(args):
    """Mark experiment as REVERTED and print restore instructions."""
    name = args.name
    exp = _load_experiment(name)
    if exp is None:
        print(f"ERROR: Experiment '{name}' not found.")
        sys.exit(1)

    if exp["status"] == "REVERTED":
        print(f"Experiment '{name}' is already REVERTED.")
        return

    exp["status"] = "REVERTED"
    exp["reverted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_experiment(exp)

    print(f"Experiment '{name}' marked as REVERTED.")
    print()
    print("  Restore baseline config:")
    print(f"    Parameter: {exp['parameter']}")
    print(f"    Restore to: {exp['baseline_value']}  (was: {exp['experiment_value']})")
    print()
    print("  Update the live config file for this pair and restart the runner.")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_value(s: str):
    """Try to parse a string as int, float, or leave as string."""
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def _days_since(iso_date: str) -> float:
    """Days elapsed since an ISO date string."""
    if not iso_date:
        return 0.0
    try:
        start = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - start).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Config Experiment Framework (Phase C rules)"
    )
    parser.add_argument("--create", action="store_true", help="Create a new experiment")
    parser.add_argument("--status", action="store_true", help="List all experiments")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate an experiment")
    parser.add_argument("--revert", action="store_true", help="Revert an experiment")

    parser.add_argument("--name", help="Experiment name")
    parser.add_argument("--pair", help="Trading pair (e.g., GBPUSD)")
    parser.add_argument("--param", help="Parameter path (e.g., risk.stop_pips)")
    parser.add_argument("--baseline", help="Baseline value")
    parser.add_argument("--experiment", help="Experiment value")

    args = parser.parse_args()

    if args.create:
        if not all([args.name, args.pair, args.param, args.baseline, args.experiment]):
            print("ERROR: --create requires --name, --pair, --param, --baseline, --experiment")
            sys.exit(1)
        cmd_create(args)
    elif args.status:
        cmd_status(args)
    elif args.evaluate:
        if not args.name:
            print("ERROR: --evaluate requires --name")
            sys.exit(1)
        cmd_evaluate(args)
    elif args.revert:
        if not args.name:
            print("ERROR: --revert requires --name")
            sys.exit(1)
        cmd_revert(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
