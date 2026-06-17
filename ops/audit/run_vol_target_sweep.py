"""Vol-targeted sizing sweep — does inverse-vol position scaling
improve Sharpe and reduce DD across our universes?

The 5/25 cohort correlation audit found variants de-correlate
poorly in tail months (DD 23-66% → 50-88% in bear regimes). Vol-
targeting scales exposure inversely to recent realized vol, so the
fleet automatically reduces size as volatility rises. Theory: smoother
equity curve, lower max DD, similar or slightly lower CAGR.

Method:
  1. Walk the backtest month-by-month
  2. At each rebalance, compute trailing 6-month realized vol of
     the strategy's returns
  3. Scale position size = target_vol / realized_vol (clamped 0.5-2.0)
  4. Compare end-of-period metrics to fixed-sizing baseline

USAGE
-----
    python -m ops.audit.run_vol_target_sweep
    python -m ops.audit.run_vol_target_sweep --target-vol 0.20
    python -m ops.audit.run_vol_target_sweep --json
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

UNIVERSES_TO_TEST = [
    ("broad_8",         None),
    ("sectors_spdr_11", "sectors_spdr_11"),
    ("style_factors_8", "style_factors_8"),
    ("legacy_15",       "legacy_sectors_countries_15"),
]

VOL_WINDOW_MONTHS = 6
SLIPPAGE_BPS = 10.0


def _backtest_vol_target(
    universe: list[str],
    *,
    period: str = "20y",
    target_vol_annual: float = 0.15,
    apply_vol_target: bool = True,
) -> dict:
    """Walk the backtest, applying vol-targeted sizing each month."""
    try:
        import pandas as pd
        from forge.xs_momentum import runner as xs
        from helio.xs_momentum import (
            rank_universe_by_momentum, select_top_quintile,
            LOOKBACK_LONG_DAYS, LOOKBACK_SHORT_DAYS, TOP_QUINTILE_FRACTION,
        )
        from helio.vol_target import vol_target_scale
    except Exception as exc:
        return {"error": f"import: {exc}"}

    try:
        closes = xs._fetch_history(universe, period=period)
    except Exception as exc:
        return {"error": f"fetch: {exc}"}
    if closes.empty:
        return {"error": "no closes"}
    closes.index = closes.index.tz_convert("UTC")
    month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index

    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    monthly_returns: list[float] = []  # for vol-target lookup

    for month_end in month_ends:
        i = closes.index.get_loc(month_end)
        if i <= LOOKBACK_LONG_DAYS + LOOKBACK_SHORT_DAYS:
            continue

        per_asset = {
            t: closes[t].iloc[: i + 1].dropna().tolist()
            for t in universe if t in closes.columns
        }
        ranked = rank_universe_by_momentum(
            per_asset,
            long_lookback=LOOKBACK_LONG_DAYS,
            short_lookback=LOOKBACK_SHORT_DAYS,
        )
        if not ranked:
            continue
        picks = select_top_quintile(ranked, fraction=TOP_QUINTILE_FRACTION)
        pick_tickers = {p.ticker for p in picks}

        # Compute size scale (the meta-overlay)
        if apply_vol_target and len(monthly_returns) >= VOL_WINDOW_MONTHS:
            recent = monthly_returns[-VOL_WINDOW_MONTHS:]
            scale = vol_target_scale(recent,
                                     target_annual_vol=target_vol_annual)
        else:
            scale = 1.0

        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        # Sell removed
        month_pnl_contribs = []
        for ticker in list(holdings.keys()):
            if ticker not in pick_tickers:
                entry = holdings.pop(ticker)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                # Apply the scale at which the position was opened
                scaled_pnl = pnl_pct * entry["scale"]
                trades.append({
                    "ticker": ticker, "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"], "exit_px": exit_px,
                    "scale": entry["scale"],
                    "pnl_pct": pnl_pct,
                    "scaled_pnl_pct": scaled_pnl,
                })
                month_pnl_contribs.append(scaled_pnl)

        # Buy new
        for p in picks:
            if p.ticker in holdings:
                continue
            holdings[p.ticker] = {
                "entry_px": float(closes[p.ticker].iloc[next_idx]),
                "entry_date": exec_date.isoformat(),
                "scale": scale,
            }

        # Record this month's return (avg of contributing trades)
        if month_pnl_contribs:
            monthly_returns.append(sum(month_pnl_contribs) / len(month_pnl_contribs) / 100)

    # Close remaining
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for ticker, entry in holdings.items():
        exit_px = float(closes[ticker].iloc[final_idx])
        pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
        trades.append({
            "ticker": ticker, "entry_date": entry["entry_date"],
            "exit_date": final_date.isoformat(),
            "entry_px": entry["entry_px"], "exit_px": exit_px,
            "scale": entry["scale"],
            "pnl_pct": pnl_pct,
            "scaled_pnl_pct": pnl_pct * entry["scale"],
        })

    if not trades:
        return {"error": "no trades"}

    drag = 2.0 * (SLIPPAGE_BPS / 100.0)
    # PnL after slippage; scale only affects gross pnl, slippage is
    # proportional to notional (which also scales) so drag scales too
    pnls = [t["scaled_pnl_pct"] - (drag * t["scale"]) for t in trades]

    eq = 1.0; peak = 1.0; max_dd = 0.0
    by_month: dict[str, list[float]] = {}
    for i, t in enumerate(trades):
        m = t["exit_date"][:7]
        by_month.setdefault(m, []).append(pnls[i])
    for m in sorted(by_month):
        ret = sum(by_month[m]) / len(by_month[m]) / 100
        eq *= (1 + ret)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    rets = [sum(by_month[m]) / len(by_month[m]) / 100 for m in sorted(by_month)]
    n_months = len(rets)
    if n_months > 1:
        mean = sum(rets) / n_months
        var = sum((r - mean) ** 2 for r in rets) / (n_months - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = mean / sd * math.sqrt(12) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    cagr = (eq ** (12 / max(n_months, 1)) - 1) * 100 if eq > 0 else -100

    # Distribution of scales applied
    scales = [t["scale"] for t in trades]
    return {
        "n_trades": len(trades),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(max_dd * 100, 1),
        "sharpe_proxy": round(sharpe, 2),
        "scale_mean": round(sum(scales) / len(scales), 3),
        "scale_min": round(min(scales), 3),
        "scale_max": round(max(scales), 3),
        "scale_p25": round(sorted(scales)[int(len(scales) * 0.25)], 3),
        "scale_p75": round(sorted(scales)[int(len(scales) * 0.75)], 3),
    }


def _run_one(u_label: str, u_key: str | None,
             *, period: str, target_vol: float) -> dict:
    try:
        from helio.xs_momentum_universes import get_universe
        from helio.xs_momentum import DEFAULT_UNIVERSE
    except Exception as exc:
        return {"universe": u_label, "error": f"import: {exc}"}
    universe = list(get_universe(u_key)) if u_key else list(DEFAULT_UNIVERSE)

    # Run baseline (no vol-target) + vol-targeted side by side
    baseline = _backtest_vol_target(
        universe, period=period,
        target_vol_annual=target_vol,
        apply_vol_target=False,
    )
    voltarget = _backtest_vol_target(
        universe, period=period,
        target_vol_annual=target_vol,
        apply_vol_target=True,
    )
    if baseline.get("error") or voltarget.get("error"):
        return {"universe": u_label,
                "baseline_error": baseline.get("error"),
                "voltarget_error": voltarget.get("error")}

    return {
        "universe": u_label,
        "universe_size": len(universe),
        "target_vol_annual": target_vol,
        "baseline": baseline,
        "voltarget": voltarget,
        "delta_sharpe": round(voltarget["sharpe_proxy"] - baseline["sharpe_proxy"], 2),
        "delta_dd_pct": round(voltarget["max_dd_pct"] - baseline["max_dd_pct"], 1),
        "delta_cagr_pct": round(voltarget["cagr_pct"] - baseline["cagr_pct"], 1),
    }


def _classify(delta_sharpe: float, delta_dd: float) -> str:
    if delta_sharpe > 0.1 and delta_dd < -3:
        return "CLEAN_WIN"           # Sharpe up + DD meaningfully lower
    if delta_sharpe > 0.05 and delta_dd <= 0:
        return "MILD_WIN"            # Both improved a bit
    if delta_sharpe > 0 and delta_dd > 0:
        return "DD_TRADE_OFF"        # Higher Sharpe, higher DD
    if delta_sharpe < -0.1:
        return "WORSE"               # vol-target hurts
    return "ROUGHLY_FLAT"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--target-vol", type=float, default=0.15,
                        help="Target annualized vol (default 0.15 = 15%)")
    parser.add_argument("--universe", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.universe:
        universes = [u for u in UNIVERSES_TO_TEST if u[0] == args.universe]
    else:
        universes = UNIVERSES_TO_TEST

    rows: list[dict] = []
    for u_label, u_key in universes:
        print(f"  {u_label} (target {args.target_vol*100:.0f}% vol)...",
              file=sys.stderr, end=" ", flush=True)
        r = _run_one(u_label, u_key, period=args.period,
                     target_vol=args.target_vol)
        rows.append(r)
        if r.get("baseline_error") or r.get("voltarget_error"):
            print(f"ERR", file=sys.stderr)
        else:
            r["verdict"] = _classify(r["delta_sharpe"], r["delta_dd_pct"])
            b = r["baseline"]; v = r["voltarget"]
            print(
                f"baseline {b['sharpe_proxy']:.2f}/{b['max_dd_pct']:.1f}% DD "
                f"-> voltgt {v['sharpe_proxy']:.2f}/{v['max_dd_pct']:.1f}% "
                f"({r['verdict']})",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "vol_target_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print()
    print("# Vol-targeted sizing sweep")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"Target vol: {args.target_vol*100:.0f}% annualized. Slippage: {SLIPPAGE_BPS} bps.")
    print()
    print("| Universe | Sharpe (base->vt) | DD% (base->vt) | CAGR (base->vt) | dSharpe | dDD | Verdict |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("baseline_error"):
            print(f"| {r['universe']} | ERR | — | — | — | — | — |")
            continue
        b = r["baseline"]; v = r["voltarget"]
        print(
            f"| {r['universe']} | "
            f"{b['sharpe_proxy']:.2f} -> {v['sharpe_proxy']:.2f} | "
            f"{b['max_dd_pct']:.1f}% -> {v['max_dd_pct']:.1f}% | "
            f"{b['cagr_pct']:.1f}% -> {v['cagr_pct']:.1f}% | "
            f"{r['delta_sharpe']:+.2f} | "
            f"{r['delta_dd_pct']:+.1f}% | "
            f"**{r['verdict']}** |"
        )
    print()
    print("## Scale distribution observed (per universe)")
    print()
    for r in rows:
        if r.get("baseline_error"):
            continue
        v = r["voltarget"]
        print(f"- **{r['universe']}**: mean {v['scale_mean']}, "
              f"p25-p75 [{v['scale_p25']}, {v['scale_p75']}], "
              f"range [{v['scale_min']}, {v['scale_max']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
