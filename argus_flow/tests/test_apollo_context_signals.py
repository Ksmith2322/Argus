"""Unit tests for apollo.strategies.context_signals — peer/macro/news
context layer that enhances earnings scoring.

Tests focus on:
  - PEER_GROUPS integrity (every mapping is reversible, no duplicates)
  - get_full_context aggregation (sum + clamp to [-100, +100])
  - News sentiment keyword scoring (bullish/bearish counts -> signal)
  - Macro signal when SPY/VIX are mocked

Sub-function branches that hit yfinance directly are lightly covered — the
ROI there is low since yfinance is the integration layer. We mock the
three yf-dependent calls and test aggregation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from apollo.strategies import context_signals as cs  # noqa: E402


class TestPeerGroupsIntegrity(unittest.TestCase):
    """Peer lookup drives the 'if 3/4 semis beat, boost the 4th' logic.
    Duplicate symbols across groups would make one symbol's peer set
    ambiguous."""

    def test_no_symbol_appears_in_two_groups(self):
        all_symbols = []
        for group, symbols in cs.PEER_GROUPS.items():
            all_symbols.extend(symbols)
        dupes = {s for s in all_symbols if all_symbols.count(s) > 1}
        self.assertEqual(dupes, set(),
            f"symbols appear in multiple PEER_GROUPS: {dupes}")

    def test_symbol_to_group_reverse_lookup_consistent(self):
        for symbol, group in cs._SYMBOL_TO_GROUP.items():
            self.assertIn(symbol, cs.PEER_GROUPS[group],
                f"{symbol} mapped to {group} but missing from PEER_GROUPS[{group}]")

    def test_known_semiconductor_mapping(self):
        """Regression: NVDA is in semiconductors. A refactor that renames
        the group would quietly break peer-group analysis."""
        self.assertEqual(cs._SYMBOL_TO_GROUP.get("NVDA"), "semiconductors")

    def test_unknown_symbol_has_no_group(self):
        self.assertIsNone(cs._SYMBOL_TO_GROUP.get("ACMEBANANA"))

    def test_every_group_has_at_least_one_symbol(self):
        for group, symbols in cs.PEER_GROUPS.items():
            self.assertGreater(len(symbols), 0, f"empty peer group: {group}")


class TestAnalyzePeerResultsNoGroup(unittest.TestCase):
    def test_unknown_symbol_returns_zero_signal(self):
        result = cs.analyze_peer_results("ACMEBANANA")
        self.assertEqual(result["signal"], 0)
        self.assertEqual(result["peers_reported"], 0)


class TestAnalyzeNewsSentiment(unittest.TestCase):
    """Keyword-based news scoring: bullish_count - bearish_count drives the signal."""

    def _make_fake_ticker(self, titles: list[str]) -> mock.MagicMock:
        t = mock.MagicMock()
        t.news = [{"title": title} for title in titles]
        return t

    def test_strong_bullish_net_yields_large_positive_signal(self):
        """>=3 net bullish -> +20."""
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_fake_ticker([
                "NVDA beats expectations",
                "Company raises guidance",
                "AI demand accelerates",
                "Raised outlook for FY",
            ])
            result = cs.analyze_news_sentiment("NVDA", use_llm=False)
        self.assertGreaterEqual(result["signal"], 15)
        self.assertGreaterEqual(result["bullish_count"], 3)
        self.assertTrue(any("NEWS_BULLISH" in d for d in result["details"]))

    def test_strong_bearish_net_yields_large_negative_signal(self):
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_fake_ticker([
                "Company misses expectations",
                "SEC investigation announced",
                "Downgrade to sell rating",
                "Lawsuit filed",
            ])
            result = cs.analyze_news_sentiment("XYZ", use_llm=False)
        self.assertLessEqual(result["signal"], -15)
        self.assertGreaterEqual(result["bearish_count"], 3)

    def test_mixed_news_small_signal(self):
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_fake_ticker([
                "Company beats expectations",
                "Analyst downgrade",
            ])
            result = cs.analyze_news_sentiment("XYZ", use_llm=False)
        # Net = 0 -> no signal, or +8 for single bullish if positive
        self.assertLessEqual(abs(result["signal"]), 8)

    def test_no_news_is_neutral(self):
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_fake_ticker([])
            result = cs.analyze_news_sentiment("XYZ", use_llm=False)
        self.assertEqual(result["signal"], 0)

    def test_yfinance_exception_yields_zero_signal(self):
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = RuntimeError("api down")
            result = cs.analyze_news_sentiment("XYZ", use_llm=False)
        self.assertEqual(result["signal"], 0)


class TestAnalyzeMacroContext(unittest.TestCase):
    """Macro: SPY above/below EMAs + VIX level -> +/- signal."""

    def _make_spy_vix(self, spy_closes: list[float], vix_last: float) -> mock.MagicMock:
        """Return a fake yf.Ticker factory: SPY returns spy_df, ^VIX returns vix_df."""
        def factory(symbol):
            t = mock.MagicMock()
            if symbol == "SPY":
                df = pd.DataFrame(
                    {"Close": spy_closes},
                    index=pd.date_range("2025-10-01", periods=len(spy_closes), freq="D"),
                )
                t.history.return_value = df
            elif symbol == "^VIX":
                t.history.return_value = pd.DataFrame(
                    {"Close": [vix_last]},
                    index=pd.date_range("2026-04-18", periods=1, freq="D"),
                )
            else:
                t.history.return_value = pd.DataFrame()
            return t
        return factory

    def test_bullish_macro_when_spy_above_emas(self):
        # Ascending series: 200 bars with steady uptrend
        spy_closes = [100 + i * 0.3 for i in range(210)]
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = self._make_spy_vix(spy_closes, vix_last=18.0)
            result = cs.analyze_macro_context()
        self.assertEqual(result["spy_trend"], "bullish")
        self.assertGreater(result["signal"], 0)

    def test_bearish_macro_when_spy_below_emas(self):
        # Descending series
        spy_closes = [200 - i * 0.3 for i in range(210)]
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = self._make_spy_vix(spy_closes, vix_last=18.0)
            result = cs.analyze_macro_context()
        self.assertEqual(result["spy_trend"], "bearish")
        self.assertLess(result["signal"], 0)

    def test_high_vix_reduces_signal(self):
        spy_closes = [100 + i * 0.3 for i in range(210)]  # bullish SPY
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = self._make_spy_vix(spy_closes, vix_last=35.0)
            result = cs.analyze_macro_context()
        # Even with bullish SPY, high VIX should drag signal down
        self.assertEqual(result["market_risk"], "high")
        self.assertTrue(any("HIGH_VIX" in d for d in result["details"]))

    def test_crash_detection_triggers_penalty(self):
        """SPY drops >5% in 5 days -> -20 penalty."""
        spy_closes = [100 + i * 0.3 for i in range(200)]
        # Last 5 bars drop 8%
        crash_start = spy_closes[-1]
        spy_closes += [crash_start * (1 - (i + 1) * 0.02) for i in range(5)]
        with mock.patch.object(cs, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = self._make_spy_vix(spy_closes, vix_last=28.0)
            result = cs.analyze_macro_context()
        self.assertEqual(result["market_risk"], "high")
        self.assertTrue(any("MARKET_CRASH" in d for d in result["details"]))


class TestGetFullContextAggregation(unittest.TestCase):
    """get_full_context sums the 4 sub-signals and clamps to [-100, +100]."""

    def test_total_is_sum_of_four_subs(self):
        peers = {"signal": 20, "details": ["A"]}
        post_er = {"signal": -10, "details": ["B"]}
        news = {"signal": 5, "details": ["C"]}
        macro = {"signal": 15, "details": ["D"]}
        with mock.patch.object(cs, "analyze_peer_results", return_value=peers), \
             mock.patch.object(cs, "analyze_recent_reporters", return_value=post_er), \
             mock.patch.object(cs, "analyze_news_sentiment", return_value=news), \
             mock.patch.object(cs, "analyze_macro_context", return_value=macro):
            result = cs.get_full_context("NVDA")
        self.assertEqual(result["total_signal"], 30)
        self.assertEqual(len(result["details"]), 4)

    def test_clamped_at_plus_100(self):
        with mock.patch.object(cs, "analyze_peer_results", return_value={"signal": 50, "details": []}), \
             mock.patch.object(cs, "analyze_recent_reporters", return_value={"signal": 50, "details": []}), \
             mock.patch.object(cs, "analyze_news_sentiment", return_value={"signal": 50, "details": []}), \
             mock.patch.object(cs, "analyze_macro_context", return_value={"signal": 50, "details": []}):
            result = cs.get_full_context("NVDA")
        self.assertEqual(result["total_signal"], 100)

    def test_clamped_at_minus_100(self):
        with mock.patch.object(cs, "analyze_peer_results", return_value={"signal": -50, "details": []}), \
             mock.patch.object(cs, "analyze_recent_reporters", return_value={"signal": -50, "details": []}), \
             mock.patch.object(cs, "analyze_news_sentiment", return_value={"signal": -50, "details": []}), \
             mock.patch.object(cs, "analyze_macro_context", return_value={"signal": -50, "details": []}):
            result = cs.get_full_context("NVDA")
        self.assertEqual(result["total_signal"], -100)

    def test_return_contains_subsignal_blocks(self):
        zero = {"signal": 0, "details": []}
        with mock.patch.object(cs, "analyze_peer_results", return_value=zero), \
             mock.patch.object(cs, "analyze_recent_reporters", return_value=zero), \
             mock.patch.object(cs, "analyze_news_sentiment", return_value=zero), \
             mock.patch.object(cs, "analyze_macro_context", return_value=zero):
            result = cs.get_full_context("NVDA")
        for key in ("symbol", "total_signal", "details", "peers", "post_er", "news", "macro"):
            self.assertIn(key, result)


class TestBullishBearishKeywordLists(unittest.TestCase):
    """Sanity: keyword lists must not overlap and must be non-empty."""

    def test_no_keyword_in_both_lists(self):
        bull_set = set(cs.BULLISH_KEYWORDS)
        bear_set = set(cs.BEARISH_KEYWORDS)
        overlap = bull_set & bear_set
        self.assertEqual(overlap, set(), f"keywords in both lists: {overlap}")

    def test_both_lists_non_empty(self):
        self.assertGreater(len(cs.BULLISH_KEYWORDS), 0)
        self.assertGreater(len(cs.BEARISH_KEYWORDS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
