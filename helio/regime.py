"""Regime-detection gates for fleet-level de-risking.

The factor decomposition (5/19) showed xs_momentum is captured
factor beta, not skill. The 37% backtest max-DD comes from holding
through bear markets identically.

Regime gates skip (or scale down) trading when market conditions
suggest the long-bias edge is impaired. This module is pure
functions — strategies opt in by calling these and bypassing
entry on FALSE.

Three canonical gates:

  spy_above_200dma   — SPY > its 200-day simple moving average.
                       The classic Fama-French / Faber tactical
                       allocation signal. Out when SPY < 200d.

  vix_below_threshold — VIX < absolute threshold (default 25).
                       Above 25 indicates stress / crisis.

  vix_below_rolling   — VIX < its own N-day rolling mean.
                       Catches volatility regime shifts that
                       absolute-threshold misses.

USAGE
=====
    from helio.regime import (
        spy_above_200dma, vix_below_threshold,
        vix_below_rolling, REGIME_GATES,
    )
    bullish = spy_above_200dma(spy_closes, asof=today)
    if not bullish:
        return  # skip entry this cycle

DESIGN
======
Each gate function returns True/False for a given `asof` date,
using only data available AT or BEFORE that date (no look-ahead).
Pass closes as a pandas Series (date-indexed) or a numpy array of
floats with a separate `asof_index` integer.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ─── SPY > 200dma ────────────────────────────────────────────────────

def spy_above_200dma(
    closes: pd.Series | list[float],
    *,
    asof_index: int | None = None,
    window: int = 200,
) -> bool:
    """True when SPY's last close (at asof_index) is above its
    trailing N-day SMA."""
    if isinstance(closes, list):
        arr = np.asarray(closes, dtype=float)
    else:
        arr = closes.dropna().to_numpy()
    if asof_index is None:
        asof_index = len(arr) - 1
    if asof_index < window or asof_index >= len(arr):
        return False  # insufficient history
    last_px = arr[asof_index]
    sma = float(np.mean(arr[asof_index - window + 1: asof_index + 1]))
    return bool(last_px > sma)


# ─── VIX gates ───────────────────────────────────────────────────────

def vix_below_threshold(
    vix_closes: pd.Series | list[float],
    *,
    asof_index: int | None = None,
    threshold: float = 25.0,
) -> bool:
    """True when latest VIX close is below the absolute threshold."""
    if isinstance(vix_closes, list):
        arr = np.asarray(vix_closes, dtype=float)
    else:
        arr = vix_closes.dropna().to_numpy()
    if asof_index is None:
        asof_index = len(arr) - 1
    if asof_index < 0 or asof_index >= len(arr):
        return False
    return bool(arr[asof_index] < threshold)


def vix_below_rolling(
    vix_closes: pd.Series | list[float],
    *,
    asof_index: int | None = None,
    window: int = 60,
) -> bool:
    """True when latest VIX close is below its trailing N-day mean.
    Catches regime shifts that fixed-threshold misses (e.g. when VIX
    is elevated for months and finally starts to come down)."""
    if isinstance(vix_closes, list):
        arr = np.asarray(vix_closes, dtype=float)
    else:
        arr = vix_closes.dropna().to_numpy()
    if asof_index is None:
        asof_index = len(arr) - 1
    if asof_index < window or asof_index >= len(arr):
        return False
    last_vix = arr[asof_index]
    rolling = float(np.mean(arr[asof_index - window + 1: asof_index + 1]))
    return bool(last_vix < rolling)


# ─── Gate registry ──────────────────────────────────────────────────

REGIME_GATES = {
    "spy_above_200dma": {
        "fn": spy_above_200dma,
        "needs": ["SPY_closes"],
        "description": "SPY > its trailing 200-day SMA",
    },
    "vix_below_25": {
        "fn": lambda vix, **kw: vix_below_threshold(vix, threshold=25.0, **kw),
        "needs": ["VIX_closes"],
        "description": "VIX < 25",
    },
    "vix_below_30": {
        "fn": lambda vix, **kw: vix_below_threshold(vix, threshold=30.0, **kw),
        "needs": ["VIX_closes"],
        "description": "VIX < 30",
    },
    "vix_below_rolling_60": {
        "fn": lambda vix, **kw: vix_below_rolling(vix, window=60, **kw),
        "needs": ["VIX_closes"],
        "description": "VIX < its 60-day rolling mean",
    },
}


# ─── Convenience: fetch SPY + VIX from yfinance ─────────────────────

def fetch_regime_data(period: str = "20y") -> dict[str, pd.Series]:
    """Pull SPY + VIX daily closes for regime testing. Returns a dict
    with `SPY_closes` and `VIX_closes` — empty if fetch fails."""
    try:
        import yfinance as yf
    except Exception:
        return {}
    out: dict[str, pd.Series] = {}
    for symbol, key in (("SPY", "SPY_closes"), ("^VIX", "VIX_closes")):
        try:
            df = yf.download(symbol, period=period, interval="1d",
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close = df["Close"].dropna()
            close.index = pd.to_datetime(close.index).tz_localize(None)
            out[key] = close
        except Exception:
            pass
    return out
