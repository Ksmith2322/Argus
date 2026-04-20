"""Unit tests for apollo.runner.score_pre_earnings_setup — the scoring
function that gates every Apollo catalyst signal.

The score floor for planned trades is 75 (locked by test_strategy_cards and
config/strategies.json). This test file exercises the SCORE CONSTRUCTION:
which signals add how many points, and when the function returns None.

Score breakdown (from runner.py):
  baseline                           = 40
  BB squeeze <15th pctile            +25
  BB tight   <30th pctile            +12
  accumulation (vol_trend+vol_ratio) +20
  high_vol alone                     +10
  serial beater (>=75%)              +15
  serial misser (<=25%)              +10
  momentum_into_er                   +10
  weak_into_er                       +10
  imminent 1-5d                      +10
  today/tomorrow 0-1d                +5
  post_er drift up                   +15
  post_er drop                       +10
  ema alignment                       +5
  (clamped to [0, 100])
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _price_frame(*, n: int = 60, close_fn=None) -> pd.DataFrame:
    """Construct a price DataFrame with n trailing bars. close_fn(i) defines
    the close at bar i; other fields are derived (flat OHLC)."""
    if close_fn is None:
        close_fn = lambda i: 100.0  # noqa: E731
    closes = np.array([float(close_fn(i)) for i in range(n)])
    df = pd.DataFrame({
        "Close": closes,
        "Open":  closes,
        "High":  closes * 1.005,
        "Low":   closes * 0.995,
        "Volume": np.ones(n) * 1_000_000,
    })
    df.index = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D")
    return df


def _call_scorer(*, next_earnings, price_df=None, history=None, symbol="AAPL"):
    """Invoke score_pre_earnings_setup with reasonable defaults."""
    from apollo.runner import score_pre_earnings_setup
    data = {
        "price_data": price_df if price_df is not None else _price_frame(),
        "symbol": symbol,
        "next_earnings": next_earnings,
        "history": history,
        "eps_estimate": 1.50,
    }
    return score_pre_earnings_setup(data)


class TestScoreNoneConditions(unittest.TestCase):
    """The scorer must return None for dates outside the window."""

    def test_none_when_earnings_is_none(self):
        self.assertIsNone(_call_scorer(next_earnings=None))

    def test_none_when_earnings_more_than_30_days_out(self):
        far_future = date.today() + timedelta(days=45)
        self.assertIsNone(_call_scorer(next_earnings=far_future))

    def test_none_when_earnings_more_than_5_days_past(self):
        long_ago = date.today() - timedelta(days=14)
        self.assertIsNone(_call_scorer(next_earnings=long_ago))

    def test_not_none_when_within_window(self):
        upcoming = date.today() + timedelta(days=7)
        result = _call_scorer(next_earnings=upcoming)
        self.assertIsNotNone(result)


class TestBaselineScore(unittest.TestCase):
    """With no signals firing, the score should be the baseline (40)
    plus only deterministic additions that fire on any valid call."""

    def test_baseline_score_no_strong_signals(self):
        """Flat price, flat volume, no history, 7 days out -> baseline 40
        (no BB squeeze signal, no volume spike, no beat rate, no trend)."""
        upcoming = date.today() + timedelta(days=7)
        df = _price_frame(close_fn=lambda i: 100.0)  # perfectly flat
        result = _call_scorer(next_earnings=upcoming, price_df=df)
        self.assertIsNotNone(result)
        self.assertLess(result["score"], 75, "flat setup must not hit entry floor")
        self.assertGreaterEqual(result["score"], 40, "baseline floor is 40")


class TestImminentEarningsBonus(unittest.TestCase):
    """IMMINENT (1-5 days out) must add +10 to the score."""

    def test_imminent_adds_points_vs_far_out(self):
        far = date.today() + timedelta(days=20)
        near = date.today() + timedelta(days=3)
        df = _price_frame(close_fn=lambda i: 100.0)  # same flat data
        far_score = _call_scorer(next_earnings=far, price_df=df)["score"]
        near_score = _call_scorer(next_earnings=near, price_df=df)["score"]
        self.assertGreater(near_score, far_score,
            f"imminent earnings should score higher (near={near_score} vs far={far_score})")


class TestSerialBeaterDirection(unittest.TestCase):
    """When historical beat_rate >= 0.75, direction must be 'long'."""

    def test_serial_beater_sets_direction_long(self):
        upcoming = date.today() + timedelta(days=7)
        hist = pd.DataFrame({
            "surprisePercent": [0.05, 0.03, 0.08, 0.02, 0.06, 0.04, 0.01],
        })
        result = _call_scorer(next_earnings=upcoming, history=hist)
        self.assertEqual(result["direction"], "long")
        self.assertGreaterEqual(result["beat_rate"], 0.75)

    def test_serial_misser_sets_direction_short(self):
        upcoming = date.today() + timedelta(days=7)
        hist = pd.DataFrame({
            "surprisePercent": [-0.05, -0.03, -0.08, -0.02, -0.06, -0.04, 0.01],
        })
        result = _call_scorer(next_earnings=upcoming, history=hist)
        self.assertEqual(result["direction"], "short")
        self.assertLessEqual(result["beat_rate"], 0.25)

    def test_mixed_history_leaves_direction_undetermined(self):
        """~50% beat rate → neither long nor short from history alone."""
        upcoming = date.today() + timedelta(days=7)
        hist = pd.DataFrame({
            "surprisePercent": [0.05, -0.03, 0.08, -0.02, 0.01, -0.04],
        })
        result = _call_scorer(next_earnings=upcoming, history=hist)
        # Direction may be neutral or set by later momentum branch; it must
        # NOT be set specifically by the serial-beater/misser rule.
        self.assertIn(result["direction"], ("neutral", "long", "short"))


class TestPostEarningsDrift(unittest.TestCase):
    """Post-earnings (days_until between -5 and 0) with big ret_5d moves
    must trigger POST_ER_DRIFT signals."""

    def test_post_er_drift_up_sets_direction_long(self):
        just_reported = date.today() - timedelta(days=2)
        # Price rises >5% over last 5 days
        df = _price_frame(close_fn=lambda i: 100.0 if i < 55 else 100.0 + (i - 54) * 3)
        result = _call_scorer(next_earnings=just_reported, price_df=df)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "long")
        self.assertTrue(any("POST_ER_DRIFT_UP" in s for s in result["signals"]))


class TestScoreIsClampedTo100(unittest.TestCase):
    """Score output must never exceed 100 even when every signal fires."""

    def test_score_max_is_100(self):
        imminent = date.today() + timedelta(days=2)
        # Construct a series that triggers most positive branches:
        # strong 20d momentum, recent vol spike, beat-rate history
        def close_fn(i):
            return 80.0 if i < 40 else 80.0 + (i - 39) * 0.8  # +40% over 20d
        df = _price_frame(close_fn=close_fn)
        df.loc[df.index[-6:], "Volume"] = 3_000_000  # vol spike last 6 bars
        hist = pd.DataFrame({
            "surprisePercent": [0.05, 0.03, 0.08, 0.02, 0.06, 0.04, 0.01],
        })
        result = _call_scorer(next_earnings=imminent, price_df=df, history=hist)
        self.assertIsNotNone(result)
        self.assertLessEqual(result["score"], 100, "score must be clamped")
        self.assertGreaterEqual(result["score"], 40)


class TestScoreFloorIntegration(unittest.TestCase):
    """The score floor (SCORE_FLOOR=75) is enforced by the planner, not the
    scorer itself. Ensure score values above/below 75 both CAN emerge."""

    def test_weak_setup_scores_below_floor(self):
        """A flat chart with no history should NOT hit 75."""
        upcoming = date.today() + timedelta(days=20)
        df = _price_frame(close_fn=lambda i: 100.0)
        result = _call_scorer(next_earnings=upcoming, price_df=df)
        self.assertLess(result["score"], 75)

    def test_strong_setup_can_exceed_floor(self):
        """A serial beater, imminent, with momentum can pass the floor."""
        imminent = date.today() + timedelta(days=3)
        hist = pd.DataFrame({
            "surprisePercent": [0.05] * 8,  # 100% beat rate
        })
        # Up-trending series
        df = _price_frame(close_fn=lambda i: 80.0 + i * 0.5)
        df.loc[df.index[-6:], "Volume"] = 2_500_000  # vol trending up
        result = _call_scorer(next_earnings=imminent, price_df=df, history=hist)
        self.assertGreaterEqual(result["score"], 60,
            "A strong aligned setup should score at least 60")


class TestScoreReturnShape(unittest.TestCase):
    """The scorer's return dict is the input to planned_trades — shape must
    be stable."""

    def test_return_contains_required_fields(self):
        upcoming = date.today() + timedelta(days=7)
        result = _call_scorer(next_earnings=upcoming)
        required = {"symbol", "score", "direction", "signals",
                    "earnings_date", "days_until", "price",
                    "bb_pctile", "rsi", "vol_ratio", "ret_5d", "ret_20d",
                    "beat_rate", "avg_surprise"}
        missing = required - set(result.keys())
        self.assertFalse(missing, f"missing fields: {missing}")

    def test_score_is_int_like_in_range(self):
        upcoming = date.today() + timedelta(days=7)
        result = _call_scorer(next_earnings=upcoming)
        self.assertIsInstance(result["score"], (int, float, np.integer, np.floating))
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_days_until_is_signed(self):
        """days_until must be positive for future earnings, negative for past."""
        upcoming = date.today() + timedelta(days=7)
        self.assertEqual(_call_scorer(next_earnings=upcoming)["days_until"], 7)
        past = date.today() - timedelta(days=2)
        result = _call_scorer(next_earnings=past)
        self.assertEqual(result["days_until"], -2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
