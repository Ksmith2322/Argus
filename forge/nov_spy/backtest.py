"""forge.nov_spy.backtest — November SPY single-month strategy.

Mechanics: long SPY at the first trading day of November (entry at
close of that day), sell at close of the last trading day of November.
One trade per year. n=30 with 30y of data.

The backtest mirrors forge.tom_spy.backtest's interface so the same
audit harness (run_seasonality_research / promotion_panel wrapper)
can consume it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


DEFAULT_TICKER = "SPY"


@dataclass
class NovTrade:
    year: int
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    pnl_pct: float
    holding_days: int

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "entry_px": round(self.entry_px, 4),
            "exit_px": round(self.exit_px, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "holding_days": self.holding_days,
        }


def _fetch_daily(ticker: str, period: str = "30y") -> pd.DataFrame:
    """Daily closes via yfinance. auto_adjust=True because we hold the
    full month — Q4 dividend ex-dates fall within the window."""
    import yfinance as yf
    df = yf.download(ticker, period=period, interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def backtest(
    *,
    ticker: str = DEFAULT_TICKER,
    period: str = "30y",
) -> dict:
    """Run a November-SPY backtest. Returns summary + per-trade ledger."""
    df = _fetch_daily(ticker, period=period)
    if df.empty or len(df) < 50:
        return {"error": f"insufficient data: {len(df)} bars"}

    # Group November-only bars by year
    nov = df[df.index.month == 11]
    if nov.empty:
        return {"error": "no November bars"}

    trades: list[NovTrade] = []
    for year in sorted(nov.index.year.unique()):
        year_nov = nov[nov.index.year == year]
        if len(year_nov) < 2:
            continue  # need at least first + last
        entry_dt = year_nov.index[0]
        exit_dt = year_nov.index[-1]
        entry_px = float(year_nov["Close"].iloc[0])
        exit_px = float(year_nov["Close"].iloc[-1])
        if entry_px <= 0 or exit_px <= 0:
            continue
        pnl_pct = (exit_px / entry_px - 1.0) * 100.0
        trades.append(NovTrade(
            year=int(year),
            entry_date=entry_dt.strftime("%Y-%m-%d"),
            exit_date=exit_dt.strftime("%Y-%m-%d"),
            entry_px=entry_px,
            exit_px=exit_px,
            pnl_pct=pnl_pct,
            holding_days=(exit_dt - entry_dt).days,
        ))

    if not trades:
        return {"error": "no November trades produced"}

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)
    avg = sum(pnls) / len(pnls)

    # Equity curve (compound across years)
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    # ~1 trade/year so CAGR = compound growth ^ (1/n_years) - 1
    n_years = len(trades)
    cagr_when_invested = (eq ** (1.0 / n_years) - 1.0) * 100.0 \
        if n_years > 0 else 0.0

    # SPY full-year buy-and-hold reference
    spy_total = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1.0) * 100.0
    days_total = (df.index[-1] - df.index[0]).days
    spy_cagr = ((df["Close"].iloc[-1] / df["Close"].iloc[0])
                ** (365.25 / max(days_total, 1)) - 1.0) * 100.0

    return {
        "ticker": ticker,
        "period": period,
        "n_trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 3),
        "avg_pnl_pct": round(avg, 4),
        "total_pct": round(sum(pnls), 2),
        "compound_growth_pct": round((eq - 1.0) * 100, 2),
        "cagr_when_invested_pct": round(cagr_when_invested, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "spy_buyhold_cagr_pct": round(spy_cagr, 2),
        "spy_buyhold_total_pct": round(spy_total, 2),
        "first_year": trades[0].year,
        "last_year": trades[-1].year,
        "trades_detail": [t.to_dict() for t in trades],
    }


def per_trade_pnls(result: dict) -> list[float]:
    return [float(t["pnl_pct"]) for t in result.get("trades_detail", [])]


def per_trade_dates(result: dict) -> list[str]:
    return [t["entry_date"] for t in result.get("trades_detail", [])]
