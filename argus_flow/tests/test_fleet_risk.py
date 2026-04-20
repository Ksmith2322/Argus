"""Unit tests for helio.fleet_risk — cross-family double-exposure guard.

Callers (apollo/runner, hermes/runner, titan/runner, forge/conviction) ask
this module "does any other family hold SYMBOL?" before opening a position.
A false-negative here means two families hold the same instrument — paying
spread against themselves and concentrating risk.

Tests pin:
  - REPO path resolves to the repo root (not one level above — a real bug
    fixed 2026-04-19, which had neutered the check in production).
  - Per-family JSON shape parsing (titan/hermes/ares/apollo each use
    different shapes).
  - check_exposure returns blocked=True only when overlap actually exists.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import fleet_risk as fr  # noqa: E402


class TestRepoPath(unittest.TestCase):
    """Regression guard: REPO must point to the actual repo root, not above it.
    If someone moves this file again and forgets to update .parents[N],
    get_fleet_positions() will silently return {} and the check is neutered."""

    def test_repo_path_points_to_repo_root(self):
        # The module-level REPO must contain the helio/ subdir itself
        self.assertTrue((fr.REPO / "helio").is_dir(),
            f"fleet_risk.REPO={fr.REPO} doesn't contain helio/ — "
            "path resolution is broken, see 2026-04-19 bug fix")
        self.assertTrue((fr.REPO / "argus_flow").is_dir())


class TestGetFleetPositions(unittest.TestCase):
    """Each family has a different positions.json shape — parser must
    handle each one without cross-contamination."""

    def _setup_repo(self, tdp: Path, files: dict[str, dict]) -> None:
        """Create tdp/{path}/positions.json with the given dict contents."""
        for subpath, content in files.items():
            p = tdp / subpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(content), encoding="utf-8")

    def test_titan_positions_parsed(self):
        """Titan stores positions as {symbol: {...}, 'last_rebalance': '...', ...}
        — we want only the actual symbols."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._setup_repo(tdp, {
                "titan/logs/positions.json": {
                    "NVDA": {"shares": 10},
                    "AAPL": {"shares": 5},
                    "last_rebalance": "2026-04-18",  # metadata — must be filtered
                    "last_signal": "2026-04-17",      # ditto
                    "holdings": {"NVDA": 10},         # ditto
                },
            })
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertIn("Titan", pos)
        self.assertEqual(sorted(pos["Titan"]), ["AAPL", "NVDA"])
        self.assertNotIn("last_rebalance", pos["Titan"])

    def test_ares_positions_parsed_from_holdings(self):
        """Ares nests positions under 'holdings'."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._setup_repo(tdp, {
                "ares/logs/positions.json": {"holdings": {"SPY": 100, "QQQ": 50}},
            })
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertIn("Ares", pos)
        self.assertEqual(sorted(pos["Ares"]), ["QQQ", "SPY"])

    def test_apollo_positions_parsed_from_positions_key(self):
        """Apollo nests under 'positions' key."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._setup_repo(tdp, {
                "apollo/logs/positions.json": {
                    "positions": {"MSFT": {"score": 80}, "GOOG": {"score": 75}},
                },
            })
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertIn("Apollo", pos)
        self.assertEqual(sorted(pos["Apollo"]), ["GOOG", "MSFT"])

    def test_hermes_positions_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._setup_repo(tdp, {
                "hermes/logs/positions.json": {
                    "XAUUSD": {"entry": 2050},
                    "last_rebalance": "2026-04-18",  # filtered
                },
            })
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertEqual(pos["Hermes"], ["XAUUSD"])

    def test_empty_positions_file_skipped(self):
        """Empty holdings -> family not included in result."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._setup_repo(tdp, {
                "titan/logs/positions.json": {"last_rebalance": "2026-04-18"},
                "ares/logs/positions.json":  {"holdings": {}},
            })
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertNotIn("Titan", pos)
        self.assertNotIn("Ares", pos)

    def test_missing_file_tolerated(self):
        """No positions.json anywhere -> empty result, no exception."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(fr, "REPO", Path(td)):
                pos = fr.get_fleet_positions()
        self.assertEqual(pos, {})

    def test_malformed_json_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "titan" / "logs" / "positions.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(fr, "REPO", tdp):
                pos = fr.get_fleet_positions()
        self.assertNotIn("Titan", pos)


class TestCheckExposure(unittest.TestCase):
    """check_exposure: blocked=True iff another family already holds SYMBOL."""

    def test_blocked_when_same_symbol_held_in_another_family(self):
        fleet = {"Titan": ["NVDA", "AAPL"], "Hermes": ["XAUUSD"]}
        with mock.patch.object(fr, "get_fleet_positions", return_value=fleet):
            result = fr.check_exposure("NVDA")
        self.assertTrue(result["blocked"])
        self.assertIn("DOUBLE_EXPOSURE", result["reason"])
        self.assertIn("Titan", result["reason"])
        self.assertEqual(len(result["existing"]), 1)

    def test_not_blocked_when_symbol_absent(self):
        fleet = {"Titan": ["NVDA"], "Hermes": ["XAUUSD"]}
        with mock.patch.object(fr, "get_fleet_positions", return_value=fleet):
            result = fr.check_exposure("MSFT")
        self.assertFalse(result["blocked"])
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["existing"], [])

    def test_blocked_lists_all_offenders_when_held_in_multiple_families(self):
        """If TWO families hold the same symbol, the reason lists both."""
        fleet = {"Titan": ["NVDA"], "Apollo": ["NVDA"]}
        with mock.patch.object(fr, "get_fleet_positions", return_value=fleet):
            result = fr.check_exposure("NVDA")
        self.assertTrue(result["blocked"])
        self.assertEqual(len(result["existing"]), 2)
        self.assertIn("Titan", result["reason"])
        self.assertIn("Apollo", result["reason"])

    def test_empty_fleet_allows_entry(self):
        with mock.patch.object(fr, "get_fleet_positions", return_value={}):
            result = fr.check_exposure("NVDA")
        self.assertFalse(result["blocked"])


class TestFleetSummary(unittest.TestCase):
    def test_empty_fleet_message(self):
        with mock.patch.object(fr, "get_fleet_positions", return_value={}):
            s = fr.fleet_summary()
        self.assertIn("no open positions", s.lower())

    def test_summary_lists_each_family(self):
        fleet = {"Titan": ["NVDA", "AAPL"], "Hermes": ["XAUUSD"]}
        with mock.patch.object(fr, "get_fleet_positions", return_value=fleet):
            s = fr.fleet_summary()
        self.assertIn("Titan", s)
        self.assertIn("Hermes", s)
        self.assertIn("NVDA", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
