"""Signal primitives for the backtest factory.

Pure functions on pandas inputs. Each returns a Series aligned with the
input index. No side effects, no I/O. All NaN-leading regions are
preserved (warm-up); the engine is responsible for skipping warm-up rows.

v1 primitives (5):
    - sma(close, n)       simple moving average
    - ema(close, n)       exponential moving average
    - rsi(close, n=14)    Wilder's RSI
    - atr(h, l, c, n=14)  Wilder's ATR (range volatility)
    - donchian(h, l, n)   returns (upper, lower) tuple of N-bar high/low

Plus 2 helpers for signal composition:
    - cross_above(a, b)   True on bar where a crosses from <=b to >b
    - cross_below(a, b)   True on bar where a crosses from >=b to <b

Both helpers accept Series or scalar. Output is a bool Series indexed
like `a`; first row is always False (no prior bar to cross from).
"""
from __future__ import annotations

from typing import Tuple, Union

import numpy as np
import pandas as pd


SeriesOrFloat = Union[pd.Series, float]


def sma(close: pd.Series, n: int) -> pd.Series:
    if n < 1:
        raise ValueError(f"sma: n must be >= 1, got {n}")
    return close.rolling(window=n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    if n < 1:
        raise ValueError(f"ema: n must be >= 1, got {n}")
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. Uses EMA-of-gain / EMA-of-loss with alpha=1/n."""
    if n < 2:
        raise ValueError(f"rsi: n must be >= 2, got {n}")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 and avg_gain > 0, RSI should be 100 (not NaN)
    out = out.where(~(avg_loss.eq(0.0) & avg_gain.gt(0.0)), 100.0)
    # When both are 0 (flat market), RSI is undefined → 50 by convention
    out = out.where(~(avg_loss.eq(0.0) & avg_gain.eq(0.0)), 50.0)
    return out


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ATR (true range smoothed with alpha=1/n)."""
    if n < 1:
        raise ValueError(f"atr: n must be >= 1, got {n}")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def donchian(high: pd.Series, low: pd.Series, n: int) -> Tuple[pd.Series, pd.Series]:
    """N-bar rolling high and low. Returns (upper, lower).

    The bar at index i looks BACK n bars (i-n+1 ... i inclusive). Engines
    using donchian for breakout detection should shift(1) the upper/lower
    so the comparison is against bars STRICTLY BEFORE the current bar
    (no look-ahead).
    """
    if n < 1:
        raise ValueError(f"donchian: n must be >= 1, got {n}")
    upper = high.rolling(window=n, min_periods=n).max()
    lower = low.rolling(window=n, min_periods=n).min()
    return upper, lower


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger bands. Returns (middle, upper, lower).

    middle = SMA(n); upper = middle + k*std; lower = middle - k*std.
    """
    if n < 2:
        raise ValueError(f"bollinger: n must be >= 2, got {n}")
    mid = close.rolling(window=n, min_periods=n).mean()
    std = close.rolling(window=n, min_periods=n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """MACD = ema(fast) - ema(slow); signal_line = ema(macd, signal_n).
    Returns (macd_line, signal_line, histogram). The histogram is macd
    minus signal — bullish cross fires when histogram crosses 0 upward.
    """
    if fast >= slow:
        raise ValueError(f"macd: fast ({fast}) must be < slow ({slow})")
    if signal < 1:
        raise ValueError(f"macd: signal must be >= 1, got {signal}")
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def zscore(close: pd.Series, n: int) -> pd.Series:
    """Rolling z-score: (close - SMA(n)) / std(n). Captures "how stretched
    is price vs its recent mean." Used for mean-reversion entries when
    z is extreme (e.g. z < -2 = oversold candidate)."""
    if n < 2:
        raise ValueError(f"zscore: n must be >= 2, got {n}")
    mean = close.rolling(window=n, min_periods=n).mean()
    std = close.rolling(window=n, min_periods=n).std(ddof=0)
    return (close - mean) / std.replace(0.0, float("nan"))


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative sum where today's volume is ADDED if
    close > prev_close, SUBTRACTED if close < prev_close, unchanged if equal.

    Use as a confirmation primitive — if price breaks out but OBV doesn't
    track, the breakout is suspect."""
    if len(close) != len(volume):
        raise ValueError(f"obv: close and volume must align, got {len(close)} vs {len(volume)}")
    delta = close.diff()
    direction = pd.Series(0.0, index=close.index)
    direction = direction.mask(delta > 0, 1.0).mask(delta < 0, -1.0)
    signed_volume = volume * direction
    return signed_volume.cumsum()


def volume_zscore(volume: pd.Series, n: int = 20) -> pd.Series:
    """Rolling z-score of volume: (vol - mean(n)) / std(n). Identifies
    volume surges that may confirm breakouts (z > 2) or signal climaxes."""
    if n < 2:
        raise ValueError(f"volume_zscore: n must be >= 2, got {n}")
    mean = volume.rolling(window=n, min_periods=n).mean()
    std = volume.rolling(window=n, min_periods=n).std(ddof=0)
    return (volume - mean) / std.replace(0.0, float("nan"))


def cross_above(a: pd.Series, b: SeriesOrFloat) -> pd.Series:
    """True on bars where a crosses from <=b to >b.

    a must be a Series. b may be a Series (aligned) or a scalar.
    NaN-positions evaluate False.
    """
    if not isinstance(a, pd.Series):
        raise TypeError("cross_above: a must be a pd.Series")
    if isinstance(b, pd.Series):
        b = b.reindex(a.index)
        b_now = b
        b_prev = b.shift(1)
    else:
        b_now = b
        b_prev = b
    a_prev = a.shift(1)
    cond = (a > b_now) & (a_prev <= b_prev)
    return cond.fillna(False).astype(bool)


def cross_below(a: pd.Series, b: SeriesOrFloat) -> pd.Series:
    """True on bars where a crosses from >=b to <b."""
    if not isinstance(a, pd.Series):
        raise TypeError("cross_below: a must be a pd.Series")
    if isinstance(b, pd.Series):
        b = b.reindex(a.index)
        b_now = b
        b_prev = b.shift(1)
    else:
        b_now = b
        b_prev = b
    a_prev = a.shift(1)
    cond = (a < b_now) & (a_prev >= b_prev)
    return cond.fillna(False).astype(bool)
