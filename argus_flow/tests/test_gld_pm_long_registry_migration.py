"""Regression tests for the gld_pm_long -> registry migration (Phase 2).

forge/gld_pm_long/runner.py now loads PARAMS from config/strategies.json
via helio.strategy_registry.load_registry() with a fallback to
_HARDCODED_DEFAULTS if the registry fails. The Phase 1 equivalence suite
(test_registry_equivalence) already guarantees registry and defaults
match, so this migration is a no-op behavior change today.

These tests pin:
  1. PARAMS at import time equals the defaults (registry is available).
  2. Fallback kicks in when registry raises.
  3. Every key in _HARDCODED_DEFAULTS is present in PARAMS (no silent drop).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestRegistrySourcedParams(unittest.TestCase):
    def test_params_match_hardcoded_defaults_by_value(self):
        """Registry + defaults must agree — enforced by Phase 1 equivalence
        test. This test confirms the actual PARAMS value at import time
        sees the same numbers."""
        from forge.gld_pm_long import runner as r
        d = r._HARDCODED_DEFAULTS
        p = r.PARAMS
        self.assertEqual(p["symbol"], d["symbol"])
        self.assertEqual(p["timeframe"], d["timeframe"])
        self.assertEqual(sorted(p["signal_hours_utc"]), sorted(d["signal_hours_utc"]))
        self.assertEqual(p["atr_period"], d["atr_period"])
        self.assertEqual(p["stop_atr"], d["stop_atr"])
        self.assertEqual(p["target_atr"], d["target_atr"])
        self.assertEqual(p["hold_bars"], d["hold_bars"])

    def test_every_default_key_survives_in_params(self):
        """Field drop is the dangerous failure — the registry loader must
        populate every key the _HARDCODED_DEFAULTS advertises."""
        from forge.gld_pm_long import runner as r
        for key in r._HARDCODED_DEFAULTS:
            self.assertIn(key, r.PARAMS,
                f"_load_params_from_registry dropped key '{key}'")

    def test_fallback_used_when_registry_raises(self):
        """If load_registry throws, _load_params_from_registry must fall
        back to the hardcoded defaults and not raise."""
        from forge.gld_pm_long import runner as r
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken json")):
            result = r._load_params_from_registry()
        # All keys present, matching defaults
        for key, val in r._HARDCODED_DEFAULTS.items():
            self.assertEqual(result.get(key), val,
                f"fallback diverged on '{key}': got {result.get(key)!r}, "
                f"expected {val!r}")

    def test_fallback_result_is_mutation_safe(self):
        """The fallback should hand back a COPY of the defaults, not the
        dict object itself — otherwise downstream mutation would poison
        future fallback calls."""
        from forge.gld_pm_long import runner as r
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken")):
            result1 = r._load_params_from_registry()
            result2 = r._load_params_from_registry()
        self.assertIsNot(result1, r._HARDCODED_DEFAULTS,
            "fallback returned the defaults dict object — not mutation-safe")
        self.assertIsNot(result1, result2,
            "fallback returned the same object twice — not mutation-safe")

    def test_registry_path_returns_same_keys_as_fallback(self):
        """Both code paths (registry hit + fallback) must expose the same
        key set. Otherwise consumers that touch a specific key hit
        KeyError only when the registry is down."""
        from forge.gld_pm_long import runner as r
        registry_result = r._load_params_from_registry()
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("broken")):
            fallback_result = r._load_params_from_registry()
        self.assertEqual(set(registry_result.keys()), set(fallback_result.keys()))


class TestMigrationDoesNotBreakRunnerMath(unittest.TestCase):
    """The ATR/stop/target math downstream of PARAMS must still work."""

    def test_atr_function_still_callable(self):
        import pandas as pd
        from forge.gld_pm_long.runner import atr, PARAMS
        df = pd.DataFrame({
            "High":  [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
            "Low":   [ 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
            "Close": [ 9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5,
                     17.5, 18.5, 19.5, 20.5, 21.5, 22.5, 23.5],
        })
        a = atr(df, n=PARAMS["atr_period"])
        # Value at idx 13 should be defined (n=14 needs 14 bars)
        self.assertFalse(pd.isna(a.iloc[-1]))

    def test_signal_hours_still_the_expected_three(self):
        from forge.gld_pm_long.runner import PARAMS
        self.assertEqual(sorted(PARAMS["signal_hours_utc"]), [18, 19, 20])


if __name__ == "__main__":
    unittest.main(verbosity=2)
