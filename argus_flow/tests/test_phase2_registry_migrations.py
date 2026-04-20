"""Regression tests for Phase 2 registry-consumption migrations.

gld_pm_long migrated first (tested in test_gld_pm_long_registry_migration).
This file covers the second wave:

  - forge/wick_gbpusd: PARAMS reads from registry
  - apollo/execution/planned_trades: SCORE_FLOOR + HORIZON + MAX_PLANNED
    read from registry
  - forge/gdx_gld_runner: INTENTIONAL DIVERGENCE pinned (runner uses 3.5,
    backtest uses 3.0 per live sensitivity analysis). If either drifts,
    this test catches it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestWickGbpusdMigration(unittest.TestCase):
    def test_params_equal_hardcoded_defaults(self):
        from forge.wick_gbpusd import runner as r
        for key, expected in r._HARDCODED_DEFAULTS.items():
            self.assertEqual(r.PARAMS[key], expected,
                f"wick_gbpusd PARAMS['{key}'] diverged from defaults")

    def test_fallback_on_registry_failure(self):
        from forge.wick_gbpusd import runner as r
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken registry")):
            result = r._load_params_from_registry()
        for key, val in r._HARDCODED_DEFAULTS.items():
            self.assertEqual(result.get(key), val)

    def test_fallback_is_mutation_safe(self):
        from forge.wick_gbpusd import runner as r
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken")):
            result1 = r._load_params_from_registry()
            result2 = r._load_params_from_registry()
        self.assertIsNot(result1, r._HARDCODED_DEFAULTS)
        self.assertIsNot(result1, result2)

    def test_all_default_keys_survive(self):
        """No silent drop — every hardcoded key must appear in PARAMS."""
        from forge.wick_gbpusd import runner as r
        for key in r._HARDCODED_DEFAULTS:
            self.assertIn(key, r.PARAMS, f"lost key '{key}' in registry path")

    def test_signal_math_still_functions(self):
        """Post-migration, the signal-generation path must still work —
        quick smoke with synthetic features."""
        import pandas as pd
        from forge.wick_gbpusd.runner import signal_long
        df = pd.DataFrame({
            "Open": [1.0] * 100, "High": [1.0] * 100,
            "Low": [1.0] * 100, "Close": [1.0] * 100,
        })
        feats = pd.DataFrame({
            "upper_wick_pct":     [0.80] * 100,
            "close_pos_in_range": [0.10] * 100,
            "bb_width_pct":       [0.05] * 100,
            "bb_width_q33":       [0.10] * 100,
            "choppiness_14":      [70.0] * 100,
            "chop_q67":           [60.0] * 100,
            "atr":                [0.001] * 100,
        })
        sig = signal_long(df, feats)
        self.assertTrue(bool(sig.iloc[-1]), "signal_long broken after migration")


class TestApolloScoreFloorMigration(unittest.TestCase):
    def test_score_floor_is_75(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.SCORE_FLOOR, 75)

    def test_horizon_trading_days_is_3(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.HORIZON_TRADING_DAYS, 3)

    def test_max_planned_is_1(self):
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt.MAX_PLANNED_PER_SYMBOL_PER_EARNINGS, 1)

    def test_hardcoded_defaults_match_module_constants(self):
        """Registry failure fallback must match the constants exposed."""
        from apollo.execution import planned_trades as pt
        self.assertEqual(pt._HARDCODED_DEFAULTS["SCORE_FLOOR"], pt.SCORE_FLOOR)
        self.assertEqual(pt._HARDCODED_DEFAULTS["HORIZON_TRADING_DAYS"],
                         pt.HORIZON_TRADING_DAYS)
        self.assertEqual(pt._HARDCODED_DEFAULTS["MAX_PLANNED_PER_SYMBOL_PER_EARNINGS"],
                         pt.MAX_PLANNED_PER_SYMBOL_PER_EARNINGS)

    def test_fallback_on_registry_failure(self):
        """If registry is unreadable, _load_from_registry returns the
        hardcoded defaults."""
        from apollo.execution import planned_trades as pt
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken")):
            result = pt._load_from_registry()
        self.assertEqual(result, pt._HARDCODED_DEFAULTS)


class TestGdxGldDivergencePin(unittest.TestCase):
    """INTENTIONAL DIVERGENCE: gdx_gld_runner.ZSCORE_STOP = 3.5 (live,
    tuned) while the backtest still uses 3.0 (original research value).

    Now that the runner reads from the registry (via forge_gdx_gld_live),
    the divergence is explicit in config/strategies.json — the backtest
    entry (forge_gdx_gld) stays at 3.0, and the live-runner entry
    (forge_gdx_gld_live) carries the 3.5 override with a _divergence_note.

    If alignment is ever desired:
      1. Decide which value is right (based on post-live data)
      2. Update BOTH forge_gdx_gld and forge_gdx_gld_live in strategies.json
      3. Update this test
    """

    def test_runner_uses_widened_stop(self):
        import forge.gdx_gld_runner as gr
        self.assertEqual(gr.ZSCORE_STOP, 3.5,
            "gdx_gld_runner.ZSCORE_STOP changed — update this test AND "
            "config/strategies.json forge_gdx_gld_live if intentional")

    def test_backtest_module_uses_original_stop(self):
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.ZSCORE_STOP, 3.0,
            "gdx_gld_pairs backtest ZSCORE_STOP changed — confirm whether "
            "this should also update the live runner")

    def test_registry_backtest_entry_uses_3_point_0(self):
        """forge_gdx_gld (backtest) registry entry mirrors gdx_gld_pairs.py."""
        from helio.strategy_registry import load_registry
        reg = load_registry().gdx_gld
        self.assertEqual(reg.zscore_stop, 3.0)

    def test_registry_live_entry_uses_3_point_5(self):
        """forge_gdx_gld_live registry entry mirrors the live runner's 3.5."""
        from helio.strategy_registry import load_registry
        reg = load_registry().gdx_gld_live
        self.assertEqual(reg.zscore_stop, 3.5)

    def test_divergence_note_documented_in_registry(self):
        """The _divergence_note key in strategies.json must document WHY
        the two entries differ — drop it and future maintainers lose context."""
        import json
        path = _REPO / "config" / "strategies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        live = raw["strategies"]["forge_gdx_gld_live"]
        self.assertIn("_divergence_note", live)
        self.assertIn("3.5", live["_divergence_note"])
        self.assertIn("3.0", live["_divergence_note"])

    def test_divergence_note_still_in_source(self):
        """The sensitivity-analysis rationale must remain in the source —
        if someone edits it away, future maintainers lose the context for
        why the two values differ."""
        src = (_REPO / "forge" / "gdx_gld_runner.py").read_text(encoding="utf-8")
        self.assertIn("sensitivity analysis", src.lower())


class TestAllFourShortlistStrategiesCovered(unittest.TestCase):
    """After this batch, 3 of 4 shortlist strategies have registry
    consumption (gld_pm_long + wick_gbpusd + apollo). gdx_gld has pinned
    divergence. Document the current migration state so future readers can
    see what's migrated vs still hardcoded-only."""

    def test_gld_pm_long_uses_registry(self):
        from forge.gld_pm_long import runner as r
        self.assertTrue(hasattr(r, "_load_params_from_registry"))
        self.assertTrue(hasattr(r, "_HARDCODED_DEFAULTS"))

    def test_wick_gbpusd_uses_registry(self):
        from forge.wick_gbpusd import runner as r
        self.assertTrue(hasattr(r, "_load_params_from_registry"))
        self.assertTrue(hasattr(r, "_HARDCODED_DEFAULTS"))

    def test_apollo_planned_trades_uses_registry(self):
        from apollo.execution import planned_trades as pt
        self.assertTrue(hasattr(pt, "_load_from_registry"))
        self.assertTrue(hasattr(pt, "_HARDCODED_DEFAULTS"))

    def test_gdx_gld_uses_registry_via_live_entry(self):
        """gdx_gld_runner now reads z-score constants from
        forge_gdx_gld_live (separate registry entry from the backtest
        forge_gdx_gld because of the 3.5 vs 3.0 divergence)."""
        import forge.gdx_gld_runner as gr
        self.assertTrue(hasattr(gr, "_load_constants_from_registry"))
        self.assertTrue(hasattr(gr, "_HARDCODED_DEFAULTS"))
        # Live value preserved
        self.assertEqual(gr.ZSCORE_STOP, 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
