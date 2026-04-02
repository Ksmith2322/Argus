"""Ablation Test — determine which trigger components actually drive edge.

Generates config variants with individual trigger features disabled, then
compares replay results to isolate signal vs noise.

The key question: is range_accel the signal, or is it just session + location?

Variants generated:
  baseline      — current config (no changes)
  no_range_pct  — range_pct_min=0 (always passes)
  no_accel      — range_accel_min=0 (already 0 in most configs, verify)
  no_session    — session_start=0, session_end=24 (all hours)
  no_direction  — dist_long=0, dist_short=1 (always triggers both)
  session_only  — range_pct=0, accel=0, vol_z=null (pure session+direction)

Usage:
    python -m argus_flow.ops.ablation_test                     # generate configs
    python -m argus_flow.ops.ablation_test --source eurusd     # specific symbol
    python -m argus_flow.ops.ablation_test --run               # generate + run replays
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
ABLATION_DIR = REPO / "argus_flow" / "configs" / "_ablation"


VARIANTS = {
    "baseline": {},  # no changes
    "no_range_pct": {"trigger.range_pct_min": 0.0},
    "no_accel": {"trigger.range_accel_min": 0.0},
    "no_session": {"trigger.session_start_utc": 0, "trigger.session_end_utc": 24},
    "no_direction": {"direction.dist_long_threshold": 0.0, "direction.dist_short_threshold": 1.0},
    "session_only": {
        "trigger.range_pct_min": 0.0,
        "trigger.range_accel_min": 0.0,
        "trigger.vol_z_min": None,
    },
}


def _deep_set(cfg: dict, dotted_key: str, value) -> None:
    """Set a nested config value using dotted key (e.g., 'trigger.range_pct_min')."""
    keys = dotted_key.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def generate_ablation_configs(source_symbol: str | None = None) -> list[dict]:
    """Generate ablation config variants for one or all FX symbols."""
    generated = []

    configs = sorted(CONFIGS_DIR.glob("*_t4_paper_v1.json"))
    if source_symbol:
        target = source_symbol.lower()
        configs = [c for c in configs if target in c.stem.lower()]

    if not configs:
        print(f"  No matching configs found" + (f" for '{source_symbol}'" if source_symbol else ""))
        return []

    ABLATION_DIR.mkdir(parents=True, exist_ok=True)

    for config_path in configs:
        base_cfg = json.loads(config_path.read_text())
        symbol = base_cfg.get("symbol", config_path.stem.split("_")[0]).upper()

        for variant_name, overrides in VARIANTS.items():
            variant_cfg = copy.deepcopy(base_cfg)

            # Apply overrides
            for key, value in overrides.items():
                _deep_set(variant_cfg, key, value)

            # Mark as ablation variant
            variant_cfg["_ablation"] = {
                "variant": variant_name,
                "source_config": config_path.name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            variant_cfg["version"] = f"ablation_{variant_name}"

            # Write
            out_name = f"{symbol.lower()}_ablation_{variant_name}.json"
            out_path = ABLATION_DIR / out_name
            out_path.write_text(json.dumps(variant_cfg, indent=4), encoding="utf-8")

            generated.append({
                "symbol": symbol,
                "variant": variant_name,
                "path": str(out_path),
                "overrides": overrides,
            })

    return generated


def main():
    parser = argparse.ArgumentParser(description="Generate ablation test configs")
    parser.add_argument("--source", type=str, default=None,
                        help="Source symbol to ablate (e.g., eurusd). Omit for all.")
    parser.add_argument("--run", action="store_true",
                        help="Generate configs AND run replays (requires replay infrastructure)")
    args = parser.parse_args()

    print("=" * 55)
    print(f"  Ablation Test — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 55)

    generated = generate_ablation_configs(args.source)

    if not generated:
        return

    print(f"\n  Generated {len(generated)} ablation configs:")
    by_symbol = {}
    for g in generated:
        by_symbol.setdefault(g["symbol"], []).append(g)

    for symbol, variants in by_symbol.items():
        print(f"\n  {symbol}:")
        for v in variants:
            overrides_str = ", ".join(f"{k}={val}" for k, val in v["overrides"].items()) if v["overrides"] else "(no changes)"
            print(f"    {v['variant']:20s} -> {overrides_str}")

    print(f"\n  Output: {ABLATION_DIR}")

    if args.run:
        print("\n  To run ablation replays, use:")
        print(f"    python -m argus_flow.backtest.replay --configs {ABLATION_DIR}/*.json")
        print("  Then compare results across variants to identify which features drive edge.")

    print("\n  Analysis guide:")
    print("    1. Run all variants through replay with identical historical data")
    print("    2. Compare PnL, win rate, profit factor across variants")
    print("    3. If 'no_range_pct' PnL ~= baseline -> range_pct is NOT the signal")
    print("    4. If 'session_only' PnL ~= baseline -> strategy is session+location, not range_accel")
    print("    5. The variant with the biggest PnL drop identifies the key signal component")


if __name__ == "__main__":
    main()
