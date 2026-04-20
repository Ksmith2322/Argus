"""Equivalence tests — registry MIRROR must match runtime constants.

Phase 1 of the restructure (per RESTRUCTURE_GUIDE_20260419.md) adds
config/strategies.json as a MIRROR of the edge thresholds inside each
strategy runner. Runners still own the authoritative values; the mirror
just gives us one file to diff when a knob changes.

These tests fail loudly if the mirror drifts from runtime. Fix the mirror
(or the runtime) until both agree — then both get updated together.

When these pass, Phase 2 can safely flip runners to load from the registry
instead of their in-module PARAMS dicts.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio.strategy_registry import load_registry  # noqa: E402


class TestPromotionGateMirror(unittest.TestCase):
    """Registry's promotion gates must equal helio.promotion_readiness constants."""

    def test_review_gate_mirrors_runtime(self):
        from helio import promotion_readiness as pr
        reg = load_registry()
        self.assertEqual(reg.promotion.review_gate.min_trades,
                         pr.REVIEW_GATE["min_trades"])
        self.assertAlmostEqual(reg.promotion.review_gate.min_pf,
                                pr.REVIEW_GATE["min_pf"], places=4)
        self.assertAlmostEqual(reg.promotion.review_gate.min_expectancy,
                                pr.REVIEW_GATE["min_expectancy"], places=4)

    def test_canonical_gate_mirrors_runtime(self):
        from helio import promotion_readiness as pr
        reg = load_registry()
        self.assertEqual(reg.promotion.canonical_gate.min_trades,
                         pr.CANONICAL_GATE["min_trades"])
        self.assertAlmostEqual(reg.promotion.canonical_gate.min_pf,
                                pr.CANONICAL_GATE["min_pf"], places=4)


class TestGldPmLongMirror(unittest.TestCase):
    """Registry's forge_gld_pm_long must equal runner.PARAMS."""

    def test_all_params_mirror_runtime(self):
        from forge.gld_pm_long import runner as r
        reg = load_registry().gld_pm_long
        self.assertEqual(reg.symbol, r.PARAMS["symbol"])
        self.assertEqual(reg.timeframe, r.PARAMS["timeframe"])
        self.assertEqual(sorted(reg.signal_hours_utc),
                         sorted(r.PARAMS["signal_hours_utc"]))
        self.assertEqual(reg.atr_period, r.PARAMS["atr_period"])
        self.assertEqual(reg.stop_atr, r.PARAMS["stop_atr"])
        self.assertEqual(reg.target_atr, r.PARAMS["target_atr"])
        self.assertEqual(reg.hold_bars, r.PARAMS["hold_bars"])


class TestWickGbpusdMirror(unittest.TestCase):
    """Registry's forge_wick_gbpusd must equal runner.PARAMS."""

    def test_all_params_mirror_runtime(self):
        from forge.wick_gbpusd import runner as r
        reg = load_registry().wick_gbpusd
        self.assertEqual(reg.symbol, r.PARAMS["symbol"])
        self.assertEqual(reg.timeframe, r.PARAMS["timeframe"])
        self.assertEqual(reg.uw_min, r.PARAMS["uw_min"])
        self.assertEqual(reg.cp_max, r.PARAMS["cp_max"])
        self.assertEqual(reg.bb_width_quantile_max, r.PARAMS["bb_width_quantile_max"])
        self.assertEqual(reg.chop_quantile_min, r.PARAMS["chop_quantile_min"])
        self.assertEqual(reg.regime_window, r.PARAMS["regime_window"])
        self.assertEqual(reg.atr_period, r.PARAMS["atr_period"])
        self.assertEqual(reg.stop_atr, r.PARAMS["stop_atr"])
        self.assertEqual(reg.target_atr, r.PARAMS["target_atr"])
        self.assertEqual(reg.hold_bars, r.PARAMS["hold_bars"])


class TestGdxGldMirror(unittest.TestCase):
    """Registry's forge_gdx_gld must equal module constants in gdx_gld_pairs."""

    def test_zscore_params_mirror_runtime(self):
        from forge import gdx_gld_pairs as g
        reg = load_registry().gdx_gld
        self.assertEqual(reg.zscore_entry, g.ZSCORE_ENTRY)
        self.assertEqual(reg.zscore_exit, g.ZSCORE_EXIT)
        self.assertEqual(reg.zscore_stop, g.ZSCORE_STOP)
        self.assertEqual(reg.zscore_lookback, g.ZSCORE_LOOKBACK)

    def test_window_params_mirror_runtime(self):
        from forge import gdx_gld_pairs as g
        reg = load_registry().gdx_gld
        self.assertEqual(reg.coint_window, g.COINT_WINDOW)
        self.assertEqual(reg.cost_per_side_bps, g.COST_PER_SIDE_BPS)


class TestApolloEarningsMirror(unittest.TestCase):
    """Registry's apollo_earnings_drift must equal planned_trades constants."""

    def test_score_floor_mirrors_runtime(self):
        from apollo.execution import planned_trades as pt
        reg = load_registry().apollo_earnings_drift
        self.assertEqual(reg.score_floor, pt.SCORE_FLOOR)

    def test_horizon_mirrors_runtime(self):
        from apollo.execution import planned_trades as pt
        reg = load_registry().apollo_earnings_drift
        self.assertEqual(reg.horizon_trading_days, pt.HORIZON_TRADING_DAYS)

    def test_dedup_cap_mirrors_runtime(self):
        from apollo.execution import planned_trades as pt
        reg = load_registry().apollo_earnings_drift
        self.assertEqual(reg.max_planned_per_symbol_per_earnings,
                         pt.MAX_PLANNED_PER_SYMBOL_PER_EARNINGS)

    def test_mode_default_mirrors_runtime(self):
        from apollo.execution import planned_trades as pt
        reg = load_registry().apollo_earnings_drift
        self.assertEqual(reg.mode_default, pt.MODE)


class TestShortlistMirror(unittest.TestCase):
    """Registry's shortlist must match promotion_readiness.SHORTLIST
    for the research-card-backed candidates."""

    def test_shortlist_contains_expected_labels(self):
        from helio.strategy_registry import shortlist_labels
        labels = shortlist_labels()
        for expected in ["forge_gdx_gld", "forge_gld_pm_long",
                         "forge_wick_gbpusd", "apollo_earnings_drift"]:
            self.assertIn(expected, labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
