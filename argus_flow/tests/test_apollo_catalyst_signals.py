"""Unit tests for apollo.strategies.catalyst_signals — composite signal
aggregation for earnings catalyst scoring.

Sub-signals (options_flow, insider, analyst) each return a partial score
in [-50, +50]. get_all_signals aggregates to a combined [-100, +100] score
with a consensus direction. The consensus logic has three rules:

  1. 2-out-of-3 directions agree -> that direction wins
  2. Tiebreak: total_signal magnitude breaks ties
  3. Clamp: total_signal capped at [-100, +100]

Tests mock the three sub-functions and verify the aggregation + clamp.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _stub(signal: int, direction: str, details: list[str] | None = None) -> dict:
    """Build a sub-signal return dict."""
    return {
        "signal": signal,
        "direction": direction,
        "details": details or [],
    }


class TestGetAllSignalsConsensus(unittest.TestCase):
    """Consensus direction rules: 2-of-3 agreement wins, else total_signal."""

    def test_two_bullish_one_neutral_yields_bullish(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(20, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(15, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(0, "neutral")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["consensus"], "bullish")

    def test_two_bearish_one_bullish_yields_bearish(self):
        """2-of-3 wins even when the lone bullish has high magnitude."""
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(50, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(-10, "bearish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(-15, "bearish")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["consensus"], "bearish")

    def test_all_neutral_yields_neutral(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(0, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(0, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(0, "neutral")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["consensus"], "neutral")

    def test_no_majority_but_strong_total_signal_still_picks_direction(self):
        """If only 1 sub-signal has a direction but total_signal > 10, that
        becomes the consensus (fallback rule)."""
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(25, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(0, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(0, "neutral")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["consensus"], "bullish")

    def test_weak_signal_yields_neutral_consensus(self):
        """Total signal in [-10, +10] without majority direction -> neutral."""
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(5, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(3, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(0, "neutral")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["consensus"], "neutral")


class TestGetAllSignalsAggregation(unittest.TestCase):
    """total_signal = sum of three sub-signals, clamped to [-100, +100]."""

    def test_total_is_sum_of_three_subsignals(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(20, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(15, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(10, "bullish")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["total_signal"], 45)

    def test_total_clamped_at_plus_100(self):
        """Three 50-score bullish signals = 150 raw, clamped to 100."""
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(50, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(50, "bullish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(50, "bullish")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["total_signal"], 100)

    def test_total_clamped_at_minus_100(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(-50, "bearish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(-50, "bearish")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(-50, "bearish")):
            result = get_all_signals("AAPL")
        self.assertEqual(result["total_signal"], -100)

    def test_details_concatenated_from_all_sources(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(10, "bullish", ["OPT_A", "OPT_B"])), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(10, "bullish", ["INS_A"])), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(10, "bullish", ["ANA_A"])):
            result = get_all_signals("AAPL")
        self.assertEqual(len(result["details"]), 4)
        self.assertIn("OPT_A", result["details"])
        self.assertIn("INS_A", result["details"])
        self.assertIn("ANA_A", result["details"])


class TestGetAllSignalsReturnShape(unittest.TestCase):
    def test_return_contains_symbol_and_subsignals(self):
        from apollo.strategies.catalyst_signals import get_all_signals
        with mock.patch("apollo.strategies.catalyst_signals.analyze_options_flow",
                        return_value=_stub(0, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_insider_activity",
                        return_value=_stub(0, "neutral")), \
             mock.patch("apollo.strategies.catalyst_signals.analyze_analyst_revisions",
                        return_value=_stub(0, "neutral")):
            result = get_all_signals("MSFT")
        for key in ("symbol", "total_signal", "consensus", "details",
                    "options", "insider", "analyst"):
            self.assertIn(key, result)
        self.assertEqual(result["symbol"], "MSFT")


class TestSubsignalDirectionBoundaries(unittest.TestCase):
    """Direction string is set from signal value in each sub-function.
    The boundaries (>5 bullish, <-5 bearish, else neutral) live in every
    sub-function and must agree. These tests operate against the live
    functions with yfinance forced unavailable so only the zero-case runs."""

    def test_no_yfinance_returns_neutral_direction(self):
        """When yf=None (simulated by import failure), sub-functions return
        the default neutral result without crashing."""
        import apollo.strategies.catalyst_signals as cs
        with mock.patch.object(cs, "yf", None):
            opts = cs.analyze_options_flow("AAPL")
            ins = cs.analyze_insider_activity("AAPL")
            anl = cs.analyze_analyst_revisions("AAPL")
        for result in (opts, ins, anl):
            self.assertEqual(result["signal"], 0)
            self.assertEqual(result["direction"], "neutral")

    def test_yfinance_exception_yields_neutral_default(self):
        """Any yfinance exception must return the default result, not raise."""
        import apollo.strategies.catalyst_signals as cs
        bad_ticker = mock.MagicMock()
        bad_ticker.options = mock.PropertyMock(side_effect=RuntimeError("api boom"))
        bad_ticker_cls = mock.MagicMock(return_value=bad_ticker)
        with mock.patch.object(cs, "yf", mock.MagicMock(Ticker=bad_ticker_cls)):
            result = cs.analyze_options_flow("AAPL")
        self.assertEqual(result["signal"], 0)
        self.assertEqual(result["direction"], "neutral")


if __name__ == "__main__":
    unittest.main(verbosity=2)
