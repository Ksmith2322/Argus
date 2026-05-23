"""Unit tests for apollo.strategies.market_data and position_rules.

market_data pulls short interest / analyst targets / valuation signals;
position_rules decides post-ER entry/exit per the backtest-proven
"Day-After Big Gap" play (PF 2.61 on 7%+ gaps, 6.14 on 8%+).

Tests cover:
  - get_full_profile aggregation (sum of three sub-signals)
  - yfinance-unavailable fallback to zeroed signal
  - should_enter_post_er: the gap-size gate (< 6% rejected) and direction
    logic (long on beat+gap-up, short on miss+gap-down)
  - Stop-size + max-hold scale with gap magnitude
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from apollo.strategies import market_data as md  # noqa: E402
from apollo.strategies import position_rules as pr  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# market_data
# ═══════════════════════════════════════════════════════════════

class TestMarketDataFallbacks(unittest.TestCase):
    """yfinance missing / erroring out -> zero-signal defaults."""

    def test_short_interest_no_yfinance(self):
        with mock.patch.object(md, "yf", None):
            r = md.get_short_interest("AAPL")
        self.assertEqual(r["signal"], 0)
        self.assertEqual(r["short_pct_float"], 0)

    def test_analyst_targets_no_yfinance(self):
        with mock.patch.object(md, "yf", None):
            r = md.get_analyst_targets("AAPL")
        self.assertEqual(r["signal"], 0)

    def test_valuation_no_yfinance(self):
        with mock.patch.object(md, "yf", None):
            r = md.get_valuation_context("AAPL")
        self.assertEqual(r["signal"], 0)


class TestShortInterestScoring(unittest.TestCase):
    def _mock_info(self, info: dict) -> mock.MagicMock:
        t = mock.MagicMock()
        t.info = info
        mock_yf = mock.MagicMock()
        mock_yf.Ticker.return_value = t
        return mock_yf

    def test_high_short_pct_adds_points(self):
        mock_yf = self._mock_info({"shortPercentOfFloat": 0.25,
                                    "shortRatio": 3.0})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_short_interest("AMC")
        self.assertGreaterEqual(r["signal"], 10)
        self.assertTrue(any("HIGH_SHORT" in d for d in r["details"]))

    def test_short_covering_yields_bullish(self):
        """shares_short fell > 15% MoM -> bullish."""
        mock_yf = self._mock_info({"sharesShort": 800_000,
                                    "sharesShortPriorMonth": 1_000_000})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_short_interest("AMC")
        self.assertGreater(r["signal"], 0)
        self.assertTrue(any("SHORT_COVERING" in d for d in r["details"]))

    def test_short_building_yields_bearish(self):
        mock_yf = self._mock_info({"sharesShort": 1_200_000,
                                    "sharesShortPriorMonth": 1_000_000})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_short_interest("AMC")
        self.assertLess(r["signal"], 0)
        self.assertTrue(any("SHORT_BUILDING" in d for d in r["details"]))


class TestAnalystTargets(unittest.TestCase):
    def _mock_info(self, info: dict) -> mock.MagicMock:
        t = mock.MagicMock()
        t.info = info
        mock_yf = mock.MagicMock()
        mock_yf.Ticker.return_value = t
        return mock_yf

    def test_big_upside_adds_points(self):
        mock_yf = self._mock_info({"currentPrice": 100, "targetMeanPrice": 150,
                                    "recommendationMean": 2.0})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_analyst_targets("MSFT")
        self.assertGreaterEqual(r["signal"], 10)
        self.assertEqual(r["upside_pct"], 50.0)

    def test_strong_buy_consensus_adds_points(self):
        mock_yf = self._mock_info({"currentPrice": 100, "targetMeanPrice": 105,
                                    "recommendationMean": 1.4})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_analyst_targets("MSFT")
        self.assertTrue(any("STRONG_BUY" in d for d in r["details"]))

    def test_sell_consensus_subtracts(self):
        mock_yf = self._mock_info({"currentPrice": 100, "targetMeanPrice": 95,
                                    "recommendationMean": 4.0})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_analyst_targets("XYZ")
        self.assertLess(r["signal"], 0)

    def test_overvalued_above_target_subtracts(self):
        mock_yf = self._mock_info({"currentPrice": 100, "targetMeanPrice": 80,
                                    "recommendationMean": 3.0})
        with mock.patch.object(md, "yf", mock_yf):
            r = md.get_analyst_targets("XYZ")
        self.assertLess(r["signal"], 0)


class TestGetFullProfile(unittest.TestCase):
    """get_full_profile sums short + targets + valuation sub-signals."""

    def test_total_is_sum_of_three(self):
        with mock.patch.object(md, "get_short_interest",
                               return_value={"signal": 10, "details": ["a"]}), \
             mock.patch.object(md, "get_analyst_targets",
                               return_value={"signal": 5, "details": ["b"]}), \
             mock.patch.object(md, "get_valuation_context",
                               return_value={"signal": -3, "details": ["c"]}):
            result = md.get_full_profile("AAPL")
        self.assertEqual(result["total_signal"], 12)
        self.assertEqual(len(result["details"]), 3)
        self.assertEqual(result["symbol"], "AAPL")


# ═══════════════════════════════════════════════════════════════
# position_rules: should_enter_post_er
# ═══════════════════════════════════════════════════════════════

class _PostErLogicTestBase(unittest.TestCase):
    """Shared setUp: disable the SCOPE_APOLLO_VALIDATED_FILTER overlay so
    these tests exercise the underlying gap/surprise/direction logic.
    The scope filter (added 2026-04-20) narrows live apollo to a tiny
    validated universe; tests below validate the rules the live overlay
    sits on top of."""

    def setUp(self):
        self._prev_scope_filter = pr.SCOPE_APOLLO_VALIDATED_FILTER
        pr.SCOPE_APOLLO_VALIDATED_FILTER = False

    def tearDown(self):
        pr.SCOPE_APOLLO_VALIDATED_FILTER = self._prev_scope_filter


class TestShouldEnterPostErGapGate(_PostErLogicTestBase):
    """The 6% gap floor is the PROVEN edge boundary per 110-stock backtest.
    Below 6% = breakeven or negative. Changing this gate without re-running
    the backtest invalidates the strategy."""

    def test_gap_5pct_rejected(self):
        plan = pr.should_enter_post_er(gap_pct=5.0, surprise_pct=3.0)
        self.assertIsNone(plan, "gap < 6% must be rejected")

    def test_gap_neg_5pct_rejected(self):
        """Symmetric: small negative gap also rejected."""
        plan = pr.should_enter_post_er(gap_pct=-5.0, surprise_pct=-3.0)
        self.assertIsNone(plan)

    def test_gap_6pct_with_beat_is_long(self):
        plan = pr.should_enter_post_er(gap_pct=6.5, surprise_pct=2.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, "long")

    def test_gap_6pct_with_miss_rejected(self):
        """6% gap UP but negative surprise -> not a beat -> no entry."""
        plan = pr.should_enter_post_er(gap_pct=6.5, surprise_pct=-1.0)
        self.assertIsNone(plan)


class TestPostErDirectionLogic(_PostErLogicTestBase):
    def test_strong_beat_big_gap_very_strong_bucket(self):
        """8%+ gap + beat -> 'VERY_STRONG' reason + long."""
        plan = pr.should_enter_post_er(gap_pct=9.0, surprise_pct=5.0)
        self.assertEqual(plan.direction, "long")
        self.assertIn("VERY_STRONG", plan.reason)

    def test_miss_gap_down_short(self):
        """Miss + gap down >= 2% -> SHORT."""
        plan = pr.should_enter_post_er(gap_pct=-6.0, surprise_pct=-3.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, "short")

    def test_serial_beater_reason_appended(self):
        plan = pr.should_enter_post_er(gap_pct=7.0, surprise_pct=3.0, beat_rate=0.80)
        self.assertIn("SERIAL_BEATER", plan.reason)

    def test_low_volume_warning_appended(self):
        plan = pr.should_enter_post_er(gap_pct=7.0, surprise_pct=3.0,
                                        day1_volume_ratio=0.5)
        self.assertIn("LOW_VOL", plan.reason)

    def test_high_volume_confirmation_appended(self):
        plan = pr.should_enter_post_er(gap_pct=7.0, surprise_pct=3.0,
                                        day1_volume_ratio=3.0)
        self.assertIn("VOLUME_CONFIRMED", plan.reason)


class TestPostErPlanSizing(_PostErLogicTestBase):
    """Stop% and max_hold scale with gap magnitude."""

    def test_moderate_gap_uses_4pct_stop(self):
        plan = pr.should_enter_post_er(gap_pct=7.0, surprise_pct=3.0)
        self.assertEqual(plan.stop_pct, 4.0)

    def test_very_big_gap_uses_5pct_stop(self):
        """|gap| >= 10 -> wider stop (5%) + longer hold."""
        plan = pr.should_enter_post_er(gap_pct=12.0, surprise_pct=5.0)
        self.assertEqual(plan.stop_pct, 5.0)

    def test_short_has_shorter_max_hold(self):
        """Long drift lasts longer than short drift per backtest."""
        long_plan = pr.should_enter_post_er(gap_pct=7.0, surprise_pct=3.0)
        short_plan = pr.should_enter_post_er(gap_pct=-7.0, surprise_pct=-3.0)
        self.assertGreater(long_plan.max_hold_days, short_plan.max_hold_days)

    def test_all_plans_target_day2_open(self):
        """entry_timing is always 'day2_open' — PDT-safe + captures the
        confirmed gap. A refactor that slipped to 'day1' or 'market' would
        break the edge."""
        for gap in (7.0, 10.0, 12.0, -7.0, -10.0):
            surprise = 3.0 if gap > 0 else -3.0
            plan = pr.should_enter_post_er(gap_pct=gap, surprise_pct=surprise)
            self.assertEqual(plan.entry_timing, "day2_open",
                f"plan for gap={gap} has wrong entry_timing")


class TestExitRules(unittest.TestCase):
    """EXIT_RULES dict must have entries for both long and short."""

    def test_long_has_required_rules(self):
        rules = pr.EXIT_RULES["long"]
        names = {r["name"] for r in rules}
        for required in ("TRAILING_STOP", "HARD_STOP", "TIMEOUT"):
            self.assertIn(required, names)

    def test_short_has_required_rules(self):
        rules = pr.EXIT_RULES["short"]
        names = {r["name"] for r in rules}
        for required in ("TRAILING_STOP", "HARD_STOP", "TIMEOUT"):
            self.assertIn(required, names)


class TestFormatPostErPlan(_PostErLogicTestBase):
    def test_format_contains_all_plan_fields(self):
        plan = pr.should_enter_post_er(gap_pct=8.0, surprise_pct=4.0)
        plan.symbol = "NVDA"
        plan.entry_price = 500.0
        out = pr.format_post_er_plan(plan)
        self.assertIn("NVDA", out)
        self.assertIn("LONG", out)
        self.assertIn("500", out)
        self.assertIn("TRAILING_STOP", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
