"""Unit tests for forge.conviction — cross-intelligence sizing multiplier.

score_conviction aggregates 5 factors (atlas regime, atlas cascade,
options flow, themis signal, cross-system) plus signal_strength into a
total score, then buckets to a size_multiplier [0.25, 2.0].

Tests pin the threshold map + return shape. Sub-factor functions all
depend on files in forge/logs/ that may or may not exist; we mock them
to zero unless specifically exercised.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from forge import conviction as cv  # noqa: E402


def _zero_factor() -> dict:
    return {"score": 0.0, "detail": ""}


class TestScoreConvictionBuckets(unittest.TestCase):
    """Score-to-multiplier mapping is load-bearing for sizing. Every
    bucket boundary must fire at the exact expected value."""

    def _mock_all_zero(self):
        """Patch all sub-factor functions to return zero."""
        patches = [
            mock.patch.object(cv, "_score_atlas_regime", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_atlas_cascade", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_options_flow", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_themis", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_cross_system", return_value=_zero_factor()),
        ]
        return patches

    def test_medium_conviction_at_signal_strength_1_0(self):
        """signal_strength=1.0, all factors zero -> total=1.0 -> medium (1.0x)."""
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=1.0)
        self.assertEqual(result["conviction"], "medium")
        self.assertEqual(result["size_multiplier"], 1.0)

    def test_high_conviction_at_1_2(self):
        """Boundary: total=1.2 -> high (1.5x)."""
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=1.2)
        self.assertEqual(result["conviction"], "high")
        self.assertEqual(result["size_multiplier"], 1.5)

    def test_max_conviction_at_1_5(self):
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=1.5)
        self.assertEqual(result["conviction"], "max")
        self.assertEqual(result["size_multiplier"], 2.0)

    def test_low_conviction_warning_at_negative_total(self):
        """Conflicting signals (total < 0) -> 0.25x + warning."""
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=-0.5)
        self.assertEqual(result["conviction"], "low")
        self.assertEqual(result["size_multiplier"], 0.25)
        self.assertTrue(any("conflicting" in w for w in result["warnings"]))

    def test_low_conviction_at_0_4(self):
        """total in [0, 0.5) -> 0.5x."""
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=0.4)
        self.assertEqual(result["size_multiplier"], 0.5)

    def test_low_conviction_at_0_7(self):
        """total in [0.5, 0.8) -> 0.75x."""
        patches = self._mock_all_zero()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG", signal_strength=0.7)
        self.assertEqual(result["size_multiplier"], 0.75)


class TestScoreConvictionReturnShape(unittest.TestCase):
    def test_returns_all_expected_keys(self):
        patches = [
            mock.patch.object(cv, "_score_atlas_regime", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_atlas_cascade", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_options_flow", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_themis", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_cross_system", return_value=_zero_factor()),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG")
        for key in ("size_multiplier", "conviction", "factors", "warnings", "total_score"):
            self.assertIn(key, result)

    def test_factor_dict_contains_all_five_sources(self):
        patches = [
            mock.patch.object(cv, "_score_atlas_regime", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_atlas_cascade", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_options_flow", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_themis", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_cross_system", return_value=_zero_factor()),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = cv.score_conviction("apollo", "NVDA", "LONG")
        for f in ("atlas_regime", "atlas_event", "options_flow",
                  "themis_signal", "cross_system"):
            self.assertIn(f, result["factors"])


class TestDirectionAndTickerNormalisation(unittest.TestCase):
    """Input normalization: ticker upper, direction upper, system lower."""

    def test_lowercase_inputs_normalised(self):
        patches = [
            mock.patch.object(cv, "_score_atlas_regime", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_atlas_cascade", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_options_flow", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_themis", return_value=_zero_factor()),
            mock.patch.object(cv, "_score_cross_system", return_value=_zero_factor()),
        ]
        with patches[0] as m_regime, patches[1] as m_cascade, \
             patches[2] as m_flow, patches[3] as m_themis, patches[4] as m_cross:
            cv.score_conviction("APOLLO", "nvda", "long")
            # sub-functions called with NORMALISED args
            args_cascade = m_cascade.call_args
            # Ticker passed in upper, direction in upper
            self.assertEqual(args_cascade[0][0], "NVDA")
            self.assertEqual(args_cascade[0][1], "LONG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
