"""Sweep: 52-week-high breakout strategies on liquid ETFs.

Hypothesis (William O'Neil / Jegadeesh): instruments breaking to
new 52-week highs tend to continue. Test the cleanest version:
when daily close crosses above the trailing 252-day high, enter
long, hold for N days or until trailing stop.

VARIANTS:
  A. fresh_52w_high
     Enter when close crosses 252-day high. Hold 21 trading days
     or stop at -3% from entry.

  B. fresh_high_with_atr_stop
     Same entry. Exit on +2 ATR target / -1 ATR stop / 21-day timeout.

  C. monthly_high_breakout (faster cadence variant)
     Enter when close crosses 21-day high (1-month, not 1-year).
     Hold 5 days or -2% stop.

Disciplined gate:
  - 6bps round-trip slippage
  - Bootstrap PF CI lower >= 1.20
  - Point PF >= 1.30
  - Min n=50

Output:
  ops/reports/system_audit/breakout_sweep.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"
_DATA_DIR = _REPO / "helio" / "data_yfinance"

# Universe: all liquid ETFs we have daily data for
CANDIDATES = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    "MTUM", "QUAL", "USMV", "VIG", "VLUE", "VTV", "VUG", "VYM",
    "EFA", "EEM", "VEA", "EWJ", "EWG", "EWZ", "INDA", "FXI",
    "GLD", "TLT", "IEF",
]

ATR_PERIOD = 14
SLIPPAGE_BPS_ROUND_TRIP = 6.0
PROMOTION_PF_FLOOR = 1.20
MIN_TRADES_FLOOR = 50
POINT_PF_MARGIN = 1.30


def _load_daily(ticker: str) -> pd.DataFrame:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def _atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _backtest_variant(df: pd.DataFrame, variant: str) -> list[dict]:
    if df.empty or len(df) < 260:
        return []
    a_arr = _atr(df).values
    H = df["High"].values
    L = df["Low"].values
    C = df["Close"].values
    idx = df.index
    trades = []
    open_until = -1

    # Pre-compute rolling highs
    if variant in ("fresh_52w_high", "fresh_high_with_atr_stop"):
        roll_high = df["Close"].rolling(252).max().values
        prior_high = pd.Series(roll_high).shift(1).values
    elif variant == "monthly_high_breakout":
        roll_high = df["Close"].rolling(21).max().values
        prior_high = pd.Series(roll_high).shift(1).values
    else:
        return []

    for i in range(255, len(df)):
        if i <= open_until:
            continue
        # Signal: today's close > prior trailing high (a fresh high)
        if not np.isfinite(prior_high[i]):
            continue
        if C[i] <= prior_high[i]:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        # Exit logic per variant
        if variant == "fresh_52w_high":
            stop_px = entry * 0.97  # -3% stop
            target_px = None
            max_hold = 21
        elif variant == "fresh_high_with_atr_stop":
            stop_px = entry - 1.0 * a
            target_px = entry + 2.0 * a
            max_hold = 21
        elif variant == "monthly_high_breakout":
            stop_px = entry * 0.98  # -2% stop
            target_px = None
            max_hold = 5

        exit_px = None
        exit_idx = None
        exit_reason = None
        for j in range(i + 1, min(i + 1 + max_hold, len(df))):
            if L[j] <= stop_px:
                exit_px = stop_px
                exit_idx = j
                exit_reason = "stop"
                break
            if target_px is not None and H[j] >= target_px:
                exit_px = target_px
                exit_idx = j
                exit_reason = "target"
                break
        if exit_px is None:
            exit_idx = min(i + max_hold, len(df) - 1)
            exit_px = C[exit_idx]
            exit_reason = "timeout"
        # PnL pct with slippage
        gross_pct = (exit_px - entry) / entry * 100.0
        pnl_pct = gross_pct - (SLIPPAGE_BPS_ROUND_TRIP / 100.0)
        trades.append({
            "entry_dt": str(idx[i].date()),
            "exit_dt": str(idx[exit_idx].date()),
            "entry_px": float(entry),
            "exit_px": float(exit_px),
            "pnl_pct": float(pnl_pct),
            "exit_reason": exit_reason,
            "held_days": int(exit_idx - i),
        })
        open_until = exit_idx
    return trades


def evaluate_gate(trades: list[dict]) -> dict:
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
    if n < MIN_TRADES_FLOOR:
        verdicts.append(f"n={n} < {MIN_TRADES_FLOOR}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is None:
        verdicts.append("CI unavailable")
    elif ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")
    if not verdicts:
        verdict = "SURVIVES"
    elif (n >= MIN_TRADES_FLOOR and pf > 1.0 and ci_lower is not None
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


VARIANTS = ["fresh_52w_high", "fresh_high_with_atr_stop", "monthly_high_breakout"]


def run_sweep() -> dict:
    results = []
    for ticker in CANDIDATES:
        df = _load_daily(ticker)
        if df.empty:
            continue
        actual_years = (df.index[-1] - df.index[0]).days / 365.25
        for variant in VARIANTS:
            trades = _backtest_variant(df, variant)
            if not trades:
                continue
            gate = evaluate_gate(trades)
            tpy = round(gate["n"] / actual_years, 2) if actual_years > 0 else 0
            row = {"ticker": ticker, "variant": variant,
                   "years_actual": round(actual_years, 1),
                   "trades_per_year": tpy, **gate}
            results.append(row)
            marker = {"SURVIVES": "PASS", "MARGINAL": "MARG",
                      "FAIL": "fail", "INSUFFICIENT_DATA": "-"}.get(gate["verdict"], "?")
            if gate["verdict"] in ("SURVIVES", "MARGINAL"):
                print(f"  {ticker:5s} {variant:30s}: n={gate['n']:3d} PF={gate['pf']:5.2f} "
                      f"CI_lo={gate['ci_lower_95']} WR={gate['win_rate']} "
                      f"avg={gate.get('avg_pnl_pct')}% tpy={tpy:5.1f} -> [{marker}] {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "candidates": CANDIDATES,
        "variants": VARIANTS,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Breakout sweep -- 52-week / monthly high breakouts on ETFs")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"{len(report['candidates'])} tickers x {len(report['variants'])} variants  |  "
                 f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"Gate: CI >= {report['promotion_pf_floor']}, PF >= {report['point_pf_margin']}, "
                 f"min n={report['min_trades_floor']}")
    lines.append("")
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    fails = [r for r in report["results"] if r.get("verdict") == "FAIL"]
    insufficient = [r for r in report["results"] if r.get("verdict") == "INSUFFICIENT_DATA"]
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
                 f"{len(fails)} FAIL, {len(insufficient)} INSUFFICIENT")
    lines.append("")
    if survivors:
        lines.append("**Ship**:")
        for s in survivors:
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['variant']}` -> "
                f"+{s['trades_per_year']:.1f} fills/yr, "
                f"PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}"
            )
    if marginals:
        lines.append("")
        lines.append("**Marginals**:")
        for m in marginals:
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['variant']}` -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CI lower {m['ci_lower_95']:.2f}"
            )
    lines.append("")
    lines.append("## All results")
    lines.append("")
    lines.append("| Ticker | Variant | Years | n | trades/yr | PF | CI lower | WR | Avg | Verdict |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in sorted(report["results"], key=lambda x: -(x.get("pf") or 0)):
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "—"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "—"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "—"
        avg = f"{r['avg_pnl_pct']:.2f}%" if r.get('avg_pnl_pct') is not None else "—"
        years = f"{r['years_actual']:.1f}" if r.get('years_actual') else "—"
        lines.append(
            f"| {r['ticker']} | {r['variant']} | {years} | {r['n']} | "
            f"{r['trades_per_year']} | {pf} | {ci} | {wr} | {avg} | **{r['verdict']}** |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    print("=== Breakout sweep ===")
    print(f"{len(CANDIDATES)} tickers x {len(VARIANTS)} variants")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, CI >= {PROMOTION_PF_FLOOR}, "
          f"PF >= {POINT_PF_MARGIN}, min n={MIN_TRADES_FLOOR}")
    print()
    print("Printing only SURVIVES / MARGINAL during run; full table in MD output.")
    print()
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "breakout_sweep.json"
    md_path = _OUT_DIR / "breakout_sweep.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")
    print()
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    print(f"=== DONE: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
          f"{len(report['results'])} cells total ===")
    print()
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
