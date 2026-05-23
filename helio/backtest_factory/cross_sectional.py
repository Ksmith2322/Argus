"""Cross-sectional momentum harness for the backtest factory.

The factory's engine is single-ticker — it can't natively express "long
the top K of N by metric X." This harness implements that paradigm
deliberately separately, then emits trades in the SAME schema the
factory's bootstrap CI tools consume.

Validation target: forge_xs_momentum-style edges (12-1 momentum on an
ETF universe, monthly rebalance, long top quintile).

Mechanism:
    For each rebalance date (monthly):
        - Compute 12-1 momentum for each ticker
          = (close[t-21] / close[t-252]) - 1
          (skip the most recent ~1 month per Jegadeesh-Titman 1993)
        - Rank tickers, take top-K as positions
        - At next rebalance: close all positions, compute new top-K

A "trade" is one (ticker, entry, exit) — pnl_pct is the held-period
return per ticker. Bootstrap PF CI then evaluates the strategy.

Same discipline as the factory: 20y data + slippage + period stability.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class XSMomentumResult:
    n_trades: int
    trades: list  # list of {"ticker", "entry_dt", "exit_dt", "pnl_pct", ...}


def _load_universe_closes(
    tickers: list[str],
    data_dir: Path,
    suffix: str = "_daily",
) -> pd.DataFrame:
    """Build a wide DataFrame: index=Date, columns=Tickers, values=Close."""
    frames = {}
    for t in tickers:
        path = data_dir / f"{t}{suffix}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
        frames[t] = df["Close"]
    if not frames:
        raise ValueError(f"no usable tickers found in {data_dir}")
    combined = pd.DataFrame(frames)
    return combined.dropna(how="any")  # intersection of dates


def _month_starts(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Return the first trading day of each month in the index."""
    months = index.to_series().groupby([index.year, index.month]).first()
    return list(months.values)


def run_xs_momentum(
    tickers: list[str],
    *,
    data_dir: Path,
    suffix: str = "_daily",
    lookback_days: int = 252,
    skip_recent_days: int = 21,
    top_k: int = 2,
    slippage_bps: float = 0.0,
    absolute_filter: bool = False,
    safe_ticker: str | None = None,
    short_bottom_k: int = 0,
) -> XSMomentumResult:
    """Run 12-1 momentum on the universe, monthly rebalance.

    top_k: number of tickers to hold long each period.

    absolute_filter: Antonacci-style dual momentum. If True, only enter
        tickers whose absolute momentum (return over lookback) is > 0. If
        a ticker would be in the top_k by relative momentum but has
        negative absolute momentum, skip it (potentially holding fewer than
        top_k positions, or going entirely to `safe_ticker` if specified).

    safe_ticker: When absolute_filter=True and no tickers pass the filter,
        allocate to this safe asset instead (typically a bond ETF). If
        None, the harness sits in cash (no positions) when the universe
        fails absolute momentum.

    short_bottom_k: When > 0, also SHORT the bottom-K tickers by relative
        momentum each period (long-short / market-neutral mode). The
        emitted trade for a short has direction="SHORT" and pnl_pct is
        computed inverted (entry - exit) / entry. Combining top_k longs
        + short_bottom_k shorts mimics the AQR market-neutral momentum
        factor.
    """
    # Auto-include safe_ticker in the load list so the dual-momentum
    # fallback branch can find its data. It is NOT included in ranking.
    load_list = list(tickers)
    if safe_ticker and safe_ticker not in load_list:
        load_list.append(safe_ticker)
    closes = _load_universe_closes(load_list, data_dir, suffix)
    n_universe = len(tickers)  # ranking universe excludes safe_ticker
    if top_k > n_universe:
        raise ValueError(f"top_k ({top_k}) > universe size ({n_universe})")

    rebal_dates = _month_starts(closes.index)
    trades = []
    held_positions: Dict[str, pd.Timestamp] = {}  # ticker -> entry_dt
    held_entry_prices: Dict[str, float] = {}
    held_directions: Dict[str, str] = {}  # ticker -> "LONG" or "SHORT"

    for i, rebal in enumerate(rebal_dates):
        # Close prior positions (entry was at previous rebal_date)
        if held_positions and i > 0:
            exit_dt = rebal
            for t, entry_dt in held_positions.items():
                try:
                    exit_px = float(closes.at[exit_dt, t])
                    entry_px = held_entry_prices[t]
                except (KeyError, IndexError):
                    continue
                direction = held_directions.get(t, "LONG")
                if direction == "LONG":
                    gross = (exit_px - entry_px) / entry_px * 100.0
                else:  # SHORT — profit when price falls
                    gross = (entry_px - exit_px) / entry_px * 100.0
                pnl_pct = gross - (slippage_bps / 100.0)
                bars_held = int((pd.Timestamp(exit_dt) - pd.Timestamp(entry_dt)).days)
                trades.append({
                    "ticker": t,
                    "entry_dt": pd.Timestamp(entry_dt),
                    "exit_dt": pd.Timestamp(exit_dt),
                    "entry_price": round(entry_px, 4),
                    "exit_price": round(exit_px, 4),
                    "pnl_pct": round(pnl_pct, 4),
                    "gross_pnl_pct": round(gross, 4),
                    "bars_held": bars_held,
                    "direction": direction,
                    "exit_reason": "rebalance",
                })
            held_positions.clear()
            held_entry_prices.clear()
            held_directions.clear()

        # Compute 12-1 momentum at this rebalance: price (skip_recent_days) / price (lookback_days)
        # We need closes.iloc[rebal_idx - skip_recent_days] / closes.iloc[rebal_idx - lookback_days] - 1
        rebal_pos = closes.index.get_loc(rebal)
        if rebal_pos < lookback_days:
            # Not enough history for momentum calc
            continue
        recent_px = closes.iloc[rebal_pos - skip_recent_days]
        old_px = closes.iloc[rebal_pos - lookback_days]
        mom = (recent_px / old_px) - 1.0
        # Restrict ranking to the ranking universe (excludes safe_ticker
        # which is loaded separately as a fallback asset).
        rank_mom = mom.loc[[t for t in tickers if t in mom.index]]
        winners = rank_mom.sort_values(ascending=False).head(top_k).index.tolist()
        # Dual-momentum filter: drop tickers with negative absolute momentum.
        if absolute_filter:
            winners = [t for t in winners if rank_mom.get(t, -1.0) > 0.0]
            # If no tickers pass and a safe asset is named, allocate there
            if not winners and safe_ticker is not None and safe_ticker in closes.columns:
                winners = [safe_ticker]
        # Pick shorts (bottom-K by relative momentum) for long-short mode
        shorts: list[str] = []
        if short_bottom_k > 0:
            shorts = rank_mom.sort_values(ascending=True).head(short_bottom_k).index.tolist()
            # Don't short tickers we're already long
            shorts = [s for s in shorts if s not in winners]
        # Open positions at this rebal_date
        for t in winners:
            entry_px = float(closes.at[rebal, t])
            held_positions[t] = rebal
            held_entry_prices[t] = entry_px
            held_directions[t] = "LONG"
        for t in shorts:
            entry_px = float(closes.at[rebal, t])
            held_positions[t] = rebal
            held_entry_prices[t] = entry_px
            held_directions[t] = "SHORT"

    return XSMomentumResult(n_trades=len(trades), trades=trades)
