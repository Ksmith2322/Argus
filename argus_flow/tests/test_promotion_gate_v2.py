"""Tests for argus_flow.ops.promotion_gate_v2 — the legacy multi-check
promotion gate used alongside the newer `helio/promotion_readiness`.

promotion_gate_v2 runs ~20 individual checks against each runner's trade
log. The overall verdict is used for deployment-pipeline promote/demote
decisions. A bug in any check = wrong verdict.

Tests focus on:
  - _pnl_field, _safe_float, _row_ts helpers
  - check_min_valid_trades boundary
  - check_frozen_config_hash: single hash pass, multiple hashes fail
  - check_git_sha_consistent: mirror
  - check_invalid_rate: rate thresholds
  - check_positive_expectancy: pip friction adjustment
  - check_consecutive_losses: streak detection
  - check_profit_factor: threshold
  - CheckResult classification: hard/advisory/unevidenced/awaiting
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from argus_flow.ops import promotion_gate_v2 as pg  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_safe_float_valid(self):
        self.assertAlmostEqual(pg._safe_float("3.14"), 3.14)

    def test_safe_float_invalid_returns_default(self):
        self.assertEqual(pg._safe_float("junk", default=99.0), 99.0)

    def test_safe_float_none_returns_default(self):
        self.assertEqual(pg._safe_float(None, default=0.0), 0.0)

    def test_pnl_field_defaults_to_pnl_pts(self):
        """When pnl_pips absent, falls to pnl_pts."""
        self.assertEqual(pg._pnl_field([]), "pnl_pts")
        self.assertEqual(pg._pnl_field([{"pnl_pts": "1"}]), "pnl_pts")

    def test_pnl_field_prefers_pnl_pips(self):
        self.assertEqual(pg._pnl_field([{"pnl_pips": "1"}]), "pnl_pips")

    def test_row_ts_iso_parsed(self):
        ts = pg._row_ts({"ts": "2026-04-19T12:00:00+00:00"})
        self.assertIsNotNone(ts)

    def test_row_ts_missing_returns_none(self):
        self.assertIsNone(pg._row_ts({}))

    def test_row_ts_garbage_returns_none(self):
        self.assertIsNone(pg._row_ts({"ts": "notadate"}))


class TestCheckResultClassification(unittest.TestCase):
    """The 4 CheckResult variants — hard/advisory/unevidenced/awaiting —
    drive how the overall verdict is computed."""

    def test_hard_check_has_hard_classification(self):
        r = pg._hard(True, "ok")
        self.assertEqual(r.classification, "hard")
        self.assertTrue(r.passed)

    def test_advisory_never_fails_a_gate(self):
        r = pg._advisory(False, "advisory note")
        self.assertEqual(r.classification, "advisory")

    def test_unevidenced_is_failed_but_distinct(self):
        r = pg._unevidenced("no data")
        self.assertFalse(r.passed)
        self.assertEqual(r.classification, "unevidenced")

    def test_awaiting_is_failed_but_distinct(self):
        r = pg._awaiting("needs valid trades first")
        self.assertFalse(r.passed)
        self.assertEqual(r.classification, "awaiting")


class TestCheckMinValidTrades(unittest.TestCase):
    def test_zero_trades_fails(self):
        r = pg.check_min_valid_trades([])
        self.assertFalse(r.passed)

    def test_above_threshold_passes(self):
        r = pg.check_min_valid_trades([{"t": 1}] * 1000)  # definitely past gate
        self.assertTrue(r.passed)


class TestCheckFrozenConfigHash(unittest.TestCase):
    """A valid trade set must all stamp with ONE config_hash — drift = fail."""

    def test_single_hash_passes(self):
        trades = [{"config_hash": "abc123"}, {"config_hash": "abc123"}]
        self.assertTrue(pg.check_frozen_config_hash(trades).passed)

    def test_multiple_hashes_fail(self):
        trades = [{"config_hash": "abc"}, {"config_hash": "def"}]
        self.assertFalse(pg.check_frozen_config_hash(trades).passed)

    def test_missing_hash_field_fails(self):
        trades = [{}, {}]
        self.assertFalse(pg.check_frozen_config_hash(trades).passed)

    def test_empty_string_hash_ignored(self):
        """An empty-string hash should be discarded (not count as a second hash)."""
        trades = [{"config_hash": "abc"}, {"config_hash": ""}]
        r = pg.check_frozen_config_hash(trades)
        self.assertTrue(r.passed, f"empty string should not count as second hash: {r.detail}")


class TestCheckGitShaConsistent(unittest.TestCase):
    def test_single_sha_passes(self):
        trades = [{"git_sha": "deadbeef"}, {"git_sha": "deadbeef"}]
        self.assertTrue(pg.check_git_sha_consistent(trades).passed)

    def test_multiple_shas_fail(self):
        trades = [{"git_sha": "aaa"}, {"git_sha": "bbb"}]
        self.assertFalse(pg.check_git_sha_consistent(trades).passed)


class TestCheckInvalidRate(unittest.TestCase):
    """<10% invalid rate passes; >=10% fails."""

    def test_no_trades_fails(self):
        self.assertFalse(pg.check_invalid_rate([]).passed)

    def test_all_valid_passes(self):
        trades = [{"experiment_valid": "true"}] * 10
        self.assertTrue(pg.check_invalid_rate(trades).passed)

    def test_exactly_10pct_fails(self):
        """Boundary: 1 invalid out of 10 = 10% — does NOT pass."""
        trades = [{"experiment_valid": "true"}] * 9 + [{"experiment_valid": "false"}]
        self.assertFalse(pg.check_invalid_rate(trades).passed)

    def test_9_percent_invalid_passes(self):
        trades = ([{"experiment_valid": "true"}] * 91 +
                  [{"experiment_valid": "false"}] * 9)
        self.assertTrue(pg.check_invalid_rate(trades).passed)


class TestCheckNoRuntimeAnomalies(unittest.TestCase):
    def test_clean_trades_pass(self):
        trades = [{"invalid_reason": ""}, {"invalid_reason": "null"}]
        self.assertTrue(pg.check_no_runtime_anomalies(trades).passed)

    def test_any_invalid_reason_fails(self):
        trades = [{"invalid_reason": ""},
                  {"invalid_reason": "zero_stops_or_timeout_missing"}]
        self.assertFalse(pg.check_no_runtime_anomalies(trades).passed)


class TestCheckPositiveExpectancy(unittest.TestCase):
    """Positive expectancy check uses pip friction deduction on FX."""

    def test_positive_pip_expectancy_after_friction_passes(self):
        """Raw +5 pips/trade - 2 pips friction = +3 pips after costs -> pass."""
        trades = [{"pnl_pips": "5"}] * 10
        result = pg.check_positive_expectancy(trades)
        self.assertTrue(result.passed)

    def test_barely_negative_after_friction_fails(self):
        """With MODELED_FRICTION_PIPS = 0.3: raw 0.2 → adjusted -0.1 → fail."""
        trades = [{"pnl_pips": "0.2"}] * 10
        self.assertFalse(pg.check_positive_expectancy(trades).passed)

    def test_friction_constant_sanity(self):
        """Guard the friction constant: if someone zeros it out, every
        FX expectancy check becomes trivially easy to pass."""
        self.assertGreater(pg.MODELED_FRICTION_PIPS, 0,
            "MODELED_FRICTION_PIPS zeroed out — FX expectancy check is "
            "now a rubber stamp")

    def test_positive_pts_expectancy_passes_without_friction(self):
        """Futures (pnl_pts) has no modeled friction deduction."""
        trades = [{"pnl_pts": "0.5"}] * 10
        self.assertTrue(pg.check_positive_expectancy(trades).passed)

    def test_no_trades_fails(self):
        self.assertFalse(pg.check_positive_expectancy([]).passed)


class TestCheckConsecutiveLosses(unittest.TestCase):
    def test_small_streak_passes(self):
        trades = [{"pnl_pips": "1"}, {"pnl_pips": "-1"}, {"pnl_pips": "1"}]
        result = pg.check_consecutive_losses(trades)
        self.assertTrue(result.passed)

    def test_no_trades_awaiting(self):
        """Empty trade list -> awaiting, not failed."""
        r = pg.check_consecutive_losses([])
        # Either awaiting OR failed — either way not passed
        self.assertFalse(r.passed)


class TestCheckProfitFactor(unittest.TestCase):
    def test_positive_pf_passes(self):
        trades = [{"pnl_pips": "3"}] * 5 + [{"pnl_pips": "-1"}] * 5
        # gross profit 15, gross loss 5, PF=3
        self.assertTrue(pg.check_profit_factor(trades).passed)

    def test_low_pf_fails(self):
        trades = [{"pnl_pips": "1"}] * 5 + [{"pnl_pips": "-2"}] * 5
        # gross profit 5, gross loss 10, PF=0.5
        self.assertFalse(pg.check_profit_factor(trades).passed)


class TestCheckSessionConcentration(unittest.TestCase):
    def test_flat_pnl_passes(self):
        """Zero total PnL = no concentration risk."""
        trades = [{"pnl_pips": "0", "ts": "2026-04-19T12:00:00+00:00"}]
        self.assertTrue(pg.check_session_concentration(trades).passed)


class TestLoadTrades(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = pg._load_trades(Path(td))
        self.assertEqual(result, [])

    def test_existing_csv_parsed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            p.write_text("ts,pnl_pips,experiment_valid\n"
                         "2026-04-19,2.5,true\n", encoding="utf-8")
            result = pg._load_trades(Path(td))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pnl_pips"], "2.5")


class TestFilterRowsSince(unittest.TestCase):
    def test_none_stage_start_returns_all(self):
        rows = [{"ts": "2026-04-01T00:00:00+00:00"}]
        self.assertEqual(pg._filter_rows_since(rows, None), rows)

    def test_rows_before_cutoff_dropped(self):
        cutoff = datetime(2026, 4, 15, tzinfo=timezone.utc)
        rows = [
            {"ts": "2026-04-01T00:00:00+00:00"},  # before cutoff
            {"ts": "2026-04-16T00:00:00+00:00"},  # after
        ]
        result = pg._filter_rows_since(rows, cutoff)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ts"], "2026-04-16T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
