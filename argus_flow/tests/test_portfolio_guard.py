"""Unit tests for helio.portfolio_guard — money-critical ceilings.

portfolio_guard gates every new entry across the fleet. A bug here means
either silent over-exposure (budget-breaking) or silent over-blocking
(missed trades). These tests pin the four ceilings at exact boundaries
plus the correlated-instrument and opposite-direction rules.

Ceilings (from DEFAULT_* constants):
  - MAX_TOTAL_POSITIONS      = 6
  - MAX_PER_FAMILY           = 3
  - MAX_DIRECTIONAL_BIAS     = 4
  - MAX_CORRELATED_PAIRS     = 2
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import portfolio_guard as pg  # noqa: E402


def _mk(family: str, symbol: str, direction: str = "LONG") -> pg.OpenPosition:
    return pg.OpenPosition(
        family=family,
        symbol=symbol,
        symbol_norm=pg._normalise_symbol(symbol),
        direction=direction,
        entry_price=0.0,
        state_path="<test>",
    )


class TestSymbolNormalisation(unittest.TestCase):
    """Symbol normalization drives correlation detection. Drift here means
    GOLD_F and xauusd no longer match as the same instrument."""

    def test_gold_variants_normalise_identically(self):
        self.assertEqual(pg._normalise_symbol("XAU_USD"), "xauusd")
        self.assertEqual(pg._normalise_symbol("xau/usd"), "xauusd")
        self.assertEqual(pg._normalise_symbol("gold_f"), "xauusd")
        self.assertEqual(pg._normalise_symbol("XAUUSD"), "xauusd")

    def test_fx_pairs_normalise_consistently(self):
        self.assertEqual(pg._normalise_symbol("EUR/USD"), "eurusd")
        self.assertEqual(pg._normalise_symbol("eur_usd"), "eurusd")
        self.assertEqual(pg._normalise_symbol("eurusd"), "eurusd")

    def test_unknown_symbol_strips_separators(self):
        self.assertEqual(pg._normalise_symbol("BTC-USD"), "btcusd")


class TestDirectionNormalisation(unittest.TestCase):
    def test_buy_maps_to_long(self):
        self.assertEqual(pg._normalise_direction("BUY"), "LONG")
        self.assertEqual(pg._normalise_direction("buy"), "LONG")

    def test_sell_maps_to_short(self):
        self.assertEqual(pg._normalise_direction("SELL"), "SHORT")

    def test_unknown_falls_through_to_default(self):
        self.assertEqual(pg._normalise_direction("???", default="LONG"), "LONG")

    def test_none_falls_through_to_default(self):
        self.assertEqual(pg._normalise_direction(None), "LONG")


class TestComputeMetrics(unittest.TestCase):
    def test_empty_portfolio_is_zero(self):
        m = pg.compute_metrics([])
        self.assertEqual(m["total_open_positions"], 0)
        self.assertEqual(m["directional_bias"], 0)
        self.assertEqual(m["correlated_pair_count"], 0)

    def test_directional_bias_is_longs_minus_shorts(self):
        positions = [
            _mk("argus_fx", "usdjpy", "LONG"),
            _mk("argus_fx", "gbpusd", "LONG"),
            _mk("helio_swing", "gld", "LONG"),
            _mk("apollo", "aapl", "SHORT"),
        ]
        m = pg.compute_metrics(positions)
        self.assertEqual(m["longs"], 3)
        self.assertEqual(m["shorts"], 1)
        self.assertEqual(m["directional_bias"], 2)

    def test_correlated_pair_detected_across_families(self):
        positions = [
            _mk("helio_swing", "gld"),
            _mk("titan", "gld"),         # same instrument different family
        ]
        m = pg.compute_metrics(positions)
        self.assertEqual(m["correlated_pair_count"], 1)
        self.assertIn("gld", m["correlated_exposure"])

    def test_correlated_pair_uses_normalised_symbol(self):
        """GOLD_F from Hermes and XAUUSD from Apollo collide after norm."""
        positions = [
            _mk("hermes", "GOLD_F"),
            _mk("apollo", "xauusd"),
        ]
        m = pg.compute_metrics(positions)
        self.assertEqual(m["correlated_pair_count"], 1)
        self.assertIn("xauusd", m["correlated_exposure"])

    def test_single_family_concentration_is_one(self):
        positions = [_mk("argus_fx", s) for s in ["usdjpy", "gbpusd", "cadjpy"]]
        m = pg.compute_metrics(positions)
        self.assertAlmostEqual(m["concentration"], 1.0, places=3)


class TestCheckLimitsCeilings(unittest.TestCase):
    """Hard-ceiling boundaries. Each must fire AT the limit, not one trade after."""

    def test_total_ceiling_fires_at_six(self):
        m = {
            "total_open_positions": 6,
            "positions_by_family": {"argus_fx": 2, "helio_swing": 2, "apollo": 2},
            "directional_bias": 0, "concentration": 0.33,
            "correlated_pair_count": 0, "correlated_exposure": {},
            "longs": 3, "shorts": 3, "positions_detail": [],
        }
        ok, reason, _ = pg.check_limits(m)
        self.assertFalse(ok)
        self.assertIn("max_total_positions", reason)

    def test_total_at_five_is_allowed_with_near_warning(self):
        m = {
            "total_open_positions": 5,
            "positions_by_family": {"argus_fx": 2, "helio_swing": 2, "apollo": 1},
            "directional_bias": 1, "concentration": 0.4,
            "correlated_pair_count": 0, "correlated_exposure": {},
            "longs": 3, "shorts": 2, "positions_detail": [],
        }
        ok, reason, warnings = pg.check_limits(m)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertTrue(any("near max_total_positions" in w for w in warnings))

    def test_per_family_ceiling_fires_at_three(self):
        m = {
            "total_open_positions": 3,
            "positions_by_family": {"argus_fx": 3},
            "directional_bias": 3, "concentration": 1.0,
            "correlated_pair_count": 0, "correlated_exposure": {},
            "longs": 3, "shorts": 0, "positions_detail": [],
        }
        ok, reason, _ = pg.check_limits(m)
        self.assertFalse(ok)
        self.assertIn("max_per_family", reason)

    def test_directional_bias_ceiling_fires_at_four_long(self):
        m = {
            "total_open_positions": 4,
            "positions_by_family": {"a": 1, "b": 1, "c": 1, "d": 1},
            "directional_bias": 4, "concentration": 0.25,
            "correlated_pair_count": 0, "correlated_exposure": {},
            "longs": 4, "shorts": 0, "positions_detail": [],
        }
        ok, reason, _ = pg.check_limits(m)
        self.assertFalse(ok)
        self.assertIn("max_directional_bias", reason)
        self.assertIn("LONG", reason)

    def test_directional_bias_ceiling_fires_at_four_short(self):
        m = {
            "total_open_positions": 4,
            "positions_by_family": {"a": 1, "b": 1, "c": 1, "d": 1},
            "directional_bias": -4, "concentration": 0.25,
            "correlated_pair_count": 0, "correlated_exposure": {},
            "longs": 0, "shorts": 4, "positions_detail": [],
        }
        ok, reason, _ = pg.check_limits(m)
        self.assertFalse(ok)
        self.assertIn("SHORT", reason)

    def test_correlated_pair_ceiling_fires_at_two(self):
        m = {
            "total_open_positions": 4,
            "positions_by_family": {"a": 2, "b": 2},
            "directional_bias": 0, "concentration": 0.5,
            "correlated_pair_count": 2,
            "correlated_exposure": {"gld": ["a", "b"], "spy": ["a", "b"]},
            "longs": 2, "shorts": 2, "positions_detail": [],
        }
        ok, reason, _ = pg.check_limits(m)
        self.assertFalse(ok)
        self.assertIn("max_correlated_pairs", reason)


class TestCheckNewEntry(unittest.TestCase):
    """check_new_entry is the public API — exercise the full path."""

    def test_opposite_direction_same_instrument_blocks(self):
        """If apollo is LONG gld, hermes cannot open SHORT gld — paying spread
        for zero net exposure."""
        existing = [_mk("apollo", "gld", "LONG")]
        with mock.patch.object(pg, "scan_all_positions", return_value=existing), \
             mock.patch.object(pg, "_write_result"):
            result = pg.check_new_entry("hermes", "gld", "SHORT")
        self.assertFalse(result.allowed)
        self.assertIn("opposite_direction_conflict", result.reason)

    def test_same_direction_same_instrument_is_only_blocked_by_correlation(self):
        """LONG gld in two families is permitted up to the correlated-pair
        ceiling — not a direction conflict."""
        existing = [_mk("apollo", "gld", "LONG")]
        with mock.patch.object(pg, "scan_all_positions", return_value=existing), \
             mock.patch.object(pg, "_write_result"):
            result = pg.check_new_entry("hermes", "gld", "LONG")
        self.assertTrue(result.allowed, f"blocked: {result.reason}")

    def test_empty_portfolio_allows_entry(self):
        with mock.patch.object(pg, "scan_all_positions", return_value=[]), \
             mock.patch.object(pg, "_write_result"):
            result = pg.check_new_entry("argus_fx", "usdjpy", "LONG")
        self.assertTrue(result.allowed)

    def test_sixth_entry_blocks_on_total_ceiling(self):
        existing = [_mk(f"fam_{i}", f"sym_{i}", "LONG") for i in range(6)]
        with mock.patch.object(pg, "scan_all_positions", return_value=existing), \
             mock.patch.object(pg, "_write_result"):
            # Proposed would make 7 but ceiling is 6
            result = pg.check_new_entry("argus_fx", "newone", "LONG")
        self.assertFalse(result.allowed)
        self.assertIn("max_total_positions", result.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
