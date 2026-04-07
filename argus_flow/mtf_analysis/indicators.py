"""Technical indicators for multi-timeframe analysis.

All functions are pure: DataFrame in, result out. Uses numpy for
performance-critical paths.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── EMAs ─────────────────────────────────────────────────────────────

def compute_emas(df: pd.DataFrame, periods: list[int] | None = None) -> dict[int, pd.Series]:
    """Compute exponential moving averages on Close prices.

    Parameters
    ----------
    df : DataFrame with a ``Close`` column.
    periods : EMA periods (default [50, 100, 200]).

    Returns
    -------
    dict mapping period -> pd.Series of EMA values.
    """
    if periods is None:
        periods = [50, 100, 200]
    close = df["Close"].astype(float)
    return {p: close.ewm(span=p, adjust=False).mean() for p in periods}


# ── Bollinger Bands ──────────────────────────────────────────────────

def compute_bollinger(
    df: pd.DataFrame, period: int = 20, num_std: float = 2.0
) -> dict[str, pd.Series]:
    """Compute Bollinger Bands on Close prices.

    Returns
    -------
    dict with keys: upper, lower, middle, bandwidth, pct_b.
    ``pct_b`` maps price to 0 (lower band) .. 1 (upper band).
    """
    close = df["Close"].astype(float)
    middle = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    band_range = upper - lower
    bandwidth = band_range / middle
    pct_b = (close - lower) / band_range.replace(0, np.nan)
    return {
        "upper": upper,
        "lower": lower,
        "middle": middle,
        "bandwidth": bandwidth,
        "pct_b": pct_b,
    }


# ── RSI ──────────────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute RSI (Wilder smoothing) on Close prices."""
    close = df["Close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder's smoothed averages via ewm (alpha = 1/period)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi.name = "RSI"
    return rsi


def detect_rsi_divergence(df: pd.DataFrame, rsi: pd.Series, lookback: int = 20) -> str:
    """Detect RSI divergence over the last *lookback* bars.

    Returns
    -------
    "bullish"  — price makes lower low, RSI makes higher low.
    "bearish"  — price makes higher high, RSI makes lower high.
    "none"     — no divergence detected.
    """
    if len(df) < lookback or len(rsi) < lookback:
        return "none"

    price = df["Close"].iloc[-lookback:].values.astype(float)
    rsi_vals = rsi.iloc[-lookback:].values.astype(float)

    # Split window into halves to compare swings
    mid = lookback // 2

    # --- Bullish divergence: lower low in price, higher low in RSI ---
    price_low1_idx = int(np.nanargmin(price[:mid]))
    price_low2_idx = mid + int(np.nanargmin(price[mid:]))
    rsi_low1_idx = int(np.nanargmin(rsi_vals[:mid]))
    rsi_low2_idx = mid + int(np.nanargmin(rsi_vals[mid:]))

    if price[price_low2_idx] < price[price_low1_idx] and rsi_vals[rsi_low2_idx] > rsi_vals[rsi_low1_idx]:
        return "bullish"

    # --- Bearish divergence: higher high in price, lower high in RSI ---
    price_hi1_idx = int(np.nanargmax(price[:mid]))
    price_hi2_idx = mid + int(np.nanargmax(price[mid:]))
    rsi_hi1_idx = int(np.nanargmax(rsi_vals[:mid]))
    rsi_hi2_idx = mid + int(np.nanargmax(rsi_vals[mid:]))

    if price[price_hi2_idx] > price[price_hi1_idx] and rsi_vals[rsi_hi2_idx] < rsi_vals[rsi_hi1_idx]:
        return "bearish"

    return "none"


# ── VWAP ─────────────────────────────────────────────────────────────

def compute_vwap(df_1m: pd.DataFrame, session_start_hour: int = 0) -> pd.Series:
    """Compute session-based VWAP from 1-minute bars.

    Resets at *session_start_hour* UTC each day (default 0 = NY midnight).
    The input DataFrame should have columns: High, Low, Close, Volume
    and a DatetimeIndex.

    Parameters
    ----------
    df_1m : 1-minute OHLCV DataFrame.
    session_start_hour : UTC hour at which to reset cumulative sums.

    Returns
    -------
    pd.Series of VWAP values aligned to the input index.
    """
    high = df_1m["High"].values.astype(float)
    low = df_1m["Low"].values.astype(float)
    close = df_1m["Close"].values.astype(float)
    volume = df_1m["Volume"].values.astype(float)

    typical = (high + low + close) / 3.0

    # Determine session boundaries (reset points)
    idx = df_1m.index
    hours = np.array([t.hour for t in idx])
    minutes = np.array([t.minute for t in idx])
    dates = np.array([t.date() for t in idx])

    # Build a session-group key: changes when we cross session_start_hour
    # Simple approach: assign a session_id that increments at each reset
    session_id = np.zeros(len(idx), dtype=int)
    sid = 0
    for i in range(len(idx)):
        if i > 0 and hours[i] == session_start_hour and minutes[i] == 0 and dates[i] != dates[i - 1]:
            sid += 1
        elif i > 0 and hours[i] == session_start_hour and minutes[i] == 0:
            # Same date but hour matched — daily reset
            sid += 1
        session_id[i] = sid

    # Cumulative sums per session
    cum_tv = np.zeros(len(idx), dtype=float)
    cum_v = np.zeros(len(idx), dtype=float)
    vwap = np.zeros(len(idx), dtype=float)

    running_tv = 0.0
    running_v = 0.0
    prev_sid = session_id[0]

    for i in range(len(idx)):
        if session_id[i] != prev_sid:
            running_tv = 0.0
            running_v = 0.0
            prev_sid = session_id[i]
        running_tv += typical[i] * volume[i]
        running_v += volume[i]
        vwap[i] = running_tv / running_v if running_v > 0 else typical[i]

    return pd.Series(vwap, index=idx, name="VWAP")
