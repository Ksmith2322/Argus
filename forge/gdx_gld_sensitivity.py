"""
GDX/GLD Pairs Sensitivity Analysis
===================================
Tests parameter robustness of the log-ratio pairs strategy.
If edge holds across a range of parameters, it's real. If it only
exists at one exact point, it's curve-fit.

Sweeps: z-entry, z-lookback, z-stop, z-exit (1D then 2D heatmap).

Usage:
    python -m forge.gdx_gld_sensitivity
"""

import sys
import itertools
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("Required: pip install pandas numpy yfinance")
    sys.exit(1)

from forge.gdx_gld_pairs import (
    download_data,
    compute_log_ratio_spread,
    Trade,
    COST_PER_SIDE_BPS,
)

# ---------------------------------------------------------------------------
# Default parameters (baseline from the original backtest)
# ---------------------------------------------------------------------------
DEFAULT_ENTRY = 2.0
DEFAULT_LOOKBACK = 60
DEFAULT_STOP = 3.0
DEFAULT_EXIT = 0.0

# Parameter grids
ENTRY_GRID = [1.5, 1.75, 2.0, 2.25, 2.5]
LOOKBACK_GRID = [30, 45, 60, 75, 90, 120]
STOP_GRID = [2.5, 3.0, 3.5, 4.0, None]  # None = no stop
EXIT_GRID = [0.0, 0.25, 0.5]


# ---------------------------------------------------------------------------
# Parameterised z-score + backtest
# ---------------------------------------------------------------------------

def compute_zscore(spread: pd.Series, lookback: int) -> pd.Series:
    """Rolling z-score with explicit lookback."""
    mean = spread.rolling(lookback).mean()
    std = spread.rolling(lookback).std()
    return (spread - mean) / std


def run_backtest_param(
    df: pd.DataFrame,
    spread: pd.Series,
    zscore: pd.Series,
    entry_z: float,
    exit_z: float,
    stop_z: Optional[float],
) -> List[Trade]:
    """Run pairs backtest with explicit parameter values."""
    trades: List[Trade] = []
    position: Optional[Trade] = None

    valid = zscore.dropna()
    if len(valid) == 0:
        return trades

    start_idx = valid.index[0]
    mask = df.index >= start_idx
    dates = df.index[mask]
    z_vals = zscore[mask]
    s_vals = spread[mask]

    for i in range(1, len(dates)):
        date = dates[i]
        z = z_vals.iloc[i]
        z_prev = z_vals.iloc[i - 1]
        s = s_vals.iloc[i]

        if pd.isna(z) or pd.isna(z_prev) or pd.isna(s):
            continue

        # --- Exits ---
        if position is not None:
            close = False
            reason = ""

            # Long spread entered at z < -entry -> exit when z rises to -exit_z
            # (exit_z=0 means exit at mean, exit_z=0.5 means exit 0.5 before mean)
            long_exit = -exit_z
            short_exit = exit_z
            if position.direction == "long_spread" and z_prev < long_exit <= z:
                close, reason = True, "mean_revert"
            elif position.direction == "long_spread" and z >= long_exit and z_prev < long_exit:
                close, reason = True, "mean_revert"
            # Short spread entered at z > +entry -> exit when z drops to +exit_z
            elif position.direction == "short_spread" and z_prev > short_exit >= z:
                close, reason = True, "mean_revert"
            elif position.direction == "short_spread" and z <= short_exit and z_prev > short_exit:
                close, reason = True, "mean_revert"

            if stop_z is not None:
                if position.direction == "long_spread" and z <= -stop_z:
                    close, reason = True, "stop"
                elif position.direction == "short_spread" and z >= stop_z:
                    close, reason = True, "stop"

            if close:
                position.exit_date = date.strftime("%Y-%m-%d")
                position.exit_zscore = z
                position.exit_spread = s
                if position.direction == "long_spread":
                    raw_pnl = s - position.entry_spread
                else:
                    raw_pnl = position.entry_spread - s
                denom = abs(position.entry_spread) if abs(position.entry_spread) > 0.01 else 1.0
                position.pnl_pct = (raw_pnl / denom) * 100.0 - 2 * COST_PER_SIDE_BPS / 100.0
                position.exit_reason = reason
                trades.append(position)
                position = None

        # --- Entries ---
        if position is None:
            if z_prev > -entry_z and z <= -entry_z:
                position = Trade(
                    entry_date=date.strftime("%Y-%m-%d"),
                    direction="long_spread",
                    entry_zscore=z,
                    entry_spread=s,
                )
            elif z_prev < entry_z and z >= entry_z:
                position = Trade(
                    entry_date=date.strftime("%Y-%m-%d"),
                    direction="short_spread",
                    entry_zscore=z,
                    entry_spread=s,
                )

    # Close open position at end
    if position is not None:
        position.exit_date = dates[-1].strftime("%Y-%m-%d")
        position.exit_zscore = z_vals.iloc[-1]
        position.exit_spread = s_vals.iloc[-1]
        if position.direction == "long_spread":
            raw_pnl = s_vals.iloc[-1] - position.entry_spread
        else:
            raw_pnl = position.entry_spread - s_vals.iloc[-1]
        denom = abs(position.entry_spread) if abs(position.entry_spread) > 0.01 else 1.0
        position.pnl_pct = (raw_pnl / denom) * 100.0 - 2 * COST_PER_SIDE_BPS / 100.0
        position.exit_reason = "end_of_data"
        trades.append(position)

    return trades


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    trades: int = 0
    win_rate: float = 0.0
    pf: float = 0.0
    avg_ret: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0


def calc_metrics(trades: List[Trade]) -> Metrics:
    """Compute summary metrics from a trade list."""
    m = Metrics()
    n = len(trades)
    m.trades = n
    if n == 0:
        return m

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    m.win_rate = len(wins) / n * 100
    m.avg_ret = float(np.mean(pnls))

    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.001
    m.pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    if n > 1:
        first = pd.Timestamp(trades[0].entry_date)
        last = pd.Timestamp(trades[-1].exit_date)
        years = max((last - first).days / 365.25, 1)
        tpy = n / years
        m.sharpe = (np.mean(pnls) / np.std(pnls, ddof=1)) * np.sqrt(max(tpy, 1))
    else:
        m.sharpe = 0.0

    cum = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum)
    dd = cum - running_max
    m.max_dd = float(np.min(dd)) if len(dd) > 0 else 0.0

    return m


# ---------------------------------------------------------------------------
# Sweep helpers
# ---------------------------------------------------------------------------

def sweep_1d(
    df: pd.DataFrame,
    spread: pd.Series,
    param_name: str,
    grid: list,
) -> List[Tuple]:
    """Sweep one parameter while holding others at default. Returns list of (value, Metrics)."""
    results = []
    # Pre-compute z-scores for each lookback we might need
    zscore_cache: Dict[int, pd.Series] = {}

    for val in grid:
        entry_z = DEFAULT_ENTRY
        lookback = DEFAULT_LOOKBACK
        stop_z = DEFAULT_STOP
        exit_z = DEFAULT_EXIT

        if param_name == "entry":
            entry_z = val
        elif param_name == "lookback":
            lookback = val
        elif param_name == "stop":
            stop_z = val
        elif param_name == "exit":
            exit_z = val

        if lookback not in zscore_cache:
            zscore_cache[lookback] = compute_zscore(spread, lookback)
        zs = zscore_cache[lookback]

        trades = run_backtest_param(df, spread, zs, entry_z, exit_z, stop_z)
        m = calc_metrics(trades)
        results.append((val, m))

    return results


def print_1d_table(param_name: str, results: List[Tuple]):
    """Print a 1D sweep table."""
    label_map = {
        "entry": "Z-Entry",
        "lookback": "Lookback",
        "stop": "Z-Stop",
        "exit": "Z-Exit",
    }
    label = label_map.get(param_name, param_name)

    print(f"\n{'='*78}")
    print(f"  1D Sweep: {label}")
    print(f"{'='*78}")
    print(f"  {'Value':>8} | {'Trades':>6} | {'WinRate':>7} | {'PF':>6} | {'AvgRet':>8} | {'Sharpe':>7} | {'MaxDD':>8}")
    print(f"  {'-'*72}")

    for val, m in results:
        val_str = f"{val}" if val is not None else "None"
        pf_str = f"{m.pf:.2f}" if m.pf < 100 else "Inf"
        print(f"  {val_str:>8} | {m.trades:>6} | {m.win_rate:>6.1f}% | {pf_str:>6} | {m.avg_ret:>+7.2f}% | {m.sharpe:>7.2f} | {m.max_dd:>+7.2f}%")


def sweep_2d(
    df: pd.DataFrame,
    spread: pd.Series,
    param1_name: str,
    param1_grid: list,
    param2_name: str,
    param2_grid: list,
) -> Dict[Tuple, Metrics]:
    """Sweep two parameters. Returns dict of (p1_val, p2_val) -> Metrics."""
    results = {}
    zscore_cache: Dict[int, pd.Series] = {}

    for p1, p2 in itertools.product(param1_grid, param2_grid):
        entry_z = DEFAULT_ENTRY
        lookback = DEFAULT_LOOKBACK
        stop_z = DEFAULT_STOP
        exit_z = DEFAULT_EXIT

        for name, val in [(param1_name, p1), (param2_name, p2)]:
            if name == "entry":
                entry_z = val
            elif name == "lookback":
                lookback = val
            elif name == "stop":
                stop_z = val
            elif name == "exit":
                exit_z = val

        if lookback not in zscore_cache:
            zscore_cache[lookback] = compute_zscore(spread, lookback)
        zs = zscore_cache[lookback]

        trades = run_backtest_param(df, spread, zs, entry_z, exit_z, stop_z)
        m = calc_metrics(trades)
        results[(p1, p2)] = m

    return results


def print_2d_heatmap(
    param1_name: str,
    param1_grid: list,
    param2_name: str,
    param2_grid: list,
    results: Dict[Tuple, Metrics],
    metric: str = "pf",
):
    """Print a text-based 2D heatmap of a metric."""
    label_map = {"entry": "Z-Entry", "lookback": "Lookback", "stop": "Z-Stop", "exit": "Z-Exit"}
    l1 = label_map.get(param1_name, param1_name)
    l2 = label_map.get(param2_name, param2_name)

    print(f"\n{'='*78}")
    print(f"  2D Heatmap: Profit Factor  ({l1} x {l2})")
    print(f"{'='*78}")

    # Column headers
    col_w = 8
    header = f"  {l1:>8} |"
    for p2 in param2_grid:
        p2s = f"{p2}" if p2 is not None else "None"
        header += f" {p2s:>{col_w}}"
    print(header)
    print(f"  {'-' * (10 + (col_w + 1) * len(param2_grid))}")

    for p1 in param1_grid:
        p1s = f"{p1}" if p1 is not None else "None"
        row = f"  {p1s:>8} |"
        for p2 in param2_grid:
            m = results.get((p1, p2), Metrics())
            val = getattr(m, metric) if metric != "pf" else m.pf
            if val >= 100:
                cell = "   Inf"
            elif metric == "pf":
                # Color coding via markers
                if val >= 1.5:
                    cell = f" {val:.2f}++"
                elif val >= 1.3:
                    cell = f"  {val:.2f}+"
                elif val >= 1.0:
                    cell = f"  {val:.2f} "
                else:
                    cell = f"  {val:.2f}-"
            else:
                cell = f" {val:>7.2f}"
            row += f" {cell:>{col_w}}"
        print(row)

    # Also print trade count heatmap
    print(f"\n  2D Heatmap: Trade Count  ({l1} x {l2})")
    print(f"  {'-' * (10 + (col_w + 1) * len(param2_grid))}")
    header = f"  {l1:>8} |"
    for p2 in param2_grid:
        p2s = f"{p2}" if p2 is not None else "None"
        header += f" {p2s:>{col_w}}"
    print(header)
    print(f"  {'-' * (10 + (col_w + 1) * len(param2_grid))}")

    for p1 in param1_grid:
        p1s = f"{p1}" if p1 is not None else "None"
        row = f"  {p1s:>8} |"
        for p2 in param2_grid:
            m = results.get((p1, p2), Metrics())
            row += f" {m.trades:>{col_w}}"
        print(row)


# ---------------------------------------------------------------------------
# Analysis: robust zone + optimal
# ---------------------------------------------------------------------------

def find_robust_zone(all_results: Dict[str, List[Tuple]]) -> Dict[str, list]:
    """Find parameter ranges where PF >= 1.3 (with 30+ trades)."""
    robust = {}
    for param_name, results in all_results.items():
        passing = [val for val, m in results if m.pf >= 1.3 and m.trades >= 30]
        robust[param_name] = passing
    return robust


def find_optimal(results_2d: Dict[Tuple, Metrics], p1_name: str, p2_name: str) -> Tuple:
    """Find highest PF combo with 50+ trades from 2D sweep."""
    best_pf = 0.0
    best_key = None
    best_m = None
    for key, m in results_2d.items():
        if m.trades >= 50 and m.pf > best_pf:
            best_pf = m.pf
            best_key = key
            best_m = m
    return best_key, best_m


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 78)
    print("  GDX/GLD Pairs — Parameter Sensitivity Analysis")
    print("  Testing robustness of log-ratio mean-reversion strategy")
    print("=" * 78)

    # Download data once
    df = download_data()
    lr = compute_log_ratio_spread(df)
    spread = lr["spread"]

    print(f"\n  Baseline: entry={DEFAULT_ENTRY}, lookback={DEFAULT_LOOKBACK}, "
          f"stop={DEFAULT_STOP}, exit={DEFAULT_EXIT}")

    # ------------------------------------------------------------------
    # Phase 1: 1D sweeps
    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("  PHASE 1: One-at-a-time parameter sweeps")
    print("=" * 78)

    sweeps_1d: Dict[str, List[Tuple]] = {}

    for name, grid in [
        ("entry", ENTRY_GRID),
        ("lookback", LOOKBACK_GRID),
        ("stop", STOP_GRID),
        ("exit", EXIT_GRID),
    ]:
        results = sweep_1d(df, spread, name, grid)
        sweeps_1d[name] = results
        print_1d_table(name, results)

    # ------------------------------------------------------------------
    # Rank parameters by impact (PF range across grid)
    # ------------------------------------------------------------------
    impacts = {}
    for name, results in sweeps_1d.items():
        pfs = [m.pf for _, m in results if m.trades >= 20]
        if pfs:
            impacts[name] = max(pfs) - min(pfs)
        else:
            impacts[name] = 0.0

    ranked = sorted(impacts.items(), key=lambda x: -x[1])
    print(f"\n{'='*78}")
    print(f"  Parameter Impact Ranking (PF range across 1D sweep)")
    print(f"{'='*78}")
    for name, impact in ranked:
        print(f"    {name:<12} PF range: {impact:.3f}")

    top2 = [name for name, _ in ranked[:2]]
    print(f"\n  Top 2 most impactful: {top2[0]}, {top2[1]}")

    # ------------------------------------------------------------------
    # Phase 2: 2D sweep on the two most impactful parameters
    # ------------------------------------------------------------------
    grid_map = {"entry": ENTRY_GRID, "lookback": LOOKBACK_GRID, "stop": STOP_GRID, "exit": EXIT_GRID}

    p1_name, p2_name = top2[0], top2[1]
    p1_grid, p2_grid = grid_map[p1_name], grid_map[p2_name]

    print(f"\n{'='*78}")
    print(f"  PHASE 2: 2D Sweep — {p1_name} x {p2_name}  ({len(p1_grid)}x{len(p2_grid)} = {len(p1_grid)*len(p2_grid)} combos)")
    print(f"{'='*78}")

    results_2d = sweep_2d(df, spread, p1_name, p1_grid, p2_name, p2_grid)
    print_2d_heatmap(p1_name, p1_grid, p2_name, p2_grid, results_2d)

    # ------------------------------------------------------------------
    # Phase 3: Robust zone
    # ------------------------------------------------------------------
    print(f"\n{'='*78}")
    print(f"  ROBUST ZONE (PF >= 1.30 with 30+ trades)")
    print(f"{'='*78}")

    robust = find_robust_zone(sweeps_1d)
    for name, vals in robust.items():
        if vals:
            vals_str = ", ".join(str(v) if v is not None else "None" for v in vals)
            pct = len(vals) / len(grid_map[name]) * 100
            print(f"    {name:<12} {vals_str}  ({pct:.0f}% of grid)")
        else:
            print(f"    {name:<12} NONE — edge does not survive any variation")

    # Robustness score: what fraction of the full 2D grid has PF >= 1.3 with 30+ trades?
    total_2d = len(results_2d)
    robust_2d = sum(1 for m in results_2d.values() if m.pf >= 1.3 and m.trades >= 30)
    robust_pct = robust_2d / total_2d * 100 if total_2d > 0 else 0

    print(f"\n    2D grid robustness: {robust_2d}/{total_2d} combos ({robust_pct:.0f}%) have PF >= 1.3 & 30+ trades")

    # ------------------------------------------------------------------
    # Phase 4: Optimal parameters
    # ------------------------------------------------------------------
    print(f"\n{'='*78}")
    print(f"  OPTIMAL PARAMETERS (highest PF with 50+ trades)")
    print(f"{'='*78}")

    best_key, best_m = find_optimal(results_2d, p1_name, p2_name)
    if best_key and best_m:
        print(f"    {p1_name}={best_key[0]}, {p2_name}={best_key[1]}")
        print(f"    Trades: {best_m.trades}  WinRate: {best_m.win_rate:.1f}%  "
              f"PF: {best_m.pf:.2f}  AvgRet: {best_m.avg_ret:+.2f}%  "
              f"Sharpe: {best_m.sharpe:.2f}  MaxDD: {best_m.max_dd:+.2f}%")
    else:
        print("    No combo met the 50-trade threshold.")

    # Also find best from 1D sweeps with 50+ trades
    print(f"\n  Best from each 1D sweep (50+ trades):")
    for name, results in sweeps_1d.items():
        eligible = [(v, m) for v, m in results if m.trades >= 50]
        if eligible:
            best_v, best_m_1d = max(eligible, key=lambda x: x[1].pf)
            print(f"    {name:<12} best={best_v}  PF={best_m_1d.pf:.2f}  trades={best_m_1d.trades}")
        else:
            eligible_any = [(v, m) for v, m in results if m.trades >= 30]
            if eligible_any:
                best_v, best_m_1d = max(eligible_any, key=lambda x: x[1].pf)
                print(f"    {name:<12} best={best_v}  PF={best_m_1d.pf:.2f}  trades={best_m_1d.trades} (relaxed to 30+)")
            else:
                print(f"    {name:<12} no combo with 30+ trades")

    # ------------------------------------------------------------------
    # Phase 5: Verdict
    # ------------------------------------------------------------------
    print(f"\n{'='*78}")
    print(f"  VERDICT: CURVE-FIT OR ROBUST?")
    print(f"{'='*78}")

    # Count how many 1D grid points have PF >= 1.3
    total_1d_points = sum(len(grid_map[n]) for n in sweeps_1d)
    robust_1d_points = sum(len(v) for v in robust.values())
    robust_1d_pct = robust_1d_points / total_1d_points * 100 if total_1d_points > 0 else 0

    # Check if baseline PF is within 20% of neighbors
    baseline_results = []
    for name, results in sweeps_1d.items():
        pfs_valid = [m.pf for v, m in results if m.trades >= 20]
        if pfs_valid:
            baseline_results.append((name, np.std(pfs_valid), np.mean(pfs_valid)))

    print(f"\n    1D robustness: {robust_1d_points}/{total_1d_points} individual param values have PF >= 1.3")
    print(f"    2D robustness: {robust_2d}/{total_2d} combos ({robust_pct:.0f}%) have PF >= 1.3")

    if robust_pct >= 60 and robust_1d_pct >= 60:
        verdict = "ROBUST — Edge persists across wide parameter ranges. Safe to deploy."
    elif robust_pct >= 35 and robust_1d_pct >= 40:
        verdict = "MODERATELY ROBUST — Edge exists in a zone, not just one point. Proceed with caution."
    elif robust_pct >= 15:
        verdict = "FRAGILE — Edge is sensitive to parameters. High curve-fit risk."
    else:
        verdict = "CURVE-FIT — Edge collapses outside narrow parameter window. Do not deploy."

    print(f"\n    >>> {verdict}")
    print()


if __name__ == "__main__":
    main()
