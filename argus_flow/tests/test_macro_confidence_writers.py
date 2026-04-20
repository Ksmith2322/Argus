"""Tests for VIX Mean-Reversion and Sector Rotation confidence-artifact writers.

Each writer is exercised against a stubbed CSV input that mirrors the
schema the real writer consumes:
  - VIX Revert: backtest_trades.csv with return_pct + exit_reason columns
  - Sector Rot: backtest_trades.csv with month_return + regime columns

Fenced properties:
  1. Writer builds a dict that validates against StrategyConfidenceArtifact.
  2. strategy field matches the label.
  3. n_live == 0 (these are backfill-only artifacts).
  4. Dry-run does not write to disk.
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


def _write_csv(path: Path, header: list[str], rows: list[list]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


class TestVixRevertWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        """16 rows: 12 winners + 4 losers — shape mirrors the real VIX
        Revert backtest where wins outnumber losses roughly 3:1."""
        p = td / "backtest_trades.csv"
        header = ["entry_date", "exit_date", "vix_at_entry", "entry_price",
                  "exit_price", "return_pct", "days_held", "exit_reason"]
        rows = []
        for i in range(8):
            rows.append([f"2022-01-{i+1:02d}", f"2022-02-{i+1:02d}",
                         32.0, 400.0, 420.0, 5.0, 15, "VIX_BELOW_20"])
        for i in range(4):
            rows.append([f"2023-01-{i+1:02d}", f"2023-03-{i+1:02d}",
                         31.0, 380.0, 410.0, 7.9, 60, "TIME_EXIT"])
        for i in range(4):
            rows.append([f"2024-01-{i+1:02d}", f"2024-01-{i+10:02d}",
                         40.0, 500.0, 450.0, -10.0, 10, "STOP"])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.vix_revert_confidence_writer as vw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            with mock.patch.object(vw, "BACKTEST_CSV", csv_path):
                data = vw.build_vix_revert_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "vix_revert")
        self.assertEqual(data["bt_trades"], 16)
        self.assertEqual(data["n_total"], 16)
        self.assertEqual(data["n_live"], 0)
        self.assertEqual(data["n_paper"], 16)

    def test_dry_run_does_not_write(self):
        import forge.vix_revert_confidence_writer as vw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_path = tdp / "vix_revert.json"
            with mock.patch.object(vw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(vw, "ARTIFACT_PATH", out_path):
                result = vw.write_vix_revert_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_per_exit_reason_bucket(self):
        """Artifact should surface STOP / VIX_BELOW_20 / TIME_EXIT buckets."""
        import forge.vix_revert_confidence_writer as vw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(vw, "BACKTEST_CSV", csv_path):
                data = vw.build_vix_revert_artifact()
        per = data.get("per_instrument")
        self.assertIsNotNone(per)
        groups = {b["group"] for b in per["buckets"]}
        self.assertIn("STOP", groups)
        self.assertIn("VIX_BELOW_20", groups)
        self.assertIn("TIME_EXIT", groups)


class TestSectorRotWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        """24 monthly rebalance records spread across RISK_ON, NEUTRAL,
        RISK_OFF, CRISIS — mirrors the real regime distribution."""
        p = td / "backtest_trades.csv"
        header = ["date", "regime", "holdings", "month_return", "cumulative"]
        rows = []
        # 10 RISK_ON months (mostly up)
        for i in range(10):
            ret = 3.0 if i % 3 != 0 else -1.5
            rows.append([f"2023-{i+1:02d}-01", "RISK_ON", "XLK|XLY|XLF",
                         ret, ret * (i + 1)])
        # 8 NEUTRAL months
        for i in range(8):
            ret = 2.0 if i % 2 == 0 else -1.0
            rows.append([f"2024-{i+1:02d}-01", "NEUTRAL", "XLK|XLV|XLF",
                         ret, ret * (i + 1)])
        # 4 RISK_OFF months
        for i in range(4):
            ret = 1.5 if i % 2 == 0 else -2.0
            rows.append([f"2025-{i+1:02d}-01", "RISK_OFF", "XLU|XLP|GLD",
                         ret, ret * (i + 1)])
        # 2 CRISIS months
        for i in range(2):
            rows.append([f"2025-{i+6:02d}-01", "CRISIS", "GLD|TLT|XLU",
                         4.0, 4.0 * (i + 1)])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.sector_rot_confidence_writer as sw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            with mock.patch.object(sw, "BACKTEST_CSV", csv_path):
                data = sw.build_sector_rot_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "sector_rot")
        self.assertEqual(data["bt_trades"], 24)
        self.assertEqual(data["n_total"], 24)
        self.assertEqual(data["n_live"], 0)
        self.assertEqual(data["n_paper"], 24)

    def test_dry_run_does_not_write(self):
        import forge.sector_rot_confidence_writer as sw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_path = tdp / "sector_rot.json"
            with mock.patch.object(sw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(sw, "ARTIFACT_PATH", out_path):
                result = sw.write_sector_rot_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_per_regime_bucket(self):
        """Artifact should surface RISK_ON / NEUTRAL / RISK_OFF / CRISIS buckets."""
        import forge.sector_rot_confidence_writer as sw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(sw, "BACKTEST_CSV", csv_path):
                data = sw.build_sector_rot_artifact()
        per = data.get("per_instrument")
        self.assertIsNotNone(per)
        groups = {b["group"] for b in per["buckets"]}
        self.assertIn("RISK_ON", groups)
        self.assertIn("NEUTRAL", groups)
        self.assertIn("RISK_OFF", groups)
        self.assertIn("CRISIS", groups)

    def test_sample_warning_flags_monthly_nature(self):
        """Reader must not misread per-month PF as per-trade."""
        import forge.sector_rot_confidence_writer as sw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(sw, "BACKTEST_CSV", csv_path):
                data = sw.build_sector_rot_artifact()
        warn = data.get("sample_warning", "")
        self.assertIn("monthly rebalances", warn)


if __name__ == "__main__":
    unittest.main()
