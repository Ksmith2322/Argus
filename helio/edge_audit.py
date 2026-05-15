#!/usr/bin/env python3
"""helio/edge_audit.py — Stress-test + friction-model surviving signals.

After walk-forward validation identifies a real signal, this module answers
the questions that decide whether it's actually tradeable:

  1. Distribution shape: is the +8.75% mean driven by 10 moonshots or 800
     consistent +9% trades? (Skew / kurtosis / win rate / percentiles.)
  2. Sector / per-ticker concentration: does the edge come from one bucket
     (e.g. all biotech) or generalize?
  3. Friction-adjusted return: subtract Agent 3's spread + IBKR fees +
     slippage estimate. Does +8.75% gross survive 1.5-4% round-trip cost?

Output is per-signal decision support, not pretty graphs.

Usage:
    python -m helio.edge_audit --signal-csv helio/data/breakout_results/forward_returns_sub10_exp.csv \
                                --catalyst quarterly_report --direction DOWN --window 10
    python -m helio.edge_audit --all-survivors
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"

# Friction model — round-trip cost as % of notional, by price bucket.
# Refined 2026-05-14 for small-cap PEAD work. Numbers reflect Agent 3's
# execution audit plus typical IBKR retail spreads observed during RTH.
# Components: bid-ask spread + IBKR fees ($0.0035/share, $0.35 min) +
# 0.1-0.3% slippage on market orders.
def round_trip_friction_pct(entry_price: float) -> float:
    """Estimate round-trip cost as % of entry notional.

    Tiers (decreasing friction as price rises — % spread shrinks faster than fees):
      $25-$50:  0.30%  (institutional names, tight spreads)
      $10-$25:  0.60%  (small/mid-cap with decent liquidity)
      $5-$10:   1.20%  (sub-$10 but tradeable)
      $2-$5:    2.50%  (junk tier)
      $1-$2:    4.00%
      $0.50-$1: 8.00%
      <$0.50:  15.00%  (basically untradeable)
    """
    if entry_price >= 25.0:
        return 0.003
    elif entry_price >= 10.0:
        return 0.006
    elif entry_price >= 5.0:
        return 0.012
    elif entry_price >= 2.0:
        return 0.025
    elif entry_price >= 1.0:
        return 0.040
    elif entry_price >= 0.50:
        return 0.080
    else:
        return 0.150


def percentile(arr: np.ndarray, q: float) -> float:
    if len(arr) == 0:
        return float("nan")
    return float(np.quantile(arr, q))


def audit_signal(csv_path: Path, catalyst: str, direction: str,
                  window_label: str = "10d",
                  apply_friction: bool = True) -> dict:
    """Run stress-test + friction model on one signal.

    csv_path: forward_returns CSV with per-trade returns and catalyst_label.
    catalyst: filter to one catalyst (e.g. 'quarterly_report')
    direction: 'UP' or 'DOWN'
    window_label: '1d', '3d', '5d', or '10d'
    apply_friction: if True, also compute net returns after spread + fees.

    Returns dict with full stats + per-trade distribution summary.
    """
    df = pd.read_csv(csv_path)
    # forward_returns CSV doesn't carry catalyst_label OR price; merge both from
    # the source breakouts CSV. Price is critical for the friction model — a
    # missed merge silently makes round-trip cost 0% (the bug caught 2026-05-14).
    needs_merge_cols = [c for c in ("catalyst_label", "price", "sector") if c not in df.columns]
    if needs_merge_cols:
        suffix = csv_path.stem.replace("forward_returns", "all_breakouts")
        bo_path = RESULTS_DIR / f"{suffix}.csv"
        if not bo_path.exists():
            stem = csv_path.stem
            if "_sub10_exp" in stem:
                bo_path = RESULTS_DIR / "all_breakouts_sub10_exp.csv"
        if not bo_path.exists():
            raise FileNotFoundError(f"breakouts CSV not found for merge: {bo_path}")
        merge_cols = ["symbol", "date"] + needs_merge_cols
        bo = pd.read_csv(bo_path)[merge_cols]
        bo["date"] = pd.to_datetime(bo["date"]).dt.strftime("%Y-%m-%d")
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.merge(bo, on=["symbol", "date"], how="left")

    # Filter
    mask = (df["catalyst_label"] == catalyst) & (df["direction"] == direction)
    sub = df[mask].copy()
    excess_col = f"excess_{window_label}"
    stock_col = f"ret_{window_label}"
    if excess_col not in sub.columns:
        raise KeyError(f"{excess_col} not in CSV")

    sub = sub.dropna(subset=[excess_col, stock_col])
    if len(sub) == 0:
        return {"error": "no rows after filter", "catalyst": catalyst,
                "direction": direction, "window": window_label}

    raw_returns = sub[stock_col].to_numpy()        # absolute stock returns
    excess_returns = sub[excess_col].to_numpy()    # vs SPY

    # Apply friction model
    if apply_friction and "price" in sub.columns:
        prices = sub["price"].fillna(2.0).to_numpy()
        friction_pcts = np.array([round_trip_friction_pct(p) for p in prices])
        net_raw = raw_returns - friction_pcts
        net_excess = excess_returns - friction_pcts
    else:
        net_raw = raw_returns
        net_excess = excess_returns
        friction_pcts = np.zeros(len(raw_returns))

    # Per-sector breakdown
    sector_stats = {}
    if "sector" in sub.columns:
        for sector, grp in sub.groupby("sector"):
            if len(grp) < 20:
                continue
            grp_excess = grp[excess_col].dropna().to_numpy()
            sector_stats[sector] = {
                "n": len(grp_excess),
                "mean_excess_pct": round(float(np.mean(grp_excess)) * 100, 3),
                "win_rate": round(float(np.mean(grp_excess > 0)) * 100, 1),
            }

    # Per-symbol concentration check (top 5 tickers by trade count)
    top_symbols = []
    if "symbol" in sub.columns:
        symbol_counts = sub["symbol"].value_counts().head(5)
        top_symbols = [{"symbol": s, "n": int(c)} for s, c in symbol_counts.items()]

    n = len(sub)
    result = {
        "catalyst": catalyst,
        "direction": direction,
        "window": window_label,
        "n_trades": n,
        # Raw (gross) stats
        "gross": {
            "mean_pct": round(float(np.mean(raw_returns)) * 100, 3),
            "median_pct": round(float(np.median(raw_returns)) * 100, 3),
            "std_pct": round(float(np.std(raw_returns, ddof=1)) * 100, 3),
            "win_rate_pct": round(float(np.mean(raw_returns > 0)) * 100, 1),
            "p10": round(percentile(raw_returns, 0.10) * 100, 2),
            "p25": round(percentile(raw_returns, 0.25) * 100, 2),
            "p50": round(percentile(raw_returns, 0.50) * 100, 2),
            "p75": round(percentile(raw_returns, 0.75) * 100, 2),
            "p90": round(percentile(raw_returns, 0.90) * 100, 2),
            "max_win_pct": round(float(np.max(raw_returns)) * 100, 2),
            "max_loss_pct": round(float(np.min(raw_returns)) * 100, 2),
        },
        "gross_excess": {
            "mean_pct": round(float(np.mean(excess_returns)) * 100, 3),
            "ir": round(_ir_5d_annualized(excess_returns), 3),
            "win_rate_pct": round(float(np.mean(excess_returns > 0)) * 100, 1),
        },
        # Friction-adjusted
        "friction_model": {
            "avg_round_trip_cost_pct": round(float(np.mean(friction_pcts)) * 100, 3),
            "median_entry_price": round(float(np.median(sub["price"])), 2) if "price" in sub.columns else None,
        },
        "net_after_friction": {
            "mean_pct": round(float(np.mean(net_raw)) * 100, 3),
            "median_pct": round(float(np.median(net_raw)) * 100, 3),
            "win_rate_pct": round(float(np.mean(net_raw > 0)) * 100, 1),
            "p25": round(percentile(net_raw, 0.25) * 100, 2),
            "p75": round(percentile(net_raw, 0.75) * 100, 2),
        },
        "net_excess_after_friction": {
            "mean_pct": round(float(np.mean(net_excess)) * 100, 3),
            "ir": round(_ir_5d_annualized(net_excess), 3),
            "win_rate_pct": round(float(np.mean(net_excess > 0)) * 100, 1),
        },
        "top_5_tickers_by_trade_count": top_symbols,
        "sector_breakdown": sector_stats,
    }

    # Outlier sensitivity: how much does the mean shift if we drop top N winners?
    sorted_returns = np.sort(raw_returns)
    if len(sorted_returns) >= 50:
        result["outlier_sensitivity"] = {
            "mean_pct": result["gross"]["mean_pct"],
            "mean_drop_top5_pct": round(float(np.mean(sorted_returns[:-5])) * 100, 3),
            "mean_drop_top10_pct": round(float(np.mean(sorted_returns[:-10])) * 100, 3),
            "mean_drop_top25_pct": round(float(np.mean(sorted_returns[:-25])) * 100, 3),
            "mean_drop_top_5pct_quantile": round(float(np.mean(sorted_returns[:int(len(sorted_returns) * 0.95)])) * 100, 3),
        }

    return result


def _ir_5d_annualized(excess: np.ndarray) -> float:
    """Same IR convention as breakout_forward_returns (5d-anchored)."""
    if len(excess) < 2:
        return float("nan")
    m = float(np.mean(excess))
    s = float(np.std(excess, ddof=1))
    if s == 0:
        return float("inf") if m > 0 else (float("-inf") if m < 0 else 0.0)
    return (m / s) * np.sqrt(252.0 / 5.0)


def print_audit(result: dict) -> None:
    """Pretty-print the audit result to stdout."""
    print(f"\n{'='*70}")
    print(f"EDGE AUDIT: {result['catalyst']} / {result['direction']} / {result['window']}")
    print(f"{'='*70}")
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return
    n = result["n_trades"]
    print(f"  n_trades: {n}")

    g = result["gross"]
    print(f"\n  GROSS RETURNS (no friction):")
    print(f"    mean  = {g['mean_pct']:+.2f}%   median = {g['median_pct']:+.2f}%   std = {g['std_pct']:.2f}%")
    print(f"    win rate: {g['win_rate_pct']:.1f}%")
    print(f"    percentiles: p10={g['p10']:+.1f}%  p25={g['p25']:+.1f}%  p50={g['p50']:+.1f}%  "
          f"p75={g['p75']:+.1f}%  p90={g['p90']:+.1f}%")
    print(f"    max win:  {g['max_win_pct']:+.1f}%")
    print(f"    max loss: {g['max_loss_pct']:+.1f}%")
    print(f"    gross excess IR (vs SPY): {result['gross_excess']['ir']:+.2f}")

    if "outlier_sensitivity" in result:
        os = result["outlier_sensitivity"]
        print(f"\n  OUTLIER SENSITIVITY:")
        print(f"    mean (all):         {os['mean_pct']:+.2f}%")
        print(f"    mean (drop top 5):  {os['mean_drop_top5_pct']:+.2f}%")
        print(f"    mean (drop top 10): {os['mean_drop_top10_pct']:+.2f}%")
        print(f"    mean (drop top 25): {os['mean_drop_top25_pct']:+.2f}%")
        print(f"    mean (drop top 5%): {os['mean_drop_top_5pct_quantile']:+.2f}%")

    f = result["friction_model"]
    nf = result["net_after_friction"]
    nfe = result["net_excess_after_friction"]
    print(f"\n  FRICTION MODEL:")
    print(f"    median entry price: ${f['median_entry_price']}")
    print(f"    avg round-trip cost: {f['avg_round_trip_cost_pct']:.2f}% of notional")
    print(f"\n  NET RETURNS (after spread + fees):")
    print(f"    mean = {nf['mean_pct']:+.2f}%   median = {nf['median_pct']:+.2f}%   win rate: {nf['win_rate_pct']:.1f}%")
    print(f"    p25 = {nf['p25']:+.1f}%   p75 = {nf['p75']:+.1f}%")
    print(f"    net excess IR vs SPY: {nfe['ir']:+.2f}")
    print(f"    net excess win rate:  {nfe['win_rate_pct']:.1f}%")

    if result.get("top_5_tickers_by_trade_count"):
        print(f"\n  TOP 5 TICKERS BY TRADE COUNT:")
        for r in result["top_5_tickers_by_trade_count"]:
            print(f"    {r['symbol']:6s}: {r['n']} trades")

    if result.get("sector_breakdown"):
        print(f"\n  SECTOR BREAKDOWN (sectors with n>=20):")
        sectors = sorted(result["sector_breakdown"].items(),
                         key=lambda x: -x[1]["mean_excess_pct"])
        for s, st in sectors[:10]:
            print(f"    {s:24s}: n={st['n']:>5d}  mean_excess={st['mean_excess_pct']:>+6.2f}%  "
                  f"win={st['win_rate']:>5.1f}%")


# The signals that survived walk-forward (per 2026-05-14 audit).
SURVIVORS = [
    ("quarterly_report",     "DOWN", "10d"),  # the main edge
    ("quarterly_report",     "DOWN", "5d"),   # weaker window
    ("quarterly_report",     "DOWN", "3d"),   # weaker window
    ("annual_report",        "DOWN", "10d"),  # secondary bounce
    ("primary_registration", "DOWN", "10d"),  # FADE / short signal
]


def main():
    parser = argparse.ArgumentParser(description="Edge Audit (stress-test + friction)")
    parser.add_argument("--signal-csv", default="helio/data/breakout_results/forward_returns_sub10_exp.csv",
                        help="Per-trade forward-returns CSV")
    parser.add_argument("--catalyst", default=None)
    parser.add_argument("--direction", default="DOWN", choices=["UP", "DOWN"])
    parser.add_argument("--window", default="10d", choices=["1d", "3d", "5d", "10d"])
    parser.add_argument("--all-survivors", action="store_true",
                        help="Run audit on every survivor of walk-forward validation")
    parser.add_argument("--no-friction", action="store_true",
                        help="Skip the friction model (just stress-test gross)")
    args = parser.parse_args()

    csv_path = Path(args.signal_csv)
    if not csv_path.is_absolute():
        csv_path = REPO / csv_path

    all_results = {}
    if args.all_survivors:
        for cat, dir_, win in SURVIVORS:
            r = audit_signal(csv_path, cat, dir_, win,
                              apply_friction=not args.no_friction)
            print_audit(r)
            all_results[f"{cat}_{dir_}_{win}"] = r
    elif args.catalyst:
        r = audit_signal(csv_path, args.catalyst, args.direction, args.window,
                          apply_friction=not args.no_friction)
        print_audit(r)
        all_results[f"{args.catalyst}_{args.direction}_{args.window}"] = r
    else:
        parser.print_help()
        return

    # Save consolidated result
    out_path = RESULTS_DIR / "edge_audit_results.json"
    out_path.write_text(json.dumps({
        "results": all_results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, default=str))
    print(f"\n\nResults saved: {out_path}")


if __name__ == "__main__":
    main()
