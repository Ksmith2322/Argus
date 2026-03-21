"""Simulate entry delays to test if edge survives slower execution.

Critical for Kraken validation — if the edge dies with 1-3 bar delay,
it won't survive real execution latency.

Usage:
    python -m argus_flow.analytics.delay_simulator \
        --events argus_flow/replay_out/breakout_events.csv \
        --bars argus_flow/replay_out/bars_with_features.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


DELAY_BARS = [0, 1, 2, 3, 5]

# Kraken fees
DEFAULT_FEE_BPS = 20.0
DEFAULT_SLIPPAGE_BPS = 5.0


def simulate_delays(
    events_path: str,
    bars_path: str,
    outdir: str = "argus_flow/replay_out",
    forward_look: int = 30,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    events = pd.read_csv(events_path)
    bars = pd.read_csv(bars_path)

    if events.empty:
        print("No events to simulate.")
        return {}

    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)

    total_cost = (fee_bps + slippage_bps) * 2 / 10000.0
    results = []

    for delay in DELAY_BARS:
        delayed_records = []

        for _, ev in events.iterrows():
            entry_idx = int(ev["entry_idx"])
            delayed_idx = entry_idx + delay
            direction = ev["direction"]

            if delayed_idx >= len(bars) - 1:
                continue

            delayed_entry_px = float(bars.at[delayed_idx, "close"])
            end_idx = min(delayed_idx + forward_look, len(bars) - 1)
            window = bars.iloc[delayed_idx + 1 : end_idx + 1]

            if window.empty:
                continue

            if direction == "long":
                mfe = float((window["high"] - delayed_entry_px).max())
                mae = float((delayed_entry_px - window["low"]).max())
            else:
                mfe = float((delayed_entry_px - window["low"]).max())
                mae = float((window["high"] - delayed_entry_px).max())

            mfe_pct = mfe / delayed_entry_px if delayed_entry_px else 0
            mae_pct = mae / delayed_entry_px if delayed_entry_px else 0

            delayed_records.append({
                "ts": ev["ts"],
                "direction": direction,
                "original_entry_px": ev["entry_px"],
                "delayed_entry_px": delayed_entry_px,
                "delay_bars": delay,
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "mfe_mae_ratio": mfe_pct / mae_pct if mae_pct > 0 else np.nan,
                "false_breakout": mfe_pct < mae_pct,
            })

        df = pd.DataFrame(delayed_records)
        if df.empty:
            results.append({
                "delay_bars": delay,
                "event_count": 0,
            })
            continue

        wins = df["mfe_pct"] > df["mae_pct"]
        win_rate = float(wins.mean())
        avg_win = float(df.loc[wins, "mfe_pct"].mean()) if wins.any() else 0
        avg_loss = float(df.loc[~wins, "mae_pct"].mean()) if (~wins).any() else 0
        exp = (avg_win * win_rate) - (avg_loss * (1 - win_rate)) - total_cost

        row = {
            "delay_bars": delay,
            "event_count": len(df),
            "false_breakout_rate": float(df["false_breakout"].mean()),
            "avg_mfe_pct": float(df["mfe_pct"].mean()),
            "avg_mae_pct": float(df["mae_pct"].mean()),
            "median_mfe_pct": float(df["mfe_pct"].median()),
            "median_mae_pct": float(df["mae_pct"].median()),
            "win_rate": win_rate,
            "expectancy_pct": exp,
        }
        results.append(row)

        # Save per-delay detail
        df.to_csv(outdir_path / f"delay_{delay}_events.csv", index=False)

    # Summary
    summary = {
        "delays_tested": DELAY_BARS,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "results": results,
    }

    (outdir_path / "delay_summary.json").write_text(json.dumps(summary, indent=2))

    results_df = pd.DataFrame(results)
    results_df.to_csv(outdir_path / "delay_impact.csv", index=False)

    # Print table
    print(f"\n{'='*80}")
    print(f"ENTRY DELAY IMPACT (Kraken execution simulation)")
    print(f"{'='*80}")
    print(f"{'delay':>6} {'events':>7} {'fbr':>7} {'mfe%':>9} {'mae%':>9} "
          f"{'WR':>6} {'exp%':>10}")
    print("-" * 80)
    for r in results:
        if r.get("expectancy_pct") is not None:
            print(
                f"{r['delay_bars']:>6} "
                f"{r['event_count']:>7} "
                f"{r['false_breakout_rate']:>7.3f} "
                f"{r['avg_mfe_pct']:>9.6f} "
                f"{r['avg_mae_pct']:>9.6f} "
                f"{r['win_rate']:>6.3f} "
                f"{r['expectancy_pct']:>10.6f}"
            )
        else:
            print(f"{r['delay_bars']:>6} {r['event_count']:>7} NO DATA")

    # Key verdict
    if len(results) >= 2 and results[0].get("expectancy_pct") and results[-1].get("expectancy_pct"):
        exp_0 = results[0]["expectancy_pct"]
        exp_last = results[-1]["expectancy_pct"]
        decay = (exp_0 - exp_last) / abs(exp_0) * 100 if exp_0 != 0 else 0
        print(f"\nExpectancy decay 0→{DELAY_BARS[-1]} bars: {decay:.1f}%")
        if exp_last > 0:
            print("Edge SURVIVES delay — Kraken execution viable")
        elif exp_0 > 0:
            print("Edge DIES with delay — may not survive Kraken latency")
        else:
            print("Edge negative even at 0 delay")

    print(f"\nSaved: {outdir_path / 'delay_impact.csv'}")
    print(f"Saved: {outdir_path / 'delay_summary.json'}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate entry delays")
    parser.add_argument("--events", required=True, help="Path to breakout_events.csv")
    parser.add_argument("--bars", required=True, help="Path to bars_with_features.csv")
    parser.add_argument("--outdir", default="argus_flow/replay_out")
    parser.add_argument("--forward-look", type=int, default=30)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    args = parser.parse_args()

    simulate_delays(
        args.events, args.bars, args.outdir,
        args.forward_look, args.fee_bps, args.slippage_bps,
    )


if __name__ == "__main__":
    main()