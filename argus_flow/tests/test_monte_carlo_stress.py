"""Tests for the Monte Carlo trade-order shuffle stress test.

This is discovery-test #8 from project_discovery_tests_20260419.md —
answers "is the headline PF driven by 1-3 outlier trades or broad
profitability?" by shuffling trade order 5000 times and measuring
drawdown tails + outlier concentration.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestMonteCarloShuffle(unittest.TestCase):
    def test_returns_none_under_sanity_bar(self):
        from helio.fleet_state import _monte_carlo_shuffle
        self.assertIsNone(_monte_carlo_shuffle([10.0] * 5))

    def test_deterministic_same_input_same_output(self):
        from helio.fleet_state import _monte_carlo_shuffle
        pnls = [100.0, -50.0, 80.0, -30.0, 60.0, -40.0, 90.0, -20.0, 70.0, -10.0,
                120.0, -60.0]
        a = _monte_carlo_shuffle(list(pnls))
        b = _monte_carlo_shuffle(list(pnls))
        self.assertEqual(a, b)

    def test_all_winners_gives_100pct_profitable_zero_ruin(self):
        """With every shuffle producing positive cumulative PnL, profitable
        fraction must be 1.0 and ruin fraction must be 0.0."""
        from helio.fleet_state import _monte_carlo_shuffle
        result = _monte_carlo_shuffle([10.0] * 20)
        self.assertEqual(result["pct_shuffles_profitable"], 1.0)
        self.assertEqual(result["ruin_fraction"], 0.0)

    def test_all_losers_gives_0pct_profitable(self):
        from helio.fleet_state import _monte_carlo_shuffle
        result = _monte_carlo_shuffle([-10.0] * 20)
        self.assertEqual(result["pct_shuffles_profitable"], 0.0)

    def test_outlier_concentration_flags_single_trade_dominance(self):
        """A strategy where one trade carries most of the PnL should show
        high top1_pct_of_total_pnl — a red flag for outlier dependency."""
        from helio.fleet_state import _monte_carlo_shuffle
        # 14 small losers + 1 huge winner = outlier-dominant
        pnls = [-10.0] * 14 + [200.0]
        result = _monte_carlo_shuffle(pnls)
        # Total PnL = +60. One trade contributes +200. That's >100% of total.
        self.assertGreater(result["top1_pct_of_total_pnl"], 1.0)

    def test_ex_top1_pnl_subtracts_best_trade(self):
        from helio.fleet_state import _monte_carlo_shuffle
        pnls = [10.0] * 10 + [500.0]  # best trade +500
        result = _monte_carlo_shuffle(pnls)
        # Total = 600, ex-top1 = 100
        self.assertAlmostEqual(result["ex_top1_total_pnl_usd"], 100.0, places=2)

    def test_shuffle_count_configured(self):
        from helio.fleet_state import _monte_carlo_shuffle, _MC_SHUFFLES
        result = _monte_carlo_shuffle([10.0] * 15)
        self.assertEqual(result["shuffles"], _MC_SHUFFLES)

    def test_ruin_fraction_between_0_and_1(self):
        from helio.fleet_state import _monte_carlo_shuffle
        # Mixed sample
        pnls = [10.0, -15.0] * 10
        result = _monte_carlo_shuffle(pnls)
        self.assertGreaterEqual(result["ruin_fraction"], 0.0)
        self.assertLessEqual(result["ruin_fraction"], 1.0)


class TestArtifactsCarryMcStress(unittest.TestCase):
    """All three writers now include mc_stress when n ≥ 10."""

    def test_real_tori_artifact_has_mc_stress(self):
        import forge.tori.confidence_writer as tw
        data = tw.build_tori_artifact()
        if data["n_total"] >= 10:
            self.assertIn("mc_stress", data)
            mc = data["mc_stress"]
            self.assertIn("max_drawdown_usd", mc)
            self.assertIn("top1_pct_of_total_pnl", mc)

    def test_real_mamba_artifact_has_mc_stress(self):
        import forge.mamba.confidence_writer as mw
        data = mw.build_mamba_artifact()
        if data["n_total"] >= 10:
            self.assertIn("mc_stress", data)

    def test_real_cuebanks_artifact_has_mc_stress(self):
        import forge.cuebanks.confidence_writer as cw
        data = cw.build_cuebanks_artifact()
        if data["n_total"] >= 10:
            self.assertIn("mc_stress", data)

    def test_mc_stress_passes_schema_validation(self):
        """Extended schema with mc_stress must accept writer output."""
        import forge.tori.confidence_writer as tw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        data = tw.build_tori_artifact()
        # Should not raise
        StrategyConfidenceArtifact.model_validate(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
