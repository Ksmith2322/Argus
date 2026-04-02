"""Parameter Sensitivity Analysis — measure how fragile the strategy is to config changes.

Generates ±N% perturbations of key parameters, runs replays, and measures
PnL/Sharpe sensitivity. Identifies "fragile" parameters that need recalibration
vs "robust" parameters that tolerate noise.

Principle: if ±5% change in a parameter flips PnL from positive to negative,
the "edge" may be overfitted to that exact value.

Usage:
    python -m argus_flow.ops.param_sensitivity                     # generate perturbation configs
    python -m argus_flow.ops.param_sensitivity --symbol eurusd     # single symbol
    python -m argus_flow.ops.param_sensitivity --pct 10            # ±10% perturbation
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
SENSITIVITY_DIR = REPO / "argus_flow" / "configs" / "_sensitivity"

# Parameters to perturb (dotted path → display name)
SENSITIVITY_PARAMS = {
    "trigger.range_pct_min": "range_pct_min",
    "risk.stop_pips": "stop_pips",
    "risk.target_pips": "target_pips",
    "risk.timeout_minutes": "timeout_minutes",
    "risk.min_signal_gap_minutes": "min_signal_gap",
    "direction.dist_long_threshold": "dist_long",
    "direction.dist_short_threshold": "dist_short",
    "trigger.session_start_utc": "session_start",
    "trigger.session_end_utc": "session_end",
}

DEFAULT_PERTURBATION_PCT = 5  # ±5%


def _deep_get(cfg: dict, dotted_key: str):
    """Get a nested config value using dotted key."""
    keys = dotted_key.split(".")
    d = cfg
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


def _deep_set(cfg: dict, dotted_key: str, value) -> None:
    """Set a nested config value using dotted key."""
    keys = dotted_key.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def generate_sensitivity_configs(
    source_symbol: str | None = None,
    perturbation_pct: float = DEFAULT_PERTURBATION_PCT,
) -> list[dict]:
    """Generate ±N% perturbation configs for sensitivity analysis."""
    generated = []

    configs = sorted(CONFIGS_DIR.glob("*_t4_paper_v1.json"))
    if source_symbol:
        target = source_symbol.lower()
        configs = [c for c in configs if target in c.stem.lower()]

    if not configs:
        print(f"  No matching configs found")
        return []

    SENSITIVITY_DIR.mkdir(parents=True, exist_ok=True)
    multipliers = [1 - perturbation_pct / 100, 1 + perturbation_pct / 100]
    labels = [f"minus{perturbation_pct}pct", f"plus{perturbation_pct}pct"]

    for config_path in configs:
        base_cfg = json.loads(config_path.read_text())
        symbol = base_cfg.get("symbol", "").upper()

        # Baseline
        baseline = copy.deepcopy(base_cfg)
        baseline["_sensitivity"] = {
            "variant": "baseline",
            "param": None,
            "original_value": None,
            "perturbed_value": None,
        }
        baseline["version"] = "sensitivity_baseline"
        out_path = SENSITIVITY_DIR / f"{symbol.lower()}_sens_baseline.json"
        out_path.write_text(json.dumps(baseline, indent=4), encoding="utf-8")
        generated.append({
            "symbol": symbol,
            "param": "baseline",
            "variant": "baseline",
            "path": str(out_path),
        })

        for param_key, param_name in SENSITIVITY_PARAMS.items():
            original = _deep_get(base_cfg, param_key)
            if original is None or not isinstance(original, (int, float)):
                continue
            if original == 0:
                continue  # can't perturb zero

            for mult, label in zip(multipliers, labels):
                perturbed = copy.deepcopy(base_cfg)
                new_value = original * mult

                # Round appropriately
                if isinstance(original, int):
                    new_value = max(1, round(new_value))
                else:
                    new_value = round(new_value, 6)

                _deep_set(perturbed, param_key, new_value)

                perturbed["_sensitivity"] = {
                    "variant": f"{param_name}_{label}",
                    "param": param_key,
                    "original_value": original,
                    "perturbed_value": new_value,
                    "perturbation_pct": perturbation_pct if "plus" in label else -perturbation_pct,
                }
                perturbed["version"] = f"sensitivity_{param_name}_{label}"

                out_name = f"{symbol.lower()}_sens_{param_name}_{label}.json"
                out_path = SENSITIVITY_DIR / out_name
                out_path.write_text(json.dumps(perturbed, indent=4), encoding="utf-8")

                generated.append({
                    "symbol": symbol,
                    "param": param_name,
                    "variant": label,
                    "original": original,
                    "perturbed": new_value,
                    "path": str(out_path),
                })

    return generated


def main():
    parser = argparse.ArgumentParser(description="Parameter sensitivity analysis")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--pct", type=float, default=DEFAULT_PERTURBATION_PCT,
                        help=f"Perturbation percentage (default: ±{DEFAULT_PERTURBATION_PCT}%%)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Parameter Sensitivity — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Perturbation: ±{args.pct}%")
    print("=" * 60)

    generated = generate_sensitivity_configs(args.symbol, args.pct)

    if not generated:
        return

    # Group by symbol
    by_symbol = {}
    for g in generated:
        by_symbol.setdefault(g["symbol"], []).append(g)

    for symbol, variants in by_symbol.items():
        params = set(v["param"] for v in variants if v["param"] != "baseline")
        print(f"\n  {symbol}: {len(variants)} configs ({len(params)} parameters × 2 directions + baseline)")
        for v in variants:
            if v.get("param") == "baseline":
                continue
            if "original" in v:
                print(f"    {v['param']:>20s} {v['variant']:>12s}: {v['original']} → {v['perturbed']}")

    total = len(generated)
    print(f"\n  Total: {total} configs generated in {SENSITIVITY_DIR}")
    print(f"\n  Next steps:")
    print(f"    1. Run all configs through replay with identical data")
    print(f"    2. Compare PnL, win rate, profit factor vs baseline")
    print(f"    3. Parameters where ±{args.pct}% flips PnL sign = FRAGILE (likely overfitted)")
    print(f"    4. Parameters where ±{args.pct}% barely changes PnL = ROBUST")

    # Save manifest
    manifest_path = SENSITIVITY_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "perturbation_pct": args.pct,
        "configs": generated,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\n  Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
