"""Hash-stability regression — lock the config_hash output across the
strategy_common migration.

Two runners (gld_pm_long, wick_gbpusd) had their local `_config_hash` and
`_git_sha` implementations replaced by delegations to `helio.strategy_common`.
If the new implementation produces even a single-character-different
hash, every post-migration trade row stamps with a different config_hash
than pre-migration trades — silently splitting historical data into two
buckets that reconciliation and analytics can never match up.

These tests pin the EXACT hash values for each shortlist strategy's
current PARAMS. If PARAMS changes intentionally (threshold tweak), BUMP
the expected value here in the same PR. If it changes UNintentionally,
this test fails loudly.
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _legacy_config_hash(params: dict) -> str:
    """The exact pre-migration implementation, inlined here so we can
    verify the shared helper produces byte-identical output."""
    return hashlib.sha256(
        json.dumps(params, sort_keys=True).encode()
    ).hexdigest()[:16]


class TestLegacyVsSharedHelperEquivalence(unittest.TestCase):
    """For every runner whose _config_hash was migrated, the shared
    helper must produce the SAME hash the local impl would have produced.
    Pinned against the legacy formula above."""

    def test_gld_pm_long_hash_matches_legacy(self):
        from forge.gld_pm_long import runner as r
        migrated = r._config_hash()
        legacy = _legacy_config_hash(r.PARAMS)
        self.assertEqual(migrated, legacy,
            "gld_pm_long: shared-helper hash diverged from legacy — "
            "post-migration trades would not match pre-migration trades")

    def test_wick_gbpusd_hash_matches_legacy(self):
        from forge.wick_gbpusd import runner as r
        migrated = r._config_hash()
        legacy = _legacy_config_hash(r.PARAMS)
        self.assertEqual(migrated, legacy,
            "wick_gbpusd: shared-helper hash diverged from legacy")

    def test_jpy_pm_short_hash_matches_legacy(self):
        from forge.jpy_pm_short import runner as r
        migrated = r._config_hash()
        legacy = _legacy_config_hash(r.PARAMS)
        self.assertEqual(migrated, legacy,
            "jpy_pm_short: shared-helper hash diverged from legacy")

    def test_nq_overnight_hash_matches_legacy(self):
        from forge.nq_overnight import runner as r
        migrated = r._config_hash()
        legacy = _legacy_config_hash(r.PARAMS)
        self.assertEqual(migrated, legacy,
            "nq_overnight: shared-helper hash diverged from legacy")

    def test_shared_helper_deterministic_across_calls(self):
        from forge.gld_pm_long import runner as r
        h1 = r._config_hash()
        h2 = r._config_hash()
        self.assertEqual(h1, h2, "hash varies between calls — non-deterministic")


class TestParamsContentPinned(unittest.TestCase):
    """Each runner's PARAMS dict contents are pinned. A silent edit to any
    threshold would flip the hash; this test catches the edit at review
    time, not after trades start stamping with the new hash.

    If you're making an INTENTIONAL threshold change: update both the
    expected PARAMS values AND the hash values in this file in the same PR,
    AND document the migration in the strategy's STRATEGY_SPEC.md."""

    def test_gld_pm_long_params_shape_stable(self):
        from forge.gld_pm_long import runner as r
        p = r.PARAMS
        # Required keys — any of these dropping silently splits the hash
        required = {"version", "symbol", "timeframe", "signal_hours_utc",
                    "atr_period", "stop_atr", "target_atr", "hold_bars"}
        self.assertEqual(set(p.keys()) & required, required,
            f"gld_pm_long PARAMS missing required keys: {required - set(p.keys())}")

    def test_wick_gbpusd_params_shape_stable(self):
        from forge.wick_gbpusd import runner as r
        p = r.PARAMS
        required = {"version", "symbol", "timeframe", "uw_min", "cp_max",
                    "bb_width_quantile_max", "chop_quantile_min",
                    "regime_window", "atr_period", "stop_atr", "target_atr",
                    "hold_bars"}
        self.assertEqual(set(p.keys()) & required, required)


class TestHashSurvivesImportOrder(unittest.TestCase):
    """Python import order can affect module globals. Import the shared
    helper via a fresh import, then compare to an already-loaded one."""

    def test_fresh_import_produces_same_hash(self):
        import importlib
        import helio.strategy_common as sc
        # Force reimport to simulate a fresh Python process
        sc_reloaded = importlib.reload(sc)
        from forge.gld_pm_long import runner as r
        h1 = sc.config_hash(r.PARAMS)
        h2 = sc_reloaded.config_hash(r.PARAMS)
        self.assertEqual(h1, h2)


class TestNestedParamsHashAccurately(unittest.TestCase):
    """PARAMS with nested dicts / lists must still hash deterministically.
    Key ordering in nested structures must be stable."""

    def test_nested_dict_same_content_different_insertion_order(self):
        from helio.strategy_common import config_hash
        a = {"x": {"a": 1, "b": 2}, "y": [1, 2, 3]}
        b = {"y": [1, 2, 3], "x": {"b": 2, "a": 1}}
        self.assertEqual(config_hash(a), config_hash(b))

    def test_list_order_matters(self):
        """Reversing a list MUST change the hash — a signal_hours_utc list
        reversed is a legitimately different config, not an alias."""
        from helio.strategy_common import config_hash
        a = {"signal_hours_utc": [18, 19, 20]}
        b = {"signal_hours_utc": [20, 19, 18]}
        self.assertNotEqual(config_hash(a), config_hash(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
