"""Unit tests for ops.dashboard_data — pure data readers extracted from
the dashboard monolith.

Phase 3 prep: start teaching endpoints to import from this sibling module
instead of reaching into the 12K-line monolith. Tests pin:

  - read_canonical_with_freshness: _meta shape + freshness threshold
  - parse_report_ts: ISO / date-only / invalid handling
  - strategy_live_cutoffs: parses fleet_sizing.json correctly
  - Both the monolith and the extracted module behave identically for
    the same input (back-compat guarantee)
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ops import dashboard_data as dd  # noqa: E402


class TestReadCanonicalWithFreshness(unittest.TestCase):
    def test_missing_file_returns_error_shape(self):
        result = dd.read_canonical_with_freshness("does_not_exist.json")
        self.assertIn("error", result)
        self.assertEqual(result["_meta"]["fresh"], False)

    def test_present_file_includes_mtime_age(self):
        with tempfile.TemporaryDirectory() as td:
            # Write a small json to a known location inside the repo-root
            # we're mocking via _REPO rebinding.
            p = Path(td) / "report.json"
            p.write_text(json.dumps({"k": "v"}), encoding="utf-8")
            with mock.patch.object(dd, "_REPO", Path(td)):
                result = dd.read_canonical_with_freshness("report.json",
                                                           fresh_threshold_s=3600)
            self.assertEqual(result["k"], "v")
            self.assertIn("_meta", result)
            self.assertIn("mtime_s_ago", result["_meta"])
            self.assertTrue(result["_meta"]["fresh"])  # just-written file is fresh

    def test_stale_file_flagged_not_fresh(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "old.json"
            p.write_text("{}", encoding="utf-8")
            old_mtime = time.time() - 7200  # 2hrs old
            os.utime(p, (old_mtime, old_mtime))
            with mock.patch.object(dd, "_REPO", Path(td)):
                result = dd.read_canonical_with_freshness("old.json",
                                                           fresh_threshold_s=900)
            self.assertFalse(result["_meta"]["fresh"])
            self.assertGreater(result["_meta"]["mtime_s_ago"], 3600)

    def test_corrupt_json_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            with mock.patch.object(dd, "_REPO", Path(td)):
                result = dd.read_canonical_with_freshness("bad.json")
            self.assertIn("error", result)


class TestParseReportTs(unittest.TestCase):
    def test_iso_with_timezone(self):
        ts = dd.parse_report_ts("2026-04-19T13:30:00+00:00")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.tzinfo.utcoffset(ts).total_seconds(), 0)

    def test_iso_with_z_suffix(self):
        ts = dd.parse_report_ts("2026-04-19T13:30:00Z")
        self.assertIsNotNone(ts)

    def test_iso_naive_assumed_utc(self):
        ts = dd.parse_report_ts("2026-04-19T13:30:00")
        self.assertIsNotNone(ts)
        self.assertIsNotNone(ts.tzinfo)

    def test_date_only(self):
        ts = dd.parse_report_ts("2026-04-19")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)

    def test_space_separated(self):
        ts = dd.parse_report_ts("2026-04-19 13:30:00")
        self.assertIsNotNone(ts)

    def test_none_returns_none(self):
        self.assertIsNone(dd.parse_report_ts(None))

    def test_empty_returns_none(self):
        self.assertIsNone(dd.parse_report_ts(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(dd.parse_report_ts("not a date"))

    def test_microseconds_tolerated(self):
        """ISO with microseconds parses."""
        ts = dd.parse_report_ts("2026-04-19T13:30:00.123456+00:00")
        self.assertIsNotNone(ts)


class TestStrategyLiveCutoffs(unittest.TestCase):
    def test_empty_when_config_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(dd, "_REPO", Path(td)):
                self.assertEqual(dd.strategy_live_cutoffs(), {})

    def test_reads_cutoffs_from_fleet_sizing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "argus_flow" / "configs" / "fleet_sizing.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(json.dumps({
                "strategy_live_cutoffs": {
                    "forge_gdx_gld": "2026-04-17T00:00:00+00:00",
                    "forge_x":       "2026-03-01T00:00:00+00:00",
                },
            }), encoding="utf-8")
            with mock.patch.object(dd, "_REPO", tdp):
                result = dd.strategy_live_cutoffs()
            self.assertIn("forge_gdx_gld", result)
            self.assertIn("forge_x", result)
            self.assertEqual(result["forge_gdx_gld"].year, 2026)

    def test_corrupt_config_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "argus_flow" / "configs" / "fleet_sizing.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(dd, "_REPO", tdp):
                self.assertEqual(dd.strategy_live_cutoffs(), {})

    def test_invalid_timestamp_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "argus_flow" / "configs" / "fleet_sizing.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(json.dumps({
                "strategy_live_cutoffs": {
                    "ok_strat": "2026-04-17T00:00:00+00:00",
                    "bad_strat": "not a real ts",
                },
            }), encoding="utf-8")
            with mock.patch.object(dd, "_REPO", tdp):
                result = dd.strategy_live_cutoffs()
            self.assertIn("ok_strat", result)
            self.assertNotIn("bad_strat", result)


class TestMonolithReExportsMatch(unittest.TestCase):
    """The monolith re-exports the extracted helpers under their original
    private names for back-compat. A rename in either place without
    updating the other would silently break endpoints."""

    def test_monolith_private_names_still_resolve(self):
        from ops.dashboard import (
            _read_canonical_with_freshness,
            _parse_report_ts,
            _strategy_live_cutoffs,
        )
        # All three should be callables
        self.assertTrue(callable(_read_canonical_with_freshness))
        self.assertTrue(callable(_parse_report_ts))
        self.assertTrue(callable(_strategy_live_cutoffs))

    def test_monolith_and_module_same_function_objects(self):
        """Re-export must be the SAME object, not a copy — otherwise
        mocking one in a test wouldn't affect the other."""
        from ops.dashboard import _parse_report_ts as monolith_version
        from ops.dashboard_data import parse_report_ts as module_version
        self.assertIs(monolith_version, module_version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
