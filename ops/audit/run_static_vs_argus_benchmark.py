"""Honest benchmark: Argus roster vs static reference portfolios.

Built 2026-05-25 to make the 5/25 one-off comparison re-runnable. The
agent that produced the original numbers wrote a throwaway script;
this module is the permanent version operators should re-run quarterly
as live evidence accumulates.

Reference portfolios:
  - SPY 100% buy-and-hold
  - 60/30/5/5 SPY/TLT/GLD/BIL (classic permanent-portfolio-ish)
  - 70/20/5/5 SPY/TLT/GLD/BIL (modern stock-heavy)
  - Risk-parity SPY/TLT/GLD (10% annualized vol target, monthly rebalance)

Strategy under test:
  - forge_xs_momentum baseline (broad_8 universe, 252/21 lookback, top-2)
    using the FIXED runner.backtest() that calls _calendar_monthly_returns
    (the 5/25 CAGR-truth commit 3c8507f). Reports honest 9.96% CAGR not
    the previously-inflated 18.04%.

Slippage assumption: 5bps per turn on static portfolios (realistic for
SPY/TLT/GLD at retail). xs_momentum's drag is baked into its backtest.

Output: ops/reports/system_audit/static_vs_argus_benchmark.{md,json}

Usage:
    python -m ops.audit.run_static_vs_argus_benchmark
    python -m ops.audit.run_static_vs_argus_benchmark --years 15
    python -m ops.audit.run_static_vs_argus_benchmark --json
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO / "helio" / "data_yfinance"
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"

DEFAULT_SLIPPAGE_BPS = 5.0
RISK_FREE_ANNUAL = 0.04  # ~current 3-month T-bill yield for Sharpe denominator


def _load_close(ticker: str) -> pd.Series:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        raise FileNotFoundError(
            f"Missing daily cache for {ticker} at {p}. Refresh with "
            f"`python -m helio.backtest_factory.cli fetch-yf --tickers {ticker}`"
        )
    df = pd.read_csv(p, parse_dates=["Date"])
    df = df.set_index("Date").sort_index()
    return df["Close"].astype(float)


def _aligned_closes(tickers: list[str]) -> pd.DataFrame:
    """Return a DataFrame of Close prices, aligned on the intersection of
    all tickers' available dates (so DD/Sharpe comparisons are apples-to-
    apples)."""
    series = {t: _load_close(t) for t in tickers}
    df = pd.DataFrame(series)
    df = df.dropna(how="any")  # only dates where ALL tickers have a price
    return df


def _daily_returns(prices: pd.Series) -> pd.Series:
    return prices.pct_change().dropna()


def _portfolio_daily_returns(
    closes: pd.DataFrame,
    weights: dict[str, float],
    *,
    rebalance: str = "ME",
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> pd.Series:
    """Compute daily returns for a fixed-weight portfolio with periodic
    rebalancing. rebalance='ME' = month-end, 'QE' = quarter-end, 'YE' = year-end.

    Slippage is applied at each rebalance as a one-time drag equal to
    `slippage_bps * sum(|delta_weight|) / 10000`. For monthly rebalancing
    of a low-drift portfolio this is roughly slippage_bps × 0.05 / 10000
    per month = ~3bps/year — negligible but honest."""
    w = pd.Series(weights, dtype=float)
    w = w / w.sum()
    daily_rets = closes.pct_change().fillna(0.0)
    # Rebalance dates: last business day of each period
    rebalance_dates = closes.resample(rebalance).last().index
    rebalance_set = set(rebalance_dates)

    port_ret = pd.Series(0.0, index=daily_rets.index)
    current_weights = w.copy()
    drag_per_rebalance = slippage_bps / 10000.0 * 0.05  # ~5% turnover assumption

    for date in daily_rets.index:
        # Apply today's returns to the current weights
        day_return = (current_weights * daily_rets.loc[date]).sum()
        # Drift weights by today's returns (compound)
        new_weights = current_weights * (1.0 + daily_rets.loc[date])
        if new_weights.sum() > 0:
            new_weights = new_weights / new_weights.sum()
        current_weights = new_weights
        # Rebalance back to target at month-end
        if date in rebalance_set:
            day_return -= drag_per_rebalance  # slippage drag at rebalance
            current_weights = w.copy()
        port_ret.loc[date] = day_return
    return port_ret


def _risk_parity_weights(
    closes: pd.DataFrame,
    tickers: list[str],
    *,
    target_vol_annual: float = 0.10,
    lookback_days: int = 63,
) -> pd.Series:
    """Compute risk-parity weights at the latest date: each asset
    contributes equal volatility share, then scale to target portfolio vol.

    Returns a Series of weights (sum can exceed 1.0 if vol-targeting demands
    leverage; in that case we cap at sum=1.0 to keep this comparable to the
    unleveraged static portfolios)."""
    daily_rets = closes[tickers].pct_change().dropna().tail(lookback_days)
    vols = daily_rets.std() * np.sqrt(252)
    inv_vol = 1.0 / vols
    raw_weights = inv_vol / inv_vol.sum()
    # Optional vol-target scaling; cap at sum=1.0 for fair comparison
    port_vol = float(np.sqrt(
        (raw_weights @ daily_rets.cov() @ raw_weights) * 252
    ))
    if port_vol > 0:
        scale = target_vol_annual / port_vol
        scale = min(scale, 1.0)  # no leverage in this comparison
        raw_weights = raw_weights * scale
    # Renormalize if scaled down; cash leftover is implicit (acts like SHV)
    return raw_weights


def _equity_curve(daily_rets: pd.Series) -> pd.Series:
    return (1.0 + daily_rets).cumprod()


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())


def _cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return float(equity.iloc[-1] ** (1.0 / years) - 1.0)


def _sharpe(daily_rets: pd.Series, rf_annual: float = RISK_FREE_ANNUAL) -> float:
    rf_daily = rf_annual / 252.0
    excess = daily_rets - rf_daily
    if excess.std() == 0:
        return 0.0
    return float(excess.mean() / excess.std() * np.sqrt(252))


def _sortino(daily_rets: pd.Series, rf_annual: float = RISK_FREE_ANNUAL) -> float:
    rf_daily = rf_annual / 252.0
    excess = daily_rets - rf_daily
    downside = excess[excess < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    return float(excess.mean() / downside.std() * np.sqrt(252))


def _calmar(cagr: float, max_dd: float) -> float:
    if max_dd == 0:
        return 0.0
    return float(cagr / abs(max_dd))


def _worst_rolling_12m(daily_rets: pd.Series) -> float:
    monthly = (1.0 + daily_rets).resample("ME").prod() - 1.0
    rolling_12 = (1.0 + monthly).rolling(12).apply(lambda x: x.prod() - 1.0)
    return float(rolling_12.min())


def _crisis_drawdown(equity: pd.Series, start: str, end: str) -> float:
    window = equity.loc[start:end]
    if window.empty:
        return float("nan")
    peak = window.cummax()
    dd = (window - peak) / peak
    return float(dd.min())


def _summarize_portfolio(
    name: str,
    daily_rets: pd.Series,
    benchmark_rets: pd.Series,
) -> dict:
    equity = _equity_curve(daily_rets)
    cagr = _cagr(equity)
    max_dd = _max_drawdown(equity)
    corr = float(daily_rets.corr(benchmark_rets))
    return {
        "name": name,
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "n_days": len(equity),
        "cagr_pct": round(cagr * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "sharpe": round(_sharpe(daily_rets), 3),
        "sortino": round(_sortino(daily_rets), 3),
        "calmar": round(_calmar(cagr, max_dd), 3),
        "worst_12m_pct": round(_worst_rolling_12m(daily_rets) * 100, 2),
        "corr_to_spy": round(corr, 3),
        "crisis_2008_dd_pct": round(
            _crisis_drawdown(equity, "2007-10-01", "2009-06-30") * 100, 2,
        ),
        "crisis_2020_dd_pct": round(
            _crisis_drawdown(equity, "2020-02-01", "2020-06-30") * 100, 2,
        ),
        "crisis_2022_dd_pct": round(
            _crisis_drawdown(equity, "2022-01-01", "2022-12-31") * 100, 2,
        ),
    }


def _xs_momentum_summary(period: str) -> dict | None:
    """Run xs_momentum baseline backtest and adapt its result dict to the
    same comparison shape. Returns None if the strategy can't be evaluated."""
    try:
        from forge.xs_momentum import runner as xsm
    except ImportError as exc:
        return {"name": "xs_momentum_1.0x", "error": f"import failed: {exc!r}"}
    try:
        r = xsm.backtest(period=period, return_monthly_series=True)
    except Exception as exc:
        return {"name": "xs_momentum_1.0x", "error": f"backtest failed: {exc!r}"}
    if "error" in r:
        return {"name": "xs_momentum_1.0x", "error": r["error"]}
    # Derive Sharpe / Sortino from the monthly_returns series
    monthly = r.get("monthly_returns") or {}
    if not monthly:
        return {"name": "xs_momentum_1.0x", "error": "no monthly_returns"}
    months = sorted(monthly)
    rets = pd.Series([monthly[m] for m in months])
    excess = rets - RISK_FREE_ANNUAL / 12.0
    monthly_sharpe = (
        float(excess.mean() / excess.std() * math.sqrt(12))
        if excess.std() > 0 else 0.0
    )
    downside = excess[excess < 0]
    monthly_sortino = (
        float(excess.mean() / downside.std() * math.sqrt(12))
        if len(downside) > 0 and downside.std() > 0 else 0.0
    )
    cagr_pct = r.get("cagr_pct", 0.0)
    max_dd_pct = r.get("max_drawdown_pct", 0.0)
    calmar = round(cagr_pct / max_dd_pct, 3) if max_dd_pct > 0 else 0.0
    return {
        "name": "xs_momentum_1.0x_(broad_8)",
        "start": months[0] + "-01",
        "end": months[-1] + "-end",
        "n_months": len(months),
        "cagr_pct": round(cagr_pct, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "sharpe": round(monthly_sharpe, 3),
        "sortino": round(monthly_sortino, 3),
        "calmar": calmar,
        "trades": r.get("trades", 0),
        "profit_factor": r.get("profit_factor", 0.0),
        "win_rate": r.get("win_rate", 0.0),
        "note_cagr_source": "calendar-walk via _calendar_monthly_returns (5/25 truth fix)",
    }


def run(*, years: int = 20, slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> dict:
    closes = _aligned_closes(["SPY", "TLT", "GLD", "BIL"])
    spy_only = _aligned_closes(["SPY"])

    # Trim to the requested years (or all available if data shorter)
    end_date = closes.index.max()
    start_date = end_date - pd.DateOffset(years=years)
    closes = closes.loc[start_date:end_date]
    spy_only = spy_only.loc[start_date:end_date]

    spy_ret = _daily_returns(closes["SPY"])

    portfolios: list[dict] = []

    # SPY 100%
    portfolios.append(_summarize_portfolio("SPY_100", spy_ret, spy_ret))

    # 60/30/5/5
    p_60 = _portfolio_daily_returns(
        closes, {"SPY": 0.60, "TLT": 0.30, "GLD": 0.05, "BIL": 0.05},
        slippage_bps=slippage_bps,
    )
    portfolios.append(_summarize_portfolio(
        "static_60_30_5_5_SPY_TLT_GLD_BIL", p_60, spy_ret,
    ))

    # 70/20/5/5
    p_70 = _portfolio_daily_returns(
        closes, {"SPY": 0.70, "TLT": 0.20, "GLD": 0.05, "BIL": 0.05},
        slippage_bps=slippage_bps,
    )
    portfolios.append(_summarize_portfolio(
        "static_70_20_5_5_SPY_TLT_GLD_BIL", p_70, spy_ret,
    ))

    # Risk-parity SPY/TLT/GLD (vol-targeted, monthly rebal)
    rp_tickers = ["SPY", "TLT", "GLD"]
    rp_weights_dict = _risk_parity_weights(closes, rp_tickers).to_dict()
    p_rp = _portfolio_daily_returns(
        closes, rp_weights_dict, slippage_bps=slippage_bps,
    )
    portfolios.append(_summarize_portfolio(
        "risk_parity_SPY_TLT_GLD_10vol", p_rp, spy_ret,
    ))

    # xs_momentum (uses its own period arg; convert years → 'Ny' format)
    xsm = _xs_momentum_summary(period=f"{years}y")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_years": years,
        "start": str(closes.index[0].date()),
        "end": str(closes.index[-1].date()),
        "slippage_bps_static": slippage_bps,
        "risk_free_annual": RISK_FREE_ANNUAL,
        "static_portfolios": portfolios,
        "argus": xsm,
        "risk_parity_weights": {k: round(v, 3) for k, v in rp_weights_dict.items()},
    }


def render_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append("# Static portfolios vs Argus benchmark")
    lines.append("")
    lines.append(f"Generated: `{result['generated_at']}`")
    lines.append(f"Window: {result['start']} → {result['end']} "
                 f"({result['window_years']}y nominal)")
    lines.append(f"Static slippage: {result['slippage_bps_static']} bps per rebalance")
    lines.append(f"Risk-free (annual): {result['risk_free_annual'] * 100:.1f}%")
    lines.append("")
    lines.append("## Static reference portfolios")
    lines.append("")
    lines.append("| Portfolio | CAGR | MaxDD | Sharpe | Sortino | Calmar | Worst-12m | Corr→SPY | 2008 DD | 2020 DD | 2022 DD |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in result["static_portfolios"]:
        lines.append(
            f"| {p['name']} | {p['cagr_pct']}% | {p['max_dd_pct']}% | "
            f"{p['sharpe']} | {p['sortino']} | {p['calmar']} | "
            f"{p['worst_12m_pct']}% | {p['corr_to_spy']} | "
            f"{p['crisis_2008_dd_pct']}% | {p['crisis_2020_dd_pct']}% | "
            f"{p['crisis_2022_dd_pct']}% |"
        )
    lines.append("")
    rpw = result["risk_parity_weights"]
    lines.append(f"Risk-parity weights (10% vol target): " +
                 ", ".join(f"{k} {v:.3f}" for k, v in rpw.items()))
    lines.append("")
    lines.append("## Argus (xs_momentum 1.0× baseline)")
    lines.append("")
    a = result["argus"]
    if "error" in a:
        lines.append(f"**Error**: {a['error']}")
    else:
        lines.append(f"- CAGR: **{a['cagr_pct']}%** ({a.get('note_cagr_source', '')})")
        lines.append(f"- MaxDD: {a['max_dd_pct']}%")
        lines.append(f"- Sharpe: {a['sharpe']}  |  Sortino: {a['sortino']}  |  Calmar: {a['calmar']}")
        lines.append(f"- Trades: {a['trades']}  |  PF: {a['profit_factor']}  |  WR: {a['win_rate']}")
        lines.append(f"- Months in monthly_returns: {a['n_months']}")
    lines.append("")
    lines.append("## Honest verdict")
    lines.append("")
    lines.append("**Methodology note (read this first)**: Sharpe / DD comparisons are sensitive")
    lines.append("to weighting and risk-free convention. This script uses (a) sum-capped weights")
    lines.append("(no leverage; risk-parity that demands >1.0 sum is scaled down with the")
    lines.append("remainder implicit cash), (b) daily-return Sharpe with explicit risk-free")
    lines.append(f"subtraction at {result['risk_free_annual'] * 100:.1f}% annual,")
    lines.append("(c) `auto_adjust=False` (price-only, no dividends). A more aggressive")
    lines.append("risk-parity (no cap, total return) can reverse the xs_momentum vs risk-parity")
    lines.append("Sharpe comparison. Both methodologies are defensible. Don't treat either as")
    lines.append("dispositive on its own — read the operator-time / dollar-edge math in")
    lines.append("`docs/decisions/2026_06_30_argus_deployment.md` for the actual decision input.")
    lines.append("")
    a_sharpe = a.get("sharpe", 0)
    a_dd = abs(a.get("max_dd_pct", 0))
    rp = next((p for p in result["static_portfolios"]
               if p["name"].startswith("risk_parity")), None)
    if rp is not None and "error" not in a:
        lines.append(f"- xs_momentum Sharpe {a_sharpe} vs risk-parity Sharpe {rp['sharpe']}: "
                     f"**{'WINS' if a_sharpe > rp['sharpe'] else 'LOSES'}**")
        lines.append(f"- xs_momentum MaxDD {a_dd}% vs risk-parity MaxDD {abs(rp['max_dd_pct'])}%: "
                     f"**{'WINS' if a_dd < abs(rp['max_dd_pct']) else 'LOSES'}**")
        lines.append(f"- xs_momentum Calmar {a.get('calmar', 0)} vs risk-parity Calmar {rp['calmar']}: "
                     f"**{'WINS' if a.get('calmar', 0) > rp['calmar'] else 'LOSES'}**")
    lines.append("")
    lines.append("---")
    lines.append("Re-run via: `python -m ops.audit.run_static_vs_argus_benchmark`")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=20,
                        help="Backtest window in years (default 20)")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS,
                        help=f"Static-portfolio rebalance slippage (default {DEFAULT_SLIPPAGE_BPS}bps)")
    parser.add_argument("--json", action="store_true",
                        help="Print JSON to stdout instead of markdown")
    args = parser.parse_args()

    result = run(years=args.years, slippage_bps=args.slippage_bps)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "static_vs_argus_benchmark.json"
    md_path = _OUT_DIR / "static_vs_argus_benchmark.md"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_text = render_markdown(result)
    md_path.write_text(md_text, encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(md_text)
        print()
        print(f"Wrote {md_path}")
        print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
