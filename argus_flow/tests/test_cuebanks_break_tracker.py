"""Tests for the Cue Banks BreakTracker — stateful break + retest detection.

Fences the core mechanical contract from his "no retest, no entry" rule:
a level must close past by more than tolerance (body not wick), must
retest within the decay window, and the retest fires only during a short
recency window after the touch.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _bar(o, h, l, c):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c})


class TestBreakTrackerBreakDetection(unittest.TestCase):
    def setUp(self):
        from forge.cuebanks.confluence import BreakTracker
        self.tracker = BreakTracker(tolerance_pct=0.002)
        # Level at 100 with 0.2% tolerance band = 99.8 - 100.2
        self.tracker.register_levels([{"level": 100.0, "type": "resistance"}])

    def _prime_below(self):
        """Prime the tracker with a bar whose close sits comfortably below
        the level so the next close past tolerance registers as a cross."""
        self.tracker.update(-1, _bar(o=98.0, h=98.5, l=97.5, c=98.2))

    def _prime_above(self):
        self.tracker.update(-1, _bar(o=101.5, h=102.0, l=101.2, c=101.8))

    def test_wick_alone_does_not_break(self):
        """Wick pokes above the level but close stays below — not a break."""
        self._prime_below()
        self.tracker.update(0, _bar(o=99.0, h=100.5, l=98.8, c=99.5))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["status"], "armed")

    def test_close_above_tolerance_breaks_upward(self):
        self._prime_below()
        self.tracker.update(0, _bar(o=99.0, h=101.5, l=99.0, c=101.0))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["status"], "broken")
        self.assertEqual(state["broken_at_bar"], 0)
        self.assertEqual(state["broken_direction"], "up")

    def test_close_below_tolerance_breaks_downward(self):
        self._prime_above()
        self.tracker.update(0, _bar(o=101.0, h=101.5, l=99.0, c=99.0))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["broken_direction"], "down")

    def test_close_inside_tolerance_band_does_not_break(self):
        self._prime_below()
        self.tracker.update(0, _bar(o=99.9, h=100.2, l=99.8, c=100.1))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["status"], "armed")

    def test_first_bar_never_breaks_without_prior_position(self):
        """A standalone first bar can't be a cross (no prev close)."""
        self.tracker.update(0, _bar(o=99.0, h=102.0, l=99.0, c=101.5))
        self.assertEqual(self.tracker.levels[100.0]["status"], "armed")


class TestBreakTrackerRetestDetection(unittest.TestCase):
    def setUp(self):
        from forge.cuebanks.confluence import BreakTracker
        self.tracker = BreakTracker(tolerance_pct=0.002, retest_window_bars=30,
                                     recency_bars=3)
        self.tracker.register_levels([{"level": 100.0, "type": "resistance"}])

    def test_retest_from_above_after_upward_break(self):
        # Prime below the level, then break up, then pullback-retest
        self.tracker.update(-1, _bar(o=98.0, h=98.5, l=97.8, c=98.2))
        self.tracker.update(0, _bar(o=99.0, h=102.0, l=99.0, c=101.5))
        self.tracker.update(1, _bar(o=101.5, h=102.0, l=101.0, c=101.5))
        self.tracker.update(2, _bar(o=101.5, h=101.8, l=101.2, c=101.4))
        # Pullback touches the broken level from above
        self.tracker.update(3, _bar(o=101.4, h=101.5, l=99.9, c=100.5))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["retest_touched_at"], 3)

    def test_retest_from_below_after_downward_break(self):
        self.tracker.update(-1, _bar(o=101.5, h=102.0, l=101.2, c=101.8))
        self.tracker.update(0, _bar(o=101.0, h=101.0, l=98.0, c=98.5))
        self.tracker.update(1, _bar(o=98.5, h=99.0, l=98.0, c=98.5))
        # Bounce back up to the level
        self.tracker.update(2, _bar(o=98.5, h=100.2, l=98.5, c=99.5))
        state = self.tracker.levels[100.0]
        self.assertEqual(state["retest_touched_at"], 2)


class TestBreakTrackerStaleReset(unittest.TestCase):
    def test_break_goes_stale_after_window_with_no_retest(self):
        from forge.cuebanks.confluence import BreakTracker
        tracker = BreakTracker(tolerance_pct=0.002, retest_window_bars=5,
                                recency_bars=3)
        tracker.register_levels([{"level": 100.0, "type": "resistance"}])
        # Prime below so the upward close registers as a cross
        tracker.update(-1, _bar(o=98.0, h=98.5, l=97.8, c=98.2))
        tracker.update(0, _bar(o=99.0, h=102.0, l=99.0, c=101.5))
        # Price drifts far above the level and never retests
        for i in range(1, 10):
            tracker.update(i, _bar(o=105.0, h=106.0, l=104.5, c=105.5))
        state = tracker.levels[100.0]
        self.assertEqual(state["status"], "stale")
        self.assertIsNone(tracker.is_fresh_retest(10, "LONG"))
        self.assertIsNone(tracker.is_fresh_retest(10, "SHORT"))


class TestBreakTrackerFreshRetest(unittest.TestCase):
    def _prime_and_break_up(self, tracker):
        tracker.update(-1, _bar(o=98.0, h=98.5, l=97.8, c=98.2))
        tracker.update(0, _bar(o=99.0, h=102.0, l=99.0, c=101.5))  # break up
        tracker.update(3, _bar(o=101.5, h=101.5, l=99.9, c=100.5))  # retest touch at 3

    def test_is_fresh_retest_returns_level_during_recency_window(self):
        from forge.cuebanks.confluence import BreakTracker
        tracker = BreakTracker(tolerance_pct=0.002, recency_bars=3)
        tracker.register_levels([{"level": 100.0, "type": "resistance"}])
        self._prime_and_break_up(tracker)
        # Bar 4 is within recency (age=1)
        self.assertEqual(tracker.is_fresh_retest(4, "LONG"), 100.0)

    def test_is_fresh_retest_returns_none_after_recency_elapsed(self):
        from forge.cuebanks.confluence import BreakTracker
        tracker = BreakTracker(tolerance_pct=0.002, recency_bars=3)
        tracker.register_levels([{"level": 100.0, "type": "resistance"}])
        self._prime_and_break_up(tracker)
        # Bar 10 is way outside recency
        self.assertIsNone(tracker.is_fresh_retest(10, "LONG"))

    def test_direction_mismatch_returns_none(self):
        from forge.cuebanks.confluence import BreakTracker
        tracker = BreakTracker(tolerance_pct=0.002, recency_bars=3)
        tracker.register_levels([{"level": 100.0, "type": "resistance"}])
        self._prime_and_break_up(tracker)
        self.assertIsNotNone(tracker.is_fresh_retest(4, "LONG"))
        self.assertIsNone(tracker.is_fresh_retest(4, "SHORT"))


class TestBreakTrackerMultipleLevels(unittest.TestCase):
    def test_handles_multiple_levels_independently(self):
        from forge.cuebanks.confluence import BreakTracker
        tracker = BreakTracker(tolerance_pct=0.002)
        tracker.register_levels([
            {"level": 100.0, "type": "resistance"},
            {"level": 110.0, "type": "resistance"},
        ])
        # Prime below both levels
        tracker.update(-1, _bar(o=98.0, h=98.5, l=97.8, c=98.2))
        # Now close above 100 but still below 110 → only 100 breaks up
        tracker.update(0, _bar(o=99.0, h=105.0, l=99.0, c=104.0))
        self.assertEqual(tracker.levels[100.0]["status"], "broken")
        self.assertEqual(tracker.levels[100.0]["broken_direction"], "up")
        self.assertEqual(tracker.levels[110.0]["status"], "armed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
