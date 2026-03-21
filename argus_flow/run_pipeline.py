"""End-to-end Cascade pipeline: validate → bars → baseline → sweep → classify.

One command to go from raw trades to edge verdict.

Usage:
    python -m argus_flow.run_pipeline --trades argus_flow/data/kraken_btc_trades.csv
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PYTHON = sys.executable


def _run(label: str, cmd: list[str]) -> bool:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")
    print(f"  cmd: {' '.join(cmd)}\n")

    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[1]))
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"\n  FAILED ({elapsed:.1f}s)")
        return False
    print(f"\n  OK ({elapsed:.1f}s)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Full Cascade pipeline")
    parser.add_argument("--trades", required=True, help="Path to raw trades CSV")
    parser.add_argument("--outdir", default="argus_flow/replay_out", help="Output directory")
    parser.add_argument("--fee-bps", type=float, default=20.0, help="One-way fee bps (default: 20)")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="One-way slippage bps (default: 5)")
    parser.add_argument("--skip-validate", action="store_true", help="Skip validation step")
    args = parser.parse_args()

    trades_path = Path(args.trades)
    outdir = Path(args.outdir)
    bars_path = outdir / "bars_1s.csv"

    if not trades_path.exists():
        print(f"ERROR: Trades file not found: {trades_path}")
        sys.exit(1)

    t_start = time.time()

    # Step 1: Validate
    if not args.skip_validate:
        ok = _run("Validate trades", [
            PYTHON, "-m", "argus_flow.capture.validate_trades",
            "--trades", str(trades_path),
        ])
        if not ok:
            print("\nPipeline ABORTED at validation.")
            sys.exit(1)

    # Step 2: Build bars
    ok = _run("Build 1s bars", [
        PYTHON, "-m", "argus_flow.capture.build_bars_from_trades",
        "--trades", str(trades_path),
        "--output", str(bars_path),
        "--bar-seconds", "1",
    ])
    if not ok:
        print("\nPipeline ABORTED at bar construction.")
        sys.exit(1)

    # Step 3: Baseline replay
    baseline_dir = outdir / "baseline"
    ok = _run("Baseline replay (default config)", [
        PYTHON, "-m", "argus_flow.analytics.replay_breakout_flow",
        "--bars", str(bars_path),
        "--outdir", str(baseline_dir),
        "--fee-bps", str(args.fee_bps),
        "--slippage-bps", str(args.slippage_bps),
    ])
    if not ok:
        print("\nPipeline ABORTED at baseline replay.")
        sys.exit(1)

    # Step 4: 9-sweep grid
    ok = _run("Parameter sweep (3x3 grid)", [
        PYTHON, "-m", "argus_flow.analytics.run_sweep",
        "--bars", str(bars_path),
        "--outdir", str(outdir),
        "--fee-bps", str(args.fee_bps),
        "--slippage-bps", str(args.slippage_bps),
    ])
    if not ok:
        print("\nPipeline ABORTED at sweep.")
        sys.exit(1)

    # Step 5: Classify edge
    ok = _run("Edge classification", [
        PYTHON, "-m", "argus_flow.analytics.classify_edge",
        "--input", str(outdir / "sweep_summary.json"),
    ])
    if not ok:
        print("\nPipeline ABORTED at classification.")
        sys.exit(1)

    # Step 6: Delay simulation (using baseline events)
    baseline_events = baseline_dir / "breakout_events.csv"
    baseline_bars = baseline_dir / "bars_with_features.csv"
    if baseline_events.exists() and baseline_bars.exists():
        _run("Entry delay simulation", [
            PYTHON, "-m", "argus_flow.analytics.delay_simulator",
            "--events", str(baseline_events),
            "--bars", str(baseline_bars),
            "--outdir", str(outdir),
            "--fee-bps", str(args.fee_bps),
            "--slippage-bps", str(args.slippage_bps),
        ])

    # Final summary
    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE ({elapsed:.1f}s)")
    print(f"{'='*60}")

    # Print verdict
    edge_file = outdir / "edge_classification.json"
    if edge_file.exists():
        edge = json.loads(edge_file.read_text())
        cls = edge.get("classification", "UNKNOWN")
        print(f"\nVERDICT: {cls}")
        print(f"Reason: {edge.get('reason', '')}")

    print(f"\nAll outputs in: {outdir}/")
    print(f"Key files:")
    print(f"  Baseline:       {baseline_dir / 'summary.json'}")
    print(f"  Sweep:          {outdir / 'sweep_summary.json'}")
    print(f"  Classification: {edge_file}")
    print(f"  Delay impact:   {outdir / 'delay_summary.json'}")


if __name__ == "__main__":
    main()