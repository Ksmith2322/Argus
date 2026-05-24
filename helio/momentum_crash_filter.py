"""Momentum crash filter — regime detector for cross-sectional momentum.

Cross-sectional momentum strategies have a documented "momentum crash"
failure mode: when the market regime reverses sharply (e.g., March 2020
COVID crash, late-2018 rate shock, August 2007 quant crash), the
strategy's largest-momentum picks get hit hardest because they were
the previously-winning concentrated positions.

Daniel + Moskowitz (2016) "Momentum Crashes" documented that:
  - Crashes occur in "bear market + high market volatility" regimes
  - A regime filter that goes to cash during these conditions
    materially improves Sharpe / reduces max DD

This module implements the regime detector and exposes a single
`is_crash_regime()` function that backtests + live runners can consult.

REGIME DEFINITION (parameter-tuneable, but defaults from the paper)

A "crash regime" is active when BOTH:
  (1) Market 6-month rolling return is negative (bear market signal)
  (2) Market realized volatility (60-day) is in the top quartile
      historically (high-uncertainty signal)

Both conditions matched simultaneously → expect mean-reversion
violence → cross-sectional momentum is vulnerable → go to cash.

USAGE

  from helio.momentum_crash_filter import is_crash_regime
  if is_crash_regime(spy_closes_through_date):
      # skip rebalance, hold cash
"""
from __future__ import annotations

import numpy as np
import pandas as pd


DEFAULT_BEAR_LOOKBACK_DAYS = 126     # ~6 months trading days
DEFAULT_VOL_LOOKBACK_DAYS = 60        # ~3 months
DEFAULT_VOL_HISTORY_DAYS = 1260       # ~5 years for quantile baseline
DEFAULT_VOL_QUANTILE = 0.75           # top 25% volatility


def compute_market_metrics(
    closes: pd.Series,
    *,
    bear_lookback: int = DEFAULT_BEAR_LOOKBACK_DAYS,
    vol_lookback: int = DEFAULT_VOL_LOOKBACK_DAYS,
) -> dict:
    """Compute the two crash-regime metrics from a daily SPY series:
      - bear_ret: trailing 6mo total return
      - realized_vol: 60-day annualised volatility (std of daily returns x sqrt(252))
    """
    if len(closes) < max(bear_lookback, vol_lookback) + 5:
        return {"bear_ret": None, "realized_vol": None, "n": len(closes)}
    p_now = float(closes.iloc[-1])
    p_6mo = float(closes.iloc[-(bear_lookback + 1)])
    bear_ret = (p_now / p_6mo) - 1.0 if p_6mo > 0 else None
    daily_returns = closes.pct_change().dropna()
    if len(daily_returns) < vol_lookback:
        return {"bear_ret": bear_ret, "realized_vol": None,
                "n": len(closes)}
    recent_returns = daily_returns.iloc[-vol_lookback:]
    realized_vol = float(recent_returns.std() * np.sqrt(252))
    return {"bear_ret": bear_ret, "realized_vol": realized_vol,
            "n": len(closes)}


def vol_quantile_threshold(
    closes: pd.Series,
    *,
    vol_lookback: int = DEFAULT_VOL_LOOKBACK_DAYS,
    history_days: int = DEFAULT_VOL_HISTORY_DAYS,
    quantile: float = DEFAULT_VOL_QUANTILE,
) -> float | None:
    """Compute the volatility threshold that 'high vol' means: the
    `quantile`-th percentile of trailing rolling-`vol_lookback`
    annualized vol over the past `history_days` days."""
    daily_returns = closes.pct_change().dropna()
    if len(daily_returns) < history_days:
        history_days = len(daily_returns)
    rolling_vol = daily_returns.rolling(vol_lookback).std() * np.sqrt(252)
    rolling_vol = rolling_vol.dropna()
    if len(rolling_vol) < 10:
        return None
    recent = rolling_vol.iloc[-history_days:] if len(rolling_vol) > history_days \
        else rolling_vol
    return float(recent.quantile(quantile))


def is_crash_regime(
    closes: pd.Series,
    *,
    bear_lookback: int = DEFAULT_BEAR_LOOKBACK_DAYS,
    vol_lookback: int = DEFAULT_VOL_LOOKBACK_DAYS,
    history_days: int = DEFAULT_VOL_HISTORY_DAYS,
    vol_quantile: float = DEFAULT_VOL_QUANTILE,
) -> bool:
    """Return True if the market is in a momentum-crash regime as of the
    LAST observation in `closes`.

    Crash regime requires BOTH:
      (1) trailing bear_lookback-day return is negative
      (2) trailing vol_lookback-day realized vol is above the
          vol_quantile-th percentile of historical rolling vol
    """
    metrics = compute_market_metrics(closes, bear_lookback=bear_lookback,
                                        vol_lookback=vol_lookback)
    if metrics["bear_ret"] is None or metrics["realized_vol"] is None:
        return False
    if metrics["bear_ret"] >= 0:
        return False
    threshold = vol_quantile_threshold(
        closes, vol_lookback=vol_lookback,
        history_days=history_days, quantile=vol_quantile,
    )
    if threshold is None:
        return False
    return metrics["realized_vol"] > threshold


def regime_signal_at_dates(
    closes: pd.Series,
    target_dates: list,
    *,
    bear_lookback: int = DEFAULT_BEAR_LOOKBACK_DAYS,
    vol_lookback: int = DEFAULT_VOL_LOOKBACK_DAYS,
    history_days: int = DEFAULT_VOL_HISTORY_DAYS,
    vol_quantile: float = DEFAULT_VOL_QUANTILE,
) -> dict:
    """For each target_date, return the crash-regime flag using only
    data UP TO that date (no look-ahead). Returns {date_str → bool}."""
    out: dict = {}
    # Ensure both sides are tz-naive for the comparison
    if closes.index.tz is not None:
        closes = closes.copy()
        closes.index = closes.index.tz_localize(None)
    for d in target_dates:
        d_ts = pd.Timestamp(d)
        if d_ts.tz is not None:
            d_ts = d_ts.tz_localize(None)
        # Use closes up to (and including) d
        sub = closes[closes.index <= d_ts]
        out[str(d)] = is_crash_regime(
            sub, bear_lookback=bear_lookback,
            vol_lookback=vol_lookback, history_days=history_days,
            vol_quantile=vol_quantile,
        )
    return out
