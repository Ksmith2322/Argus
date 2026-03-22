"""EUR/USD Payoff-First Strategy Test.

Phase 4 (trigger rules) + Phase 5 (payoff test) for EUR/USD via IBKR.
Uses precursor findings: range_pct, range_accel, vol_z, session, position.

Usage:
    python -m argus_flow.analytics.eurusd_payoff_test
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def load_and_prepare(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["bar_range"] = df["high"] - df["low"]

    lookback = 30
    ctx_win = 240

    df["ctx_bar_range_mean"] = df["bar_range"].rolling(ctx_win, min_periods=60).mean()
    df["pre_bar_range_mean"] = df["bar_range"].rolling(lookback, min_periods=lookback).mean()
    df["vol_z"] = (df["pre_bar_range_mean"] - df["ctx_bar_range_mean"]) / df["ctx_bar_range_mean"]

    df["pre_high"] = df["high"].rolling(lookback, min_periods=lookback).max()
    df["pre_low"] = df["low"].rolling(lookback, min_periods=lookback).min()
    df["range_pct"] = (df["pre_high"] - df["pre_low"]) / df["close"]

    df["bar_range_first15"] = df["bar_range"].shift(15).rolling(15, min_periods=15).mean()
    df["bar_range_last15"] = df["bar_range"].rolling(15, min_periods=15).mean()
    df["range_accel"] = (df["bar_range_last15"] - df["bar_range_first15"]) / df["bar_range_first15"]

    df["ctx_high"] = df["high"].rolling(ctx_win, min_periods=60).max()
    df["ctx_low"] = df["low"].rolling(ctx_win, min_periods=60).min()
    df["ctx_range"] = df["ctx_high"] - df["ctx_low"]
    df["dist_from_low"] = np.where(
        df["ctx_range"] > 0,
        (df["close"] - df["ctx_low"]) / df["ctx_range"],
        0.5,
    )

    df["hour"] = df["ts"].dt.hour
    return df


def find_signals(df: pd.DataFrame, rule_fn, min_gap: int = 15) -> pd.Index:
    valid = df.dropna(subset=["range_pct", "range_accel", "vol_z", "dist_from_low"])
    matches = valid[valid.apply(rule_fn, axis=1)]
    deduped = []
    next_allowed = 0
    for idx in matches.index:
        if idx >= next_allowed:
            deduped.append(idx)
            next_allowed = idx + min_gap
    return pd.Index(deduped)


def simulate_trades(
    df: pd.DataFrame,
    signal_indices: pd.Index,
    stop_pct: float,
    target_pct: float,
    max_hold: int,
    fee_rt: float = 0.00002,
) -> pd.DataFrame:
    results = []
    for idx in signal_indices:
        entry_px = float(df.at[idx, "close"])
        dist_low = float(df.at[idx, "dist_from_low"])

        direction = "long" if dist_low < 0.4 else ("short" if dist_low > 0.6 else "long")

        if direction == "long":
            stop_px = entry_px * (1 - stop_pct)
            target_px = entry_px * (1 + target_pct)
        else:
            stop_px = entry_px * (1 + stop_pct)
            target_px = entry_px * (1 - target_pct)

        outcome = "timeout"
        exit_px = entry_px
        exit_bar = max_hold

        for j in range(1, max_hold + 1):
            bi = idx + j
            if bi >= len(df):
                break
            bar = df.iloc[bi]
            if direction == "long":
                if bar["low"] <= stop_px:
                    outcome, exit_px, exit_bar = "stop", stop_px, j
                    break
                if bar["high"] >= target_px:
                    outcome, exit_px, exit_bar = "target", target_px, j
                    break
            else:
                if bar["high"] >= stop_px:
                    outcome, exit_px, exit_bar = "stop", stop_px, j
                    break
                if bar["low"] <= target_px:
                    outcome, exit_px, exit_bar = "target", target_px, j
                    break

        if outcome == "timeout":
            last_bi = min(idx + max_hold, len(df) - 1)
            exit_px = float(df.at[last_bi, "close"])

        if direction == "long":
            raw_pnl = (exit_px - entry_px) / entry_px
        else:
            raw_pnl = (entry_px - exit_px) / entry_px

        results.append({
            "idx": idx,
            "direction": direction,
            "outcome": outcome,
            "exit_bar": exit_bar,
            "raw_pnl_pct": raw_pnl,
            "net_pnl_pct": raw_pnl - fee_rt,
        })

    return pd.DataFrame(results)


def main():
    csv_path = os.environ.get("EURUSD_CSV", "argus_flow/data/ibkr_eurusd_1m_extended.csv")
    df = load_and_prepare(csv_path)
    n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400

    print(f"Loaded {len(df):,} bars ({n_days:.1f} days)")

    # Trigger rules
    triggers = {
        "T1: range+accel": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0,
        "T2: +session": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and 8 <= r["hour"] <= 19,
        "T3: +vol": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and r["vol_z"] > 0,
        "T4: full": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and r["vol_z"] > 0 and 8 <= r["hour"] <= 19,
        "T5: tight range": lambda r: r["range_pct"] <= 0.0008 and r["range_accel"] > 0.5 and r["vol_z"] > 0,
        "T6: range+pos": lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and (r["dist_from_low"] < 0.3 or r["dist_from_low"] > 0.7),
    }

    stop_targets = [
        (0.0008, 0.0015, 15),
        (0.0010, 0.0020, 15),
        (0.0010, 0.0020, 30),
        (0.0010, 0.0030, 30),
        (0.0015, 0.0020, 15),
        (0.0015, 0.0030, 30),
        (0.0015, 0.0040, 60),
        (0.0020, 0.0040, 30),
        (0.0020, 0.0040, 60),
    ]

    all_results = []

    for tname, rule in triggers.items():
        signals = find_signals(df, rule)
        print(f"\n{'='*80}")
        print(f"TRIGGER: {tname}")
        print(f"Signals: {len(signals)} ({len(signals)/n_days:.1f}/day)")
        print(f"{'='*80}")

        if len(signals) < 5:
            print("  Too few signals.")
            continue

        print(f"  {'SL':>5} {'TP':>5} {'Hold':>5} | {'n':>5} {'WR':>6} {'tgt%':>6} {'stp%':>6} {'tmo%':>6} | {'exp(pip)':>9} {'verdict':>8}")
        print(f"  {'-'*72}")

        for stop_pct, target_pct, max_hold in stop_targets:
            rdf = simulate_trades(df, signals, stop_pct, target_pct, max_hold)
            if rdf.empty:
                continue

            wr = (rdf["net_pnl_pct"] > 0).mean()
            tgt = (rdf["outcome"] == "target").mean()
            stp = (rdf["outcome"] == "stop").mean()
            tmo = (rdf["outcome"] == "timeout").mean()
            exp = rdf["net_pnl_pct"].mean()
            exp_pips = exp * 10000

            s_pips = stop_pct * 10000
            t_pips = target_pct * 10000
            verdict = "VIABLE" if exp > 0 else "dead"

            print(f"  {s_pips:>4.0f}p {t_pips:>4.0f}p {max_hold:>4}m | {len(rdf):>5} {wr:>6.3f} {tgt:>5.1%} {stp:>5.1%} {tmo:>5.1%} | {exp_pips:>+8.2f}p {verdict:>8}")

            all_results.append({
                "trigger": tname,
                "stop_pips": s_pips,
                "target_pips": t_pips,
                "max_hold_min": max_hold,
                "n_signals": len(rdf),
                "win_rate": wr,
                "target_rate": tgt,
                "stop_rate": stp,
                "timeout_rate": tmo,
                "expectancy_pips": exp_pips,
                "viable": exp > 0,
            })

    # Summary
    print(f"\n{'='*80}")
    print("OVERALL SUMMARY")
    print(f"{'='*80}")

    viable = [r for r in all_results if r["viable"]]
    total = len(all_results)

    print(f"Total configs tested: {total}")
    print(f"Viable (positive expectancy): {len(viable)}")

    if viable:
        print(f"\nTOP VIABLE CONFIGS (sorted by expectancy):")
        viable.sort(key=lambda x: x["expectancy_pips"], reverse=True)
        print(f"  {'Trigger':>20s} {'SL':>4} {'TP':>4} {'H':>3} {'n':>5} {'WR':>6} {'tgt%':>6} {'exp(pip)':>9}")
        print(f"  {'-'*62}")
        for r in viable[:15]:
            print(f"  {r['trigger']:>20s} {r['stop_pips']:>3.0f}p {r['target_pips']:>3.0f}p {r['max_hold_min']:>3}m {r['n_signals']:>5} {r['win_rate']:>6.3f} {r['target_rate']:>5.1%} {r['expectancy_pips']:>+8.2f}p")

        best = viable[0]
        daily_trades = best["n_signals"] / n_days
        annual_trades = daily_trades * 252  # trading days
        annual_pips = best["expectancy_pips"] * annual_trades
        # At $10/pip (mini lot), annual_pips * $10
        annual_usd_mini = annual_pips * 10
        annual_usd_standard = annual_pips * 100

        print(f"\nBEST CONFIG: {best['trigger']} SL={best['stop_pips']:.0f}p TP={best['target_pips']:.0f}p Hold={best['max_hold_min']}m")
        print(f"  Expectancy: {best['expectancy_pips']:+.2f} pips/trade")
        print(f"  Daily trades: {daily_trades:.1f}")
        print(f"  Annual trades: {annual_trades:.0f}")
        print(f"  Annual pips: {annual_pips:+.0f}")
        print(f"  Annual P&L (mini lot $10/pip): ${annual_usd_mini:+,.0f}")
        print(f"  Annual P&L (standard lot $100/pip): ${annual_usd_standard:+,.0f}")
    else:
        print("\nNO VIABLE CONFIGS FOUND.")

    # Save
    outdir = Path("argus_flow/replay_out")
    outdir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(all_results).to_csv(outdir / "eurusd_payoff_results.csv", index=False)
    (outdir / "eurusd_payoff_summary.json").write_text(json.dumps({
        "total_configs": total,
        "viable_configs": len(viable),
        "results": all_results,
    }, indent=2, default=str))
    print(f"\nSaved: {outdir / 'eurusd_payoff_results.csv'}")


if __name__ == "__main__":
    main()