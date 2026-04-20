"""Tests for Hermes, Ares, and Index Rebal confidence-artifact writers.

Each writer is exercised against a stubbed input file:
  - Hermes: trades_*.csv with pnl_pct column
  - Ares: backtest_*.json with trade_list + pnl_usd
  - Index Rebal: index_rebalance_trades.csv with alpha_ann_pct

Fenced properties:
  1. Writer builds a dict that validates against StrategyConfidenceArtifact.
  2. bt_trades / n_total reflect the stub input.
  3. Dry-run does not write to disk.
"""
from __future__ import annotations

import csv
import json
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


class TestHermesWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        """12 rows: 7 winners + 5 losers, score buckets spread across 60/70/80+."""
        p = td / "trades_stub.csv"
        header = ["ticker", "direction", "score", "entry_price",
                  "exit_price", "pnl_pct", "entry_date", "exit_date",
                  "exit_reason"]
        rows = []
        for i in range(5):
            rows.append(["AAPL", "LONG", 85, 100, 103, 3.0,
                         f"2026-01-{i+1:02d}", f"2026-01-{i+1:02d}", "target"])
        for i in range(2):
            rows.append(["NVDA", "LONG", 75, 200, 204, 2.0,
                         f"2026-02-{i+1:02d}", f"2026-02-{i+1:02d}", "target"])
        for i in range(3):
            rows.append(["TSLA", "LONG", 65, 300, 297, -1.0,
                         f"2026-03-{i+1:02d}", f"2026-03-{i+1:02d}", "stop"])
        for i in range(2):
            rows.append(["META", "LONG", 82, 400, 396, -1.0,
                         f"2026-03-{i+5:02d}", f"2026-03-{i+5:02d}", "stop"])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        """The write path validates after remapping per_score_bucket →
        per_instrument (which the build path leaves custom), so we exercise
        write_hermes_artifact(dry_run=True) here rather than build directly."""
        import hermes.confidence_writer as hw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            bt_dir = tdp / "bt"
            bt_dir.mkdir()
            (bt_dir / "trades_stub.csv").write_bytes(csv_path.read_bytes())
            out_path = tdp / "hermes.json"
            with mock.patch.object(hw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(hw, "ARTIFACT_PATH", out_path):
                data = hw.write_hermes_artifact(dry_run=True)
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "hermes")
        self.assertEqual(data["bt_trades"], 12)
        self.assertEqual(data["n_total"], 12)
        self.assertEqual(data["n_live"], 0)

    def test_dry_run_does_not_write(self):
        import hermes.confidence_writer as hw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            bt_dir = tdp / "bt"
            bt_dir.mkdir()
            (bt_dir / "trades_stub.csv").write_bytes(csv_path.read_bytes())
            out_path = tdp / "hermes.json"
            with mock.patch.object(hw, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(hw, "ARTIFACT_PATH", out_path):
                result = hw.write_hermes_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)


class TestAresWriter(unittest.TestCase):
    def _stub_json(self, td: Path) -> Path:
        bt_dir = td / "bt"
        bt_dir.mkdir()
        p = bt_dir / "backtest_stub.json"
        trade_list = []
        for i in range(8):
            trade_list.append({
                "symbol": "SPY", "pnl_usd": 100.0, "pnl_pct": 1.0,
                "entry_date": f"2026-01-{i+1:02d}",
                "exit_date": f"2026-01-{i+2:02d}",
                "exit_reason": "rotation",
            })
        for i in range(4):
            trade_list.append({
                "symbol": "QQQ", "pnl_usd": -50.0, "pnl_pct": -0.5,
                "entry_date": f"2026-02-{i+1:02d}",
                "exit_date": f"2026-02-{i+2:02d}",
                "exit_reason": "rotation",
            })
        data = {
            "trades": 12, "wins": 8, "losses": 4,
            "win_rate": 66.7, "profit_factor": 4.0,
            "total_return_pct": 20.0, "max_drawdown_pct": -5.0,
            "trade_list": trade_list,
        }
        p.write_text(json.dumps(data))
        return p

    def test_writer_builds_valid_artifact(self):
        import ares.confidence_writer as aw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            json_path = self._stub_json(tdp)
            with mock.patch.object(aw, "_BACKTEST_DIR", json_path.parent):
                data = aw.build_ares_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "ares")
        self.assertEqual(data["bt_trades"], 12)
        self.assertIsNotNone(data["bt_pf"])

    def test_dry_run_does_not_write(self):
        import ares.confidence_writer as aw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            json_path = self._stub_json(tdp)
            out_path = tdp / "ares.json"
            with mock.patch.object(aw, "_BACKTEST_DIR", json_path.parent), \
                 mock.patch.object(aw, "ARTIFACT_PATH", out_path):
                result = aw.write_ares_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)


class TestIndexRebalWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        p = td / "index_rebalance_trades.csv"
        header = ["ticker", "action", "announce_date", "effective_date",
                  "alpha_ann_pct", "alpha_post_eff_20d_pct"]
        rows = []
        for i in range(6):
            rows.append([f"ADD{i}", "ADD",
                         f"2026-01-{i+1:02d}", f"2026-01-{i+6:02d}",
                         2.5, 1.0])
        for i in range(4):
            rows.append([f"DEL{i}", "DELETE",
                         f"2026-02-{i+1:02d}", f"2026-02-{i+6:02d}",
                         -1.5, -0.8])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.index_rebalance_confidence_writer as iw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            with mock.patch.object(iw, "BACKTEST_CSV", csv_path):
                data = iw.build_index_rebal_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "index_rebal")
        self.assertEqual(data["bt_trades"], 10)
        self.assertEqual(data["n_live"], 0)
        self.assertEqual(data["n_paper"], 10)

    def test_dry_run_does_not_write(self):
        import forge.index_rebalance_confidence_writer as iw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_path = tdp / "index_rebal.json"
            with mock.patch.object(iw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(iw, "ARTIFACT_PATH", out_path):
                result = iw.write_index_rebal_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_per_action_bucket_reflects_add_delete_split(self):
        """Artifact should surface ADD vs DELETE as bucket groups."""
        import forge.index_rebalance_confidence_writer as iw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(iw, "BACKTEST_CSV", csv_path):
                data = iw.build_index_rebal_artifact()
        per = data.get("per_instrument")
        self.assertIsNotNone(per)
        groups = {b["group"] for b in per["buckets"]}
        self.assertIn("ADD", groups)
        self.assertIn("DELETE", groups)


if __name__ == "__main__":
    unittest.main()
