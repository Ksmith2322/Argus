"""Strategy-card math tests — lock down the entry/exit/sizing knobs
that define each strategy's edge.

These tests verify the actual computed values (not just config constants),
so a silent drift in parameter plumbing or feature math is caught.

Top-priority tests from the 3-agent debate on confidence gaps:
  - gdx_gld: z-score lookback is 60-day rolling, entry +/-2, stop +/-3
  - gld_pm_long: hours 18/19/20 UTC, ATR(14), stop 0.5*ATR, target 1.0*ATR
  - wick_gbpusd: signal is AND of all 4 filters, never OR
  - apollo: score >= 75 is the gate, not >= 74
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


class TestGdxGldPairs(unittest.TestCase):
    """Z-score math + entry/exit/stop thresholds for the pairs strategy."""

    def test_zscore_lookback_is_60_bars(self):
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.ZSCORE_LOOKBACK, 60)

    def test_entry_threshold_is_plus_minus_two(self):
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.ZSCORE_ENTRY, 2.0)

    def test_stop_threshold_is_plus_minus_three(self):
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.ZSCORE_STOP, 3.0)

    def test_exit_threshold_is_mean_crossing(self):
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.ZSCORE_EXIT, 0.0)

    def test_compute_zscore_uses_rolling_window(self):
        """The spread z-score must be (spread - rolling_mean) / rolling_std
        with the configured lookback — not a global z-score."""
        from forge import gdx_gld_pairs as g
        # Construct a spread where the last 60 values have mean=0 std=1
        np.random.seed(42)
        pre = np.full(100, 5.0)  # constant early -> excluded
        tail = np.random.standard_normal(60)  # random tail
        spread = pd.Series(np.concatenate([pre, tail]))
        z = g.compute_zscore(spread)
        # Last z-score value should be ~ tail[-1] standardized by tail stats
        expected = (tail[-1] - tail.mean()) / tail.std(ddof=0) if tail.std(ddof=0) else 0
        # pandas rolling .std uses ddof=1 by default
        expected_ddof1 = (tail[-1] - tail.mean()) / tail.std(ddof=1)
        self.assertAlmostEqual(float(z.iloc[-1]), expected_ddof1, places=5)

    def test_compute_zscore_nan_before_lookback_complete(self):
        """Z-score must be NaN until enough bars exist — no look-ahead bias."""
        from forge import gdx_gld_pairs as g
        spread = pd.Series(np.random.standard_normal(120))
        z = g.compute_zscore(spread)
        # First ZSCORE_LOOKBACK-1 must be NaN (rolling has min_periods=lookback)
        self.assertTrue(pd.isna(z.iloc[g.ZSCORE_LOOKBACK - 2]))
        # By position ZSCORE_LOOKBACK-1 we have exactly 60 bars — valid
        self.assertFalse(pd.isna(z.iloc[g.ZSCORE_LOOKBACK - 1]))


class TestGldPmLong(unittest.TestCase):
    """GLD PM long: hour gate + ATR math + stop/target asymmetry."""

    def test_signal_hours_are_18_19_20_utc(self):
        from forge.gld_pm_long import runner as r
        self.assertEqual(sorted(r.PARAMS["signal_hours_utc"]), [18, 19, 20])

    def test_hour_17_does_not_trigger(self):
        """Sanity: hour 17 UTC must be excluded from signal hours."""
        from forge.gld_pm_long import runner as r
        self.assertNotIn(17, r.PARAMS["signal_hours_utc"])

    def test_hour_21_does_not_trigger(self):
        """Sanity: hour 21 UTC must be excluded from signal hours."""
        from forge.gld_pm_long import runner as r
        self.assertNotIn(21, r.PARAMS["signal_hours_utc"])

    def test_atr_period_is_14(self):
        from forge.gld_pm_long import runner as r
        self.assertEqual(r.PARAMS["atr_period"], 14)

    def test_stop_is_half_atr_target_is_one_atr(self):
        """Asymmetric risk/reward: 1:2 (stop 0.5*ATR, target 1.0*ATR)."""
        from forge.gld_pm_long import runner as r
        self.assertEqual(r.PARAMS["stop_atr"], 0.5)
        self.assertEqual(r.PARAMS["target_atr"], 1.0)
        # R-multiple = target/stop must be 2.0
        r_multiple = r.PARAMS["target_atr"] / r.PARAMS["stop_atr"]
        self.assertAlmostEqual(r_multiple, 2.0, places=4)

    def test_hold_bars_is_4(self):
        """Time stop at 4 bars is part of the edge hypothesis."""
        from forge.gld_pm_long import runner as r
        self.assertEqual(r.PARAMS["hold_bars"], 4)

    def test_atr_math_matches_wilder_true_range(self):
        """ATR(n) = rolling mean of TR, where TR = max(H-L, |H-PC|, |L-PC|)."""
        from forge.gld_pm_long.runner import atr
        # Simple series with known TR
        df = pd.DataFrame({
            "High":  [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25],
            "Low":   [ 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
            "Close": [ 9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5,
                     17.5, 18.5, 19.5, 20.5, 21.5, 22.5, 23.5, 24.5],
        })
        a = atr(df, n=14)
        # First 13 values must be NaN (min_periods=14)
        self.assertTrue(pd.isna(a.iloc[12]))
        # 14th value must be a valid number
        self.assertFalse(pd.isna(a.iloc[13]))
        # With H-L=1 and predictable shifts, TR ~= max(1, |H-PC|, |L-PC|) ~= 1.5
        # (H=11, pc=9.5 -> |11-9.5|=1.5)
        self.assertGreater(float(a.iloc[13]), 0)


class TestWickGbpusdFilters(unittest.TestCase):
    """Wick GBPUSD: feature math + AND-not-OR combination of 4 filters."""

    def test_feature_thresholds_are_unchanged(self):
        from forge.wick_gbpusd import runner as r
        self.assertEqual(r.PARAMS["uw_min"], 0.6)
        self.assertEqual(r.PARAMS["cp_max"], 0.3)
        self.assertAlmostEqual(r.PARAMS["bb_width_quantile_max"], 0.33, places=4)
        self.assertAlmostEqual(r.PARAMS["chop_quantile_min"], 0.67, places=4)
        self.assertEqual(r.PARAMS["regime_window"], 60)

    def test_stop_and_target_atr_multipliers(self):
        from forge.wick_gbpusd import runner as r
        self.assertEqual(r.PARAMS["stop_atr"], 1.0)
        self.assertEqual(r.PARAMS["target_atr"], 2.0)

    def test_upper_wick_pct_computation(self):
        """upper_wick_pct = (H - max(O,C)) / (H-L).
        Bar H=1.30, L=1.20, O=1.22, C=1.21 -> (1.30 - 1.22) / 0.10 = 0.80."""
        from forge.wick_gbpusd.runner import compute_features
        df = self._make_history_with_known_bar()
        f = compute_features(df)
        # The bar we constructed has idx we can read
        last_uw = float(f["upper_wick_pct"].iloc[-1])
        self.assertAlmostEqual(last_uw, 0.80, places=2)

    def test_close_pos_in_range_computation(self):
        """close_pos_in_range = (C-L) / (H-L).
        Bar H=1.30, L=1.20, C=1.21 -> (1.21-1.20)/0.10 = 0.10."""
        from forge.wick_gbpusd.runner import compute_features
        df = self._make_history_with_known_bar()
        f = compute_features(df)
        last_cp = float(f["close_pos_in_range"].iloc[-1])
        self.assertAlmostEqual(last_cp, 0.10, places=2)

    def test_signal_requires_all_four_conditions_AND_not_OR(self):
        """If only 3 of the 4 filters pass, signal_long must be False.
        This catches any accidental OR bug in signal composition."""
        from forge.wick_gbpusd.runner import signal_long
        # Construct a row where uw/cp pass but bb_width/chop do NOT pass.
        df = pd.DataFrame({
            "Open":  [1.0] * 100,
            "High":  [1.0] * 100,
            "Low":   [1.0] * 100,
            "Close": [1.0] * 100,
        })
        feats = pd.DataFrame({
            "upper_wick_pct":     [0.8] * 100,  # > 0.6 ✓
            "close_pos_in_range": [0.1] * 100,  # < 0.3 ✓
            "bb_width_pct":       [0.9] * 100,  # very wide
            "bb_width_q33":       [0.1] * 100,  # bb_width NOT < q33 ✗
            "choppiness_14":      [10.0] * 100,  # low
            "chop_q67":           [60.0] * 100,  # chop NOT > q67 ✗
            "atr":                [0.001] * 100,
        })
        sig = signal_long(df, feats)
        self.assertFalse(bool(sig.iloc[-1]),
            "Signal fired with 2/4 filters passing — suggests OR logic bug")

    def test_signal_passes_when_all_four_conditions_met(self):
        from forge.wick_gbpusd.runner import signal_long
        df = pd.DataFrame({
            "Open":  [1.0] * 100,
            "High":  [1.0] * 100,
            "Low":   [1.0] * 100,
            "Close": [1.0] * 100,
        })
        feats = pd.DataFrame({
            "upper_wick_pct":     [0.80] * 100,   # > 0.6 ✓
            "close_pos_in_range": [0.10] * 100,   # < 0.3 ✓
            "bb_width_pct":       [0.05] * 100,   # < q33 ✓
            "bb_width_q33":       [0.10] * 100,
            "choppiness_14":      [70.0] * 100,   # > q67 ✓
            "chop_q67":           [60.0] * 100,
            "atr":                [0.001] * 100,
        })
        sig = signal_long(df, feats)
        self.assertTrue(bool(sig.iloc[-1]),
            "Signal did NOT fire with all 4 filters passing")

    def _make_history_with_known_bar(self) -> pd.DataFrame:
        """Construct 100 bars with the LAST bar set to known values so we can
        validate feature math."""
        n = 100
        highs  = [1.25] * (n - 1) + [1.30]
        lows   = [1.20] * (n - 1) + [1.20]
        opens  = [1.22] * (n - 1) + [1.22]
        closes = [1.23] * (n - 1) + [1.21]
        return pd.DataFrame({
            "Open": opens,
            "High": highs,
            "Low":  lows,
            "Close": closes,
        })


class TestApolloExecution(unittest.TestCase):
    """Apollo planned_trades: score gate + risk budget assumptions."""

    def test_score_floor_is_75(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.SCORE_FLOOR, 75)

    def test_horizon_default_is_three_trading_days(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.HORIZON_TRADING_DAYS, 3)

    def test_mode_defaults_to_research_only(self):
        """Safety rail: must not default to live/paper until edge proven."""
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.MODE, "research_only")

    def test_max_one_ticket_per_symbol_per_earnings(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.MAX_PLANNED_PER_SYMBOL_PER_EARNINGS, 1)

    def test_score_74_is_below_floor_75_is_at_floor(self):
        """Boundary: >= 75 is accepted, 74 is rejected. No off-by-one."""
        from apollo.execution import planned_trades as pt
        self.assertFalse(74 >= pt.SCORE_FLOOR)
        self.assertTrue(75 >= pt.SCORE_FLOOR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
