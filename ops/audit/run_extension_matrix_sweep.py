"""Unified extension matrix sweep.

Goal: build a compatibility matrix showing which proven calendar/seasonal
patterns extend to which instruments. We do NOT invent new edge families;
we systematically test our existing winners (TOM, single-month calendar)
across the daily-data universe at realistic slippage.

The 21-day breakout matrix is already covered by run_breakout_sweep.py
(EWZ was the survivor); this sweep handles TOM and single-month calendar.

VARIANTS:
  A. tom_calendar
     Entry: 4th-to-last trading day of month (close).
     Exit:  3rd trading day of next month (close).
     Same logic as forge.tom_spy live runner.

  B. month_holding (12 sub-variants: Jan-Dec)
     Entry: first trading day of target month (close).
     Exit:  last trading day of target month (close).
     Live nov_spy is sub-variant Nov.

Universe: all daily-data tickers in helio/data_yfinance/.

Gate: 6bps round-trip slippage + bootstrap PF CI >= 1.20 + PF >= 1.30
+ min n=20 (relaxed for calendar; single-month patterns get 1 trade/year).

Output:
  ops/reports/system_audit/extension_matrix_sweep.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"
_DATA_DIR = _REPO / "helio" / "data_yfinance"

CANDIDATES = [
    # broad
    "SPY", "QQQ", "IWM", "DIA",
    # SPDR sectors
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    # style factors
    "MTUM", "QUAL", "USMV", "VIG", "VLUE", "VTV", "VUG", "VYM",
    # international + EM
    "EFA", "EEM", "EWJ", "EWG", "EWZ", "INDA", "FXI",
    # commodities / bonds
    "GLD", "TLT", "IEF",
]

SLIPPAGE_BPS_ROUND_TRIP = 6.0
PROMOTION_PF_FLOOR = 1.20
POINT_PF_MARGIN = 1.30
MIN_TRADES_FLOOR = 20

MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

TOM_ENTRY_OFFSET = 4  # 4th-to-last trading day
TOM_EXIT_OFFSET = 3   # 3rd trading day of next month


def _load_daily(ticker: str) -> pd.DataFrame:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def _backtest_tom(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    trades = []
    # group by (year, month) and find the entry and following-month exit
    df = df.copy()
    df["ym"] = df.index.to_period("M")
    months = sorted(df["ym"].unique())
    for i, ym in enumerate(months):
        month_df = df[df["ym"] == ym]
        if len(month_df) <= TOM_ENTRY_OFFSET:
            continue
        entry_dt = month_df.index[-(TOM_ENTRY_OFFSET + 1)]
        # exit is 3rd trading day of next month
        if i + 1 >= len(months):
            continue
        next_month_df = df[df["ym"] == months[i + 1]]
        if len(next_month_df) < TOM_EXIT_OFFSET:
            continue
        exit_dt = next_month_df.index[TOM_EXIT_OFFSET - 1]
        entry_px = float(df.loc[entry_dt, "Close"])
        exit_px = float(df.loc[exit_dt, "Close"])
        gross_pct = (exit_px - entry_px) / entry_px * 100.0
        pnl_pct = gross_pct - (SLIPPAGE_BPS_ROUND_TRIP / 100.0)
        trades.append({
            "entry_dt": str(entry_dt.date()),
            "exit_dt": str(exit_dt.date()),
            "entry_px": entry_px,
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
            "held_days": int((exit_dt - entry_dt).days),
        })
    return trades


def _backtest_month_holding(df: pd.DataFrame, target_month: int) -> list[dict]:
    if df.empty:
        return []
    trades = []
    df = df.copy()
    df["ym"] = df.index.to_period("M")
    months = sorted(df["ym"].unique())
    for ym in months:
        if ym.month != target_month:
            continue
        month_df = df[df["ym"] == ym]
        if len(month_df) < 5:
            continue
        entry_dt = month_df.index[0]
        exit_dt = month_df.index[-1]
        entry_px = float(month_df["Close"].iloc[0])
        exit_px = float(month_df["Close"].iloc[-1])
        gross_pct = (exit_px - entry_px) / entry_px * 100.0
        pnl_pct = gross_pct - (SLIPPAGE_BPS_ROUND_TRIP / 100.0)
        trades.append({
            "entry_dt": str(entry_dt.date()),
            "exit_dt": str(exit_dt.date()),
            "entry_px": entry_px,
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
            "held_days": int((exit_dt - entry_dt).days),
        })
    return trades


def evaluate_gate(trades: list[dict], min_n: int = MIN_TRADES_FLOOR) -> dict:
    if not trades:
        return {"n": 0, "verdict": "INSUFFICIENT_DATA", "reason": "no trades"}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(pnls)
    try:
        from helio.bootstrap_stats import bootstrap_profit_factor
        bs = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
        ci_lower = bs.ci_lower
    except Exception:
        ci_lower = None
    n = len(trades)
    verdicts = []
    if n < min_n:
        verdicts.append(f"n={n} < {min_n}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is None:
        verdicts.append("CI unavailable")
    elif ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")
    if not verdicts:
        verdict = "SURVIVES"
    elif (n >= min_n and pf > 1.0 and ci_lower is not None
          and ci_lower >= 1.10):
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"
    return {
        "n": n,
        "pf": round(pf, 3) if pf != float("inf") else 999.0,
        "win_rate": round(wr, 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


def run_sweep() -> dict:
    results = []
    print("=== TOM (turn-of-month) extension ===")
    for ticker in CANDIDATES:
        df = _load_daily(ticker)
        if df.empty:
            continue
        actual_years = (df.index[-1] - df.index[0]).days / 365.25
        trades = _backtest_tom(df)
        gate = evaluate_gate(trades, min_n=MIN_TRADES_FLOOR)
        tpy = round(gate["n"] / actual_years, 2) if actual_years > 0 else 0
        row = {"family": "tom_calendar", "ticker": ticker, "variant": "tom_4_3",
               "years_actual": round(actual_years, 1),
               "trades_per_year": tpy, **gate}
        results.append(row)
        if gate["verdict"] in ("SURVIVES", "MARGINAL"):
            print(f"  {ticker:5s}: n={gate['n']:3d} PF={gate['pf']:5.2f} "
                  f"CI_lo={gate['ci_lower_95']} WR={gate['win_rate']} "
                  f"avg={gate.get('avg_pnl_pct')}% tpy={tpy:5.1f} -> {gate['verdict']}")

    print()
    print("=== Single-month calendar extension (12 months x N tickers) ===")
    # Limit single-month sweep to tickers with >= 15y of history
    # (else n per ticker-month is < 15 trades)
    for ticker in CANDIDATES:
        df = _load_daily(ticker)
        if df.empty:
            continue
        actual_years = (df.index[-1] - df.index[0]).days / 365.25
        if actual_years < 15:
            continue
        for m in range(1, 13):
            trades = _backtest_month_holding(df, m)
            # single-month relaxed min_n: 15 (one trade per year)
            gate = evaluate_gate(trades, min_n=15)
            tpy = round(gate["n"] / actual_years, 2) if actual_years > 0 else 0
            variant = f"hold_{MONTH_NAMES[m].lower()}"
            row = {"family": "month_holding", "ticker": ticker, "variant": variant,
                   "month": m,
                   "years_actual": round(actual_years, 1),
                   "trades_per_year": tpy, **gate}
            results.append(row)
            if gate["verdict"] in ("SURVIVES", "MARGINAL"):
                print(f"  {ticker:5s} {variant:12s}: n={gate['n']:2d} "
                      f"PF={gate['pf']:5.2f} CI_lo={gate['ci_lower_95']} "
                      f"WR={gate['win_rate']} avg={gate.get('avg_pnl_pct')}% "
                      f"-> {gate['verdict']}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "candidates": CANDIDATES,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Extension matrix sweep -- TOM + single-month calendar on ETFs")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"Gate: CI >= {report['promotion_pf_floor']}, PF >= {report['point_pf_margin']}, "
                 f"min n=20 (TOM) / 15 (single-month)")
    lines.append("")
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    fails = [r for r in report["results"] if r.get("verdict") == "FAIL"]
    insufficient = [r for r in report["results"] if r.get("verdict") == "INSUFFICIENT_DATA"]
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
                 f"{len(fails)} FAIL, {len(insufficient)} INSUFFICIENT")
    lines.append("")
    if survivors:
        lines.append("**Ship-eligible**:")
        for s in survivors:
            extra = f" ({MONTH_NAMES[s['month']]})" if s.get("month") else ""
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['variant']}`{extra} -> "
                f"n={s['n']}, PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}, "
                f"+{s['trades_per_year']:.1f} fills/yr"
            )
    if marginals:
        lines.append("")
        lines.append("**Marginal (watch-list)**:")
        for m in marginals:
            extra = f" ({MONTH_NAMES[m['month']]})" if m.get("month") else ""
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['variant']}`{extra} -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CI lower {m['ci_lower_95']:.2f}"
            )
    lines.append("")
    lines.append("## TOM (turn-of-month) results")
    lines.append("")
    lines.append("| Ticker | n | PF | CI lower | WR | Avg | trades/yr | Verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    tom_rows = [r for r in report["results"] if r["family"] == "tom_calendar"]
    for r in sorted(tom_rows, key=lambda x: -(x.get("pf") or 0)):
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "--"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "--"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "--"
        avg = f"{r['avg_pnl_pct']:.2f}%" if r.get('avg_pnl_pct') is not None else "--"
        lines.append(
            f"| {r['ticker']} | {r['n']} | {pf} | {ci} | {wr} | {avg} | "
            f"{r['trades_per_year']} | **{r['verdict']}** |"
        )
    lines.append("")
    lines.append("## Single-month calendar results (only SURVIVES / MARGINAL shown)")
    lines.append("")
    lines.append("| Ticker | Month | n | PF | CI lower | WR | Avg | Verdict |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    mh_rows = [r for r in report["results"]
               if r["family"] == "month_holding"
               and r["verdict"] in ("SURVIVES", "MARGINAL")]
    for r in sorted(mh_rows, key=lambda x: -(x.get("pf") or 0)):
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "--"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "--"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "--"
        avg = f"{r['avg_pnl_pct']:.2f}%" if r.get('avg_pnl_pct') is not None else "--"
        mon = MONTH_NAMES.get(r.get("month"), "?")
        lines.append(
            f"| {r['ticker']} | {mon} | {r['n']} | {pf} | {ci} | {wr} | {avg} | "
            f"**{r['verdict']}** |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(f"=== Extension matrix sweep ===")
    print(f"{len(CANDIDATES)} tickers x (TOM + 12 monthly calendar variants)")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, CI >= {PROMOTION_PF_FLOOR}, "
          f"PF >= {POINT_PF_MARGIN}, min n=20 (TOM) / 15 (single-month)")
    print()
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "extension_matrix_sweep.json"
    md_path = _OUT_DIR / "extension_matrix_sweep.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print()
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    print(f"=== DONE: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
          f"{len(report['results'])} cells total ===")
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
