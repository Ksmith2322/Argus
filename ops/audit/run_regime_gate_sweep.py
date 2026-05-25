"""Regime-gate sweep: does adding a market-regime overlay reduce
drawdown without giving up too much CAGR?

For each (universe, regime_gate) combination, run xs_momentum
backtest with the gate applied at each monthly rebalance:
  - Gate TRUE  -> rebalance normally
  - Gate FALSE -> exit any open positions, no new entries

Compare to baseline (no gate) on CAGR, max DD, Sharpe-proxy, n_trades.

The right gate maximizes RISK-ADJUSTED return. Pure CAGR can drop
slightly if the DD reduction is large enough.

USAGE
-----
    python -m ops.audit.run_regime_gate_sweep
    python -m ops.audit.run_regime_gate_sweep --universe style_factors_8
    python -m ops.audit.run_regime_gate_sweep --json
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
    ("broad_8",              None),
    ("sectors_spdr_11",      "sectors_spdr_11"),
    ("style_factors_8",      "style_factors_8"),
    ("legacy_15",            "legacy_sectors_countries_15"),
]


def _gated_backtest(
    universe: list[str],
    *,
    period: str,
    gate_name: str | None,
    regime_data: dict,
) -> dict:
    """Walk universe history monthly. At each month-end, evaluate
    the regime gate (if any). If FALSE, exit holdings + skip
    new entries. If TRUE, rebalance normally."""
    try:
        import pandas as pd
        from forge.xs_momentum import runner as xs
        from helio.xs_momentum import (
            rank_universe_by_momentum, select_top_quintile,
            LOOKBACK_LONG_DAYS, LOOKBACK_SHORT_DAYS, TOP_QUINTILE_FRACTION,
        )
        from helio.regime import REGIME_GATES
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

    # Pre-build aligned regime series if a gate is provided
    gate_fn = None
    gate_series = None
    if gate_name and gate_name in REGIME_GATES:
        gate_cfg = REGIME_GATES[gate_name]
        gate_fn = gate_cfg["fn"]
        # Need is one of SPY_closes / VIX_closes
        needed_key = gate_cfg["needs"][0]
        gate_series = regime_data.get(needed_key)
        if gate_series is None or gate_series.empty:
            return {"error": f"no {needed_key} data"}
        # Make index naive UTC
        if gate_series.index.tz is not None:
            gate_series.index = gate_series.index.tz_localize(None)
        gate_series.index = pd.to_datetime(gate_series.index)

    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    gate_open_months = 0
    gate_closed_months = 0

    for month_end in month_ends:
        i = closes.index.get_loc(month_end)
        if i <= LOOKBACK_LONG_DAYS + LOOKBACK_SHORT_DAYS:
            continue

        # Evaluate gate at this month_end
        gate_pass = True
        if gate_fn is not None:
            # Find the gate_series index at or just before month_end
            me_naive = month_end.tz_convert(None) if month_end.tz else month_end
            try:
                gate_idx = gate_series.index.searchsorted(me_naive, side="right") - 1
            except Exception:
                gate_idx = -1
            if gate_idx < 0:
                gate_pass = False  # no history
            else:
                gate_pass = bool(gate_fn(gate_series, asof_index=int(gate_idx)))
        if gate_pass:
            gate_open_months += 1
        else:
            gate_closed_months += 1

        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        if not gate_pass:
            # Exit all holdings, no new entries
            for ticker in list(holdings.keys()):
                entry = holdings.pop(ticker)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                trades.append({
                    "ticker": ticker, "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"], "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                })
            continue

        # Rank + pick top quintile
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

        # Sell removed
        for ticker in list(holdings.keys()):
            if ticker not in pick_tickers:
                entry = holdings.pop(ticker)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                trades.append({
                    "ticker": ticker, "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"], "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                })

        # Buy new
        for p in picks:
            if p.ticker in holdings:
                continue
            holdings[p.ticker] = {
                "entry_px": float(closes[p.ticker].iloc[next_idx]),
                "entry_date": exec_date.isoformat(),
            }

    # Close remaining at final bar
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for ticker, entry in holdings.items():
        exit_px = float(closes[ticker].iloc[final_idx])
        pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
        trades.append({
            "ticker": ticker, "entry_date": entry["entry_date"],
            "exit_date": final_date.isoformat(),
            "entry_px": entry["entry_px"], "exit_px": exit_px,
            "pnl_pct": pnl_pct,
        })

    if not trades:
        return {"error": "no trades"}

    drag = 2.0 * 0.1  # 10 bps round-trip
    pnls = [t["pnl_pct"] - drag for t in trades]

    # Per-trade monthly equity
    eq = 1.0; peak = 1.0; max_dd = 0.0
    monthly: dict[str, list[float]] = {}
    for t in trades:
        m = t["exit_date"][:7]
        monthly.setdefault(m, []).append(t["pnl_pct"] - drag)
    for m in sorted(monthly):
        ret = sum(monthly[m]) / len(monthly[m]) / 100
        eq *= (1 + ret)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    months_active = len(monthly)
    cagr = (eq ** (12 / max(months_active, 1)) - 1) * 100 if eq > 0 else -100

    # Sharpe proxy: monthly mean / monthly sd × sqrt(12)
    rets = [(sum(monthly[m]) / len(monthly[m])) / 100 for m in sorted(monthly)]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd * math.sqrt(12)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "n_trades": len(trades),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(max_dd * 100, 1),
        "sharpe_proxy": round(sharpe, 2),
        "gate_open_months": gate_open_months,
        "gate_closed_months": gate_closed_months,
        "gate_open_fraction": (
            round(gate_open_months / (gate_open_months + gate_closed_months), 3)
            if (gate_open_months + gate_closed_months) > 0 else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=None)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from helio.regime import REGIME_GATES, fetch_regime_data
    from helio.xs_momentum_universes import get_universe
    from helio.xs_momentum import DEFAULT_UNIVERSE

    print(f"Fetching regime data ({args.period})...", file=sys.stderr)
    regime_data = fetch_regime_data(period=args.period)
    print(f"  got SPY: {len(regime_data.get('SPY_closes', []))} bars, "
          f"VIX: {len(regime_data.get('VIX_closes', []))} bars",
          file=sys.stderr)

    if args.universe:
        universes_pairs = [u for u in UNIVERSES_TO_TEST if u[0] == args.universe]
    else:
        universes_pairs = UNIVERSES_TO_TEST

    rows = []
    gate_names = [None] + list(REGIME_GATES.keys())
    for u_label, u_key in universes_pairs:
        universe = (list(get_universe(u_key)) if u_key
                    else list(DEFAULT_UNIVERSE))
        for gate in gate_names:
            label = gate or "no_gate_baseline"
            print(f"  {u_label} × {label}...", file=sys.stderr,
                  end=" ", flush=True)
            r = _gated_backtest(
                universe, period=args.period,
                gate_name=gate, regime_data=regime_data,
            )
            r["universe"] = u_label
            r["gate"] = label
            rows.append(r)
            if r.get("error"):
                print(f"ERR {r['error']}", file=sys.stderr)
            else:
                print(
                    f"CAGR {r['cagr_pct']}% DD {r['max_dd_pct']}% "
                    f"Sharpe {r['sharpe_proxy']} "
                    f"open_frac {r.get('gate_open_fraction', 'N/A')}",
                    file=sys.stderr,
                )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "regime_gate_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print()
    print("# Regime-gate sweep — does an overlay reduce DD without "
          "killing CAGR?")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("| Universe | Gate | n | CAGR | DD | Sharpe | Open % |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            print(f"| {r['universe']} | {r['gate']} | — | — | — | — | ERR {r['error'][:30]} |")
            continue
        open_pct = (f"{r['gate_open_fraction']*100:.0f}%"
                    if r.get('gate_open_fraction') is not None else "N/A")
        print(
            f"| {r['universe']} | {r['gate']} | "
            f"{r['n_trades']} | {r['cagr_pct']:.1f}% | "
            f"{r['max_dd_pct']:.1f}% | {r['sharpe_proxy']:.2f} | "
            f"{open_pct} |"
        )
    print()
    # Recommend the best gate per universe by Sharpe
    print("## Best gate per universe (by Sharpe proxy)")
    print()
    for u_label, _ in universes_pairs:
        u_rows = [r for r in rows if r.get("universe") == u_label
                  and not r.get("error")]
        if not u_rows:
            continue
        baseline = next((r for r in u_rows if r["gate"] == "no_gate_baseline"),
                        None)
        best = max(u_rows, key=lambda r: r.get("sharpe_proxy", 0))
        if baseline:
            d_sharpe = best["sharpe_proxy"] - baseline["sharpe_proxy"]
            d_dd = best["max_dd_pct"] - baseline["max_dd_pct"]
            d_cagr = best["cagr_pct"] - baseline["cagr_pct"]
            improve = ("IMPROVEMENT" if best["gate"] != "no_gate_baseline"
                       and d_sharpe > 0 else "no improvement")
            print(f"- **{u_label}**: best = **{best['gate']}** "
                  f"(Sharpe {best['sharpe_proxy']:.2f}, vs baseline "
                  f"{baseline['sharpe_proxy']:.2f}, dSharpe {d_sharpe:+.2f}, "
                  f"dDD {d_dd:+.1f}%, dCAGR {d_cagr:+.1f}%) -> "
                  f"{improve}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
