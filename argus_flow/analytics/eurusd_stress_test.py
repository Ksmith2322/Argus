"""EUR/USD Stress Tests — Full validation suite for viable configs.

Tests:
1. Entry delay simulation (1-3 bar delay)
2. Session breakdown (where do wins/losses cluster?)
3. Wider fee sensitivity (what if fees are higher than expected?)
4. Direction breakdown (long vs short performance)
5. Temporal stability (first half vs second half)
6. Parameter sensitivity (nearby configs stable or cliff?)
7. Drawdown analysis (max consecutive losses, worst streak)

Usage:
    python -m argus_flow.analytics.eurusd_stress_test
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from argus_flow.analytics.eurusd_payoff_test import load_and_prepare, find_signals, simulate_trades


def run_delay_test(df, signals, stop_pct, target_pct, max_hold):
    """Test if edge survives 1-3 bar entry delay."""
    print(f"\n{'='*70}")
    print("TEST 1: ENTRY DELAY SIMULATION")
    print(f"{'='*70}")

    print(f"  {'delay':>6} {'n':>5} {'WR':>6} {'tgt%':>6} {'stp%':>6} {'tmo%':>6} {'exp(pip)':>9} {'verdict':>8}")
    print(f"  {'-'*60}")

    results = {}
    for delay in [0, 1, 2, 3]:
        delayed_signals = pd.Index([s + delay for s in signals if s + delay < len(df)])
        rdf = simulate_trades(df, delayed_signals, stop_pct, target_pct, max_hold)
        if rdf.empty:
            continue

        wr = (rdf["net_pnl_pct"] > 0).mean()
        tgt = (rdf["outcome"] == "target").mean()
        stp = (rdf["outcome"] == "stop").mean()
        tmo = (rdf["outcome"] == "timeout").mean()
        exp = rdf["net_pnl_pct"].mean() * 10000
        v = "VIABLE" if exp > 0 else "dead"

        print(f"  {delay:>5}m {len(rdf):>5} {wr:>6.3f} {tgt:>5.1%} {stp:>5.1%} {tmo:>5.1%} {exp:>+8.2f}p {v:>8}")
        results[delay] = exp

    if 0 in results and 3 in results:
        decay = ((results[0] - results[3]) / abs(results[0]) * 100) if results[0] != 0 else 0
        print(f"\n  Decay 0->3 bars: {decay:.1f}%")
        if results[3] > 0:
            print("  Edge SURVIVES delay")
        else:
            print("  Edge DIES with delay")

    return results


def run_session_breakdown(df, signals, stop_pct, target_pct, max_hold):
    """Break down performance by session."""
    print(f"\n{'='*70}")
    print("TEST 2: SESSION BREAKDOWN")
    print(f"{'='*70}")

    rdf = simulate_trades(df, signals, stop_pct, target_pct, max_hold)
    rdf["hour"] = [int(df.at[idx, "hour"]) for idx in rdf["idx"]]
    rdf["session"] = np.select(
        [rdf["hour"].between(0, 7), rdf["hour"].between(8, 12),
         rdf["hour"].between(13, 16), rdf["hour"].between(17, 23)],
        ["asia", "london", "ny", "us_evening"],
        default="other",
    )

    print(f"  {'session':>12} {'n':>5} {'WR':>6} {'exp(pip)':>9} {'verdict':>8}")
    print(f"  {'-'*45}")

    for sess in ["asia", "london", "ny", "us_evening"]:
        sub = rdf[rdf["session"] == sess]
        if len(sub) == 0:
            continue
        wr = (sub["net_pnl_pct"] > 0).mean()
        exp = sub["net_pnl_pct"].mean() * 10000
        v = "VIABLE" if exp > 0 else "dead"
        print(f"  {sess:>12} {len(sub):>5} {wr:>6.3f} {exp:>+8.2f}p {v:>8}")

    return rdf


def run_fee_sensitivity(df, signals, stop_pct, target_pct, max_hold):
    """Test edge survival under different fee assumptions."""
    print(f"\n{'='*70}")
    print("TEST 3: FEE SENSITIVITY")
    print(f"{'='*70}")

    print(f"  {'fee_rt':>8} {'n':>5} {'WR':>6} {'exp(pip)':>9} {'verdict':>8}")
    print(f"  {'-'*42}")

    for fee_rt in [0.00001, 0.00002, 0.00005, 0.0001, 0.0002, 0.0005]:
        rdf = simulate_trades(df, signals, stop_pct, target_pct, max_hold, fee_rt=fee_rt)
        if rdf.empty:
            continue
        wr = (rdf["net_pnl_pct"] > 0).mean()
        exp = rdf["net_pnl_pct"].mean() * 10000
        fee_bps = fee_rt * 10000
        v = "VIABLE" if exp > 0 else "dead"
        print(f"  {fee_bps:>6.1f}bps {len(rdf):>5} {wr:>6.3f} {exp:>+8.2f}p {v:>8}")


def run_direction_breakdown(df, signals, stop_pct, target_pct, max_hold):
    """Break down long vs short performance."""
    print(f"\n{'='*70}")
    print("TEST 4: DIRECTION BREAKDOWN")
    print(f"{'='*70}")

    rdf = simulate_trades(df, signals, stop_pct, target_pct, max_hold)

    print(f"  {'direction':>10} {'n':>5} {'WR':>6} {'tgt%':>6} {'stp%':>6} {'exp(pip)':>9} {'verdict':>8}")
    print(f"  {'-'*55}")

    for d in ["long", "short"]:
        sub = rdf[rdf["direction"] == d]
        if len(sub) == 0:
            continue
        wr = (sub["net_pnl_pct"] > 0).mean()
        tgt = (sub["outcome"] == "target").mean()
        stp = (sub["outcome"] == "stop").mean()
        exp = sub["net_pnl_pct"].mean() * 10000
        v = "VIABLE" if exp > 0 else "dead"
        print(f"  {d:>10} {len(sub):>5} {wr:>6.3f} {tgt:>5.1%} {stp:>5.1%} {exp:>+8.2f}p {v:>8}")


def run_temporal_stability(df, signals, stop_pct, target_pct, max_hold):
    """First half vs second half of dataset."""
    print(f"\n{'='*70}")
    print("TEST 5: TEMPORAL STABILITY (first half vs second half)")
    print(f"{'='*70}")

    mid = len(df) // 2
    first_half = pd.Index([s for s in signals if s < mid])
    second_half = pd.Index([s for s in signals if s >= mid])

    print(f"  {'period':>12} {'n':>5} {'WR':>6} {'exp(pip)':>9} {'verdict':>8}")
    print(f"  {'-'*45}")

    for label, sigs in [("first_half", first_half), ("second_half", second_half)]:
        rdf = simulate_trades(df, sigs, stop_pct, target_pct, max_hold)
        if rdf.empty:
            continue
        wr = (rdf["net_pnl_pct"] > 0).mean()
        exp = rdf["net_pnl_pct"].mean() * 10000
        v = "VIABLE" if exp > 0 else "dead"
        print(f"  {label:>12} {len(rdf):>5} {wr:>6.3f} {exp:>+8.2f}p {v:>8}")


def run_parameter_sensitivity(df, signals_fn, base_stop, base_target, base_hold):
    """Test nearby parameter configs for cliff behavior."""
    print(f"\n{'='*70}")
    print("TEST 6: PARAMETER SENSITIVITY (nearby configs)")
    print(f"{'='*70}")

    # Vary each parameter independently
    stops = [base_stop * 0.75, base_stop, base_stop * 1.25, base_stop * 1.5]
    targets = [base_target * 0.75, base_target, base_target * 1.25, base_target * 1.5]
    holds = [max(10, base_hold // 2), base_hold, int(base_hold * 1.5), base_hold * 2]

    print(f"\n  Varying stop (target={base_target*10000:.0f}p, hold={base_hold}m):")
    print(f"  {'stop':>6} {'n':>5} {'WR':>6} {'exp(pip)':>9}")
    print(f"  {'-'*30}")
    for s in stops:
        signals = signals_fn()
        rdf = simulate_trades(df, signals, s, base_target, base_hold)
        if rdf.empty:
            continue
        exp = rdf["net_pnl_pct"].mean() * 10000
        wr = (rdf["net_pnl_pct"] > 0).mean()
        print(f"  {s*10000:>5.0f}p {len(rdf):>5} {wr:>6.3f} {exp:>+8.2f}p")

    print(f"\n  Varying target (stop={base_stop*10000:.0f}p, hold={base_hold}m):")
    print(f"  {'target':>6} {'n':>5} {'WR':>6} {'exp(pip)':>9}")
    print(f"  {'-'*30}")
    for t in targets:
        signals = signals_fn()
        rdf = simulate_trades(df, signals, base_stop, t, base_hold)
        if rdf.empty:
            continue
        exp = rdf["net_pnl_pct"].mean() * 10000
        wr = (rdf["net_pnl_pct"] > 0).mean()
        print(f"  {t*10000:>5.0f}p {len(rdf):>5} {wr:>6.3f} {exp:>+8.2f}p")

    print(f"\n  Varying hold (stop={base_stop*10000:.0f}p, target={base_target*10000:.0f}p):")
    print(f"  {'hold':>6} {'n':>5} {'WR':>6} {'exp(pip)':>9}")
    print(f"  {'-'*30}")
    for h in holds:
        signals = signals_fn()
        rdf = simulate_trades(df, signals, base_stop, base_target, h)
        if rdf.empty:
            continue
        exp = rdf["net_pnl_pct"].mean() * 10000
        wr = (rdf["net_pnl_pct"] > 0).mean()
        print(f"  {h:>5}m {len(rdf):>5} {wr:>6.3f} {exp:>+8.2f}p")


def run_drawdown_analysis(df, signals, stop_pct, target_pct, max_hold):
    """Analyze worst-case drawdown and losing streaks."""
    print(f"\n{'='*70}")
    print("TEST 7: DRAWDOWN & STREAK ANALYSIS")
    print(f"{'='*70}")

    rdf = simulate_trades(df, signals, stop_pct, target_pct, max_hold)
    pnls = rdf["net_pnl_pct"].values

    # Consecutive losses
    max_consec_loss = 0
    current_streak = 0
    streaks = []
    for p in pnls:
        if p <= 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            if current_streak > 0:
                streaks.append(current_streak)
            current_streak = 0
    if current_streak > 0:
        streaks.append(current_streak)

    # Cumulative PnL curve
    cum_pnl = np.cumsum(pnls) * 10000  # in pips
    peak = np.maximum.accumulate(cum_pnl)
    drawdown = cum_pnl - peak
    max_dd = drawdown.min()

    print(f"  Total trades: {len(pnls)}")
    print(f"  Total PnL: {cum_pnl[-1]:+.1f} pips")
    print(f"  Max consecutive losses: {max_consec_loss}")
    print(f"  Avg losing streak: {np.mean(streaks):.1f}" if streaks else "  No losing streaks")
    print(f"  Max drawdown: {max_dd:.1f} pips")
    print(f"  Peak PnL: {peak[-1]:.1f} pips")

    # Worst 5 trades
    worst_idx = np.argsort(pnls)[:5]
    print(f"\n  Worst 5 trades (pips):")
    for i in worst_idx:
        print(f"    Trade {i}: {pnls[i]*10000:+.2f}p ({rdf.iloc[i]['outcome']}, {rdf.iloc[i]['direction']})")


def main():
    csv_path = os.environ.get("EURUSD_CSV", "argus_flow/data/ibkr_eurusd_1m_extended.csv")
    df = load_and_prepare(csv_path)
    n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400

    print(f"Loaded {len(df):,} bars ({n_days:.1f} days)")

    # Best config: T4 full stack, 20p stop, 40p target, 60m hold
    rule_t4 = lambda r: r["range_pct"] >= 0.0012 and r["range_accel"] > 0 and r["vol_z"] > 0 and 8 <= r["hour"] <= 19
    stop_pct = 0.0020
    target_pct = 0.0040
    max_hold = 60

    signals = find_signals(df, rule_t4)
    print(f"\nBest config: T4 full stack | SL=20p TP=40p Hold=60m")
    print(f"Signals: {len(signals)} ({len(signals)/n_days:.1f}/day)")

    # Run all stress tests
    run_delay_test(df, signals, stop_pct, target_pct, max_hold)
    run_session_breakdown(df, signals, stop_pct, target_pct, max_hold)
    run_fee_sensitivity(df, signals, stop_pct, target_pct, max_hold)
    run_direction_breakdown(df, signals, stop_pct, target_pct, max_hold)
    run_temporal_stability(df, signals, stop_pct, target_pct, max_hold)
    run_parameter_sensitivity(df, lambda: find_signals(df, rule_t4), stop_pct, target_pct, max_hold)
    run_drawdown_analysis(df, signals, stop_pct, target_pct, max_hold)

    # Also test T4 with 15p/40p/60m (second best)
    print(f"\n\n{'#'*70}")
    print("SECOND CONFIG: T4 full stack | SL=15p TP=40p Hold=60m")
    print(f"{'#'*70}")
    run_delay_test(df, signals, 0.0015, 0.0040, 60)
    run_fee_sensitivity(df, signals, 0.0015, 0.0040, 60)
    run_drawdown_analysis(df, signals, 0.0015, 0.0040, 60)

    print(f"\n\n{'='*70}")
    print("ALL STRESS TESTS COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()