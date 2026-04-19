"""Tests for helio.promotion_readiness — review gate + canonical gate evaluation.

Critical paths: the gates must reject and accept at exact boundaries. Drift
here = premature promotion of losing strategies or missed promotion of
winners.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import promotion_readiness as pr  # noqa: E402


class TestReviewGateBoundaries(unittest.TestCase):
    """Review gate: 30+ trades, PF≥1.20, positive expectancy."""

    def test_fails_at_29_trades_even_if_pf_high(self):
        stats = {"trades": 29, "profit_factor": 2.0, "pnl_usd": 500}
        ok, blockers = pr._evaluate_gate(stats, pr.REVIEW_GATE)
        self.assertFalse(ok)
        self.assertTrue(any("30" in b for b in blockers))

    def test_passes_at_exactly_30_trades(self):
        stats = {"trades": 30, "profit_factor": 1.20, "pnl_usd": 100}
        ok, blockers = pr._evaluate_gate(stats, pr.REVIEW_GATE)
        self.assertTrue(ok, f"expected pass, got blockers: {blockers}")

    def test_fails_at_pf_just_below_threshold(self):
        stats = {"trades": 30, "profit_factor": 1.19, "pnl_usd": 100}
        ok, blockers = pr._evaluate_gate(stats, pr.REVIEW_GATE)
        self.assertFalse(ok)
        self.assertTrue(any("PF" in b for b in blockers))

    def test_fails_on_negative_expectancy_even_with_trades_and_pf(self):
        """Expectancy blocker fires when pnl < 0 despite hitting trade count."""
        stats = {"trades": 30, "profit_factor": 1.20, "pnl_usd": -50}  # neg expectancy
        ok, blockers = pr._evaluate_gate(stats, pr.REVIEW_GATE)
        self.assertFalse(ok)
        self.assertTrue(any("expectancy" in b for b in blockers))


class TestCanonicalGateBoundaries(unittest.TestCase):
    """Canonical gate: 60+ trades, PF≥1.30."""

    def test_fails_at_59_trades(self):
        stats = {"trades": 59, "profit_factor": 1.50, "pnl_usd": 500}
        ok, _ = pr._evaluate_gate(stats, pr.CANONICAL_GATE)
        self.assertFalse(ok)

    def test_passes_at_60_trades_pf_1_30(self):
        stats = {"trades": 60, "profit_factor": 1.30, "pnl_usd": 100}
        ok, blockers = pr._evaluate_gate(stats, pr.CANONICAL_GATE)
        self.assertTrue(ok, f"expected pass, got blockers: {blockers}")

    def test_fails_at_pf_1_29_on_60_trades(self):
        stats = {"trades": 60, "profit_factor": 1.29, "pnl_usd": 100}
        ok, _ = pr._evaluate_gate(stats, pr.CANONICAL_GATE)
        self.assertFalse(ok)


class TestNextAction(unittest.TestCase):
    """evaluate_strategy should produce correct next_action per scenario."""

    def test_zero_trades_gives_no_live_evidence(self):
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 0, "profit_factor": 0, "pnl_usd": 0}):
            result = pr.evaluate_strategy({"label": "forge_gld_pm_long",
                                            "card": "research/strategy_cards/forge_gld_pm_long.md"})
        self.assertIn("NO_LIVE_EVIDENCE", result["next_action"])

    def test_some_trades_but_below_gates_observe_more(self):
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 15, "profit_factor": 1.1, "pnl_usd": 50}):
            result = pr.evaluate_strategy({"label": "forge_gld_pm_long",
                                            "card": "research/strategy_cards/forge_gld_pm_long.md"})
        self.assertEqual(result["next_action"], "OBSERVE_MORE")

    def test_meaningful_losing_sample_is_kill_candidate(self):
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 25, "profit_factor": 0.8, "pnl_usd": -200}):
            result = pr.evaluate_strategy({"label": "forge_gld_pm_long",
                                            "card": "research/strategy_cards/forge_gld_pm_long.md"})
        self.assertIn("KILL_CANDIDATE", result["next_action"])

    def test_review_gate_passed_action(self):
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 35, "profit_factor": 1.25, "pnl_usd": 250}):
            result = pr.evaluate_strategy({"label": "forge_gld_pm_long",
                                            "card": "research/strategy_cards/forge_gld_pm_long.md"})
        self.assertIn("REVIEW_GATE_PASSED", result["next_action"])

    def test_canonical_gate_passed_promotion_eligible(self):
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 65, "profit_factor": 1.35, "pnl_usd": 800}):
            result = pr.evaluate_strategy({"label": "forge_gld_pm_long",
                                            "card": "research/strategy_cards/forge_gld_pm_long.md"})
        self.assertEqual(result["next_action"], "PROMOTION_ELIGIBLE")

    def test_no_card_prevents_promotion_even_at_canonical_gate(self):
        """If the strategy card is missing, action downgrades — we require a card."""
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value={"trades": 65, "profit_factor": 1.35, "pnl_usd": 800}):
            result = pr.evaluate_strategy({"label": "argus_usdjpy", "card": None})
        self.assertNotEqual(result["next_action"], "PROMOTION_ELIGIBLE")


class TestBuildReport(unittest.TestCase):
    def test_build_report_has_expected_shape(self):
        report = pr.build_report()
        self.assertIn("generated_at", report)
        self.assertIn("review_gate", report)
        self.assertIn("canonical_gate", report)
        self.assertIn("strategies", report)
        self.assertIn("summary", report)
        # Review gate config should be 30 trades + PF 1.20
        self.assertEqual(report["review_gate"]["min_trades"], 30)
        self.assertAlmostEqual(report["review_gate"]["min_pf"], 1.20, places=2)
        # Canonical gate should be 60 trades + PF 1.30
        self.assertEqual(report["canonical_gate"]["min_trades"], 60)
        self.assertAlmostEqual(report["canonical_gate"]["min_pf"], 1.30, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
