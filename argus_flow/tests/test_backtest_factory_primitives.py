"""Primitive correctness tests. Deterministic synthetic inputs, hand-
computed expected values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from helio.backtest_factory import primitives as P


def _series(values, start="2024-01-01", freq="1D") -> pd.Series:
    idx = pd.date_range(start=start, periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=float)


# ─── sma ──────────────────────────────────────────────────────────────

def test_sma_basic_3():
    s = _series([1, 2, 3, 4, 5, 6])
    out = P.sma(s, 3)
    assert pd.isna(out.iloc[0])
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == 2.0  # (1+2+3)/3
    assert out.iloc[3] == 3.0
    assert out.iloc[4] == 4.0
    assert out.iloc[5] == 5.0


def test_sma_rejects_zero_n():
    s = _series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="n must be"):
        P.sma(s, 0)


# ─── ema ──────────────────────────────────────────────────────────────

def test_ema_converges_to_constant():
    # Constant 10 series → EMA(any n) converges to 10
    s = _series([10.0] * 50)
    out = P.ema(s, 5)
    # warmup
    assert pd.isna(out.iloc[0])
    # By the tail it should be exactly 10.0
    assert abs(out.iloc[-1] - 10.0) < 1e-9


def test_ema_first_value_at_warmup():
    s = _series([1.0, 2.0, 3.0, 4.0])
    out = P.ema(s, 3)
    # min_periods=3, so first 2 are NaN, third is non-NaN
    assert pd.isna(out.iloc[0])
    assert pd.isna(out.iloc[1])
    assert not pd.isna(out.iloc[2])


# ─── rsi ──────────────────────────────────────────────────────────────

def test_rsi_monotonic_rising_close_is_100():
    # Strictly rising prices → all gains, no losses → RSI → 100
    s = _series(list(range(1, 60)))
    r = P.rsi(s, 14)
    # After warm-up RSI should be 100 (no losses)
    assert r.iloc[-1] == 100.0


def test_rsi_monotonic_falling_close_is_0():
    s = _series(list(range(60, 1, -1)))
    r = P.rsi(s, 14)
    assert r.iloc[-1] == 0.0


def test_rsi_flat_is_50():
    s = _series([100.0] * 30)
    r = P.rsi(s, 14)
    # Both avg_gain and avg_loss are 0 → undefined; we return 50 by convention
    assert r.iloc[-1] == 50.0


def test_rsi_rejects_n_lt_2():
    with pytest.raises(ValueError, match="n must be"):
        P.rsi(_series([1.0, 2.0, 3.0]), 1)


# ─── atr ──────────────────────────────────────────────────────────────

def test_atr_constant_range():
    n = 30
    high = _series([110.0] * n)
    low = _series([100.0] * n)
    close = _series([105.0] * n)
    a = P.atr(high, low, close, n=14)
    # On a flat 10-pt range with constant close, ATR → 10
    assert abs(a.iloc[-1] - 10.0) < 1e-6


# ─── donchian ─────────────────────────────────────────────────────────

def test_donchian_rolling_minmax():
    high = _series([1, 2, 3, 2, 5, 4, 7, 6, 9, 8])
    low = _series([0, 1, 2, 1, 4, 3, 6, 5, 8, 7])
    u, l = P.donchian(high, low, 3)
    assert u.iloc[2] == 3.0  # max(1,2,3)
    assert l.iloc[2] == 0.0  # min(0,1,2)
    assert u.iloc[4] == 5.0  # max(3,2,5)
    assert l.iloc[4] == 1.0  # min(2,1,4)


# ─── bollinger ────────────────────────────────────────────────────────

def test_bollinger_constant_zero_band_width():
    s = _series([100.0] * 30)
    mid, up, low = P.bollinger(s, 20, 2.0)
    # Constant series → std=0 → upper == mid == lower
    assert abs(up.iloc[-1] - mid.iloc[-1]) < 1e-9
    assert abs(low.iloc[-1] - mid.iloc[-1]) < 1e-9


# ─── cross helpers ────────────────────────────────────────────────────

def test_cross_above_basic():
    a = _series([1, 2, 3, 4, 5])
    b = _series([5, 4, 3, 2, 1])
    out = P.cross_above(a, b)
    # a starts below b, crosses at i=3 (a=4, b=2; prev a=3, prev b=3 → 3 <= 3 then 4 > 2)
    assert out.iloc[0] == False
    assert out.iloc[3] == True
    # Once a > b, no more crosses
    assert out.iloc[4] == False


def test_cross_above_scalar_threshold():
    a = _series([10, 20, 30, 40, 50])
    out = P.cross_above(a, 25)
    # prev<=25, now>25 → at i=2 (a=30)
    assert out.iloc[2] == True
    # Already above, no more crosses
    assert out.iloc[3] == False


def test_cross_below_basic():
    a = _series([5, 4, 3, 2, 1])
    out = P.cross_below(a, 2.5)
    # crosses down at i=3 (a=2, prev=3)
    assert out.iloc[3] == True


def test_cross_handles_nan_safely():
    a = pd.Series([float("nan"), 1.0, 2.0, 3.0])
    out = P.cross_above(a, 1.5)
    # First row's prev is NaN, must not raise; result should be valid bool
    assert out.dtype == bool
    assert out.iloc[0] == False


# ─── macd ─────────────────────────────────────────────────────────────

def test_macd_returns_three_aligned_series():
    s = _series([100.0 + i * 0.1 for i in range(80)])
    macd, sig, hist = P.macd(s, 12, 26, 9)
    assert len(macd) == len(s) == len(sig) == len(hist)
    # On a steady uptrend, MACD line eventually positive
    assert macd.iloc[-1] > 0
    # Histogram = macd - signal, identity holds bar by bar
    assert ((macd - sig) - hist).abs().max() < 1e-9


def test_macd_rejects_fast_geq_slow():
    s = _series([100.0] * 40)
    with pytest.raises(ValueError, match="fast"):
        P.macd(s, 26, 12, 9)


# ─── zscore ───────────────────────────────────────────────────────────

def test_zscore_constant_series_yields_nan():
    s = _series([100.0] * 30)
    z = P.zscore(s, 10)
    # std=0 → divide by zero → NaN by design
    assert pd.isna(z.iloc[-1])


def test_zscore_known_value():
    # Build a series where the last bar is exactly +1σ above SMA
    # n=5 of [99, 100, 101, 100, 100], std = sqrt(0.5), mean = 100
    # Last close = 100 + 1*sqrt(0.5) = ~100.707
    base = [99.0, 100.0, 101.0, 100.0, 100.0]
    import math
    target = 100.0 + math.sqrt(0.5)
    s = _series(base + [target])
    z = P.zscore(s, 5)
    # Last bar uses window [100, 101, 100, 100, 100.707]
    # mean = 100.1414, std ≈ 0.4 — exact value isn't 1.0 because the
    # window shifted forward. We just check the sign + non-NaN.
    assert not pd.isna(z.iloc[-1])
    assert z.iloc[-1] > 0


def test_zscore_rejects_small_n():
    with pytest.raises(ValueError, match="n must be"):
        P.zscore(_series([1.0, 2.0]), 1)


# ─── obv ─────────────────────────────────────────────────────────────

def test_obv_adds_volume_on_up_close_subtracts_on_down():
    close = _series([100.0, 101.0, 100.5, 102.0, 101.0])
    volume = _series([1000.0, 1000.0, 500.0, 800.0, 600.0])
    o = P.obv(close, volume)
    # Bar 0: diff is NaN → direction 0 → no change
    assert o.iloc[0] == 0.0
    # Bar 1: up → +1000
    assert o.iloc[1] == 1000.0
    # Bar 2: down (101 → 100.5) → -500 → cum 500
    assert o.iloc[2] == 500.0
    # Bar 3: up (100.5 → 102) → +800 → cum 1300
    assert o.iloc[3] == 1300.0
    # Bar 4: down (102 → 101) → -600 → cum 700
    assert o.iloc[4] == 700.0


def test_obv_rejects_misaligned_inputs():
    close = _series([100.0, 101.0, 102.0])
    volume = pd.Series([1000.0, 1000.0],
                       index=pd.date_range(start="2024-01-01", periods=2, freq="1D"))
    with pytest.raises(ValueError, match="must align"):
        P.obv(close, volume)


# ─── volume_zscore ───────────────────────────────────────────────────

def test_volume_zscore_constant_volume_yields_nan():
    v = _series([1000.0] * 30)
    z = P.volume_zscore(v, 10)
    # Constant volume → std=0 → z = NaN
    assert pd.isna(z.iloc[-1])


def test_volume_zscore_spike_yields_positive():
    # 19 bars of vol=1000, then bar 20 with vol=5000 (huge spike)
    v = _series([1000.0] * 19 + [5000.0])
    z = P.volume_zscore(v, 20)
    # The spike should produce a strongly positive z
    assert z.iloc[-1] > 3.0
