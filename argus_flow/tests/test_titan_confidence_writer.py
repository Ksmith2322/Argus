"""Tests for the Titan confidence-artifact writer.

Exercises titan.confidence_writer against a stubbed trades_*.csv.

Fenced properties:
  1. Writer builds a dict that validates against StrategyConfidenceArtifact.
  2. Dry-run does not write to disk.
  3. Per-symbol bucket surfaces the input instruments.
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


class TestTitanWriter(unittest.TestCase):
    def _stub_csv(self, bt_dir: Path) -> Path:
        """14 rows: 9 winners + 5 losers split across three symbols."""
        p = bt_dir / "trades_stub.csv"
        header = [
            "symbol", "strategy", "direction", "entry_date", "exit_date",
            "entry_price", "exit_price", "stop_price", "target_price",
            "pnl_pct", "exit_reason", "days_held", "strength",
            "risk_reward", "reason",
        ]
        rows = []
        for i in range(6):
            rows.append(["GLD", "TREND_FOLLOW", "long",
                         f"2026-01-{i+1:02d}", f"2026-01-{i+5:02d}",
                         180.0, 185.0, 177.0, 190.0,
                         2.8, "target", 4, 70, 2.0, "stub long"])
        for i in range(3):
            rows.append(["SPY", "BREAKOUT", "long",
                         f"2026-02-{i+1:02d}", f"2026-02-{i+5:02d}",
                         450.0, 456.0, 445.0, 465.0,
                         1.3, "target", 4, 65, 2.5, "stub breakout"])
        for i in range(3):
            rows.append(["QQQ", "TREND_FOLLOW", "long",
                         f"2026-03-{i+1:02d}", f"2026-03-{i+5:02d}",
                         400.0, 396.0, 395.0, 412.0,
                         -1.0, "stop", 4, 60, 2.4, "stub stop"])
        for i in range(2):
            rows.append(["GLD", "BREAKOUT", "short",
                         f"2026-03-{i+10:02d}", f"2026-03-{i+14:02d}",
                         185.0, 188.0, 188.0, 178.0,
                         -1.6, "stop", 4, 68, 2.2, "stub stop short"])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import titan.confidence_writer as tw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = tdp / "bt"
            bt_dir.mkdir()
            self._stub_csv(bt_dir)
            with mock.patch.object(tw, "_BACKTEST_DIR", bt_dir):
                data = tw.build_titan_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "titan")
        self.assertEqual(data["bt_trades"], 14)
        self.assertEqual(data["n_total"], 14)
        self.assertEqual(data["n_live"], 0)
        self.assertEqual(data["n_paper"], 14)
        self.assertIsNotNone(data["bt_pf"])

    def test_dry_run_does_not_write(self):
        import titan.confidence_writer as tw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = tdp / "bt"
            bt_dir.mkdir()
            self._stub_csv(bt_dir)
            out_path = tdp / "titan.json"
            with mock.patch.object(tw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(tw, "ARTIFACT_PATH", out_path):
                result = tw.write_titan_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_per_symbol_bucket_surfaces_instruments(self):
        """Artifact should surface each symbol as a bucket group."""
        import titan.confidence_writer as tw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = tdp / "bt"
            bt_dir.mkdir()
            self._stub_csv(bt_dir)
            with mock.patch.object(tw, "_BACKTEST_DIR", bt_dir):
                data = tw.build_titan_artifact()
        per = data.get("per_instrument")
        self.assertIsNotNone(per)
        groups = {b["group"] for b in per["buckets"]}
        self.assertIn("GLD", groups)
        self.assertIn("SPY", groups)
        self.assertIn("QQQ", groups)


if __name__ == "__main__":
    unittest.main()
