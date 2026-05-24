"""Cohort risk audit — pairwise correlations + portfolio-level DD across
the active strategy roster.

Question: with 2 active strategies (xs_momentum 1.0×, gld_pm_long 0.5×)
and a third candidate (tom_spy 0.3× proposed), is the combined portfolio
actually diversified or are we just stacking the same MKT_RF beta?

Method:
  1. Get monthly returns for each strategy via its backtest helper.
  2. Align on common months.
  3. Pairwise Pearson correlations.
  4. Combined portfolio = w_i * r_i across the proposed weights; compute
     compound return, vol, max DD, Sharpe.
  5. Compare to:
     - 100% xs_momentum baseline (the 1.0× concentration)
     - 100% SPY buy-and-hold reference

Output: ops/reports/system_audit/cohort_risk_audit.md + JSON.

USAGE:
    python -m ops.audit.run_cohort_risk_audit
    python -m ops.audit.run_cohort_risk_audit --period 10y
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _xs_momentum_monthly(period: str) -> pd.Series:
    from forge.xs_momentum.runner import monthly_portfolio_returns
    monthly = monthly_portfolio_returns(period=period)
    return pd.Series(monthly, name="xs_momentum").astype(float)


def _gld_pm_long_monthly(period: str) -> pd.Series:
    """gld_pm_long is 1h cadence; aggregate to monthly via the same
    held-position-aware logic. The backtest emits per-trade pnls
    indexed by trade exit date; we resample to month-end."""
    from forge.gld_pm_long.runner import backtest as gld_backtest
    # gld backtest only prints, not returns — we re-implement minimally
    import yfinance as yf
    df = yf.download("GLD", period=period, interval="1d",
                      progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    # As a proxy for gld_pm_long's monthly return contribution, use
    # GLD's monthly buy-and-hold return scaled by the strategy's
    # time-in-market (~30% per the disciplined gate audit). This is a
    # conservative approximation — the actual strategy is hour-granular
    # and intraday, but its market exposure is GLD-direction-correlated.
    # The point of THIS audit is correlation across the cohort, not
    # absolute return estimation.
    monthly = df["Close"].resample("ME").last().pct_change().dropna()
    monthly = monthly * 0.30  # ~30% time-in-market proxy
    monthly.name = "gld_pm_long"
    return monthly


def _tom_spy_monthly(period: str) -> pd.Series:
    """Convert tom_spy per-trade pnls to a monthly series. TOM holds for
    ~7 days per cycle = roughly one trade per calendar month. So per-trade
    pnl_pct ≈ monthly return for that month (ignoring the 23 out-of-market
    days)."""
    from forge.tom_spy.backtest import backtest as tom_backtest
    bt = tom_backtest(ticker="SPY", period=period,
                       entry_offset=4, exit_offset=3)
    if "trades_detail" not in bt:
        return pd.Series(dtype=float, name="tom_spy")
    rows = []
    for t in bt["trades_detail"]:
        # Use the exit_date's YYYY-MM as the month key
        month = t["exit_date"][:7]
        rows.append({"month": month, "ret": t["pnl_pct"] / 100.0})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.Series(dtype=float, name="tom_spy")
    series = df.groupby("month")["ret"].sum()
    series.name = "tom_spy"
    return series


def _spy_buyhold_monthly(period: str) -> pd.Series:
    import yfinance as yf
    df = yf.download("SPY", period=period, interval="1d",
                      progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    monthly = df["Close"].resample("ME").last().pct_change().dropna()
    monthly.name = "spy_buyhold"
    return monthly


def _harmonize_indexes(*series: pd.Series) -> pd.DataFrame:
    """Convert any DateTimeIndex to YYYY-MM strings and inner-join."""
    normalized: list[pd.Series] = []
    for s in series:
        if isinstance(s.index, pd.DatetimeIndex):
            s = s.copy()
            s.index = s.index.strftime("%Y-%m")
        normalized.append(s)
    df = pd.concat(normalized, axis=1, join="inner")
    return df


def _portfolio_stats(returns: pd.Series, *, name: str) -> dict:
    """Compound return, annualised vol, max DD, Sharpe from a monthly series."""
    if returns.empty:
        return {"name": name, "error": "no data"}
    eq = (1.0 + returns).cumprod()
    peak = eq.cummax()
    dd = (peak - eq) / peak
    cagr = float(eq.iloc[-1]) ** (12.0 / len(returns)) - 1.0
    vol = float(returns.std()) * (12 ** 0.5)
    sharpe = (returns.mean() * 12 - 0.045) / (returns.std() * (12 ** 0.5)) \
        if returns.std() > 0 else 0.0
    return {
        "name": name,
        "n_months": int(len(returns)),
        "cagr_pct": round(cagr * 100, 3),
        "vol_pct": round(vol * 100, 3),
        "max_dd_pct": round(float(dd.max()) * 100, 3),
        "sharpe": round(float(sharpe), 3),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="10y")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] fetching monthly returns for each strategy (period={args.period})...")
    xsm = _xs_momentum_monthly(args.period)
    print(f"    xs_momentum:    {len(xsm)} months")
    gld = _gld_pm_long_monthly(args.period)
    print(f"    gld_pm_long:    {len(gld)} months (PROXY: 30%-time-weighted GLD)")
    tom = _tom_spy_monthly(args.period)
    print(f"    tom_spy:        {len(tom)} months")
    spy = _spy_buyhold_monthly(args.period)
    print(f"    spy_buyhold:    {len(spy)} months")

    print()
    print("[2] harmonising indexes + computing correlations...")
    panel = _harmonize_indexes(xsm, gld, tom, spy)
    print(f"    aligned panel: {panel.shape}")
    corr = panel.corr()
    print()
    print("Pairwise Pearson correlation:")
    print(corr.round(3).to_string())

    print()
    print("[3] candidate-portfolio assembly...")
    # Current weights (no tom_spy yet)
    w_current = {"xs_momentum": 1.0, "gld_pm_long": 0.5}
    # Proposed weights (add tom_spy at 0.3×)
    w_proposed = {"xs_momentum": 1.0, "gld_pm_long": 0.5, "tom_spy": 0.3}
    # Normalised so weights sum to 1 (for return calc)
    def _normalised(weights: dict) -> dict:
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    def _portfolio_returns(weights: dict) -> pd.Series:
        norm = _normalised(weights)
        cols = [c for c in norm if c in panel.columns]
        if not cols:
            return pd.Series(dtype=float)
        w = np.array([norm[c] for c in cols])
        return panel[cols].dot(w)

    portfolios = {
        "current (xs_mom 1.0 + gld_pm 0.5)": _portfolio_returns(w_current),
        "proposed (+ tom_spy 0.3)":           _portfolio_returns(w_proposed),
        "100% xs_momentum":                    panel.get("xs_momentum",
                                                          pd.Series(dtype=float)),
        "100% SPY buy-and-hold":                panel.get("spy_buyhold",
                                                            pd.Series(dtype=float)),
    }
    rows = []
    for name, ret in portfolios.items():
        if isinstance(ret, pd.Series) and not ret.empty:
            rows.append(_portfolio_stats(ret, name=name))
    print()
    print("Portfolio comparison:")
    print(f"{'portfolio':<45} {'CAGR':>7} {'vol':>7} {'maxDD':>7} {'Sharpe':>7}")
    print("-" * 80)
    for r in rows:
        if "error" in r:
            print(f"{r['name']:<45}  ERROR")
            continue
        print(f"{r['name']:<45} {r['cagr_pct']:>6.2f}% {r['vol_pct']:>6.2f}% "
              f"{r['max_dd_pct']:>6.2f}% {r['sharpe']:>+7.2f}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"cohort_risk_audit_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "n_months_aligned": int(panel.shape[0]),
        "correlation_matrix": corr.round(4).to_dict(),
        "portfolios": rows,
        "current_weights": w_current,
        "proposed_weights": w_proposed,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "cohort_risk_audit.md"
    md_lines: list[str] = []
    md_lines.append("# Cohort risk audit")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append(f"Period: {args.period}  ({panel.shape[0]} aligned months)")
    md_lines.append("")
    md_lines.append("## Pairwise Pearson correlation")
    md_lines.append("")
    md_lines.append("```")
    md_lines.append(corr.round(3).to_string())
    md_lines.append("```")
    md_lines.append("")
    md_lines.append("## Portfolio comparison")
    md_lines.append("")
    md_lines.append("| Portfolio | CAGR | Vol | Max DD | Sharpe |")
    md_lines.append("|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            continue
        md_lines.append(
            f"| {r['name']} | {r['cagr_pct']:.2f}% | {r['vol_pct']:.2f}% | "
            f"{r['max_dd_pct']:.2f}% | {r['sharpe']:+.2f} |"
        )
    md_lines.append("")
    md_lines.append("## Caveat")
    md_lines.append("")
    md_lines.append(
        "gld_pm_long's monthly contribution is approximated as 30%-of-"
        "GLD-monthly-return (its observed time-in-market fraction from "
        "the disciplined-gate audit). This is a CONSERVATIVE proxy — "
        "the actual strategy is intraday and may have weaker correlation "
        "with GLD direction than the proxy implies."
    )
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
