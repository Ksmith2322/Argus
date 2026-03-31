"""Run a 9-config parameter sweep over the replay engine.

Tests stability of edge across nearby configurations.
What matters is the SHAPE of the neighborhood, not the best run.

Usage:
    python -m argus_flow.analytics.run_sweep --bars argus_flow/replay_out/bars_1s.csv
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.analytics.replay_breakout_flow import (
    ReplayConfig,
    load_bars,
    add_features,
    detect_events,
    add_time_features,
    build_summary,
)

# 3x3 grid: compression_threshold_pct × flow_confirm_delta_z
DEFAULT_COMPRESSION_VALUES = [0.0025, 0.0035, 0.0050]
DEFAULT_DELTA_Z_VALUES = [0.8, 1.0, 1.5]

# Kraken taker fee (0.4%) + 5bps slippage = 45bps one-way
DEFAULT_FEE_BPS = 20.0    # 0.4% / 2 sides → 20bps per side
DEFAULT_SLIPPAGE_BPS = 5.0


def _parse_float_list(raw: str | None, default: list[float]) -> list[float]:
    if raw is None:
        return list(default)

    values = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        values.append(float(token))

    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def run_sweep(
    bars_path: str,
    outdir: str = "argus_flow/replay_out",
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    compression_values: list[float] | None = None,
    delta_z_values: list[float] | None = None,
) -> dict:
    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)

    bars = load_bars(Path(bars_path))
    print(f"Loaded {len(bars):,} bars\n")

    compression_values = list(compression_values or DEFAULT_COMPRESSION_VALUES)
    delta_z_values = list(delta_z_values or DEFAULT_DELTA_Z_VALUES)

    results = []
    combos = list(itertools.product(compression_values, delta_z_values))

    for i, (comp, dz) in enumerate(combos, 1):
        label = f"comp={comp}_dz={dz}"
        print(f"[{i}/{len(combos)}] {label} ...", end=" ", flush=True)

        cfg = ReplayConfig(
            compression_threshold_pct=comp,
            flow_confirm_delta_z=dz,
        )

        featured = add_features(bars, cfg)
        events = detect_events(featured, cfg)
        if not events.empty:
            events = add_time_features(events)
        summary = build_summary(events, fee_bps, slippage_bps)

        # Save per-run events
        run_dir = outdir_path / label.replace("=", "").replace(" ", "")
        run_dir.mkdir(parents=True, exist_ok=True)
        events.to_csv(run_dir / "breakout_events.csv", index=False)
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

        exp = summary.get("expectancy", {})
        row = {
            "compression_threshold_pct": comp,
            "flow_confirm_delta_z": dz,
            "event_count": summary.get("event_count", 0),
            "false_breakout_rate": summary.get("false_breakout_rate", None),
            "avg_mfe_pct": summary.get("avg_mfe_pct", None),
            "avg_mae_pct": summary.get("avg_mae_pct", None),
            "median_mfe_pct": summary.get("median_mfe_pct", None),
            "median_mae_pct": summary.get("median_mae_pct", None),
            "avg_mfe_mae_ratio": summary.get("avg_mfe_mae_ratio", None),
            "expectancy_pct": exp.get("expectancy_pct", None),
            "win_rate": exp.get("win_rate", None),
        }
        results.append(row)
        print(
            f"events={row['event_count']} "
            f"fbr={row['false_breakout_rate']:.3f} "
            f"exp={row['expectancy_pct']:.6f}"
            if row["expectancy_pct"] is not None
            else f"events={row['event_count']} NO DATA"
        )

    # Save combined results
    df = pd.DataFrame(results)
    df.to_csv(outdir_path / "sweep_results.csv", index=False)

    sweep_summary = {
        "sweep_count": len(results),
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "grid": {
            "compression_values": compression_values,
            "delta_z_values": delta_z_values,
        },
        "runs": results,
    }
    (outdir_path / "sweep_summary.json").write_text(json.dumps(sweep_summary, indent=2))

    # Print comparison table
    print("\n" + "=" * 100)
    print(f"{'comp':>8} {'dz':>6} {'events':>7} {'fbr':>7} {'mfe%':>9} {'mae%':>9} "
          f"{'med_mfe':>9} {'med_mae':>9} {'ratio':>7} {'exp%':>10} {'WR':>6}")
    print("-" * 100)
    for r in results:
        print(
            f"{r['compression_threshold_pct']:>8.4f} "
            f"{r['flow_confirm_delta_z']:>6.1f} "
            f"{r['event_count']:>7} "
            f"{r['false_breakout_rate']:>7.3f} "
            f"{r['avg_mfe_pct']:>9.6f} "
            f"{r['avg_mae_pct']:>9.6f} "
            f"{r['median_mfe_pct']:>9.6f} "
            f"{r['median_mae_pct']:>9.6f} "
            f"{r['avg_mfe_mae_ratio']:>7.2f} "
            f"{r['expectancy_pct']:>10.6f} "
            f"{r['win_rate']:>6.3f}"
            if r["expectancy_pct"] is not None
            else f"{r['compression_threshold_pct']:>8.4f} "
                 f"{r['flow_confirm_delta_z']:>6.1f} "
                 f"{r['event_count']:>7} NO DATA"
        )
    print("=" * 100)

    print(f"\nSaved: {outdir_path / 'sweep_results.csv'}")
    print(f"Saved: {outdir_path / 'sweep_summary.json'}")

    return sweep_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 9-sweep parameter grid")
    parser.add_argument("--bars", required=True, help="Path to bars CSV")
    parser.add_argument("--outdir", default="argus_flow/replay_out", help="Output directory")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument(
        "--compression-values",
        default=None,
        help="Comma-separated compression_threshold_pct values",
    )
    parser.add_argument(
        "--delta-z-values",
        default=None,
        help="Comma-separated flow_confirm_delta_z values",
    )
    args = parser.parse_args()

    run_sweep(
        args.bars,
        args.outdir,
        args.fee_bps,
        args.slippage_bps,
        compression_values=_parse_float_list(args.compression_values, DEFAULT_COMPRESSION_VALUES),
        delta_z_values=_parse_float_list(args.delta_z_values, DEFAULT_DELTA_Z_VALUES),
    )


if __name__ == "__main__":
    main()
