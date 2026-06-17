"""Tail-hedge sleeve backtest — does going LONG defensive assets
(GLD + TLT) when SPY breaks below its 200dma actually hedge the
xs_momentum cohort drawdowns?

The strategy_roles registry has a HEDGE bucket with a permissive
0.80 PF floor specifically for "negative-correlation specialists"
and "short-vol / tail-protection strategies." It's currently empty.

This audit answers: should we fill it?

LOGIC
=====
At each monthly rebalance:
  if SPY > 200dma: SIT FLAT (no exposure, no carry cost)
  if SPY < 200dma: LONG 50% GLD + 50% TLT

The hedge ONLY engages in bearish regimes (~23% of months
historically). When the regime is bullish, the sleeve costs zero.

EVALUATION
==========
- Standalone PF + DD + Sharpe of the hedge sleeve
- Combined Sharpe / DD when added at 10% weight to baseline
  xs_momentum (90% momentum + 10% hedge)
- Correlation of hedge returns vs baseline xs_momentum returns
  (negative correlation in drawdown months = real hedge)

USAGE
-----
    python -m ops.audit.run_tail_hedge_backtest
    python -m ops.audit.run_tail_hedge_backtest --period 20y --json
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

HEDGE_TICKERS = ("GLD", "TLT")
HEDGE_WEIGHTS = (0.5, 0.5)
SURVIVOR_PF_FLOOR_HEDGE = 0.80   # HEDGE role per strategy_roles registry


def _fetch_history(tickers: list[str], *, period: str = "20y"):
    """Pull daily closes for `tickers` via yfinance. Returns a DataFrame
    indexed by date with one column per ticker."""
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as exc:
        return None
    try:
        df = yf.download(tickers, period=period, interval="1d",
                         progress=False, auto_adjust=False,
                         group_by="ticker", threads=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    # Unwrap to {ticker: close_series}
    cols = {}
    if isinstance(df.columns, pd.MultiIndex):
        for t in tickers:
            try:
                cols[t] = df[t]["Close"].dropna()
            except Exception:
                try:
                    cols[t] = df["Close"][t].dropna()
                except Exception:
                    pass
    elif "Close" in df.columns and len(tickers) == 1:
        cols[tickers[0]] = df["Close"].dropna()
    if not cols:
        return None
    out = pd.DataFrame(cols).dropna(how="all")
    if hasattr(out.index, "tz") and out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out


def _run_backtest(period: str = "20y") -> dict:
    """Walk daily; at each month-end, check SPY-200dma. If bearish,
    enter GLD+TLT at 50/50; if bullish, sit flat."""
    try:
        import numpy as np
        import pandas as pd
    except Exception as exc:
        return {"error": f"import: {exc}"}

    tickers_needed = list(set(HEDGE_TICKERS) | {"SPY"})
    closes = _fetch_history(tickers_needed, period=period)
    if closes is None or closes.empty:
        return {"error": "no data"}

    # Build month-end indexer
    month_ends = closes.groupby(
        [closes.index.year, closes.index.month]
    ).tail(1).index
    if len(month_ends) < 12:
        return {"error": "insufficient months"}

    holdings: dict[str, dict] = {}  # ticker -> {entry_px, entry_date}
    trades: list[dict] = []
    bearish_months = 0
    bullish_months = 0

    for me in month_ends:
        i = closes.index.get_loc(me)
        if i < 200:
            continue  # need 200d SMA history

        # Compute SPY 200dma
        try:
            spy_window = closes["SPY"].iloc[i - 199: i + 1].dropna()
            if len(spy_window) < 200:
                continue
            spy_now = float(spy_window.iloc[-1])
            spy_sma = float(spy_window.mean())
        except Exception:
            continue
        is_bullish = spy_now > spy_sma

        # Execution at NEXT trading day's close (no look-ahead)
        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        if is_bullish:
            bullish_months += 1
            # Exit any hedge holdings
            for t in list(holdings.keys()):
                entry = holdings.pop(t)
                try:
                    exit_px = float(closes[t].iloc[next_idx])
                except Exception:
                    continue
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                trades.append({
                    "ticker": t,
                    "entry_date": entry["entry_date"],
                    "exit_date": str(exec_date.date()),
                    "entry_px": entry["entry_px"],
                    "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                    "weight": entry["weight"],
                })
        else:
            bearish_months += 1
            # Enter hedge holdings if not already in
            for t, w in zip(HEDGE_TICKERS, HEDGE_WEIGHTS):
                if t in holdings:
                    continue
                try:
                    entry_px = float(closes[t].iloc[next_idx])
                except Exception:
                    continue
                holdings[t] = {
                    "entry_px": entry_px,
                    "entry_date": str(exec_date.date()),
                    "weight": w,
                }

    # Close out any remaining holdings at the final bar
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for t, entry in holdings.items():
        try:
            exit_px = float(closes[t].iloc[final_idx])
        except Exception:
            continue
        pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
        trades.append({
            "ticker": t,
            "entry_date": entry["entry_date"],
            "exit_date": str(final_date.date()),
            "entry_px": entry["entry_px"],
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
            "weight": entry["weight"],
        })

    if not trades:
        return {
            "error": "no trades",
            "bullish_months": bullish_months,
            "bearish_months": bearish_months,
        }

    # 10bp slippage drag per round-trip
    drag = 2.0 * (10.0 / 100.0)
    pnls = [t["pnl_pct"] - drag for t in trades]

    # Equity curve aggregated monthly
    by_month: dict[str, list[float]] = {}
    for i_, t in enumerate(trades):
        m = t["exit_date"][:7]
        by_month.setdefault(m, []).append(pnls[i_] * t["weight"])

    eq = 1.0; peak = 1.0; max_dd = 0.0
    monthly_rets = []
    for m in sorted(by_month):
        # Sum across positions in same month
        ret = sum(by_month[m]) / 100
        monthly_rets.append(ret)
        eq *= (1 + ret)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    # Sharpe over months when hedge was active
    if len(monthly_rets) > 1:
        mean = sum(monthly_rets) / len(monthly_rets)
        var = sum((r - mean) ** 2 for r in monthly_rets) / (len(monthly_rets) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd * math.sqrt(12)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # PF
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")

    # CAGR over the FULL period (not just active months)
    # — that's the honest "what does carrying this hedge cost annually?" number
    total_months = bullish_months + bearish_months
    if total_months > 0 and eq > 0:
        cagr_active_only = (eq ** (12 / max(len(monthly_rets), 1)) - 1) * 100
        cagr_total_window = (eq ** (12 / total_months) - 1) * 100
    else:
        cagr_active_only = 0.0
        cagr_total_window = 0.0

    return {
        "n_trades": len(trades),
        "bullish_months": bullish_months,
        "bearish_months": bearish_months,
        "fraction_engaged": round(bearish_months / max(total_months, 1), 3),
        "win_rate": round(len(wins) / max(len(pnls), 1), 3),
        "pf_point": round(pf, 2),
        "sharpe_when_engaged": round(sharpe, 2),
        "max_dd_pct": round(max_dd * 100, 1),
        "cagr_when_engaged_pct": round(cagr_active_only, 1),
        "cagr_total_window_pct": round(cagr_total_window, 1),
        "first_entry": trades[0]["entry_date"],
        "last_exit": trades[-1]["exit_date"],
        "trades": trades,
    }


def _combined_with_baseline(hedge_trades: list[dict],
                             *, baseline_universe: list[str],
                             period: str, hedge_weight: float = 0.1) -> dict:
    """Run baseline xs_momentum on the supplied universe; aggregate
    monthly returns; combine with hedge at `hedge_weight` weight
    (1-w on baseline, w on hedge)."""
    try:
        import pandas as pd
        from forge.xs_momentum.runner import backtest
    except Exception as exc:
        return {"error": f"import: {exc}"}

    base_result = backtest(period=period, universe_override=baseline_universe,
                           return_monthly_series=True)
    if "error" in base_result:
        return {"error": base_result["error"]}

    base_monthly = base_result.get("monthly_returns") or {}
    if not base_monthly:
        return {"error": "no baseline monthly returns"}

    # Hedge monthly returns
    drag = 2.0 * (10.0 / 100.0)
    hedge_by_month: dict[str, float] = {}
    for t in hedge_trades:
        m = t["exit_date"][:7]
        contrib = (t["pnl_pct"] - drag) * t["weight"] / 100
        hedge_by_month[m] = hedge_by_month.get(m, 0.0) + contrib

    # All months present in either; missing = 0 return
    all_months = sorted(set(base_monthly.keys()) | set(hedge_by_month.keys()))

    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    combined_rets = []
    for m in all_months:
        base_ret = base_monthly.get(m, 0.0)
        hedge_ret = hedge_by_month.get(m, 0.0)
        combined = (1 - hedge_weight) * base_ret + hedge_weight * hedge_ret
        combined_rets.append(combined)
        eq *= (1 + combined)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    n = len(combined_rets)
    if n > 1:
        mean = sum(combined_rets) / n
        var = sum((r - mean) ** 2 for r in combined_rets) / (n - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd * math.sqrt(12)) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    cagr = (eq ** (12 / n) - 1) * 100 if eq > 0 and n else 0.0

    # Correlation between base and hedge monthly returns
    base_arr = [base_monthly.get(m, 0.0) for m in all_months]
    hedge_arr = [hedge_by_month.get(m, 0.0) for m in all_months]
    if n > 1:
        bm = sum(base_arr) / n
        hm = sum(hedge_arr) / n
        num = sum((base_arr[i] - bm) * (hedge_arr[i] - hm) for i in range(n))
        bsd = math.sqrt(sum((b - bm) ** 2 for b in base_arr))
        hsd = math.sqrt(sum((h - hm) ** 2 for h in hedge_arr))
        corr = (num / (bsd * hsd)) if bsd > 0 and hsd > 0 else 0.0
    else:
        corr = 0.0

    return {
        "n_months": n,
        "sharpe_combined": round(sharpe, 2),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(max_dd * 100, 1),
        "correlation_base_vs_hedge": round(corr, 3),
        "hedge_weight": hedge_weight,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--hedge-weight", type=float, default=0.1,
                        help="Weight of hedge sleeve in combined portfolio "
                        "(default 0.1 = 10%)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    print(f"Running tail-hedge backtest ({args.period})...", file=sys.stderr)
    hedge = _run_backtest(period=args.period)
    if hedge.get("error"):
        print(f"  ERROR: {hedge['error']}", file=sys.stderr)
        if args.json:
            print(json.dumps(hedge, indent=2, default=str))
        return 2

    print(
        f"  hedge alone: PF {hedge['pf_point']:.2f}, "
        f"Sharpe-when-engaged {hedge['sharpe_when_engaged']}, "
        f"DD {hedge['max_dd_pct']}%, "
        f"engaged {hedge['fraction_engaged']*100:.0f}% of months",
        file=sys.stderr,
    )

    # Combined-portfolio analysis using broad-8 baseline
    try:
        from helio.xs_momentum import DEFAULT_UNIVERSE
        combined = _combined_with_baseline(
            hedge["trades"],
            baseline_universe=list(DEFAULT_UNIVERSE),
            period=args.period,
            hedge_weight=args.hedge_weight,
        )
    except Exception as exc:
        combined = {"error": f"combined failed: {exc}"}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "hedge_weight": args.hedge_weight,
        "hedge_alone": {k: v for k, v in hedge.items() if k != "trades"},
        "combined_with_broad_8": combined,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "tail_hedge_backtest.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print()
    print("# Tail-Hedge Sleeve Backtest")
    print(f"Generated: {report['generated_at']}")
    print(f"Logic: LONG GLD+TLT (50/50) when SPY < 200dma; cash otherwise")
    print()
    print("## Hedge sleeve alone")
    print()
    print(f"- Trades: {hedge['n_trades']}")
    print(f"- Win rate: {hedge['win_rate']*100:.1f}%")
    print(f"- Profit factor: {hedge['pf_point']:.2f}")
    print(f"- Sharpe (when engaged): {hedge['sharpe_when_engaged']}")
    print(f"- Max DD: {hedge['max_dd_pct']}%")
    print(f"- CAGR when engaged: {hedge['cagr_when_engaged_pct']:.1f}%")
    print(f"- CAGR over full window: {hedge['cagr_total_window_pct']:.1f}%  "
          f"(<-- the cost if you carry it always; sleeve only engages "
          f"{hedge['fraction_engaged']*100:.0f}% of months)")
    print()
    print("## Combined: 90% broad-8 baseline + 10% hedge sleeve")
    print()
    if combined.get("error"):
        print(f"ERROR: {combined['error']}")
    else:
        print(f"- n_months: {combined['n_months']}")
        print(f"- Combined Sharpe: {combined['sharpe_combined']}")
        print(f"- Combined CAGR: {combined['cagr_pct']:.1f}%")
        print(f"- Combined max DD: {combined['max_dd_pct']:.1f}%")
        print(f"- Correlation base vs hedge: {combined['correlation_base_vs_hedge']}")
    print()
    print("## Decision")
    print()
    if hedge["pf_point"] < SURVIVOR_PF_FLOOR_HEDGE:
        print("- Hedge alone FAILS the HEDGE-role 0.80 PF floor")
        print("- Carrying this sleeve costs more than it saves")
    elif combined.get("error"):
        print("- Hedge alone passes the 0.80 floor; combined eval errored")
    else:
        # Compare to baseline-alone (no hedge) from a simple base computation
        # We don't have base-alone summary here; just report the combined
        # numbers and let the operator decide.
        corr = combined["correlation_base_vs_hedge"]
        if corr > 0.3:
            print(f"- Hedge correlates +{corr:.2f} with baseline — NOT a real hedge")
        elif corr > -0.1:
            print(f"- Hedge correlation {corr:+.2f} — neutral, not protective")
        else:
            print(f"- Hedge correlation {corr:+.2f} — genuine NEGATIVE correlation")
        if hedge["pf_point"] >= 1.0:
            print(f"- Hedge profitable when engaged (PF {hedge['pf_point']:.2f})")
        else:
            print(f"- Hedge LOSES money when engaged (PF {hedge['pf_point']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
