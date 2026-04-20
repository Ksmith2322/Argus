"""Tests for the full stress battery introduced 2026-04-19 for Tori discovery:

  - _walk_forward_stability: sequential-fold PF stability
  - _cost_stress: 1x/2x/3x fee+slippage survivability
  - _top_n_sensitivity: outlier-dependency fragility
  - _per_group_profitability: per-instrument/hour/day gate
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestWalkForwardStability(unittest.TestCase):
    def test_returns_none_under_sanity_bar(self):
        from helio.fleet_state import _walk_forward_stability
        self.assertIsNone(_walk_forward_stability([1.0] * 8))

    def test_all_positive_folds_reports_stability_1(self):
        from helio.fleet_state import _walk_forward_stability
        result = _walk_forward_stability([10.0] * 40, n_folds=4)
        self.assertEqual(result["n_folds"], 4)
        self.assertEqual(result["positive_folds"], 4)
        self.assertEqual(result["stability_score"], 1.0)
        self.assertTrue(result["all_folds_positive"])

    def test_mixed_folds_reports_partial_stability(self):
        from helio.fleet_state import _walk_forward_stability
        # First half winners, second half losers
        pnls = [10.0] * 20 + [-5.0] * 20
        result = _walk_forward_stability(pnls, n_folds=4)
        # Exact count depends on fold alignment but stability should be < 1
        self.assertLess(result["stability_score"], 1.0)

    def test_fold_details_shape(self):
        from helio.fleet_state import _walk_forward_stability
        pnls = [1.0 if i % 2 == 0 else -0.5 for i in range(40)]
        result = _walk_forward_stability(pnls, n_folds=4)
        self.assertEqual(len(result["fold_details"]), 4)
        for f in result["fold_details"]:
            self.assertIn("fold_index", f)
            self.assertIn("n_trades", f)
            self.assertIn("total_pnl_usd", f)
            self.assertIn("profit_factor", f)


class TestCostStress(unittest.TestCase):
    def test_returns_none_under_sanity_bar(self):
        from helio.fleet_state import _cost_stress
        self.assertIsNone(_cost_stress([100.0] * 5))

    def test_survives_2x_when_edge_is_large(self):
        from helio.fleet_state import _cost_stress
        # 50R winner vs 25R loser, 10 of each = strong edge
        pnls = [500.0] * 10 + [-250.0] * 10
        result = _cost_stress(pnls, cost_per_trade_usd=5.0)
        self.assertTrue(result["survives_2x"])
        self.assertTrue(result["survives_3x"])

    def test_fails_2x_when_edge_is_marginal(self):
        from helio.fleet_state import _cost_stress
        # Marginal PF ~1.05 at $10/trade — a $5/trade × 2 cost of $10 wipes it
        pnls = [10.0] * 11 + [-10.0] * 10  # 11 wins, 10 losses, tiny edge
        result = _cost_stress(pnls, cost_per_trade_usd=5.0)
        self.assertFalse(result["survives_2x"])


class TestTopNSensitivity(unittest.TestCase):
    def test_returns_none_when_max_n_too_close_to_total(self):
        from helio.fleet_state import _top_n_sensitivity
        # Only 12 trades, can't remove top 5 and still have meaningful sample
        self.assertIsNone(_top_n_sensitivity([1.0] * 12, max_n=8))

    def test_distributed_edge_survives_top_removal(self):
        from helio.fleet_state import _top_n_sensitivity
        # 30 winners around $10, 20 losers around -$5 = distributed
        pnls = [10.0] * 30 + [-5.0] * 20
        result = _top_n_sensitivity(pnls, max_n=3)
        self.assertTrue(result["still_positive_after_top3_removed"])

    def test_outlier_dependent_strategy_flagged(self):
        from helio.fleet_state import _top_n_sensitivity
        # 14 tiny losers + 1 big winner = outlier dependent
        pnls = [-5.0] * 14 + [500.0]
        result = _top_n_sensitivity(pnls, max_n=3)
        # Removing the single top trade kills positive expectancy
        self.assertFalse(result["still_positive_after_top3_removed"])

    def test_sensitivity_list_has_monotonically_fewer_trades(self):
        from helio.fleet_state import _top_n_sensitivity
        pnls = [10.0] * 30 + [-5.0] * 20
        result = _top_n_sensitivity(pnls, max_n=3)
        counts = [s["n_trades"] for s in result["sensitivity"]]
        # Counts must be strictly decreasing
        self.assertEqual(counts, sorted(counts, reverse=True))


class TestPerGroupProfitability(unittest.TestCase):
    def test_groups_correctly_by_key(self):
        from helio.fleet_state import _per_group_profitability
        rows = [
            {"name": "Dow", "pnl_usd": 100},
            {"name": "Dow", "pnl_usd": -50},
            {"name": "Gold", "pnl_usd": 200},
        ]
        result = _per_group_profitability(rows, group_key="name")
        names = {b["group"] for b in result["buckets"]}
        self.assertEqual(names, {"Dow", "Gold"})
        self.assertEqual(result["n_groups"], 2)

    def test_all_positive_flag(self):
        from helio.fleet_state import _per_group_profitability
        rows = [
            {"name": "A", "pnl_usd": 100},
            {"name": "B", "pnl_usd": 50},
        ]
        result = _per_group_profitability(rows, group_key="name")
        self.assertTrue(result["all_positive"])

    def test_mixed_groups_flag(self):
        from helio.fleet_state import _per_group_profitability
        rows = [
            {"name": "A", "pnl_usd": 100},
            {"name": "B", "pnl_usd": -50},
        ]
        result = _per_group_profitability(rows, group_key="name")
        self.assertFalse(result["all_positive"])
        self.assertEqual(result["positive_groups"], 1)

    def test_empty_rows_returns_none(self):
        from helio.fleet_state import _per_group_profitability
        self.assertIsNone(_per_group_profitability([], group_key="name"))


class TestStressBatteryEndToEnd(unittest.TestCase):
    """The real Tori artifact should carry all four stress fields plus the
    existing mc_stress, validating end-to-end via the pydantic schema."""

    def test_tori_artifact_carries_full_battery(self):
        import forge.tori.confidence_writer as tw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        data = tw.build_tori_artifact()
        # Must pass schema with new fields
        StrategyConfidenceArtifact.model_validate(data)
        # All four stress fields populated (current canonical has n=665)
        if data["n_total"] >= 40:
            self.assertIn("walk_forward", data)
            self.assertIn("cost_stress", data)
            self.assertIn("top_n_sensitivity", data)
            self.assertIn("per_instrument", data)
            self.assertIn("per_day_of_week", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
