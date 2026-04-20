"""Unit tests for argus_flow.ops.usdjpy_drift_forensics — pair-level
live-vs-replay blocker analysis.

The forensics module is what we used to spot that USDJPY was hitting
MTF_BLOCKED on 34/42 signals in a 14-day window. If the blocker counter
drifts or the verdict logic regresses, we lose that visibility.

Tests cover the pure analysis path: _parse_float, window filtering,
blocker histogram math, verdict-string composition. The signal file is
mocked into a temp dir.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from argus_flow.ops import usdjpy_drift_forensics as df  # noqa: E402


def _write_signals(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Gather all keys across rows to build header
    cols = set()
    for r in rows:
        cols.update(r.keys())
    cols = sorted(cols)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestParseFloat(unittest.TestCase):
    def test_valid_number(self):
        self.assertAlmostEqual(df._parse_float("3.14"), 3.14)

    def test_empty_returns_default(self):
        self.assertEqual(df._parse_float("", default=5.0), 5.0)

    def test_none_returns_default(self):
        self.assertEqual(df._parse_float(None, default=7.5), 7.5)

    def test_nan_string_returns_default(self):
        self.assertEqual(df._parse_float("nan", default=1.0), 1.0)

    def test_junk_returns_default(self):
        self.assertEqual(df._parse_float("abc", default=2.0), 2.0)


class TestAnalyzePairNoData(unittest.TestCase):
    def test_missing_file_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(df, "_REPO", Path(td)):
                result = df.analyze_pair("usdjpy")
        self.assertEqual(result["pair"], "usdjpy")
        self.assertIn("error", result)

    def test_empty_file_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ts,action,hour\n", encoding="utf-8")
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy")
        self.assertIn("error", result)


class TestAnalyzePairCounts(unittest.TestCase):
    """Blocker histogram + trigger/entry counts."""

    def test_basic_counts(self):
        """3 NO_TRIGGER, 2 MTF_BLOCKED_LONG, 1 HOUR_FILTERED, 1 ENTRY = 7 bars."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, [
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER", "hour": "10"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER", "hour": "11"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER", "hour": "12"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "MTF_BLOCKED_LONG", "hour": "13"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "MTF_BLOCKED_LONG", "hour": "14"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "HOUR_FILTERED", "hour": "15"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY", "hour": "16"},
            ])
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        self.assertEqual(result["total_bars_evaluated"], 7)
        self.assertEqual(result["no_trigger_count"], 3)
        self.assertEqual(result["trigger_count"], 4)   # all non-NO_TRIGGER
        self.assertEqual(result["entry_count"], 1)
        # Blocker histogram excludes NO_TRIGGER and ENTRY
        self.assertEqual(result["blockers"]["MTF_BLOCKED_LONG"], 2)
        self.assertEqual(result["blockers"]["HOUR_FILTERED"], 1)
        self.assertNotIn("ENTRY", result["blockers"])
        self.assertNotIn("NO_TRIGGER", result["blockers"])

    def test_blockers_sorted_most_common_first(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, (
                [{"ts": datetime.now(timezone.utc).isoformat(), "action": "MTF_BLOCKED_LONG"}] * 10
                + [{"ts": datetime.now(timezone.utc).isoformat(), "action": "HOUR_FILTERED"}] * 5
                + [{"ts": datetime.now(timezone.utc).isoformat(), "action": "SPREAD_WIDE"}] * 2
            ))
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        keys_in_order = list(result["blockers"].keys())
        self.assertEqual(keys_in_order[0], "MTF_BLOCKED_LONG")
        self.assertEqual(keys_in_order[-1], "SPREAD_WIDE")


class TestWindowFilter(unittest.TestCase):
    """Rows older than window_days are excluded."""

    def test_old_rows_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            now = datetime.now(timezone.utc)
            old = (now - timedelta(days=30)).isoformat()
            recent = now.isoformat()
            _write_signals(p, [
                {"ts": old, "action": "ENTRY"},
                {"ts": old, "action": "NO_TRIGGER"},
                {"ts": recent, "action": "ENTRY"},
            ])
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        # Only the recent row should be counted
        self.assertEqual(result["total_bars_evaluated"], 1)
        self.assertEqual(result["entry_count"], 1)


class TestHourDistribution(unittest.TestCase):
    """hour_distribution buckets rows by hour with total/triggers/entries counts."""

    def test_hour_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, [
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER", "hour": "10"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY", "hour": "10"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "MTF_BLOCKED", "hour": "10"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY", "hour": "15"},
            ])
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        hr10 = result["hour_distribution"]["10"]
        self.assertEqual(hr10["total"], 3)
        self.assertEqual(hr10["triggers"], 2)  # ENTRY + MTF_BLOCKED
        self.assertEqual(hr10["entries"], 1)


class TestFeatureStats(unittest.TestCase):
    """feature_stats split triggered vs filtered — used for 'triggered bars
    have higher range_pct' verdict."""

    def test_feature_mean_separates_triggered_and_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, [
                # Filtered: low range_pct
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER",
                 "range_pct": "0.001", "range_accel": "0.1"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "NO_TRIGGER",
                 "range_pct": "0.0015", "range_accel": "0.2"},
                # Triggered: high range_pct
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY",
                 "range_pct": "0.005", "range_accel": "0.5"},
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY",
                 "range_pct": "0.006", "range_accel": "0.6"},
            ])
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        f = result["feature_stats"]
        self.assertEqual(f["filtered"]["range_pct"]["n"], 2)
        self.assertEqual(f["triggered"]["range_pct"]["n"], 2)
        self.assertGreater(f["triggered"]["range_pct"]["mean"],
                            f["filtered"]["range_pct"]["mean"])


class TestVerdictComposition(unittest.TestCase):
    """verdict is a list of human-readable strings; at least one always present."""

    def test_verdict_never_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, [
                {"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY",
                 "range_pct": "0.005"},
            ])
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        self.assertGreater(len(result["verdict"]), 0)

    def test_verdict_mentions_severe_drift_when_ratio_below_half(self):
        """With replay_expected=10/day and observed=0.5/day (ratio=5%), the
        verdict must call out severe drift."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Minimal signals: 1 ENTRY over 7 days = ~0.14/day observed
            p = tdp / "argus_flow" / "logs" / "usdjpy" / "signals.csv"
            _write_signals(p, [{"ts": datetime.now(timezone.utc).isoformat(), "action": "ENTRY"}])
            # Stub config with replay_expectations
            cfg = tdp / "argus_flow" / "configs" / "usdjpy_mtf_paper_v1.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps({"replay_expectations": {"signals_per_day": 10}}),
                           encoding="utf-8")
            with mock.patch.object(df, "_REPO", tdp):
                result = df.analyze_pair("usdjpy", window_days=7)
        # Ratio is well under 0.5 -> severe drift verdict
        self.assertTrue(any("severe drift" in v.lower() or "severe" in v.lower()
                            for v in result["verdict"]),
            f"verdict: {result['verdict']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
