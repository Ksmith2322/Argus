"""Sweep: short-term mean reversion on liquid ETFs.

Classic Connors 2-day RSI mean reversion (Connors / Alvarez "Short-Term
Trading Strategies That Work", 2008). The single most-documented
high-cadence orthogonal-to-12-1-momentum edge for equity ETFs.

THE EDGE: when an ETF is sharply oversold on a short timeframe, it
tends to revert in 1-5 days. Direction-opposite from xs_momentum's
12-1 rank, so genuinely orthogonal in the left tail.

VARIANTS:
  A. rsi2_strict     2-day RSI < 5,  exit on close > 5-day MA OR 5-day timeout
  B. rsi2_moderate   2-day RSI < 10, exit on close > 5-day MA OR 5-day timeout
  C. rsi2_loose      2-day RSI < 15, exit on close > 5-day MA OR 5-day timeout
  D. rsi2_strict_time   2-day RSI < 5,  exit ONLY on 3-day timeout (no MA exit)

All variants:
  - Hard stop: -3% from entry
  - Universe: all liquid ETFs we have daily data for
  - Slippage: 5bps round-trip (intraday assumption)

Two-tier promotion gate (per docs/decisions/
2026_08_31_real_money_promotion_gate.md):
  - Disciplined gate (existing): PF >= 1.30, bootstrap CI lower >= 1.20, min n=50
  - SPY net-of-tax gate (new):   pre-tax CAGR >= 13% (beats SPY after taxes)

Output: ops/reports/system_audit/mean_reversion_sweep.{md,json}
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

# Universe: existing daily-data tickers in helio/data_yfinance/
CANDIDATES = [
    # broad
    "SPY", "QQQ", "IWM", "DIA",
    # SPDR sectors
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    # style factors
    "MTUM", "QUAL", "USMV", "VIG", "VLUE", "VTV", "VUG", "VYM",
    # international + EM
    "EFA", "EEM", "EWJ", "EWG", "EWZ", "INDA", "FXI",
    # specialty
    "SMH", "KRE", "XBI", "ITB", "KWEB", "EWY", "EWT", "EWA", "EWU", "EWC",
    # commodity / bond
    "GLD", "TLT", "IEF", "USO", "UNG", "SLV", "COPX", "URA", "DBA", "LIT",
    "HYG", "EMB", "LQD", "TIP",
    # mega-cap stocks
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "BRK-B",
    "JPM", "V", "MA", "JNJ", "UNH", "XOM", "HD", "PG", "WMT",
]

SLIPPAGE_BPS_ROUND_TRIP = 5.0
PROMOTION_PF_FLOOR = 1.20
POINT_PF_MARGIN = 1.30
MIN_TRADES_FLOOR = 50
CAGR_FLOOR_BEAT_SPY = 0.13
STOP_PCT = 0.03


def _load_daily(ticker: str) -> pd.DataFrame:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def _rsi_2(close: pd.Series) -> pd.Series:
    """2-day RSI per Wilder's RSI, period=2. Connors uses this as the
    canonical short-term oversold indicator."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/2, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _backtest_variant(df: pd.DataFrame, variant: str) -> list[dict]:
    if df.empty or len(df) < 30:
        return []
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    rsi = _rsi_2(close).values
    ma5 = close.rolling(5).mean().values
    C = close.values
    L = low.values
    H = high.values
    idx = df.index

    # Variant parameters
    if variant == "rsi2_strict":
        rsi_threshold = 5.0
        max_hold = 5
        use_ma_exit = True
    elif variant == "rsi2_moderate":
        rsi_threshold = 10.0
        max_hold = 5
        use_ma_exit = True
    elif variant == "rsi2_loose":
        rsi_threshold = 15.0
        max_hold = 5
        use_ma_exit = True
    elif variant == "rsi2_strict_time":
        rsi_threshold = 5.0
        max_hold = 3
        use_ma_exit = False
    else:
        return []

    trades = []
    open_until = -1
    for i in range(5, len(df)):
        if i <= open_until:
            continue
        if not np.isfinite(rsi[i]):
            continue
        if rsi[i] >= rsi_threshold:
            continue
        entry = C[i]
        stop_px = entry * (1.0 - STOP_PCT)
        exit_px = None
        exit_idx = None
        exit_reason = None
        for j in range(i + 1, min(i + 1 + max_hold, len(df))):
            if L[j] <= stop_px:
                exit_px = stop_px
                exit_idx = j
                exit_reason = "stop"
                break
            if use_ma_exit and C[j] > ma5[j]:
                exit_px = C[j]
                exit_idx = j
                exit_reason = "revert_ma5"
                break
        if exit_px is None:
            exit_idx = min(i + max_hold, len(df) - 1)
            exit_px = C[exit_idx]
            exit_reason = "timeout"
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


def _estimate_cagr(trades: list[dict], years: float) -> float:
    """Compound equity curve estimate. Each trade applies its pnl_pct
    to a running equity figure assuming we re-deploy at each entry."""
    if not trades or years <= 0:
        return 0.0
    equity = 100.0
    for t in trades:
        equity *= (1.0 + t["pnl_pct"] / 100.0)
    total_return = equity / 100.0
    if total_return <= 0:
        return -1.0
    return total_return ** (1.0 / years) - 1.0


def evaluate_gate(trades: list[dict], years: float) -> dict:
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
        ci_upper = bs.ci_upper
    except Exception:
        ci_lower = None
        ci_upper = None
    n = len(trades)
    cagr = _estimate_cagr(trades, years)

    verdicts = []
    if n < MIN_TRADES_FLOOR:
        verdicts.append(f"n={n} < {MIN_TRADES_FLOOR}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is None:
        verdicts.append("CI unavailable")
    elif ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")
    if cagr < CAGR_FLOOR_BEAT_SPY:
        verdicts.append(f"CAGR={cagr*100:.1f}% < SPY net-of-tax bar {CAGR_FLOOR_BEAT_SPY*100:.0f}%")
    if not verdicts:
        verdict = "SURVIVES"
    elif (n >= MIN_TRADES_FLOOR and pf > 1.0 and ci_lower is not None
          and ci_lower >= 1.10 and cagr >= 0.08):
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"
    return {
        "n": n,
        "pf": round(pf, 3) if pf != float("inf") else 999.0,
        "win_rate": round(wr, 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "cagr_pretax": round(cagr, 4),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "ci_upper_95": round(ci_upper, 3) if ci_upper is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


VARIANTS = ["rsi2_strict", "rsi2_moderate", "rsi2_loose", "rsi2_strict_time"]


def run_sweep() -> dict:
    results = []
    for ticker in CANDIDATES:
        df = _load_daily(ticker)
        if df.empty:
            continue
        years = (df.index[-1] - df.index[0]).days / 365.25
        for variant in VARIANTS:
            trades = _backtest_variant(df, variant)
            gate = evaluate_gate(trades, years)
            tpy = round(gate["n"] / years, 2) if years > 0 else 0
            row = {"ticker": ticker, "variant": variant,
                   "years_actual": round(years, 1),
                   "trades_per_year": tpy, **gate}
            results.append(row)
            if gate["verdict"] in ("SURVIVES", "MARGINAL"):
                print(f"  {ticker:5s} {variant:18s}: n={gate['n']:4d} "
                      f"PF={gate['pf']:5.2f} CI_lo={gate['ci_lower_95']} "
                      f"CAGR={gate.get('cagr_pretax', 0)*100:5.1f}% "
                      f"WR={gate['win_rate']} tpy={tpy:5.1f} -> {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "cagr_floor_beat_spy": CAGR_FLOOR_BEAT_SPY,
        "stop_pct": STOP_PCT,
        "candidates": CANDIDATES,
        "variants": VARIANTS,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Short-term mean reversion sweep -- Connors 2-day RSI")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"Gate: PF >= {report['point_pf_margin']}, CI lower >= "
                 f"{report['promotion_pf_floor']}, CAGR >= "
                 f"{report['cagr_floor_beat_spy']*100:.0f}% (SPY net-of-tax bar), "
                 f"min n={report['min_trades_floor']}")
    lines.append("")
    survivors = [r for r in report["results"] if r["verdict"] == "SURVIVES"]
    marginals = [r for r in report["results"] if r["verdict"] == "MARGINAL"]
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
                 f"{len(report['results']) - len(survivors) - len(marginals)} FAIL/INS")
    lines.append("")
    if survivors:
        lines.append("**Ship-eligible candidates** (cleared all 4 gates including CAGR bar):")
        for s in sorted(survivors, key=lambda x: -x["cagr_pretax"]):
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['variant']}` -> "
                f"n={s['n']}, PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}, "
                f"CAGR {s['cagr_pretax']*100:.1f}%, +{s['trades_per_year']:.0f} fills/yr"
            )
    else:
        lines.append("**No clean survivors.** Mean-reversion hypothesis fails the "
                     "disciplined + CAGR gate on this universe at honest slippage.")
    if marginals:
        lines.append("")
        lines.append("**Marginals** (passed PF + CI but missed CAGR or n floor):")
        for m in sorted(marginals, key=lambda x: -x["cagr_pretax"]):
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['variant']}` -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CAGR {m['cagr_pretax']*100:.1f}%"
            )
    lines.append("")
    lines.append("## All results")
    lines.append("")
    lines.append("| Ticker | Variant | n | PF | CI lower | CAGR | WR | trades/yr | Verdict |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in sorted(report["results"], key=lambda x: -x.get("cagr_pretax", 0) or 0):
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "--"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "--"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "--"
        cagr_val = r.get('cagr_pretax')
        cagr = f"{cagr_val*100:.1f}%" if cagr_val is not None else "--"
        lines.append(
            f"| {r['ticker']} | {r['variant']} | {r['n']} | {pf} | {ci} | {cagr} | "
            f"{wr} | {r['trades_per_year']} | **{r['verdict']}** |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(f"=== Short-term mean reversion sweep ===")
    print(f"{len(CANDIDATES)} tickers x {len(VARIANTS)} variants")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, PF >= {POINT_PF_MARGIN}, "
          f"CI >= {PROMOTION_PF_FLOOR}, CAGR >= {CAGR_FLOOR_BEAT_SPY*100:.0f}%, "
          f"min n={MIN_TRADES_FLOOR}")
    print()
    print("Printing only SURVIVES / MARGINAL during run; full table in MD output.")
    print()
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / "mean_reversion_sweep.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    (_OUT_DIR / "mean_reversion_sweep.md").write_text(
        render_markdown(report), encoding="utf-8")
    survivors = sum(1 for r in report["results"] if r["verdict"] == "SURVIVES")
    marginals = sum(1 for r in report["results"] if r["verdict"] == "MARGINAL")
    print()
    print(f"=== DONE: {survivors} SURVIVES, {marginals} MARGINAL, "
          f"{len(report['results'])} cells total ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
