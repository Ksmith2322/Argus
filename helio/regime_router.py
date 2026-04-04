"""
Adaptive Regime Router for the Helio Greek family system.

Classifies market conditions from OHLCV data and recommends which
family member (hermes / helio / apollo) should have execution priority.

Regimes:
    TRENDING_UP   – strong upward trend (ADX > 25, EMA slope positive)
    TRENDING_DOWN – strong downward trend (ADX > 25, EMA slope negative)
    RANGING       – low trend strength (ADX < 20)
    VOLATILE      – ATR spike above 1.5x its 20-period average
    BREAKOUT      – Bollinger Band width expanding rapidly

Uses only numpy + pandas.  No ML.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Regime enum
# ---------------------------------------------------------------------------

class Regime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    BREAKOUT = "BREAKOUT"


# ---------------------------------------------------------------------------
# Priority tables
# ---------------------------------------------------------------------------

_FAMILY_PRIORITY: Dict[Regime, List[str]] = {
    Regime.TRENDING_UP:   ["hermes", "helio", "apollo"],
    Regime.TRENDING_DOWN: ["hermes", "helio", "apollo"],
    Regime.RANGING:       ["apollo", "helio", "hermes"],
    Regime.VOLATILE:      ["apollo", "hermes", "helio"],
    Regime.BREAKOUT:      ["hermes", "helio", "apollo"],
}

_SIZING_MODIFIER: Dict[Regime, float] = {
    Regime.TRENDING_UP:   1.0,
    Regime.TRENDING_DOWN: 1.0,
    Regime.RANGING:       0.8,
    Regime.VOLATILE:      0.5,
    Regime.BREAKOUT:      0.9,
}


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    """Wilder true range."""
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    tr = _true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index.

    Returns a Series of ADX values (0-100 scale).
    """
    high = df["High"]
    low = df["Low"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    # Only keep the larger of the two when positive
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_vals = _atr(df, period)

    alpha = 1.0 / period
    smooth_plus = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    smooth_minus = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    plus_di = 100.0 * smooth_plus / atr_vals.replace(0, np.nan)
    minus_di = 100.0 * smooth_minus / atr_vals.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_series = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx_series


def _bollinger_band_width(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> pd.Series:
    """Bollinger Band width as fraction of the middle band."""
    mid = df["Close"].rolling(period).mean()
    std = df["Close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (upper - lower) / mid.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------

# Tunables
ADX_TREND_THRESHOLD = 25
ADX_RANGE_THRESHOLD = 20
ATR_VOLATILE_RATIO = 1.5
BB_WIDTH_EXPANSION_RATIO = 1.5  # current width vs 20-bar avg width
EMA_SPAN = 20
ATR_PERIOD = 14
ADX_PERIOD = 14
BB_PERIOD = 20


def classify(df: pd.DataFrame) -> dict:
    """
    Classify the current market regime from an OHLCV DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: Open, High, Low, Close, Volume.
        Needs at least ~40 rows for indicator warm-up.

    Returns
    -------
    dict with keys:
        regime           – Regime enum value (str)
        confidence       – float 0-1
        family_priority  – list[str] ordered best-to-worst
        sizing_modifier  – float 0.5-1.0
    """
    if len(df) < 40:
        # Not enough data — default to ranging with low confidence
        return {
            "regime": Regime.RANGING.value,
            "confidence": 0.0,
            "family_priority": _FAMILY_PRIORITY[Regime.RANGING],
            "sizing_modifier": 0.5,
        }

    # Compute indicators on the full frame, then read the latest value
    adx_series = _adx(df, ADX_PERIOD)
    atr_series = _atr(df, ATR_PERIOD)
    ema_series = _ema(df["Close"], EMA_SPAN)
    bb_width = _bollinger_band_width(df, BB_PERIOD)

    adx_now = adx_series.iloc[-1]
    atr_now = atr_series.iloc[-1]
    atr_avg = atr_series.iloc[-20:].mean() if len(atr_series) >= 20 else atr_series.mean()
    atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0

    ema_slope = ema_series.iloc[-1] - ema_series.iloc[-5] if len(ema_series) >= 5 else 0.0

    bb_now = bb_width.iloc[-1] if not np.isnan(bb_width.iloc[-1]) else 0.0
    bb_avg = bb_width.iloc[-20:].mean() if len(bb_width) >= 20 else bb_width.mean()
    bb_ratio = bb_now / bb_avg if bb_avg > 0 else 1.0

    # ----- Decision tree (order matters: most specific first) -----

    # 1) Volatile — ATR spike dominates
    if atr_ratio >= ATR_VOLATILE_RATIO:
        confidence = min(1.0, (atr_ratio - ATR_VOLATILE_RATIO) / 1.0 + 0.6)
        regime = Regime.VOLATILE
        return _build_result(regime, confidence)

    # 2) Breakout — BB width expanding + moderate/strong ADX
    if bb_ratio >= BB_WIDTH_EXPANSION_RATIO and adx_now >= ADX_RANGE_THRESHOLD:
        confidence = min(1.0, (bb_ratio - BB_WIDTH_EXPANSION_RATIO) / 1.0 + 0.55)
        regime = Regime.BREAKOUT
        return _build_result(regime, confidence)

    # 3) Trending
    if adx_now >= ADX_TREND_THRESHOLD:
        regime = Regime.TRENDING_UP if ema_slope > 0 else Regime.TRENDING_DOWN
        confidence = min(1.0, (adx_now - ADX_TREND_THRESHOLD) / 30.0 + 0.5)
        return _build_result(regime, confidence)

    # 4) Ranging (ADX < range threshold)
    if adx_now < ADX_RANGE_THRESHOLD:
        confidence = min(1.0, (ADX_RANGE_THRESHOLD - adx_now) / 15.0 + 0.5)
        regime = Regime.RANGING
        return _build_result(regime, confidence)

    # 5) Ambiguous zone (ADX between 20-25) — lean ranging with lower confidence
    regime = Regime.RANGING
    confidence = 0.35
    return _build_result(regime, confidence)


def _build_result(regime: Regime, confidence: float) -> dict:
    return {
        "regime": regime.value,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "family_priority": _FAMILY_PRIORITY[regime],
        "sizing_modifier": _SIZING_MODIFIER[regime],
    }


# ---------------------------------------------------------------------------
# Multi-instrument classification
# ---------------------------------------------------------------------------

def classify_multi(symbols_data: Dict[str, pd.DataFrame]) -> Dict[str, dict]:
    """
    Classify regimes for multiple instruments at once.

    Parameters
    ----------
    symbols_data : dict[str, pd.DataFrame]
        Mapping of symbol name to OHLCV DataFrame.

    Returns
    -------
    dict[str, dict]
        Mapping of symbol name to classification result.
    """
    return {symbol: classify(df) for symbol, df in symbols_data.items()}


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    np.random.seed(42)

    def _make_ohlcv(n: int, close_series: np.ndarray,
                    hl_spread: float = 0.005) -> pd.DataFrame:
        """Build a plausible OHLCV frame from a close series."""
        noise_h = np.abs(np.random.randn(n)) * hl_spread
        noise_l = np.abs(np.random.randn(n)) * hl_spread
        high = close_series * (1 + noise_h)
        low = close_series * (1 - noise_l)
        opn = close_series + np.random.randn(n) * close_series.mean() * 0.001
        vol = np.random.randint(100, 10000, size=n).astype(float)
        return pd.DataFrame({
            "Open": opn,
            "High": high,
            "Low": low,
            "Close": close_series,
            "Volume": vol,
        })

    n = 200

    # --- Trending Up: strong upward ramp with realistic H/L spread ---
    # Each bar moves ~0.3% up on avg with directional noise
    returns_up = np.random.normal(0.003, 0.002, n)
    t_up = 100.0 * np.cumprod(1 + returns_up)
    df_up = _make_ohlcv(n, t_up, hl_spread=0.008)
    res_up = classify(df_up)
    print(f"Trending Up data  -> {res_up}")

    # --- Trending Down: strong downward ramp ---
    returns_down = np.random.normal(-0.003, 0.002, n)
    t_down = 130.0 * np.cumprod(1 + returns_down)
    df_down = _make_ohlcv(n, t_down, hl_spread=0.008)
    res_down = classify(df_down)
    print(f"Trending Down data -> {res_down}")

    # --- Ranging: flat with small mean-reverting oscillations ---
    t_range = 100 + np.sin(np.linspace(0, 30 * np.pi, n)) * 0.3 + np.random.randn(n) * 0.05
    df_range = _make_ohlcv(n, t_range, hl_spread=0.002)
    res_range = classify(df_range)
    print(f"Ranging data       -> {res_range}")

    # --- Volatile: calm for 190 bars, then extreme whipsaw for 10 bars ---
    # Short spike ensures ATR_now >> ATR_avg (avg window still mostly calm)
    t_vol_calm = 100 + np.random.randn(190) * 0.05
    whip = np.zeros(10)
    for i in range(10):
        whip[i] = 8.0 * ((-1) ** i) + np.random.randn() * 2.0
    t_vol_wild = t_vol_calm[-1] + np.cumsum(whip)
    t_vol = np.concatenate([t_vol_calm, t_vol_wild])
    df_vol = _make_ohlcv(n, t_vol, hl_spread=0.001)
    # Amplify High/Low in the wild zone to push true range
    df_vol.loc[df_vol.index[-10:], "High"] = df_vol["Close"].iloc[-10:].values + 8.0
    df_vol.loc[df_vol.index[-10:], "Low"] = df_vol["Close"].iloc[-10:].values - 8.0
    res_vol = classify(df_vol)
    print(f"Volatile data      -> {res_vol}")

    # --- Breakout: tight range for 160 bars, then moderate directional move ---
    # Key: BB width must expand (std dev jump) but ATR ratio stays < 1.5
    t_brk_tight = 100 + np.random.randn(160) * 0.03
    # Gradual directional move (not whipsaw)
    t_brk_expand = np.linspace(100.5, 108, 40) + np.random.randn(40) * 0.3
    t_brk = np.concatenate([t_brk_tight, t_brk_expand])
    df_brk = _make_ohlcv(n, t_brk, hl_spread=0.004)
    res_brk = classify(df_brk)
    print(f"Breakout data      -> {res_brk}")

    # --- Multi-instrument ---
    multi = classify_multi({
        "ETH-USD": df_up,
        "BTC-USD": df_range,
        "SOL-USD": df_vol,
    })
    print("\nMulti-instrument:")
    for sym, res in multi.items():
        print(f"  {sym}: {res['regime']} (conf={res['confidence']}, "
              f"priority={res['family_priority']}, sizing={res['sizing_modifier']})")

    # --- Basic assertions ---
    assert res_up["regime"] in (Regime.TRENDING_UP.value, Regime.BREAKOUT.value), \
        f"Expected trending-up or breakout, got {res_up['regime']}"
    assert res_down["regime"] in (Regime.TRENDING_DOWN.value, Regime.BREAKOUT.value), \
        f"Expected trending-down or breakout, got {res_down['regime']}"
    assert res_range["regime"] == Regime.RANGING.value, \
        f"Expected RANGING, got {res_range['regime']}"
    assert res_vol["regime"] == Regime.VOLATILE.value, \
        f"Expected VOLATILE, got {res_vol['regime']} (atr_ratio may be insufficient)"
    assert 0.0 <= res_up["confidence"] <= 1.0
    assert 0.5 <= res_vol["sizing_modifier"] <= 1.0
    assert len(multi) == 3

    print("\nAll assertions passed.")
