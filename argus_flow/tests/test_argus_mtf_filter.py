"""Unit tests for the Argus MTF (multi-timeframe) filter — the gate that
blocks trades when higher timeframes disagree with the entry direction.

Drift forensics showed USDJPY had 34/42 signals hitting MTF_BLOCKED in the
last 14-day window. That signal loss is either edge (the filter is saving
us from bad trades) or damage (the filter is too strict). Either way, the
blocker logic itself must be pinned: if we accidentally loosen the AND to
an OR, we'd stop blocking bad trades silently.

Two blocker rules under test:
  1. 15m AND 30m oppose direction        -> BLOCK
  2. 1h opposes AND no lower TF confirms  -> BLOCK
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from argus_flow.advanced_features import MultiTimeframeBuffer  # noqa: E402


def _multi(*, m5="neutral", m15="neutral", m30="neutral", h1="neutral", h4="neutral",
           alignment="neutral", score=0.0) -> dict:
    """Build a get_multi_trend() result for a given (tf, direction) profile."""
    return {
        "timeframes": {
            "5m": {"direction": m5},
            "15m": {"direction": m15},
            "30m": {"direction": m30},
            "1h":  {"direction": h1},
            "4h":  {"direction": h4},
        },
        "alignment": alignment,
        "alignment_score": score,
        "aligned_count": 0,
        "total_active": 0,
    }


class TestCheckEntryAlignment(unittest.TestCase):
    """check_entry_alignment blocks trades against opposing higher TFs."""

    def setUp(self):
        self.buf = MultiTimeframeBuffer()

    def test_allows_entry_when_all_timeframes_align_with_direction(self):
        multi = _multi(m15="up", m30="up", h1="up", h4="up", alignment="up", score=1.0)
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok, f"blocked: {reason}")
        self.assertIn("MTF_OK", reason)

    def test_blocks_long_when_both_15m_and_30m_are_down(self):
        """Primary blocker rule: 15m AND 30m opposing must BLOCK.
        If this test fails after a refactor, someone turned AND into OR."""
        multi = _multi(m15="down", m30="down", h1="neutral", h4="neutral")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertFalse(ok)
        self.assertIn("MTF_BLOCK", reason)
        self.assertIn("15m=down", reason)
        self.assertIn("30m=down", reason)

    def test_blocks_short_when_both_15m_and_30m_are_up(self):
        multi = _multi(m15="up", m30="up", h1="neutral", h4="neutral")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("short")
        self.assertFalse(ok)
        self.assertIn("MTF_BLOCK", reason)

    def test_does_NOT_block_when_only_one_of_15m_30m_opposes(self):
        """Only ONE of {15m,30m} opposing must NOT block — that's the AND,
        not OR, semantics. A refactor that swapped to OR would fail here."""
        multi = _multi(m15="down", m30="neutral", h1="up", h4="up")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok, f"unexpectedly blocked: {reason}")

    def test_blocks_when_1h_opposes_and_no_lower_confirms(self):
        """Secondary blocker rule: 1h down, neither 15m nor 30m up -> BLOCK long."""
        multi = _multi(m15="neutral", m30="neutral", h1="down", h4="down")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertFalse(ok)
        self.assertIn("1h=down", reason)

    def test_does_NOT_block_when_1h_opposes_but_15m_confirms(self):
        """1h opposes direction but 15m agrees — allowed. 15m is our
        lower-TF confirmation override."""
        multi = _multi(m15="up", m30="neutral", h1="down", h4="down")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok, f"unexpectedly blocked: {reason}")

    def test_does_NOT_block_when_1h_opposes_but_30m_confirms(self):
        multi = _multi(m15="neutral", m30="up", h1="down", h4="down")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok)

    def test_4h_bias_is_advisory_not_blocking(self):
        """4h opposite should NOT block — it's advisory. If this test fails,
        someone promoted 4h bias to a hard block."""
        multi = _multi(m15="up", m30="up", h1="up", h4="down")
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok, f"4h alone should not block: {reason}")
        # Reason should still MENTION 4h=down for the audit trail
        self.assertIn("4h=down", reason)

    def test_all_neutral_timeframes_do_not_block(self):
        multi = _multi()
        with mock.patch.object(self.buf, "get_multi_trend", return_value=multi):
            ok, reason = self.buf.check_entry_alignment("long")
        self.assertTrue(ok)


class TestGetTrend(unittest.TestCase):
    """get_trend computes direction from rolling MA on a specific timeframe.
    Math contract: ma_fast > ma_slow by > 2bps -> up, inverse -> down."""

    def test_returns_neutral_with_insufficient_bars(self):
        buf = MultiTimeframeBuffer()
        # No bars populated
        t = buf.get_trend("15m", lookback=20)
        self.assertEqual(t["direction"], "neutral")
        self.assertEqual(t["bars"], 0)

    def test_detects_up_trend_when_recent_closes_rise(self):
        buf = MultiTimeframeBuffer()
        # Populate 20 ascending closes so ma_fast > ma_slow
        buf.bars_15m = [{"close": 100.0 + i * 0.5} for i in range(20)]
        t = buf.get_trend("15m", lookback=20)
        self.assertEqual(t["direction"], "up")
        self.assertGreater(t["ma_fast"], t["ma_slow"])

    def test_detects_down_trend_when_recent_closes_fall(self):
        buf = MultiTimeframeBuffer()
        buf.bars_15m = [{"close": 100.0 - i * 0.5} for i in range(20)]
        t = buf.get_trend("15m", lookback=20)
        self.assertEqual(t["direction"], "down")
        self.assertLess(t["ma_fast"], t["ma_slow"])

    def test_flat_series_is_neutral(self):
        buf = MultiTimeframeBuffer()
        buf.bars_15m = [{"close": 100.0} for _ in range(20)]
        t = buf.get_trend("15m", lookback=20)
        self.assertEqual(t["direction"], "neutral")


class TestGet4hBias(unittest.TestCase):
    """4h bias uses close-vs-open, not MA crossover, because 4h bars are sparse."""

    def test_bullish_bar_is_up(self):
        buf = MultiTimeframeBuffer()
        buf.bars_4h = [{"open": 100, "high": 101, "low": 99.5, "close": 100.8}]
        b = buf.get_4h_bias()
        self.assertEqual(b["direction"], "up")

    def test_bearish_bar_is_down(self):
        buf = MultiTimeframeBuffer()
        buf.bars_4h = [{"open": 100, "high": 100.2, "low": 99, "close": 99.3}]
        b = buf.get_4h_bias()
        self.assertEqual(b["direction"], "down")

    def test_empty_is_neutral(self):
        buf = MultiTimeframeBuffer()
        b = buf.get_4h_bias()
        self.assertEqual(b["direction"], "neutral")

    def test_body_ratio_feeds_strength(self):
        """A decisive candle (body = 80% of range) gets higher strength
        than a doji (body ~ 10%)."""
        buf_strong = MultiTimeframeBuffer()
        buf_strong.bars_4h = [{"open": 100, "high": 100.1, "low": 99.9, "close": 100.08}]
        strong = buf_strong.get_4h_bias()

        buf_weak = MultiTimeframeBuffer()
        buf_weak.bars_4h = [{"open": 100, "high": 101, "low": 99, "close": 100.02}]
        weak = buf_weak.get_4h_bias()

        self.assertGreater(strong["strength"], weak["strength"])


class TestAlignmentScore(unittest.TestCase):
    """alignment_score = fraction of non-neutral TFs agreeing on direction."""

    def test_unanimous_up_is_score_one(self):
        buf = MultiTimeframeBuffer()
        # Populate all TFs with ascending closes (100 bars is enough for 1h/4h lookback)
        up_bars = [{"close": 100.0 + i * 0.5, "open": 100.0 + i * 0.5 - 0.1,
                    "high": 100.0 + i * 0.5 + 0.1, "low": 100.0 + i * 0.5 - 0.2} for i in range(50)]
        buf.bars_5m = list(up_bars)
        buf.bars_15m = list(up_bars)
        buf.bars_30m = list(up_bars)
        buf.bars_1h = list(up_bars)
        buf.bars_4h = [up_bars[-1]]  # one 4h bar with bullish body

        multi = buf.get_multi_trend()
        self.assertEqual(multi["alignment"], "up")
        self.assertAlmostEqual(multi["alignment_score"], 1.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
