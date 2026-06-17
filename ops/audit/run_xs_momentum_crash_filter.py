"""Compare xs_momentum WITHOUT vs WITH the momentum-crash regime filter.

If the filter materially reduces max DD and improves Sharpe without
destroying CAGR, it's a candidate to add to the live runner.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _spy_daily(period: str = "20y"):
    import yfinance as yf
    import pandas as pd
    df = yf.download("SPY", period=period, interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    closes = df["Close"].dropna()
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] running unfiltered xs_momentum backtest ({args.period})...")
    from forge.xs_momentum.runner import backtest
    bt_unfiltered = backtest(period=args.period)
    if "error" in bt_unfiltered:
        print(f"  error: {bt_unfiltered['error']}")
        return 1
    print(f"  unfiltered: PF={bt_unfiltered['profit_factor']}  "
          f"CAGR={bt_unfiltered['cagr_pct']}%  "
          f"max DD={bt_unfiltered['max_drawdown_pct']}%  "
          f"trades={bt_unfiltered['trades']}")

    print()
    print(f"[2] applying momentum-crash regime filter...")
    from helio.momentum_crash_filter import regime_signal_at_dates
    spy = _spy_daily(args.period)
    trades = bt_unfiltered.get("trades_detail") or []
    # For each trade's entry_date, look up the regime flag
    entry_dates = sorted({t["entry_date"] for t in trades
                            if isinstance(t, dict)})
    regime_flags = regime_signal_at_dates(spy, entry_dates)
    n_crash = sum(1 for v in regime_flags.values() if v)
    print(f"  crash-regime months: {n_crash} / {len(regime_flags)}")
    # Build filtered trade list: keep only trades whose entry was NOT in
    # crash regime
    filtered_trades = [t for t in trades
                          if isinstance(t, dict)
                          and not regime_flags.get(t["entry_date"], False)]
    excluded = [t for t in trades
                  if isinstance(t, dict)
                  and regime_flags.get(t["entry_date"], False)]

    print(f"  trades kept:      {len(filtered_trades)}")
    print(f"  trades excluded:  {len(excluded)}")
    if excluded:
        # What did the excluded trades produce?
        exc_pnls = [t["pnl_pct"] for t in excluded]
        avg_excluded = sum(exc_pnls) / len(exc_pnls)
        print(f"  avg pnl of excluded trades:  {avg_excluded:+.3f}%  "
              f"(filter helps if this is negative)")

    # Compute filtered-portfolio stats
    if not filtered_trades:
        print("  filter excluded ALL trades — likely too aggressive")
        return 0
    f_pnls = [t["pnl_pct"] for t in filtered_trades]
    f_wins = [p for p in f_pnls if p > 0]
    f_losses = [p for p in f_pnls if p < 0]
    f_pf = abs(sum(f_wins) / sum(f_losses)) if f_losses else float("inf")
    f_wr = len(f_wins) / len(f_pnls)
    # Equity / DD
    eq = 1.0; peak = 1.0; max_dd = 0.0
    for p in f_pnls:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    n_years = len(f_pnls) / 12.0 if len(f_pnls) > 12 else 1.0
    f_cagr = (eq ** (1.0 / n_years) - 1.0) * 100.0

    print()
    print(f"[3] disciplined gate on filtered pnls ({len(f_pnls)} trades)...")
    from helio.promotion_panel import run_panel, render_panel_report
    f_dates = [t["entry_date"] for t in filtered_trades]
    report = run_panel(
        f_pnls,
        label="xs_momentum + crash filter",
        entry_dates=f_dates,
        slippage_levels_bps=(0.0, args.slippage_bps),
        promotion_floor=1.20,
    )
    panel_text = render_panel_report(report)
    print(panel_text)

    # Comparison summary
    print()
    print("=" * 78)
    print("UNFILTERED vs FILTERED COMPARISON")
    print("=" * 78)
    print(f"{'metric':<22}  {'unfiltered':>14}  {'filtered':>14}  {'delta':>10}")
    print("-" * 70)

    def _delta(a, b):
        if a is None or b is None:
            return "?"
        try:
            return f"{(b - a):+.2f}"
        except Exception:
            return "?"

    print(f"{'n_trades':<22}  {bt_unfiltered['trades']:>14}  "
          f"{len(f_pnls):>14}  {_delta(bt_unfiltered['trades'], len(f_pnls))}")
    print(f"{'PF':<22}  {bt_unfiltered['profit_factor']:>14.2f}  "
          f"{f_pf:>14.2f}  {_delta(bt_unfiltered['profit_factor'], f_pf)}")
    print(f"{'CAGR':<22}  {bt_unfiltered['cagr_pct']:>13.2f}%  "
          f"{f_cagr:>13.2f}%  {_delta(bt_unfiltered['cagr_pct'], f_cagr)}")
    print(f"{'Max DD':<22}  {bt_unfiltered['max_drawdown_pct']:>13.2f}%  "
          f"{max_dd*100:>13.2f}%  "
          f"{_delta(bt_unfiltered['max_drawdown_pct'], max_dd*100)}")
    print(f"{'Win rate':<22}  {bt_unfiltered['win_rate']*100:>13.1f}%  "
          f"{f_wr*100:>13.1f}%  "
          f"{_delta(bt_unfiltered['win_rate']*100, f_wr*100)}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "slippage_bps": args.slippage_bps,
        "unfiltered_summary": {
            k: v for k, v in bt_unfiltered.items()
            if k not in ("trades_detail", "monthly_returns")
        },
        "filtered_summary": {
            "n_trades": len(f_pnls),
            "n_trades_excluded": len(excluded),
            "profit_factor": round(f_pf, 3),
            "win_rate": round(f_wr, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "cagr_pct": round(f_cagr, 2),
        },
        "disciplined_gate_filtered": report.to_dict(),
    }
    json_path = OUT_DIR / f"xs_momentum_crash_filter_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "xs_momentum_crash_filter.md"
    md = ["# xs_momentum + momentum-crash regime filter",
           "",
           f"Generated: {datetime.now(timezone.utc).isoformat()}",
           "",
           f"## Comparison",
           "",
           "| Metric | Unfiltered | Filtered | Delta |",
           "|---|---|---|---|",
           f"| n_trades | {bt_unfiltered['trades']} | {len(f_pnls)} | "
           f"{_delta(bt_unfiltered['trades'], len(f_pnls))} |",
           f"| PF | {bt_unfiltered['profit_factor']:.2f} | {f_pf:.2f} | "
           f"{_delta(bt_unfiltered['profit_factor'], f_pf)} |",
           f"| CAGR | {bt_unfiltered['cagr_pct']:.2f}% | {f_cagr:.2f}% | "
           f"{_delta(bt_unfiltered['cagr_pct'], f_cagr)}% |",
           f"| Max DD | {bt_unfiltered['max_drawdown_pct']:.2f}% | "
           f"{max_dd*100:.2f}% | "
           f"{_delta(bt_unfiltered['max_drawdown_pct'], max_dd*100)}% |",
           f"| WR | {bt_unfiltered['win_rate']*100:.1f}% | {f_wr*100:.1f}% | "
           f"{_delta(bt_unfiltered['win_rate']*100, f_wr*100)} |",
           "",
           "## Disciplined gate (filtered)",
           "",
           "```",
           panel_text,
           "```"]
    md_path.write_text("\n".join(md), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
