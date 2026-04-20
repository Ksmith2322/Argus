"""Unit tests for helio.fleet_perf_summary — per-strategy + fleet-level rollup.

This is the "how are we actually doing" single source of truth the dashboard
leans on. Bugs here = wrong headline numbers. The tests cover:

  - _parse_ts handles ISO timestamps with/without timezones
  - _strategy_summary USD rollup (sum, win/loss counts, win rate)
  - experiment_valid gating for Argus pairs
  - Window cutoff excludes older trades
  - alt_metric note surfaces when USD coverage is partial
  - build_summary emits both live_window and all_time sections
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import fleet_perf_summary as fps  # noqa: E402


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestParseTs(unittest.TestCase):
    def test_iso_with_tz(self):
        dt = fps._parse_ts("2026-04-18T13:30:00+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo.utcoffset(dt).total_seconds(), 0)

    def test_iso_naive_assumes_utc(self):
        dt = fps._parse_ts("2026-04-18T13:30:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_space_separated_format(self):
        dt = fps._parse_ts("2026-04-18 13:30:00")
        self.assertIsNotNone(dt)

    def test_date_only(self):
        dt = fps._parse_ts("2026-04-18")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_empty_returns_none(self):
        self.assertIsNone(fps._parse_ts(""))

    def test_unparseable_returns_none(self):
        self.assertIsNone(fps._parse_ts("not a date"))


class TestRowTsCandidates(unittest.TestCase):
    def test_prefers_ts_column(self):
        row = {"ts": "2026-04-18", "entry_ts": "2026-04-17"}
        self.assertEqual(fps._row_ts_candidates(row), "2026-04-18")

    def test_falls_through_to_entry_date_for_gdx_gld(self):
        row = {"ts": "", "entry_ts": "", "entry_date": "2026-04-18"}
        self.assertEqual(fps._row_ts_candidates(row), "2026-04-18")

    def test_returns_empty_when_all_missing(self):
        self.assertEqual(fps._row_ts_candidates({}), "")


class TestRowUsdPnl(unittest.TestCase):
    def test_returns_float_when_present(self):
        v = fps._row_usd_pnl({"pnl_usd": "199.15"}, {"usd_col": "pnl_usd"})
        self.assertAlmostEqual(v, 199.15)

    def test_returns_none_on_empty(self):
        v = fps._row_usd_pnl({"pnl_usd": ""}, {"usd_col": "pnl_usd"})
        self.assertIsNone(v)

    def test_returns_none_on_nonnumeric(self):
        v = fps._row_usd_pnl({"pnl_usd": "x"}, {"usd_col": "pnl_usd"})
        self.assertIsNone(v)


class TestStrategySummary(unittest.TestCase):
    """_strategy_summary is the row-level aggregator."""

    def test_missing_csv_marked_not_present(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(fps, "REPO", Path(td)):
                s = fps._strategy_summary(
                    {"label": "X", "path": "nowhere/trades.csv", "pnl_col": "pnl_usd",
                     "usd_col": "pnl_usd"},
                    anchor=10000, window_cutoff=None,
                )
        self.assertFalse(s["present"])
        self.assertEqual(s["trades"], 0)

    def test_sums_pnl_and_counts_wins(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_csv(tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv",
                rows=[
                    {"ts": "2026-04-15", "pnl_usd": "100"},
                    {"ts": "2026-04-16", "pnl_usd": "-50"},
                    {"ts": "2026-04-17", "pnl_usd": "200"},
                ],
                header=["ts", "pnl_usd"])
            with mock.patch.object(fps, "REPO", tdp):
                s = fps._strategy_summary(
                    {"label": "forge_gld_pm_long",
                     "path": "forge/logs/gld_pm_long/trades.csv",
                     "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
                    anchor=10000, window_cutoff=None,
                )
        self.assertEqual(s["trades"], 3)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["pnl_usd"], 250.0)
        self.assertAlmostEqual(s["win_rate_pct"], 66.7, places=0)

    def test_argus_valid_filter_excludes_invalid_rows(self):
        """valid_filter=True keeps only experiment_valid='true' rows."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_csv(tdp / "argus_flow" / "logs" / "usdjpy" / "trades.csv",
                rows=[
                    {"ts": "2026-04-15", "pnl_usd": "10", "experiment_valid": "true"},
                    {"ts": "2026-04-16", "pnl_usd": "-99", "experiment_valid": "false"},
                    {"ts": "2026-04-17", "pnl_usd": "20", "experiment_valid": "true"},
                ],
                header=["ts", "pnl_usd", "experiment_valid"])
            with mock.patch.object(fps, "REPO", tdp):
                s = fps._strategy_summary(
                    {"label": "argus_usdjpy",
                     "path": "argus_flow/logs/usdjpy/trades.csv",
                     "pnl_col": "pnl_pips", "usd_col": "pnl_usd", "valid_filter": True},
                    anchor=10000, window_cutoff=None,
                )
        self.assertEqual(s["trades"], 2)
        self.assertAlmostEqual(s["pnl_usd"], 30.0)  # not -69

    def test_window_cutoff_excludes_older_rows(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_csv(tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv",
                rows=[
                    {"ts": "2026-01-01T00:00:00+00:00", "pnl_usd": "100"},  # old
                    {"ts": "2026-04-15T00:00:00+00:00", "pnl_usd": "50"},   # within window
                ],
                header=["ts", "pnl_usd"])
            cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
            with mock.patch.object(fps, "REPO", tdp):
                s = fps._strategy_summary(
                    {"label": "forge_gld_pm_long",
                     "path": "forge/logs/gld_pm_long/trades.csv",
                     "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
                    anchor=10000, window_cutoff=cutoff,
                )
        self.assertEqual(s["trades"], 1)
        self.assertAlmostEqual(s["pnl_usd"], 50.0)

    def test_partial_usd_coverage_sets_flag(self):
        """Some historical rows have no pnl_usd — we mark coverage partial."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_csv(tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv",
                rows=[
                    {"ts": "2026-04-15", "pnl_usd": "100", "pnl_pips": "10"},
                    {"ts": "2026-04-16", "pnl_usd": "", "pnl_pips": "5"},  # no USD
                ],
                header=["ts", "pnl_usd", "pnl_pips"])
            with mock.patch.object(fps, "REPO", tdp):
                s = fps._strategy_summary(
                    {"label": "X",
                     "path": "forge/logs/gld_pm_long/trades.csv",
                     "pnl_col": "pnl_pips", "usd_col": "pnl_usd"},
                    anchor=10000, window_cutoff=None,
                )
        self.assertTrue(s.get("partial_usd"))
        self.assertEqual(s["usd_coverage"], "1/2")

    def test_first_last_trade_ts_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_csv(tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv",
                rows=[
                    {"ts": "2026-04-15T10:00:00+00:00", "pnl_usd": "10"},
                    {"ts": "2026-04-17T10:00:00+00:00", "pnl_usd": "20"},
                    {"ts": "2026-04-16T10:00:00+00:00", "pnl_usd": "30"},
                ],
                header=["ts", "pnl_usd"])
            with mock.patch.object(fps, "REPO", tdp):
                s = fps._strategy_summary(
                    {"label": "X", "path": "forge/logs/gld_pm_long/trades.csv",
                     "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
                    anchor=10000, window_cutoff=None,
                )
        self.assertTrue(s["first_trade_ts"].startswith("2026-04-15"))
        self.assertTrue(s["last_trade_ts"].startswith("2026-04-17"))


class TestBuildSummary(unittest.TestCase):
    def test_emits_both_live_window_and_all_time(self):
        with mock.patch.object(fps, "get_initial_capital_usd", return_value=10000), \
             mock.patch.object(fps, "_strategy_summary", return_value={
                 "label": "X", "present": True, "trades": 0,
                 "pnl_usd": None, "pnl_pct_of_fleet": None,
                 "alt_metric": None,
             }):
            summary = fps.build_summary(window_days=7)
        self.assertIn("live_window", summary)
        self.assertIn("all_time", summary)
        self.assertEqual(summary["live_window"]["window_days"], 7)
        self.assertIsNone(summary["all_time"]["window_days"])

    def test_anchor_captured_in_summary(self):
        with mock.patch.object(fps, "get_initial_capital_usd", return_value=12345.67), \
             mock.patch.object(fps, "_strategy_summary", return_value={
                 "label": "X", "present": True, "trades": 0,
                 "pnl_usd": None, "pnl_pct_of_fleet": None,
                 "alt_metric": None,
             }):
            summary = fps.build_summary(window_days=7)
        self.assertAlmostEqual(summary["anchor_capital_usd"], 12345.67)

    def test_unconverted_strategies_listed_when_no_usd(self):
        """A strategy with alt_metric but no pnl_usd should surface in
        fleet_total.unconverted_strategies."""
        def fake_summary(spec, anchor, cutoff):
            if spec["label"] == "has_usd":
                return {"label": "has_usd", "present": True, "trades": 5,
                        "pnl_usd": 100, "pnl_pct_of_fleet": 1.0,
                        "alt_metric": None}
            return {"label": "no_usd", "present": True, "trades": 3,
                    "pnl_usd": None, "pnl_pct_of_fleet": None,
                    "alt_metric": 5.0, "alt_metric_note": "sum of pnl_pct"}
        with mock.patch.object(fps, "get_initial_capital_usd", return_value=10000), \
             mock.patch.object(fps, "STRATEGIES",
                 [{"label": "has_usd"}, {"label": "no_usd"}]), \
             mock.patch.object(fps, "_strategy_summary", side_effect=fake_summary):
            summary = fps.build_summary(window_days=None)
        unconv = summary["all_time"]["fleet_total"]["unconverted_strategies"]
        self.assertIn("no_usd", unconv)
        self.assertNotIn("has_usd", unconv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
