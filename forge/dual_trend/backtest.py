"""forge.dual_trend.backtest — absolute-momentum trend-following.

For each asset in the universe, each month-end:
  - Compute trailing 12-month total return (close-to-close)
  - If > rf_annual_threshold (or > 0 if no threshold), include in
    next month's portfolio at equal weight
  - Else: held in cash earning the risk-free rate

Monthly rebalance. Per-trade pnls are per-asset, per-held-month.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


BROAD_8 = ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT"]
DEFAULT_LOOKBACK_MONTHS = 12
DEFAULT_RF_ANNUAL = 0.045   # ~ current 3mo T-bill


@dataclass
class TrendTrade:
    ticker: str
    month: str   # YYYY-MM
    return_pct: float

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "month": self.month,
                 "pnl_pct": round(self.return_pct, 4)}


def _fetch_monthly_closes(universe: list[str], period: str = "20y") -> pd.DataFrame:
    """Daily closes resampled to month-end. auto_adjust=True for accurate
    total returns (dividends matter on bonds, EFA, etc.)."""
    import yfinance as yf
    df = yf.download(universe, period=period, interval="1d",
                       progress=False, auto_adjust=True,
                       group_by="ticker", threads=False)
    out: dict[str, pd.Series] = {}
    for t in universe:
        if (t, "Close") in df.columns:
            out[t] = df[(t, "Close")]
    closes = pd.DataFrame(out).dropna(how="all")
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    monthly = closes.resample("ME").last()
    return monthly


def backtest(
    *,
    universe: list[str] | None = None,
    period: str = "20y",
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    rf_annual: float = DEFAULT_RF_ANNUAL,
) -> dict:
    """Run the absolute-momentum trend backtest. Returns summary +
    per-trade ledger (one entry per asset per month held)."""
    universe = universe if universe is not None else list(BROAD_8)
    monthly = _fetch_monthly_closes(universe, period=period)
    if monthly.empty or len(monthly) < lookback_months + 2:
        return {"error": f"insufficient data: {len(monthly)} months"}

    rf_monthly = (1.0 + rf_annual) ** (1 / 12) - 1.0
    # rf_annual threshold = rf_monthly compounded over lookback
    rf_lookback = (1.0 + rf_monthly) ** lookback_months - 1.0

    trades: list[TrendTrade] = []
    # For each month from lookback+1 onward, look back N months at the
    # closing price; if return > rf_lookback, take next month's return.
    for i in range(lookback_months, len(monthly) - 1):
        month_end = monthly.index[i]
        next_month_end = monthly.index[i + 1]
        for ticker in universe:
            if ticker not in monthly.columns:
                continue
            past_px = monthly[ticker].iloc[i - lookback_months]
            now_px = monthly[ticker].iloc[i]
            next_px = monthly[ticker].iloc[i + 1]
            if pd.isna(past_px) or pd.isna(now_px) or pd.isna(next_px):
                continue
            if past_px <= 0:
                continue
            lookback_ret = float(now_px / past_px - 1.0)
            if lookback_ret <= rf_lookback:
                continue  # asset's trailing return below T-bill → stay flat
            # Take next month's return
            next_ret = float(next_px / now_px - 1.0)
            trades.append(TrendTrade(
                ticker=ticker,
                month=next_month_end.strftime("%Y-%m"),
                return_pct=next_ret * 100.0,
            ))

    if not trades:
        return {"error": "no trades produced", "n_months": len(monthly)}

    pnls = [t.return_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)
    avg = sum(pnls) / len(pnls)

    # Portfolio equity curve: each month, average the per-month per-asset
    # returns across all assets that fired.
    by_month: dict[str, list[float]] = {}
    for t in trades:
        by_month.setdefault(t.month, []).append(t.return_pct / 100.0)
    monthly_port_ret: dict[str, float] = {}
    for m in sorted(by_month):
        rets = by_month[m]
        if not rets:
            continue
        # Equal-weight across the assets that fired that month
        port_ret = sum(rets) / len(rets)
        monthly_port_ret[m] = port_ret

    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for m in sorted(monthly_port_ret):
        eq *= (1.0 + monthly_port_ret[m])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    n_months = len(monthly_port_ret)
    cagr = (eq ** (12.0 / n_months) - 1.0) * 100.0 if n_months > 0 else 0.0

    # Fraction-of-time-invested metric
    n_total_slots = (len(monthly) - lookback_months - 1) * len(universe)
    fraction_invested = len(trades) / n_total_slots if n_total_slots else 0.0

    return {
        "universe": universe,
        "lookback_months": lookback_months,
        "rf_annual": rf_annual,
        "n_trades_assetmonths": len(trades),
        "n_portfolio_months": n_months,
        "fraction_time_invested": round(fraction_invested, 3),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 3),
        "avg_pct_per_assetmonth": round(avg, 4),
        "compound_growth_pct": round((eq - 1.0) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "first_month": min(t.month for t in trades),
        "last_month": max(t.month for t in trades),
        "trades_detail": [t.to_dict() for t in trades],
        "monthly_portfolio_returns": monthly_port_ret,
    }


def per_trade_pnls(result: dict) -> list[float]:
    return [float(t["pnl_pct"]) for t in result.get("trades_detail", [])]


def per_trade_dates(result: dict) -> list[str]:
    return [t["month"] for t in result.get("trades_detail", [])]
