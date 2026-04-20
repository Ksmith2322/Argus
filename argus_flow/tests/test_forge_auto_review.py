"""Unit tests for forge.auto_review — the weekly Forge family report.

Most sections depend on live log data; we test the pure paths:
  - _read_trades_for_period: date parsing + window filter
  - generate_weekly_review returns a non-empty string with expected sections
  - Missing / corrupt CSV files don't crash the review generator
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from forge import auto_review as ar  # noqa: E402


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestReadTradesForPeriod(unittest.TestCase):
    def test_missing_csv_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            trades = ar._read_trades_for_period(
                Path(td) / "nope.csv",
                date.today() - timedelta(days=7), date.today())
        self.assertEqual(trades, [])

    def test_corrupt_csv_returns_empty_no_raise(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.csv"
            p.write_text("not,valid,csv\n\x00\x01\x02", encoding="utf-8")
            # Must not raise
            trades = ar._read_trades_for_period(
                p, date.today() - timedelta(days=7), date.today())
        self.assertIsInstance(trades, list)

    def test_date_window_filters_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            today = date.today()
            _write_csv(p, [
                {"ts": (today - timedelta(days=30)).isoformat(),
                 "pnl_pct": "1.0", "pnl_usd": "10"},
                {"ts": (today - timedelta(days=3)).isoformat(),
                 "pnl_pct": "2.0", "pnl_usd": "20"},
                {"ts": today.isoformat(),
                 "pnl_pct": "0.5", "pnl_usd": "5"},
            ], header=["ts", "pnl_pct", "pnl_usd"])
            trades = ar._read_trades_for_period(
                p, today - timedelta(days=7), today)
        self.assertEqual(len(trades), 2)
        # Both in window
        for t in trades:
            self.assertIn("pnl_pct", t)

    def test_pnl_pct_parsed_as_float(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            today = date.today()
            _write_csv(p, [
                {"ts": today.isoformat(), "pnl_pct": "2.5", "pnl_usd": "100"},
            ], header=["ts", "pnl_pct", "pnl_usd"])
            trades = ar._read_trades_for_period(
                p, today - timedelta(days=1), today)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl_pct"], 2.5)
        self.assertAlmostEqual(trades[0]["pnl_usd"], 100.0)

    def test_invalid_date_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            today = date.today()
            _write_csv(p, [
                {"ts": "not-a-date", "pnl_pct": "1.0", "pnl_usd": "5"},
                {"ts": today.isoformat(), "pnl_pct": "2.0", "pnl_usd": "10"},
            ], header=["ts", "pnl_pct", "pnl_usd"])
            trades = ar._read_trades_for_period(
                p, today - timedelta(days=1), today)
        self.assertEqual(len(trades), 1)

    def test_entry_date_fallback(self):
        """If ts is missing, entry_date is used."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            today = date.today()
            _write_csv(p, [
                {"ts": "", "entry_date": today.isoformat(),
                 "pnl_pct": "1.0", "pnl_usd": "5"},
            ], header=["ts", "entry_date", "pnl_pct", "pnl_usd"])
            trades = ar._read_trades_for_period(
                p, today - timedelta(days=1), today)
        self.assertEqual(len(trades), 1)


class TestGenerateWeeklyReview(unittest.TestCase):
    """Smoke: the report should always emit a string, even when no data
    is available."""

    def test_report_is_non_empty_string(self):
        report = ar.generate_weekly_review(days=7)
        self.assertIsInstance(report, str)
        self.assertGreater(len(report), 50)

    def test_report_contains_expected_section_headers(self):
        """The report always includes these top-level sections — drift in
        section headers would break the operator's habitual grep."""
        report = ar.generate_weekly_review(days=7)
        # At minimum: fleet health + trade summary + recommendations
        expected_sections = ["Fleet Health", "Trade Summary", "Recommendations"]
        for section in expected_sections:
            self.assertIn(section, report,
                f"section '{section}' missing from report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
