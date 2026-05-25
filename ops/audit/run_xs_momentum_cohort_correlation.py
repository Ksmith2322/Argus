"""Pairwise correlation audit of the xs_momentum variant cohort.

The 5 currently-active xs_momentum strategies all run the same engine
on different universes (or different concentrations). The question:
are they actually diversifying, or do they end up holding correlated
baskets that net to the same exposure?

Method:
  1. Backtest each variant with return_monthly_series=True over the
     same window
  2. Build a monthly-returns matrix (rows = months, cols = variants)
  3. Compute pairwise Pearson correlations
  4. Compute incremental Sharpe / DD improvement from adding each
     variant to the baseline

USAGE
-----
    python -m ops.audit.run_xs_momentum_cohort_correlation
    python -m ops.audit.run_xs_momentum_cohort_correlation --period 20y
    python -m ops.audit.run_xs_momentum_cohort_correlation --json

Honest verdict per pair:
  rho < 0.50  — genuine diversification
  rho 0.50-0.75 — partial diversification
  rho > 0.75  — near-duplicate, allocation overlap question
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

# Map variant name -> (universe_key, top_pick_fraction)
VARIANTS = {
    "baseline":         (None,                          0.20),   # broad-8, top-2
    "sectors":          ("sectors_spdr_11",             0.20),   # 11 SPDRs, top-2/3
    "style":            ("style_factors_8",             0.20),   # 8 style, top-2
    "legacy15":         ("legacy_sectors_countries_15", 0.20),   # 15-mix, top-3
    "style_top3":       ("style_factors_8",             0.35),   # 8 style, top-3
    # 2026-05-25 regime gate. Same universe + concentration as
    # legacy15 — return correlation tests whether the SPY>200dma
    # overlay produces a meaningfully different return stream.
    "legacy15_regime":  ("legacy_sectors_countries_15", 0.20),
}


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return num / (sx * sy)


def _backtest_variant(variant_name: str, period: str) -> dict[str, float]:
    """Run backtest for one variant; return monthly_returns dict."""
    from forge.xs_momentum.runner import backtest
    universe_key, top_frac = VARIANTS[variant_name]
    universe_override = None
    if universe_key is not None:
        from helio.xs_momentum_universes import get_universe
        universe_override = list(get_universe(universe_key))
    result = backtest(
        period=period,
        return_monthly_series=True,
        universe_override=universe_override,
        top_pick_fraction=top_frac,
    )
    if "error" in result:
        return {}
    return result.get("monthly_returns") or {}


def _portfolio_stats(returns: list[float]) -> dict:
    """Sharpe + max DD on a return series (returns are fractions, monthly)."""
    if not returns:
        return {"sharpe": float("nan"), "max_dd_pct": 0.0, "cagr_pct": 0.0}
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / max(1, n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    sharpe = (mean / sd * math.sqrt(12)) if sd > 0 else float("nan")
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        eq *= (1 + r)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    cagr = (eq ** (12 / n) - 1) * 100 if n else 0.0
    return {
        "sharpe": round(sharpe, 2) if not math.isnan(sharpe) else None,
        "max_dd_pct": round(max_dd * 100, 1),
        "cagr_pct": round(cagr, 1),
    }


def _equal_weight(matrices: list[list[float]]) -> list[float]:
    """Combine multiple return series via equal-weight portfolio."""
    if not matrices:
        return []
    n = min(len(m) for m in matrices)
    k = len(matrices)
    return [sum(m[i] for m in matrices) / k for i in range(n)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # Run backtests
    monthly_by: dict[str, dict[str, float]] = {}
    for name in VARIANTS:
        print(f"  {name}...", file=sys.stderr, end=" ", flush=True)
        monthly = _backtest_variant(name, args.period)
        monthly_by[name] = monthly
        print(f"n={len(monthly)} months", file=sys.stderr)

    # Build aligned matrix
    all_months = sorted(set().union(*[set(m.keys()) for m in monthly_by.values()]))
    matrix: dict[str, list[float]] = {}
    for name in VARIANTS:
        matrix[name] = [monthly_by[name].get(m, 0.0) for m in all_months]

    # Pairwise correlations
    names = list(VARIANTS)
    corr: dict[str, dict[str, float]] = {n: {} for n in names}
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i > j:
                continue
            r = _pearson(matrix[a], matrix[b])
            corr[a][b] = corr[b][a] = round(r, 3) if not math.isnan(r) else None

    # Per-variant stats
    stats = {n: _portfolio_stats(matrix[n]) for n in names}

    # Equal-weight portfolios: baseline alone vs baseline+each variant
    base_returns = matrix["baseline"]
    base_stats = _portfolio_stats(base_returns)
    incremental: dict[str, dict] = {}
    for n in names:
        if n == "baseline":
            continue
        combined = _equal_weight([base_returns, matrix[n]])
        combined_stats = _portfolio_stats(combined)
        incremental[n] = {
            "vs_baseline_alone": {
                "sharpe_delta": (combined_stats["sharpe"] - base_stats["sharpe"])
                                if combined_stats["sharpe"] is not None and base_stats["sharpe"] is not None else None,
                "max_dd_delta": combined_stats["max_dd_pct"] - base_stats["max_dd_pct"],
                "cagr_delta": combined_stats["cagr_pct"] - base_stats["cagr_pct"],
            },
            "combined": combined_stats,
        }

    # 5-strategy equal-weight portfolio
    all_combined = _equal_weight([matrix[n] for n in names])
    all_stats = _portfolio_stats(all_combined)

    # Verdict per pair
    pair_verdicts = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            rho = corr[a][b]
            if rho is None:
                tag = "n/a"
            elif rho < 0.50:
                tag = "diversifies"
            elif rho < 0.75:
                tag = "partial"
            else:
                tag = "near-duplicate"
            pair_verdicts.append({"a": a, "b": b, "rho": rho, "tag": tag})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "variants": list(VARIANTS),
        "n_months_overlap": len(all_months),
        "per_variant_stats": stats,
        "pairwise_correlations": corr,
        "pair_verdicts": pair_verdicts,
        "baseline_alone": base_stats,
        "incremental_vs_baseline": incremental,
        "all_5_equal_weight": all_stats,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "xs_momentum_cohort_correlation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print()
    print("# xs_momentum cohort correlation audit")
    print(f"Period: {args.period}, overlapping months: {len(all_months)}")
    print()
    print("## Per-variant stats")
    print()
    print("| Variant | Sharpe | DD | CAGR |")
    print("|---|---|---|---|")
    for n in names:
        s = stats[n]
        print(f"| {n} | {s['sharpe']} | {s['max_dd_pct']}% | {s['cagr_pct']}% |")
    print()
    print("## Pairwise correlations (Pearson, monthly returns)")
    print()
    header = "| | " + " | ".join(names) + " |"
    print(header)
    print("|" + "---|" * (len(names) + 1))
    for a in names:
        row_cells = [f"**{a}**"]
        for b in names:
            v = corr[a][b]
            row_cells.append(f"{v:.2f}" if v is not None else "-")
        print("| " + " | ".join(row_cells) + " |")
    print()
    print("## Pair verdicts")
    print()
    for pv in pair_verdicts:
        rho = pv["rho"]
        rho_str = f"{rho:.2f}" if rho is not None else "n/a"
        print(f"- {pv['a']} vs {pv['b']}: rho={rho_str} **{pv['tag']}**")
    print()
    print("## Incremental Sharpe/DD vs baseline-alone")
    print()
    for n, v in incremental.items():
        d = v["vs_baseline_alone"]
        cs = v["combined"]
        sharpe_delta = d["sharpe_delta"]
        sharpe_str = f"{sharpe_delta:+.2f}" if sharpe_delta is not None else "n/a"
        print(
            f"- baseline + {n}: combined Sharpe {cs['sharpe']} "
            f"(delta {sharpe_str}), "
            f"DD {cs['max_dd_pct']}% (delta {d['max_dd_delta']:+.1f}%), "
            f"CAGR {cs['cagr_pct']}% (delta {d['cagr_delta']:+.1f}%)"
        )
    print()
    print("## All-5 equal-weight portfolio")
    print(f"Sharpe {all_stats['sharpe']}, "
          f"DD {all_stats['max_dd_pct']}%, "
          f"CAGR {all_stats['cagr_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
