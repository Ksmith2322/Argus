"""ares/strategies/rotation.py -- Relative strength sector rotation.

Core logic:
  1. Rank ETFs by momentum (weighted: 3-month 50%, 1-month 30%, 1-week 20%)
  2. Buy top N (default 2) — equal weight
  3. Rebalance monthly
  4. Crash filter: if SPY < 200-day EMA, go to cash (risk-off)

This is one of the most studied and replicated quantitative strategies.
Simple, mechanical, no judgment calls.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


# Default sector universe
DEFAULT_UNIVERSE = ["SPY", "QQQ", "GDX", "XLE", "SMH", "XBI"]

# Extended universe options
EXTENDED_UNIVERSE = [
    "SPY", "QQQ", "GDX", "XLE", "SMH", "XBI",
    "IWM", "TLT", "EEM", "ARKK", "GLD", "SLV",
]


@dataclass
class RotationSignal:
    """Monthly rotation recommendation."""
    date: str
    rankings: list[dict]         # [{symbol, score, rank, return_3m, return_1m, return_1w}]
    buy: list[str]               # Top N to hold
    sell: list[str]              # Positions to exit (not in top N)
    hold: list[str]              # Already held and still in top N
    risk_off: bool               # SPY below 200 EMA = go to cash
    risk_off_reason: str = ""
    spy_vs_200: float = 0.0      # SPY distance from 200 EMA %


def compute_momentum_score(prices: pd.Series, current_idx: int = -1) -> dict:
    """Compute composite momentum score from multiple lookbacks.

    Returns dict with score and component returns.
    """
    if len(prices) < 63:  # need at least 3 months
        return {"score": 0, "return_3m": 0, "return_1m": 0, "return_1w": 0}

    close = prices.values
    i = current_idx if current_idx >= 0 else len(close) - 1

    # Returns at different lookbacks
    ret_3m = (close[i] / close[max(0, i - 63)] - 1) * 100 if i >= 63 else 0
    ret_1m = (close[i] / close[max(0, i - 21)] - 1) * 100 if i >= 21 else 0
    ret_1w = (close[i] / close[max(0, i - 5)] - 1) * 100 if i >= 5 else 0

    # Composite score: weighted average
    score = ret_3m * 0.50 + ret_1m * 0.30 + ret_1w * 0.20

    return {
        "score": round(score, 3),
        "return_3m": round(ret_3m, 2),
        "return_1m": round(ret_1m, 2),
        "return_1w": round(ret_1w, 2),
    }


def rank_sectors(
    data: dict[str, pd.DataFrame],
    universe: list[str] | None = None,
    date_idx: int = -1,
) -> list[dict]:
    """Rank sectors by momentum score.

    Args:
        data: {symbol: DataFrame with 'Close' column}
        universe: list of symbols to rank
        date_idx: bar index to evaluate at (-1 = latest)

    Returns sorted list of {symbol, score, rank, return_3m, return_1m, return_1w}
    """
    symbols = universe or DEFAULT_UNIVERSE
    rankings = []

    for sym in symbols:
        df = data.get(sym)
        if df is None or len(df) < 63:
            continue

        close = df["Close"]
        momentum = compute_momentum_score(close, date_idx)

        rankings.append({
            "symbol": sym,
            **momentum,
        })

    # Sort by score descending
    rankings.sort(key=lambda x: x["score"], reverse=True)

    # Add rank
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    return rankings


def check_risk_off(spy_data: pd.DataFrame, date_idx: int = -1) -> tuple[bool, float]:
    """Check if SPY is below 200-day EMA (risk-off signal).

    Returns (is_risk_off, spy_distance_from_200_pct).
    """
    if spy_data is None or len(spy_data) < 200:
        return False, 0.0

    close = spy_data["Close"].values
    i = date_idx if date_idx >= 0 else len(close) - 1
    ema_200 = pd.Series(close).ewm(span=200).mean().values

    dist_pct = (close[i] - ema_200[i]) / ema_200[i] * 100
    risk_off = close[i] < ema_200[i]

    return risk_off, round(dist_pct, 2)


def generate_signal(
    data: dict[str, pd.DataFrame],
    current_holdings: list[str] | None = None,
    top_n: int = 2,
    universe: list[str] | None = None,
    date_idx: int = -1,
) -> RotationSignal:
    """Generate monthly rotation signal.

    Args:
        data: {symbol: DataFrame}
        current_holdings: symbols currently held
        top_n: how many sectors to hold
        universe: symbols to rank
        date_idx: bar index (-1 = latest)
    """
    current = set(current_holdings or [])
    symbols = universe or DEFAULT_UNIVERSE

    # Rank sectors
    rankings = rank_sectors(data, universe=symbols, date_idx=date_idx)

    if not rankings:
        return RotationSignal(
            date=datetime.now().strftime("%Y-%m-%d"),
            rankings=[], buy=[], sell=[], hold=[],
            risk_off=False, spy_vs_200=0.0,
        )

    # Check risk-off (SPY < 200 EMA)
    risk_off, spy_dist = check_risk_off(data.get("SPY"), date_idx)

    # Top N symbols
    top_symbols = [r["symbol"] for r in rankings[:top_n]]

    if risk_off:
        # Risk-off: sell everything, go to cash
        return RotationSignal(
            date=datetime.now().strftime("%Y-%m-%d"),
            rankings=rankings,
            buy=[],
            sell=list(current),
            hold=[],
            risk_off=True,
            risk_off_reason=f"SPY {spy_dist:+.1f}% from 200 EMA — CASH",
            spy_vs_200=spy_dist,
        )

    buy = [s for s in top_symbols if s not in current]
    sell = [s for s in current if s not in top_symbols]
    hold = [s for s in current if s in top_symbols]

    return RotationSignal(
        date=datetime.now().strftime("%Y-%m-%d"),
        rankings=rankings,
        buy=buy,
        sell=sell,
        hold=hold,
        risk_off=False,
        spy_vs_200=spy_dist,
    )


def backtest(
    data: dict[str, pd.DataFrame],
    universe: list[str] | None = None,
    top_n: int = 2,
    rebalance_freq: int = 21,  # trading days (~monthly)
    start_equity: float = 10000,
) -> dict:
    """Backtest the rotation strategy.

    Returns dict with equity curve, trades, and summary stats.
    """
    symbols = universe or DEFAULT_UNIVERSE

    # Find common date range
    spy = data.get("SPY")
    if spy is None:
        return {"error": "SPY data required"}

    n_bars = len(spy)
    warmup = 200  # need 200 bars for EMA
    if n_bars < warmup + rebalance_freq:
        return {"error": "Insufficient data"}

    equity = start_equity
    equity_curve = []
    trades = []
    holdings = {}  # {symbol: {shares, entry_price, entry_date}}
    rebalance_dates = []

    for i in range(warmup, n_bars):
        date = spy.index[i]

        # Daily equity update
        portfolio_value = 0
        for sym, pos in holdings.items():
            sym_df = data.get(sym)
            if sym_df is not None and i < len(sym_df):
                portfolio_value += pos["shares"] * sym_df["Close"].iloc[i]

        cash = equity - sum(pos["shares"] * pos["entry_price"] for pos in holdings.values())
        total_value = cash + portfolio_value
        equity_curve.append({"date": str(date)[:10], "equity": round(total_value, 2)})

        # Rebalance check (every N trading days)
        bars_since_last = i - rebalance_dates[-1] if rebalance_dates else rebalance_freq + 1
        if bars_since_last < rebalance_freq:
            continue

        rebalance_dates.append(i)

        # Generate signal
        signal = generate_signal(
            data,
            current_holdings=list(holdings.keys()),
            top_n=top_n,
            universe=symbols,
            date_idx=i,
        )

        # Execute sells
        for sym in signal.sell:
            if sym in holdings:
                pos = holdings[sym]
                sym_df = data.get(sym)
                if sym_df is not None and i < len(sym_df):
                    exit_price = sym_df["Close"].iloc[i]
                    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                    pnl_usd = pos["shares"] * (exit_price - pos["entry_price"])
                    entry_date = pos["entry_date"]
                    days_held = (date - pd.Timestamp(entry_date)).days if hasattr(date, 'day') else 0

                    trades.append({
                        "symbol": sym,
                        "direction": "long",
                        "entry_date": str(entry_date)[:10],
                        "exit_date": str(date)[:10],
                        "entry_price": round(pos["entry_price"], 2),
                        "exit_price": round(exit_price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_usd": round(pnl_usd, 2),
                        "days_held": days_held,
                        "exit_reason": "risk_off" if signal.risk_off else "rotation",
                        "rank_at_exit": next((r["rank"] for r in signal.rankings if r["symbol"] == sym), 0),
                    })
                    equity += pnl_usd
                del holdings[sym]

        # Risk-off: skip buys
        if signal.risk_off:
            continue

        # Execute buys (equal weight among top N)
        available_cash = equity - sum(pos["shares"] * pos["entry_price"] for pos in holdings.values())
        slots_to_fill = top_n - len(holdings)
        if slots_to_fill <= 0 or not signal.buy:
            continue

        per_slot = available_cash / slots_to_fill

        for sym in signal.buy:
            if sym in holdings:
                continue
            sym_df = data.get(sym)
            if sym_df is None or i >= len(sym_df):
                continue

            price = sym_df["Close"].iloc[i]
            shares = int(per_slot / price) if price > 0 else 0
            if shares <= 0:
                continue

            holdings[sym] = {
                "shares": shares,
                "entry_price": price,
                "entry_date": date,
            }

    # Final equity
    final_portfolio = 0
    for sym, pos in holdings.items():
        sym_df = data.get(sym)
        if sym_df is not None:
            final_portfolio += pos["shares"] * sym_df["Close"].iloc[-1]
    cash = equity - sum(pos["shares"] * pos["entry_price"] for pos in holdings.values())
    final_equity = cash + final_portfolio

    # Summary stats
    if not trades:
        return {"trades": 0, "equity_curve": equity_curve}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Max drawdown from equity curve
    peak = 0
    max_dd = 0
    for point in equity_curve:
        eq = point["equity"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    total_return = (final_equity / start_equity - 1) * 100
    years = len(equity_curve) / 252 if len(equity_curve) > 0 else 1
    ann_return = ((1 + total_return / 100) ** (1 / years) - 1) * 100 if years > 0 else 0

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 999,
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(ann_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_hold_days": round(np.mean([t["days_held"] for t in trades]), 1),
        "start_equity": start_equity,
        "final_equity": round(final_equity, 2),
        "rebalances": len(rebalance_dates),
        "equity_curve": equity_curve,
        "trade_list": trades,
        "years": round(years, 1),
    }
