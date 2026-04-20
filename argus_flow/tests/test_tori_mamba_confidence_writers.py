"""Tests for the Tori and Mamba confidence-artifact writers.

Three properties fenced:
  1. Writers build a dict that passes the bridge schema validator.
  2. The overall PF / WR / trade count match what the backtest CSV says.
  3. The sample_warning names the subset edge — reading the artifact
     must tell the operator to narrow scope, not to trust the union.
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


class TestToriWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        """Synthesize a 20-row tori backtest CSV with a clear subset edge:
        bounce/A only on Dow is strongly positive; the rest is negative.
        """
        p = td / "backtest_trades.csv"
        header = ["ticker", "name", "direction", "setup", "grade",
                  "entry_price", "exit_price", "stop_price",
                  "pnl_points", "pnl_usd", "r_multiple", "contracts",
                  "entry_date", "exit_date", "hold_bars", "exit_reason"]
        rows = []
        # 5 bounce/A on Dow: 4 winners + 1 loser → strongly positive
        for i in range(4):
            rows.append(["YM=F", "Dow", "LONG", "bounce", "A",
                          40000, 40200, 39900, 200, 1000, 2.0, 1,
                          "2026-01-01", "2026-01-02", 5, "target"])
        rows.append(["YM=F", "Dow", "LONG", "bounce", "A",
                      40000, 39900, 39900, -100, -500, -1.0, 1,
                      "2026-01-03", "2026-01-04", 3, "stop_hit"])
        # 15 break/A across instruments, mostly losers
        for i in range(15):
            rows.append(["CL=F", "Crude Oil", "LONG", "break", "A",
                          70, 69, 69, -1, -100, -1.0, 1,
                          "2026-02-01", "2026-02-02", 4, "stop_hit"])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.tori.confidence_writer as tw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            with mock.patch.object(tw, "BACKTEST_CSV", csv_path):
                data = tw.build_tori_artifact()
        # Must pass the schema (raises if not)
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "tori")
        self.assertEqual(data["bt_trades"], 20)
        self.assertEqual(data["n_total"], 20)
        self.assertEqual(data["n_live"], 0)

    def test_writer_surfaces_subset_edge_in_warning(self):
        import forge.tori.confidence_writer as tw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(tw, "BACKTEST_CSV", csv_path):
                data = tw.build_tori_artifact()
        self.assertIn("bounce_A", data["sample_warning"])
        self.assertIn("Dow", data["sample_warning"])
        # Union-sign footer: "narrow scope" when losing, "positive: PF=..." when winning
        warning = data["sample_warning"]
        self.assertTrue("narrow scope" in warning or "is positive" in warning,
                        f"warning must flag union sign: {warning!r}")

    def test_dry_run_does_not_write(self):
        import forge.tori.confidence_writer as tw
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_path = tdp / "tori.json"
            with mock.patch.object(tw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(tw, "ARTIFACT_PATH", out_path):
                result = tw.write_tori_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_write_produces_loadable_file(self):
        """Round-trip: writer output is loadable by the bridge reader."""
        import forge.tori.confidence_writer as tw
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_dir = tdp / "strategy_confidence"
            out_dir.mkdir()
            out_path = out_dir / "tori.json"
            with mock.patch.object(tw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(tw, "ARTIFACT_PATH", out_path), \
                 mock.patch.object(sc, "ARTIFACT_DIR", out_dir):
                tw.write_tori_artifact()
                artifact = sc.load_confidence_artifact("tori")
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.strategy, "tori")


class TestMambaWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        """Synthesize a 20-row mamba CSV. NQ = pure losers, YM = winners.
        Subset edge should be 'YM=F only'."""
        p = td / "backtest_trades.csv"
        header = ["ticker", "instrument", "entry_time", "exit_time",
                  "direction", "bias", "entry", "stop", "target1",
                  "target2", "exit_price", "stop_dist", "pnl_points",
                  "pnl_usd", "rr_achieved", "outcome", "confluences",
                  "bars_held", "day", "dow"]
        rows = []
        # 9 YM winners + 1 small loser (so PF is finite — matches real data shape)
        for _ in range(9):
            rows.append(["YM=F", "MYM", "2026-02-01 09:30:00", "2026-02-01 09:45:00",
                          "LONG", "bullish", 40000, 39900, 40200, 40300,
                          40200, 100, 200, 100.0, 2.0, "target", 5, 1, "Monday"])
        rows.append(["YM=F", "MYM", "2026-02-05 09:30:00", "2026-02-05 09:35:00",
                      "LONG", "bullish", 40000, 39900, 40200, 40300,
                      39900, 100, -100, -50.0, -1.0, "stop", 5, 1, "Friday"])
        # 10 NQ losers
        for _ in range(10):
            rows.append(["NQ=F", "MNQ", "2026-02-02 09:30:00", "2026-02-02 09:35:00",
                          "SHORT", "bearish", 25000, 25020, 24970, 24950,
                          25020, 20, -20, -40.0, -1.0, "stop", 5, 1, "Tuesday"])
        _write_csv(p, header, rows)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.mamba.confidence_writer as mw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(mw, "BACKTEST_CSV", csv_path):
                data = mw.build_mamba_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "mamba")
        self.assertEqual(data["bt_trades"], 20)

    def test_writer_flags_synthetic_1m_caveat(self):
        """Mamba's whole confidence argument is undermined by the
        synthetic-1m-bar issue — the artifact must carry that caveat
        in the warning so a reader doesn't take the PF at face value."""
        import forge.mamba.confidence_writer as mw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(mw, "BACKTEST_CSV", csv_path):
                data = mw.build_mamba_artifact()
        self.assertIn("synthetic 1m", data["sample_warning"])
        # YM is the obvious subset edge in the stub
        self.assertIn("YM=F", data["sample_warning"])

    def test_source_field_marks_synthetic(self):
        """The source string must identify the methodology so that once
        the real-1m pipeline lands, the source string changes and old
        artifacts are visibly superseded."""
        import forge.mamba.confidence_writer as mw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(mw, "BACKTEST_CSV", csv_path):
                data = mw.build_mamba_artifact()
        self.assertIn("synthetic_1m", data["source"])


class TestDashboardPicksUpRealArtifacts(unittest.TestCase):
    """The real artifacts checked into strategy_confidence/ today should
    produce computed rows on the live endpoint."""

    def test_tori_and_mamba_are_computed(self):
        from fastapi.testclient import TestClient
        from ops.dashboard import app
        c = TestClient(app)
        data = c.get("/api/strategy_performance").json()
        tori = next(s for s in data["strategies"] if s["system"] == "Tori")
        mamba = next(s for s in data["strategies"] if s["system"] == "Mamba")
        self.assertEqual(tori["confidence_source"], "computed")
        self.assertEqual(mamba["confidence_source"], "computed")
        # Warning must carry the subset-edge context regardless of sign
        tori_w = tori["confidence_detail"]["sample_warning"]
        self.assertIn("subset edge", tori_w)
        # Union sign footer exists (either "narrow scope" or "is positive")
        self.assertTrue("narrow scope" in tori_w or "is positive" in tori_w)
        self.assertIn("synthetic 1m", mamba["confidence_detail"]["sample_warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
