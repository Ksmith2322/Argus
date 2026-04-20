"""Tests for the Apollo confidence-artifact writer.

Fenced properties:
  1. Writer builds a dict that validates against StrategyConfidenceArtifact.
  2. Dry-run does not write to disk.
  3. sample_warning names the post_er subset so readers cannot conflate
     the artifact with the full Apollo universe (drift/honest_bt/runup are
     explicitly excluded — only the subset the live runner executes).
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _write_post_er_csv(path: Path) -> None:
    """12 rows: 7 winners + 5 losers, mix of symbols."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["symbol", "earnings_date", "entry_date", "entry_price",
              "exit_price", "pre_er_close", "day1_gap_pct", "surprise_pct",
              "pnl_pct", "exit_reason", "days_held"]
    rows = []
    for i in range(5):
        rows.append(["NVDA", "2025-03-31", f"2025-04-0{i+1}", 100.0, 103.0,
                     99.0, 1.5, 8.0, 3.0, "target", 10])
    for i in range(2):
        rows.append(["MU", "2025-06-30", f"2025-07-0{i+1}", 50.0, 52.0,
                     49.0, 2.0, 6.0, 4.0, "target", 12])
    for i in range(3):
        rows.append(["AAPL", "2025-09-30", f"2025-10-0{i+1}", 220.0, 209.0,
                     222.0, 0.3, 1.2, -5.0, "stop", 6])
    for i in range(2):
        rows.append(["JD", "2025-09-30", f"2025-10-0{i+5}", 30.0, 28.5,
                     30.5, -1.0, -3.0, -5.0, "stop", 4])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


class TestApolloWriter(unittest.TestCase):
    def _stub_bt_dir(self, td: Path) -> Path:
        bt_dir = td / "bt"
        bt_dir.mkdir()
        _write_post_er_csv(bt_dir / "post_er_20260409T220000.csv")
        return bt_dir

    def test_writer_builds_valid_artifact(self):
        import apollo.confidence_writer as aw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_bt_dir(Path(td))
            out_path = Path(td) / "apollo.json"
            with mock.patch.object(aw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(aw, "ARTIFACT_PATH", out_path):
                data = aw.write_apollo_artifact(dry_run=True)
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "apollo")
        self.assertEqual(data["bt_trades"], 12)
        self.assertEqual(data["n_total"], 12)
        self.assertEqual(data["n_live"], 0)
        self.assertEqual(data["n_paper"], 12)
        self.assertIsNotNone(data["bt_pf"])

    def test_dry_run_does_not_write(self):
        import apollo.confidence_writer as aw
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_bt_dir(Path(td))
            out_path = Path(td) / "apollo.json"
            with mock.patch.object(aw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(aw, "ARTIFACT_PATH", out_path):
                result = aw.write_apollo_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_sample_warning_names_subset(self):
        """sample_warning must name the post_er subset so dashboard readers
        don't confuse this with the full Apollo universe."""
        import apollo.confidence_writer as aw
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_bt_dir(Path(td))
            out_path = Path(td) / "apollo.json"
            with mock.patch.object(aw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(aw, "ARTIFACT_PATH", out_path):
                data = aw.write_apollo_artifact(dry_run=True)
        warn = data.get("sample_warning", "")
        self.assertIn("post_er", warn)
        self.assertIn("n=", warn)


if __name__ == "__main__":
    unittest.main()
