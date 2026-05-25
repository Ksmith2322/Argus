"""xs_momentum short-side sweep — does the bottom-quintile lose
money reliably enough to short profitably?

The live strategy goes long the TOP quintile of each universe (the
strongest momentum names). The factor decomposition (5/19) found
xs_momentum has zero alpha — its return is passive factor beta.

Theory: if there's any real cross-sectional momentum edge, it lives
in the LONG-SHORT spread. Going long top + short bottom captures the
momentum-rank dispersion while neutralising market beta.

This sweep tests: does shorting the BOTTOM-N picks of each universe
produce a positive-EV trade after realistic slippage at 20y?

Survivor: PF CI lower >= 1.20 at 15bp slippage (higher than long-only
because shorts carry borrow costs that are hard to capture exactly).

USAGE
-----
    python -m ops.audit.run_xs_momentum_short_sweep
    python -m ops.audit.run_xs_momentum_short_sweep --universe style_factors_8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

# Tighter survivor floor for shorts — borrow + slippage costs are
# higher than long-only.
SURVIVOR_PF_FLOOR = 1.20
SHORT_SLIPPAGE_BPS = 15.0   # vs 10bp for longs

# Universes to test
UNIVERSES_TO_TEST = [
    ("broad_8",              None),
    ("sectors_spdr_11",      "sectors_spdr_11"),
    ("style_factors_8",      "style_factors_8"),
    ("legacy_15",            "legacy_sectors_countries_15"),
]


def _backtest_short_only(
    universe: list[str],
    *,
    period: str = "20y",
    bottom_fraction: float = 0.2,
) -> dict:
    """Walk the period month by month; each month-end, rank universe
    by 12-1 momentum and SHORT the bottom-N picks. Hold to next
    month-end. Return trade ledger."""
    try:
        import pandas as pd
        from forge.xs_momentum import runner as xs
        from helio.xs_momentum import (
            rank_universe_by_momentum, LOOKBACK_LONG_DAYS, LOOKBACK_SHORT_DAYS,
        )
    except Exception as exc:
        return {"error": f"import: {exc}"}

    try:
        closes = xs._fetch_history(universe, period=period)
    except Exception as exc:
        return {"error": f"fetch: {exc}"}
    if closes.empty:
        return {"error": "no data"}

    closes.index = closes.index.tz_convert("UTC")
    month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
    if len(month_ends) < 2:
        return {"error": "insufficient months"}

    holdings: dict[str, dict] = {}
    trades: list[dict] = []

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
        # Take the BOTTOM quintile (highest negative momentum = worst)
        n = max(1, int(round(len(ranked) * bottom_fraction)))
        bottom = ranked[-n:]
        pick_tickers = {p.ticker for p in bottom}

        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        # Close shorts that are no longer in the bottom
        for ticker in list(holdings.keys()):
            if ticker not in pick_tickers:
                entry = holdings.pop(ticker)
                # SHORT: profit = entry_px - exit_px (% of entry)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (entry["entry_px"] / exit_px - 1.0) * 100.0
                trades.append({
                    "ticker": ticker, "side": "SHORT",
                    "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"],
                    "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                })

        # Open new shorts
        for p in bottom:
            if p.ticker in holdings:
                continue
            holdings[p.ticker] = {
                "entry_px": float(closes[p.ticker].iloc[next_idx]),
                "entry_date": exec_date.isoformat(),
            }

    # Close remaining shorts at final bar
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for ticker, entry in holdings.items():
        exit_px = float(closes[ticker].iloc[final_idx])
        pnl_pct = (entry["entry_px"] / exit_px - 1.0) * 100.0
        trades.append({
            "ticker": ticker, "side": "SHORT",
            "entry_date": entry["entry_date"],
            "exit_date": final_date.isoformat(),
            "entry_px": entry["entry_px"],
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
        })

    if not trades:
        return {"error": "no trades"}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / len(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    avg_pnl = sum(pnls) / len(pnls)
    return {
        "universe_size": len(universe),
        "trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2),
        "avg_pnl_pct": round(avg_pnl, 3),
        "trades_detail": trades,
    }


def _run_one(
    u_label: str, u_key: str | None,
    *, period: str = "20y",
    bottom_fraction: float = 0.2,
) -> dict:
    try:
        from helio.xs_momentum_universes import get_universe
        from helio.xs_momentum import DEFAULT_UNIVERSE
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"universe": u_label, "error": f"import: {exc}"}

    universe = list(get_universe(u_key)) if u_key else list(DEFAULT_UNIVERSE)
    result = _backtest_short_only(universe, period=period,
                                  bottom_fraction=bottom_fraction)
    if "error" in result:
        return {"universe": u_label, "error": result["error"]}

    trades = result["trades_detail"]
    # Apply 15bp short-slippage drag (2× per round-trip)
    drag = 2.0 * (SHORT_SLIPPAGE_BPS / 100.0)
    pnls = [t["pnl_pct"] - drag for t in trades]
    try:
        boot = bootstrap_profit_factor(pnls, n_resamples=2000)
        ci_lo = boot.ci_lower
        ci_hi = boot.ci_upper
    except Exception as exc:
        return {"universe": u_label, "error": f"bootstrap: {exc}"}

    # Compute equity stats
    eq = 1.0; peak = 1.0; max_dd = 0.0
    for p in pnls:
        eq *= (1 + p / 100.0)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    n_years = len(pnls) / 12.0 if pnls else 1
    cagr = (eq ** (1 / max(n_years, 0.001)) - 1) * 100 if eq > 0 else -100

    return {
        "universe": u_label,
        "universe_size": result["universe_size"],
        "n_trades": result["trades"],
        "win_rate": result["win_rate"],
        "pf_after_slippage": round(boot.point, 2),
        "ci_95_lower": round(ci_lo, 2),
        "ci_95_upper": round(ci_hi, 2),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "approx_cagr_pct": round(cagr, 1),
        "approx_max_dd_pct": round(max_dd * 100, 1),
        "slippage_bps_applied": SHORT_SLIPPAGE_BPS,
        "survivor": ci_lo >= SURVIVOR_PF_FLOOR,
    }


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# xs_momentum SHORT-side sweep",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Slippage applied: {SHORT_SLIPPAGE_BPS} bps (higher than longs to "
        f"approximate borrow cost). Survivor floor: PF CI lower >= "
        f"{SURVIVOR_PF_FLOOR}",
        "",
    ]
    survivors = [r for r in rows if r.get("survivor")]
    out.append(f"**Survivors**: {len(survivors)} / {len(rows)}")
    out.append("")
    out.append("| Universe | n | WR | PF | CI lower | Avg% | CAGR | DD | Survivor |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['universe']} | — | — | — | — | — | — | — | ERR {r['error'][:40]} |")
            continue
        out.append(
            f"| {r['universe']} | {r['n_trades']} | "
            f"{r['win_rate']*100:.1f}% | "
            f"{r['pf_after_slippage']:.2f} | "
            f"{r['ci_95_lower']:.2f} | "
            f"{r['avg_pnl_pct']:+.3f}% | "
            f"{r['approx_cagr_pct']:.1f}% | "
            f"{r['approx_max_dd_pct']:.1f}% | "
            f"{'YES' if r['survivor'] else 'no'} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=None)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--bottom-fraction", type=float, default=0.2,
                        help="Fraction of universe to short (default 0.2 = bottom quintile)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.universe:
        universes = [u for u in UNIVERSES_TO_TEST if u[0] == args.universe]
        if not universes:
            print(f"ERROR: unknown universe label")
            return 2
    else:
        universes = UNIVERSES_TO_TEST

    rows = []
    for u_label, u_key in universes:
        print(f"  {u_label}...", file=sys.stderr, end=" ", flush=True)
        r = _run_one(u_label, u_key, period=args.period,
                     bottom_fraction=args.bottom_fraction)
        rows.append(r)
        if r.get("error"):
            print(f"ERR {r['error']}", file=sys.stderr)
        else:
            print(
                f"n={r['n_trades']} PF={r['pf_after_slippage']:.2f} "
                f"CI_lo={r['ci_95_lower']:.2f} "
                f"{'SURVIVOR' if r['survivor'] else ''}",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "xs_momentum_short_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
