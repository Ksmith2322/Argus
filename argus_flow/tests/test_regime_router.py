"""Unit tests for helio.regime_router — market regime classification.

classify(df) decides whether the market is TRENDING_UP/DOWN, RANGING,
VOLATILE, or BREAKOUT. The family_priority and sizing_modifier returned
feed every family's activation logic. Wrong classification here =
wrong-family-active + wrong size.

The __main__ block of the module already contains self-tests with synthetic
data; these mirror those but run under the unittest framework so they run
in the standard suite.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import regime_router as rr  # noqa: E402


def _make_ohlcv(n: int, close_series: np.ndarray,
                hl_spread: float = 0.005,
                rng: np.random.Generator | None = None) -> pd.DataFrame:
    """Build a plausible OHLCV frame from a close series."""
    rng = rng or np.random.default_rng(42)
    noise_h = np.abs(rng.standard_normal(n)) * hl_spread
    noise_l = np.abs(rng.standard_normal(n)) * hl_spread
    high = close_series * (1 + noise_h)
    low = close_series * (1 - noise_l)
    opn = close_series + rng.standard_normal(n) * close_series.mean() * 0.001
    vol = rng.integers(100, 10000, size=n).astype(float)
    return pd.DataFrame({
        "Open": opn, "High": high, "Low": low, "Close": close_series, "Volume": vol,
    })


class TestClassifyReturnShape(unittest.TestCase):
    """Every classify() return must have the same four keys with valid values."""

    def test_return_has_all_four_keys(self):
        np.random.seed(42)
        df = _make_ohlcv(100, 100 + np.random.randn(100).cumsum())
        result = rr.classify(df)
        for k in ("regime", "confidence", "family_priority", "sizing_modifier"):
            self.assertIn(k, result)

    def test_regime_is_known_enum_value(self):
        np.random.seed(42)
        df = _make_ohlcv(100, 100 + np.random.randn(100).cumsum())
        result = rr.classify(df)
        self.assertIn(result["regime"], [r.value for r in rr.Regime])

    def test_confidence_in_zero_to_one(self):
        np.random.seed(42)
        df = _make_ohlcv(100, 100 + np.random.randn(100).cumsum())
        result = rr.classify(df)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_family_priority_has_three_entries(self):
        np.random.seed(42)
        df = _make_ohlcv(100, 100 + np.random.randn(100).cumsum())
        result = rr.classify(df)
        self.assertEqual(len(result["family_priority"]), 3)
        self.assertEqual(set(result["family_priority"]), {"hermes", "helio", "apollo"})


class TestInsufficientData(unittest.TestCase):
    def test_under_40_rows_defaults_to_ranging_zero_confidence(self):
        df = _make_ohlcv(30, np.linspace(100, 101, 30))
        result = rr.classify(df)
        self.assertEqual(result["regime"], rr.Regime.RANGING.value)
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["sizing_modifier"], 0.5)


class TestTrendingClassification(unittest.TestCase):
    """Strong upward drift -> TRENDING_UP or BREAKOUT (both acceptable —
    the module's own self-test allows either)."""

    def test_strong_uptrend_classifies_as_trend_or_breakout(self):
        rng = np.random.default_rng(42)
        returns = rng.normal(0.003, 0.002, 200)
        t_up = 100.0 * np.cumprod(1 + returns)
        df = _make_ohlcv(200, t_up, hl_spread=0.008, rng=rng)
        result = rr.classify(df)
        self.assertIn(result["regime"],
            [rr.Regime.TRENDING_UP.value, rr.Regime.BREAKOUT.value])

    def test_strong_downtrend_classifies_as_trend_down_or_breakout(self):
        rng = np.random.default_rng(42)
        returns = rng.normal(-0.003, 0.002, 200)
        t_down = 130.0 * np.cumprod(1 + returns)
        df = _make_ohlcv(200, t_down, hl_spread=0.008, rng=rng)
        result = rr.classify(df)
        self.assertIn(result["regime"],
            [rr.Regime.TRENDING_DOWN.value, rr.Regime.BREAKOUT.value])


class TestRangingClassification(unittest.TestCase):
    def test_flat_oscillating_series_is_ranging(self):
        rng = np.random.default_rng(42)
        t_range = 100 + np.sin(np.linspace(0, 30 * np.pi, 200)) * 0.3 + rng.standard_normal(200) * 0.05
        df = _make_ohlcv(200, t_range, hl_spread=0.002, rng=rng)
        result = rr.classify(df)
        self.assertEqual(result["regime"], rr.Regime.RANGING.value)


class TestVolatileClassification(unittest.TestCase):
    def test_sudden_atr_spike_is_volatile(self):
        """190 calm bars + 10 extreme whipsaw -> ATR_now >> ATR_avg."""
        rng = np.random.default_rng(42)
        t_calm = 100 + rng.standard_normal(190) * 0.05
        whip = np.zeros(10)
        for i in range(10):
            whip[i] = 8.0 * ((-1) ** i) + rng.standard_normal() * 2.0
        t_wild = t_calm[-1] + np.cumsum(whip)
        t_vol = np.concatenate([t_calm, t_wild])
        df = _make_ohlcv(200, t_vol, hl_spread=0.001, rng=rng)
        # Amplify H/L in wild zone to increase TR
        df.loc[df.index[-10:], "High"] = df["Close"].iloc[-10:].values + 8.0
        df.loc[df.index[-10:], "Low"] = df["Close"].iloc[-10:].values - 8.0
        result = rr.classify(df)
        self.assertEqual(result["regime"], rr.Regime.VOLATILE.value)


class TestPriorityTables(unittest.TestCase):
    """The family_priority and sizing_modifier tables encode capital-allocation
    intent. Changes must be intentional, not accidental."""

    def test_volatile_regime_halves_sizing(self):
        self.assertEqual(rr._SIZING_MODIFIER[rr.Regime.VOLATILE], 0.5)

    def test_trending_regime_allows_full_sizing(self):
        self.assertEqual(rr._SIZING_MODIFIER[rr.Regime.TRENDING_UP], 1.0)
        self.assertEqual(rr._SIZING_MODIFIER[rr.Regime.TRENDING_DOWN], 1.0)

    def test_trending_prefers_hermes_first(self):
        """Hermes (momentum) should lead the priority list in trends."""
        self.assertEqual(rr._FAMILY_PRIORITY[rr.Regime.TRENDING_UP][0], "hermes")

    def test_ranging_prefers_apollo_first(self):
        """Apollo (mean-reversion) leads in ranging markets."""
        self.assertEqual(rr._FAMILY_PRIORITY[rr.Regime.RANGING][0], "apollo")

    def test_every_regime_has_priority_and_modifier(self):
        """New Regime enum values must be added to both tables. A missing
        entry would KeyError at classify() time, not at import."""
        for regime in rr.Regime:
            self.assertIn(regime, rr._FAMILY_PRIORITY)
            self.assertIn(regime, rr._SIZING_MODIFIER)


class TestIndicatorMath(unittest.TestCase):
    """Core indicator helpers used by classify()."""

    def test_ema_follows_series(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ema = rr._ema(s, span=3)
        # Last value should be weighted toward the tail
        self.assertAlmostEqual(float(ema.iloc[-1]), 4.0, delta=0.5)

    def test_true_range_takes_max_of_three(self):
        """TR = max(H-L, |H-PC|, |L-PC|)."""
        df = pd.DataFrame({
            "High": [10.0, 11.0, 12.0],
            "Low":  [9.0, 10.0, 11.0],
            "Close":[9.5, 10.5, 11.5],
        })
        tr = rr._true_range(df)
        # Bar 1: H-L=1, |H-PC|=|11-9.5|=1.5 -> TR=1.5
        self.assertAlmostEqual(float(tr.iloc[1]), 1.5)

    def test_bollinger_band_width_is_fraction(self):
        # Constant series -> zero std -> zero width (but guarded against div-by-zero)
        df = pd.DataFrame({"Close": [100.0] * 30})
        bw = rr._bollinger_band_width(df, period=20)
        # Last 10 values with std=0 give width=0
        self.assertAlmostEqual(float(bw.iloc[-1]), 0.0, places=4)


class TestClassifyMulti(unittest.TestCase):
    def test_returns_one_result_per_symbol(self):
        rng = np.random.default_rng(42)
        up = 100 * np.cumprod(1 + rng.normal(0.003, 0.002, 150))
        flat = 100 + rng.standard_normal(150) * 0.05
        multi = rr.classify_multi({
            "UPSYM": _make_ohlcv(150, up, rng=rng),
            "FLATSYM": _make_ohlcv(150, flat, rng=rng),
        })
        self.assertEqual(set(multi.keys()), {"UPSYM", "FLATSYM"})
        for result in multi.values():
            self.assertIn("regime", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
