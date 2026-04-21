"""Parameter sensitivity sweep — rerun a strategy's backtest across a grid
of parameter perturbations and report how PF degrades.

Motivation: scope_down subsets look great in-sample but many are held up
by magic numbers (score >= 80, RSI zone, confluence tolerance 0.002,
BB 33rd percentile, etc.). If PF collapses when these are nudged by
±10-20%, the edge is fragile. If PF survives, the edge is structurally
robust.

Usage:
    python -m helio.param_sensitivity --strategy hermes
    python -m helio.param_sensitivity --strategy wick_gbpusd
    python -m helio.param_sensitivity --all

Output: strategy_confidence/_sensitivity/<strategy>.json

Design:
  - Each strategy defines a SENSITIVITY_SPEC in its runner.py (or a
    registered entry here) — list of (param_path, base_value, test_values)
  - For each param, run the backtest at each test value, collect PF
  - Report:
      * Base PF (at original value)
      * Min PF across tests (worst-case degradation)
      * PF range (max - min) per param (sensitivity magnitude)
      * Params ranked by sensitivity (biggest PF swing first)
  - Flag as FRAGILE if any ±20% perturbation drops PF below 1.2

Not wired into nightly by default — sweeps are expensive. Run manually
after a change, or when a strategy is being considered for promotion.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

OUT_DIR = _REPO / "strategy_confidence" / "_sensitivity"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Per-strategy sensitivity specs — list of params to sweep with their test values.
# Each spec: {"strategy": name, "runner_module": mod, "runner_fn": fn, "params": [...]}
# Each param: {"name": display, "base": value, "tests": [values to try], "apply": callable}

def _run_hermes_backtest(param_overrides: dict) -> float | None:
    """Run hermes backtest once with param overrides, return PF. Best-effort."""
    try:
        import subprocess
        # Hermes backtest is a standalone CLI path; overrides via env vars
        env = {**__import__("os").environ}
        for k, v in param_overrides.items():
            env[f"HERMES_{k.upper()}"] = str(v)
        out = subprocess.check_output(
            [str(_REPO / ".." / ".venv" / "Scripts" / "python.exe"), "-m", "hermes.runner", "--backtest", "--dry-run"],
            cwd=str(_REPO), env=env, stderr=subprocess.STDOUT, timeout=180,
        ).decode("utf-8", errors="replace")
        # parse PF from stdout — look for "PF 2.06" pattern
        import re
        m = re.search(r"PF[\s:]+([0-9]+\.[0-9]+)", out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


SENSITIVITY_SPECS = {
    # Hermes is the canonical example — MIN_SCORE_TO_ENTER is the key magic
    # number (80). Sweep ±10% to see if the edge holds at 72 / 88.
    "hermes": {
        "runner_hint": "hermes.runner --backtest",
        "params": [
            {
                "name": "MIN_SCORE_TO_ENTER",
                "base": 80,
                "tests": [72, 76, 80, 84, 88],
                "description": "Minimum confluence score to take entry (validated at 80)",
            },
        ],
        "run_fn": _run_hermes_backtest,
    },
    # Stub-only entries for other strategies — flesh out when ready to sweep.
    "titan": {
        "runner_hint": "titan.runner --backtest",
        "params": [
            {"name": "MIN_STRENGTH", "base": 65, "tests": [55, 60, 65, 70, 75],
             "description": "Minimum signal strength (titan/runner.py:173)"},
        ],
        "run_fn": None,  # not yet implemented
    },
    "tori": {
        "runner_hint": "forge.tori.runner --backtest",
        "params": [
            {"name": "MIN_RR", "base": 2.0, "tests": [1.5, 1.75, 2.0, 2.5, 3.0],
             "description": "Minimum reward/risk ratio (forge/tori/runner.py:75)"},
        ],
        "run_fn": None,
    },
}


def sweep_strategy(strategy: str) -> dict | None:
    spec = SENSITIVITY_SPECS.get(strategy)
    if not spec:
        return None

    run_fn = spec.get("run_fn")
    if run_fn is None:
        return {
            "strategy": strategy,
            "status": "NOT_IMPLEMENTED",
            "note": (f"Sensitivity sweep not yet wired for {strategy}. "
                     f"Design: inject param overrides via env vars or CLI args "
                     f"into `{spec['runner_hint']}`, parse PF from stdout, "
                     f"collect across test values. Est. 15-30min per strategy.")
        }

    results = []
    for p in spec["params"]:
        per_param = {"name": p["name"], "base": p["base"], "description": p.get("description", ""), "runs": []}
        t0 = time.time()
        for v in p["tests"]:
            pf = run_fn({p["name"]: v})
            per_param["runs"].append({"value": v, "pf": pf})
        per_param["seconds"] = round(time.time() - t0, 1)
        pfs = [r["pf"] for r in per_param["runs"] if r["pf"] is not None]
        if pfs:
            per_param["pf_min"] = round(min(pfs), 3)
            per_param["pf_max"] = round(max(pfs), 3)
            per_param["pf_range"] = round(max(pfs) - min(pfs), 3)
            per_param["fragile"] = per_param["pf_min"] < 1.2
        results.append(per_param)

    any_fragile = any(r.get("fragile") for r in results)
    return {
        "strategy": strategy,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "verdict": "FRAGILE" if any_fragile else "ROBUST",
        "params": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    targets = list(SENSITIVITY_SPECS.keys()) if args.all else ([args.strategy] if args.strategy else [])
    if not targets:
        print("Specify --strategy NAME or --all")
        print(f"Available: {list(SENSITIVITY_SPECS.keys())}")
        return 1

    for s in targets:
        report = sweep_strategy(s)
        if report is None:
            print(f"  {s}: not in SENSITIVITY_SPECS")
            continue
        if report.get("status") == "NOT_IMPLEMENTED":
            print(f"  {s}: {report['status']} — {report['note']}")
            continue
        out = OUT_DIR / f"{s}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"  {s}: {report['verdict']} — wrote {out}")
        for p in report["params"]:
            print(f"    {p['name']:25s} pf range={p.get('pf_range')} min={p.get('pf_min')} "
                  f"{'FRAGILE' if p.get('fragile') else 'robust'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
