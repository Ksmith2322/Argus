"""Unit tests for apollo.ops.backfill_forward_returns — Apollo's T+N
forward-return maturation pipeline.

Without this backfill, Apollo is structurally unfalsifiable — we'd have no
way to answer "did score >= 75 alerts actually outperform?" These tests
pin the pure logic: file parsing, anchor selection, forward-close math,
direction-flip for shorts, idempotent dedup.

Paths covered:
  - _scan_date_from_filename: accepts scan_YYYYMMDD.json, rejects junk
  - _anchor_pos: finds first close on/after scan_date
  - _forward_close: returns T+N trading days from anchor
  - short-direction ret_pct is negated
  - processed-cache idempotency
  - freshness gate: skip scans too fresh for T+3
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from apollo.ops import backfill_forward_returns as bf  # noqa: E402


class TestScanDateFromFilename(unittest.TestCase):
    def test_valid_scan_filename_returns_date(self):
        self.assertEqual(bf._scan_date_from_filename("scan_20260415.json"),
                         date(2026, 4, 15))

    def test_missing_scan_prefix_returns_none(self):
        self.assertIsNone(bf._scan_date_from_filename("daily_20260415.json"))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(bf._scan_date_from_filename("scan_notadate.json"))

    def test_handles_full_path(self):
        self.assertEqual(
            bf._scan_date_from_filename("/tmp/apollo/logs/scan_20260415.json"),
            date(2026, 4, 15))


class TestAnchorPos(unittest.TestCase):
    """_anchor_pos picks the first close whose date >= scan_date — this is
    how we align a scan with market data when yfinance returns only trading
    days (so scan dates on weekends resolve to the next Monday)."""

    def _series(self, dates: list[str]) -> pd.Series:
        idx = pd.to_datetime(dates)
        return pd.Series([float(i) for i in range(len(dates))], index=idx)

    def test_scan_on_trading_day_anchors_same_day(self):
        closes = self._series(["2026-04-15", "2026-04-16", "2026-04-17"])
        pos = bf._anchor_pos(closes, date(2026, 4, 15))
        self.assertEqual(pos, 0)

    def test_scan_on_weekend_anchors_next_monday(self):
        # scan on Saturday April 18 -> anchor Monday April 20
        closes = self._series(["2026-04-17", "2026-04-20", "2026-04-21"])
        pos = bf._anchor_pos(closes, date(2026, 4, 18))
        self.assertEqual(pos, 1)

    def test_scan_after_all_closes_returns_none(self):
        closes = self._series(["2026-04-15", "2026-04-16"])
        self.assertIsNone(bf._anchor_pos(closes, date(2026, 5, 1)))


class TestForwardClose(unittest.TestCase):
    """_forward_close returns the close N trading days after the anchor."""

    def _series(self, n: int) -> pd.Series:
        idx = pd.date_range("2026-04-15", periods=n, freq="B")
        return pd.Series([100.0 + i for i in range(n)], index=idx)

    def test_t_plus_1_returns_next_bar(self):
        closes = self._series(10)
        result = bf._forward_close(closes, anchor_pos=0, trading_days=1)
        self.assertIsNotNone(result)
        _, px = result
        self.assertAlmostEqual(px, 101.0)

    def test_t_plus_3_returns_third_bar(self):
        closes = self._series(10)
        result = bf._forward_close(closes, anchor_pos=0, trading_days=3)
        _, px = result
        self.assertAlmostEqual(px, 103.0)

    def test_horizon_beyond_end_returns_none(self):
        closes = self._series(3)
        result = bf._forward_close(closes, anchor_pos=0, trading_days=5)
        self.assertIsNone(result)


class TestProcessScanFileDirectionHandling(unittest.TestCase):
    """For short-direction scans, return_pct is negated."""

    def _write_scan_file(self, path: Path, rows: list[dict]) -> None:
        path.write_text(json.dumps(rows), encoding="utf-8")

    def _fake_closes(self) -> pd.Series:
        """20 trading days of rising closes from 100 to 120."""
        idx = pd.date_range("2026-04-01", periods=20, freq="B")
        return pd.Series([100.0 + i for i in range(20)], index=idx)

    def test_long_direction_ret_pct_positive_on_rising_market(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 80, "direction": "long",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                # Freeze "today" far enough in the future that the scan is old
                # enough for T+3 maturation to be available.
                records = bf.process_scan_file(scan, processed=set())
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertGreater(r["forward_returns_pct"]["T+3"]["return_pct"], 0,
            "long on rising market must have positive return_pct")

    def test_short_direction_ret_pct_negated(self):
        """Short on a rising market = losing short = negative ret_pct."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 80, "direction": "short",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                records = bf.process_scan_file(scan, processed=set())
        self.assertEqual(len(records), 1)
        self.assertLess(records[0]["forward_returns_pct"]["T+3"]["return_pct"], 0)

    def test_score_below_floor_filtered_out(self):
        """score < 75 (SCORE_FLOOR) must NOT produce a record."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 70, "direction": "long"},
            ])
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                records = bf.process_scan_file(scan, processed=set())
        self.assertEqual(records, [])

    def test_already_processed_symbol_skipped(self):
        """The processed-key cache prevents re-running the same (symbol, date)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 80, "direction": "long"},
            ])
            processed = {"AAPL|2026-04-01"}
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                records = bf.process_scan_file(scan, processed)
        self.assertEqual(records, [])

    def test_record_adds_key_to_processed_set(self):
        """A successful record must update the processed set in place so the
        NEXT run's cache-save picks up the new key."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 80, "direction": "long"},
            ])
            processed: set[str] = set()
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                bf.process_scan_file(scan, processed)
        self.assertIn("AAPL|2026-04-01", processed)

    def test_fresh_scan_skipped_when_horizon_not_matured(self):
        """A scan dated 'today' should be skipped — T+3 isn't ready yet."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            today = datetime.now(timezone.utc).date()
            scan = tdp / f"scan_{today.strftime('%Y%m%d')}.json"
            self._write_scan_file(scan, [
                {"symbol": "AAPL", "score": 80, "direction": "long"},
            ])
            # _fetch_closes shouldn't even be called since we skip early
            with mock.patch.object(bf, "_fetch_closes", return_value=self._fake_closes()):
                records = bf.process_scan_file(scan, processed=set())
        self.assertEqual(records, [], "fresh scan should be skipped")


class TestProcessedCache(unittest.TestCase):
    """_load_processed / _save_processed persist the (symbol, scan_date) set."""

    def test_missing_cache_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(bf, "PROCESSED_CACHE", Path(td) / "missing.json"):
                self.assertEqual(bf._load_processed(), set())

    def test_round_trip_preserves_keys(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "processed.json"
            with mock.patch.object(bf, "PROCESSED_CACHE", cache):
                bf._save_processed({"A|2026-04-01", "B|2026-04-02"})
                loaded = bf._load_processed()
        self.assertEqual(loaded, {"A|2026-04-01", "B|2026-04-02"})

    def test_corrupt_cache_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "corrupt.json"
            cache.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(bf, "PROCESSED_CACHE", cache):
                self.assertEqual(bf._load_processed(), set())


class TestRecordShape(unittest.TestCase):
    """Each forward-return record has a stable shape — downstream Apollo
    maturation (apollo.execution.planned_trades.mature_tickets) depends on
    it."""

    def test_record_has_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            scan = tdp / "scan_20260401.json"
            scan.write_text(json.dumps([{"symbol": "AAPL", "score": 80,
                                          "direction": "long"}]), encoding="utf-8")
            closes = pd.Series([100.0 + i for i in range(20)],
                               index=pd.date_range("2026-04-01", periods=20, freq="B"))
            with mock.patch.object(bf, "_fetch_closes", return_value=closes):
                records = bf.process_scan_file(scan, processed=set())
        record = records[0]
        for key in ("symbol", "scan_date", "anchor_date", "anchor_close",
                    "score", "direction", "horizon_unit", "forward_returns_pct"):
            self.assertIn(key, record)
        self.assertEqual(record["horizon_unit"], "trading_days")


if __name__ == "__main__":
    unittest.main(verbosity=2)
