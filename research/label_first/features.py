"""Feature extraction for label-first research.

All features are computed using ONLY data at or before bar B.
Anti-leakage rule: no .shift(-N), no future indices, no future-aware ops.
Returns a DataFrame keyed on df.index with one column per feature.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _slope(s: pd.Series, n: int) -> pd.Series:
    return (s - s.shift(n)) / n


def _zscore(s: pd.Series, n: int) -> pd.Series:
    mu = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std()
    return (s - mu) / sd.replace(0, np.nan)


def compute_features(df: pd.DataFrame, intraday: bool = True) -> pd.DataFrame:
    """Compute ~45 leak-free features. Returns DataFrame keyed on df.index."""
    f = pd.DataFrame(index=df.index)
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    v = df.get("Volume", pd.Series(0.0, index=df.index)).fillna(0)

    rng = (h - l).replace(0, np.nan)
    body = (c - o)
    atr = _atr(df, 14)

    # --- Price action ---
    f["body_pct"] = body / rng
    f["upper_wick_pct"] = (h - np.maximum(o, c)) / rng
    f["lower_wick_pct"] = (np.minimum(o, c) - l) / rng
    f["range_atr_ratio"] = rng / atr
    direction = np.sign(c - o).fillna(0).astype(int)
    f["consec_dir"] = direction.groupby((direction != direction.shift()).cumsum()).cumcount() + 1
    f["consec_dir"] *= direction
    f["inside_bar"] = ((h <= h.shift(1)) & (l >= l.shift(1))).astype(int)
    f["outside_bar"] = ((h >= h.shift(1)) & (l <= l.shift(1))).astype(int)
    if intraday:
        f["gap_pct"] = (o - c.shift(1)) / c.shift(1)

    # --- Trend (EMAs + slopes + stack) ---
    e8 = _ema(c, 8); e21 = _ema(c, 21); e50 = _ema(c, 50); e200 = _ema(c, 200)
    f["ema8_dist_atr"] = (c - e8) / atr
    f["ema21_dist_atr"] = (c - e21) / atr
    f["ema50_dist_atr"] = (c - e50) / atr
    f["ema200_dist_atr"] = (c - e200) / atr
    f["ema8_slope_atr"] = _slope(e8, 5) / atr
    f["ema21_slope_atr"] = _slope(e21, 5) / atr
    f["ema50_slope_atr"] = _slope(e50, 10) / atr
    bull_stack = (e8 > e21) & (e21 > e50) & (e50 > e200)
    bear_stack = (e8 < e21) & (e21 < e50) & (e50 < e200)
    f["ema_stack"] = np.where(bull_stack, 1, np.where(bear_stack, -1, 0))

    # --- Volume ---
    f["vol_z_20"] = _zscore(v, 20)
    f["vol_z_60"] = _zscore(v, 60)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    f["obv_slope_5"] = _slope(obv, 5) / (v.rolling(20).mean() + 1)
    vol_avg_20 = v.rolling(20, min_periods=5).mean()
    f["vol_dryup"] = ((v < 0.3 * vol_avg_20) & (vol_avg_20 > 0)).astype(int)
    vol_climax = (f["vol_z_20"] > 3) & (np.sign(body) != np.sign(body.shift(1)))
    f["vol_climax"] = vol_climax.astype(int)

    # --- Volatility regime ---
    f["atr_pct"] = atr / c
    f["atr_z_60"] = _zscore(atr, 60)
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    f["bb_width_pct"] = (4 * bb_std) / bb_mid
    f["bb_pos"] = (c - (bb_mid - 2 * bb_std)) / (4 * bb_std).replace(0, np.nan)
    log_ret = np.log(c / c.shift(1))
    f["realized_vol_20"] = log_ret.rolling(20).std() * np.sqrt(252)
    # Choppiness Index (simplified)
    atr_sum = atr.rolling(14).sum()
    h14 = h.rolling(14).max()
    l14 = l.rolling(14).min()
    f["choppiness_14"] = 100 * np.log10(atr_sum / (h14 - l14).replace(0, np.nan)) / np.log10(14)

    # --- Multi-timeframe context (lazy: use longer EMAs as proxy when no HTF passed) ---
    # 4x and 16x current TF approximate higher timeframes
    e_htf_fast = _ema(c, 32)  # ~ "1H" if 5min, "4H" if 1H
    e_htf_slow = _ema(c, 128)  # ~ "4H" if 5min, "1d" if 1H
    f["htf_trend"] = np.where(e_htf_fast > e_htf_slow, 1, -1)
    f["htf_dist_atr"] = (c - e_htf_slow) / atr

    # Recent swing high/low (60-bar lookback)
    swing_h = h.rolling(60, min_periods=20).max()
    swing_l = l.rolling(60, min_periods=20).min()
    f["dist_swing_high_atr"] = (swing_h - c) / atr
    f["dist_swing_low_atr"] = (c - swing_l) / atr

    # --- Time features ---
    if intraday:
        f["hour"] = df.index.hour
        f["minute"] = df.index.minute
        f["dow"] = df.index.dayofweek
        # Session bucketing for US-aligned instruments (UTC hours)
        h_utc = df.index.hour
        f["session_us"] = np.where((h_utc >= 13) & (h_utc < 20), 1,
                            np.where((h_utc >= 8) & (h_utc < 13), 2,
                                np.where((h_utc >= 20) | (h_utc < 4), 3, 4)))  # 1=NY, 2=London, 3=Asia, 4=other
    else:
        f["dow"] = df.index.dayofweek
        f["month"] = df.index.month

    # --- Order flow proxies ---
    f["close_pos_in_range"] = (c - l) / rng  # 1 = closed at high (buy pressure)
    f["consec_close_above_open"] = ((c > o).astype(int)).groupby(((c > o) != (c > o).shift()).cumsum()).cumcount() + 1
    f["delta_proxy"] = (c - o) * v / (vol_avg_20 + 1)

    # --- Momentum ---
    f["ret_1"] = c.pct_change(1)
    f["ret_5"] = c.pct_change(5)
    f["ret_20"] = c.pct_change(20)
    f["high_low_5"] = (c - l.rolling(5).min()) / (h.rolling(5).max() - l.rolling(5).min()).replace(0, np.nan)
    f["high_low_20"] = (c - l.rolling(20).min()) / (h.rolling(20).max() - l.rolling(20).min()).replace(0, np.nan)

    # --- RSI (Wilder) ---
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14, min_periods=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi_14"] = 100 - (100 / (1 + rs))

    return f
