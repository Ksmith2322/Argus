"""forge.tom_spy.backtest — SPY turn-of-month backtest.

Standalone backtest; no live runner. Produces a per-trade pnls list
that can be fed to helio.promotion_panel.run_panel to test whether the
classical TOM effect is statistically detectable in modern SPY data.

PARAMS
  - entry_offset: enter at close of the Nth-to-last trading day of
                  the month (default N=4 → 4th-to-last bar, hold from
                  close to subsequent T+M close).
  - exit_offset:  exit at close of the Mth trading day of the next
                  month (default M=3).
  - ticker:       SPY (default), or any liquid daily-priced security.

Trade lifecycle: enter at close of day T (T = entry signal day),
hold through end-of-month, exit at close of next month's day T+M.
Per-trade pnl_pct = (exit_close / entry_close - 1) * 100.

CAVEATS
  - The literature finds the effect HALVED post-1990 and may have
    fully disappeared post-2000. Run the period-stability layer
    (helio.promotion_panel) to confirm.
  - 5bps slippage is generous for SPY (~1bp is realistic on retail
    market orders); the gate is run at 10bps for an honest haircut.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


DEFAULT_TICKER = "SPY"
DEFAULT_ENTRY_OFFSET = 4   # close of 4th-to-last trading day of month
DEFAULT_EXIT_OFFSET = 3    # close of 3rd trading day of next month
DEFAULT_SLIPPAGE_BPS = 10.0


@dataclass
class TOMTrade:
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    pnl_pct: float
    holding_days: int

    def to_dict(self) -> dict:
        return {
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "entry_px": round(self.entry_px, 4),
            "exit_px": round(self.exit_px, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "holding_days": self.holding_days,
        }


def _fetch_daily(ticker: str, period: str = "20y") -> pd.DataFrame:
    """Daily closes via yfinance. auto_adjust=True because TOM is a
    multi-day hold; dividend cash flows must be included in the
    realized return."""
    import yfinance as yf
    df = yf.download(ticker, period=period, interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _month_groups(df: pd.DataFrame) -> dict[str, list[int]]:
    """Return {YYYY-MM: [row_indices, ...]} grouped by month."""
    groups: dict[str, list[int]] = {}
    for i, dt in enumerate(df.index):
        key = f"{dt.year}-{dt.month:02d}"
        groups.setdefault(key, []).append(i)
    return groups


def backtest(
    *,
    ticker: str = DEFAULT_TICKER,
    period: str = "20y",
    entry_offset: int = DEFAULT_ENTRY_OFFSET,
    exit_offset: int = DEFAULT_EXIT_OFFSET,
) -> dict:
    """Run a TOM backtest. Returns summary + per-trade ledger.

    For each month M with N trading days:
      - entry day = day index (N - entry_offset) of M (close)
      - exit day  = day index (exit_offset - 1) of month M+1 (close)
    Skip months where the entry index would be negative (short month
    edge case) or where there's no next month (final partial month).
    """
    df = _fetch_daily(ticker, period=period)
    if df.empty or len(df) < 50:
        return {"error": f"insufficient data: {len(df)} bars"}

    months = _month_groups(df)
    month_keys = sorted(months.keys())
    trades: list[TOMTrade] = []

    for i, month in enumerate(month_keys[:-1]):  # need next month for exit
        cur_idxs = months[month]
        next_month = month_keys[i + 1]
        next_idxs = months[next_month]
        if len(cur_idxs) <= entry_offset:
            continue  # not enough bars in current month
        if len(next_idxs) <= exit_offset:
            continue  # not enough bars in next month
        entry_i = cur_idxs[-(entry_offset + 1)]
        exit_i = next_idxs[exit_offset]  # 0-indexed: 3rd trading day = index 2,
                                         # but exit_offset=3 means we want the 4th
                                         # (close of T+3 = end of T+3 bar)

        # Actually re-think: exit_offset=3 → enter at close of T-4 (4th-to-last
        # bar of month), exit at close of T+3 (3rd trading day of next month,
        # i.e. INDEX 2 since 0-indexed). Use exit_offset-1 as the index then.
        # But the convention in literature is "first 3 trading days" meaning
        # days 1, 2, 3 — and the exit happens at end of day 3 (= close), which
        # is index 2 in 0-indexed terms.
        # We want to use exit_offset as the human-readable "Mth day of next
        # month" → index = exit_offset - 1.
        if exit_offset - 1 >= len(next_idxs):
            continue
        exit_i = next_idxs[exit_offset - 1]

        entry_px = float(df["Close"].iloc[entry_i])
        exit_px = float(df["Close"].iloc[exit_i])
        if entry_px <= 0 or exit_px <= 0:
            continue
        pnl_pct = (exit_px / entry_px - 1.0) * 100.0
        trades.append(TOMTrade(
            entry_date=df.index[entry_i].strftime("%Y-%m-%d"),
            exit_date=df.index[exit_i].strftime("%Y-%m-%d"),
            entry_px=entry_px,
            exit_px=exit_px,
            pnl_pct=pnl_pct,
            holding_days=exit_i - entry_i,
        ))

    if not trades:
        return {"error": "no trades produced", "n_months_seen": len(months)}

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)
    avg = sum(pnls) / len(pnls)

    # Equity curve (compound)
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    # Annualised: ~12 trades/year
    n_years = len(trades) / 12.0
    cagr = (eq ** (1.0 / n_years) - 1.0) * 100.0 if n_years > 0 else 0.0

    # Also compute "rest-of-month" benchmark — buy SPY at start of each
    # entry window, sell at start of next entry window (i.e. continuously
    # held). Useful sanity check: TOM should not just be matching SPY.
    spy_total_return = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1.0) * 100.0
    n_years_total = (df.index[-1] - df.index[0]).days / 365.25
    spy_cagr = ((df["Close"].iloc[-1] / df["Close"].iloc[0])
                ** (1.0 / n_years_total) - 1.0) * 100.0

    return {
        "ticker": ticker,
        "period": period,
        "entry_offset": entry_offset,
        "exit_offset": exit_offset,
        "n_trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 3),
        "avg_pnl_pct": round(avg, 4),
        "total_pct": round(sum(pnls), 2),
        "compound_growth_pct": round((eq - 1.0) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "spy_buyhold_cagr_pct": round(spy_cagr, 2),
        "spy_buyhold_total_pct": round(spy_total_return, 2),
        "first_entry": trades[0].entry_date,
        "last_exit": trades[-1].exit_date,
        "trades_detail": [t.to_dict() for t in trades],
    }


def per_trade_pnls(result: dict) -> list[float]:
    """Helper: extract just the per-trade pnls list for promotion_panel."""
    return [float(t["pnl_pct"]) for t in result.get("trades_detail", [])]


def per_trade_dates(result: dict) -> list[str]:
    return [t["entry_date"] for t in result.get("trades_detail", [])]
