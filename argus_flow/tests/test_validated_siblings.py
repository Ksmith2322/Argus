"""Tests for the five validated-sibling confidence writers.

Each sibling writer scopes a broad strategy down to a validated subset.
These tests fence the canonical properties:
  1. Writer builds a dict that validates against StrategyConfidenceArtifact.
  2. disposition.status == "scope_down" — the whole point of the sibling.
  3. The new `drawdown` field is attached (from _max_drawdown helper).
  4. Dry-run does not write an artifact to disk.
  5. Where applicable, the filter reduces the sample (n_total counts only
     the passing rows).

Inputs are stubbed CSVs/JSON with the exact column set each writer reads.
Path constants on each module are patched via mock.patch.object.
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


# ---------------------------------------------------------------------------
# Apollo Validated
# ---------------------------------------------------------------------------
class TestApolloValidated(unittest.TestCase):
    HEADER = ["symbol", "earnings_date", "entry_date", "entry_price",
              "exit_price", "pre_er_close", "day1_gap_pct", "surprise_pct",
              "pnl_pct", "exit_reason", "days_held"]

    def _stub_csv_dir(self, td: Path) -> Path:
        """12 passing rows (surprise in [10,20), gap>=2) + 6 failing rows."""
        bt_dir = td / "apollo" / "data" / "backtest_results"
        bt_dir.mkdir(parents=True)
        p = bt_dir / "post_er_20260101T000000.csv"
        rows = []
        # Passing: 8 winners + 4 losers → 12 rows
        for i in range(8):
            rows.append(["AAPL", "2026-01-15", f"2026-01-{16+i%10:02d}",
                         220.0, 230.0, 218.0, 2.5, 12.5, 4.0, "target", 2])
        for i in range(4):
            rows.append(["NVDA", "2026-02-15", f"2026-02-{16+i:02d}",
                         400.0, 392.0, 398.0, 3.0, 15.0, -2.0, "stop", 2])
        # Non-passing: surprise too low (<10)
        for i in range(3):
            rows.append(["TSLA", "2026-03-15", f"2026-03-{16+i:02d}",
                         300.0, 306.0, 298.0, 2.5, 5.0, 2.0, "target", 2])
        # Non-passing: gap too low (<2)
        for i in range(3):
            rows.append(["META", "2026-04-15", f"2026-04-{16+i:02d}",
                         400.0, 408.0, 398.0, 0.5, 12.0, 2.0, "target", 2])
        _write_csv(p, self.HEADER, rows)
        return bt_dir

    def test_writer_builds_valid_artifact(self):
        import apollo.confidence_writer_validated as mod
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_apollo_validated_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "apollo_validated")
        self.assertEqual(data["disposition"]["status"], "scope_down")
        self.assertIsNotNone(data.get("drawdown"))
        self.assertIn("max_drawdown_usd", data["drawdown"])

    def test_dry_run_does_not_write(self):
        import apollo.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = self._stub_csv_dir(tdp)
            out_path = tdp / "apollo_validated.json"
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(mod, "ARTIFACT_PATH", out_path):
                result = mod.write_apollo_validated_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_filter_reduces_sample(self):
        """Of 18 rows, only 12 meet surprise in [10,20) AND gap>=2."""
        import apollo.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_apollo_validated_artifact()
        self.assertEqual(data["n_total"], 12)
        self.assertEqual(data["bt_trades"], 12)


# ---------------------------------------------------------------------------
# Mamba YM-only
# ---------------------------------------------------------------------------
class TestMambaYmValidated(unittest.TestCase):
    HEADER = ["ticker", "instrument", "entry_time", "exit_time", "direction",
              "bias", "entry", "stop", "target1", "target2", "exit_price",
              "stop_dist", "pnl_points", "pnl_usd", "rr_achieved", "outcome",
              "confluences", "bars_held", "day", "dow"]

    def _stub_multi_csv(self, path: Path):
        """Multi-ticker CSV: 10 YM rows + 5 NQ rows (writer should filter to YM)."""
        rows = []
        for i in range(7):
            rows.append(["YM=F", "MYM", f"2026-01-{i+1:02d} 09:50:00-05:00",
                         f"2026-01-{i+1:02d} 09:55:00-05:00", "LONG",
                         "bullish", 38000, 37950, 38100, 38200, 38100,
                         50, 100, 50.0, 1.0, "target1", "x", 5, "2026-01-01",
                         "Monday"])
        for i in range(3):
            rows.append(["YM=F", "MYM", f"2026-02-{i+1:02d} 09:50:00-05:00",
                         f"2026-02-{i+1:02d} 09:55:00-05:00", "SHORT",
                         "bearish", 38000, 38050, 37900, 37800, 38050,
                         50, -50, -25.0, -1.0, "stop", "x", 5,
                         "2026-02-01", "Tuesday"])
        for i in range(5):
            rows.append(["NQ=F", "MNQ", f"2026-03-{i+1:02d} 09:50:00-05:00",
                         f"2026-03-{i+1:02d} 09:55:00-05:00", "SHORT",
                         "bearish", 20000, 20050, 19900, 19800, 20050,
                         50, -50, -100.0, -1.0, "stop", "x", 5,
                         "2026-03-01", "Wednesday"])
        _write_csv(path, self.HEADER, rows)

    def test_writer_builds_valid_artifact_ym_only_source(self):
        """When the YM-only CSV exists, the writer should consume it directly."""
        import forge.mamba.confidence_writer_ym as mod
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ym_csv = tdp / "backtest_trades_ym_only_60d.csv"
            self._stub_multi_csv(ym_csv)  # all rows would be treated as YM
            with mock.patch.object(mod, "BACKTEST_CSV_YM_ONLY", ym_csv), \
                 mock.patch.object(mod, "BACKTEST_CSV_MULTI", tdp / "nope.csv"):
                data = mod.build_mamba_ym_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "mamba_ym")
        self.assertEqual(data["disposition"]["status"], "scope_down")
        self.assertIsNotNone(data.get("drawdown"))

    def test_dry_run_does_not_write(self):
        import forge.mamba.confidence_writer_ym as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ym_csv = tdp / "backtest_trades_ym_only_60d.csv"
            self._stub_multi_csv(ym_csv)
            out_path = tdp / "mamba_ym.json"
            with mock.patch.object(mod, "BACKTEST_CSV_YM_ONLY", ym_csv), \
                 mock.patch.object(mod, "BACKTEST_CSV_MULTI", tdp / "nope.csv"), \
                 mock.patch.object(mod, "ARTIFACT_PATH", out_path):
                result = mod.write_mamba_ym_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_multi_csv_filters_to_ym(self):
        """When only the multi-ticker CSV exists, writer should exclude NQ rows."""
        import forge.mamba.confidence_writer_ym as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ym_csv = tdp / "does_not_exist.csv"
            multi_csv = tdp / "backtest_trades.csv"
            self._stub_multi_csv(multi_csv)
            with mock.patch.object(mod, "BACKTEST_CSV_YM_ONLY", ym_csv), \
                 mock.patch.object(mod, "BACKTEST_CSV_MULTI", multi_csv):
                data = mod.build_mamba_ym_artifact()
        # 10 YM rows total — writer excludes NQ rows via filter
        self.assertEqual(data["n_total"], 10)
        self.assertEqual(data["bt_trades"], 10)


# ---------------------------------------------------------------------------
# Titan Validated
# ---------------------------------------------------------------------------
class TestTitanValidated(unittest.TestCase):
    HEADER = ["symbol", "strategy", "direction", "entry_date", "exit_date",
              "entry_price", "exit_price", "stop_price", "target_price",
              "pnl_pct", "exit_reason", "days_held", "strength",
              "risk_reward", "reason"]

    def _stub_csv_dir(self, td: Path) -> Path:
        bt_dir = td / "titan" / "data" / "backtest_results"
        bt_dir.mkdir(parents=True)
        p = bt_dir / "trades_20260101T000000.csv"
        rows = []
        # 10 passing: TREND_FOLLOW + long
        for i in range(7):
            rows.append(["AAPL", "TREND_FOLLOW", "long",
                         f"2026-01-{i+1:02d}", f"2026-01-{i+5:02d}",
                         100.0, 103.0, 98.0, 108.0, 3.0, "target", 4,
                         70, 2.0, "ema align"])
        for i in range(3):
            rows.append(["NVDA", "TREND_FOLLOW", "long",
                         f"2026-02-{i+1:02d}", f"2026-02-{i+5:02d}",
                         200.0, 196.0, 195.0, 210.0, -2.0, "stop", 4,
                         60, 2.0, "ema align"])
        # Non-passing: TREND_FOLLOW + short
        for i in range(4):
            rows.append(["GLD", "TREND_FOLLOW", "short",
                         f"2026-03-{i+1:02d}", f"2026-03-{i+5:02d}",
                         170.0, 168.0, 172.0, 160.0, 1.2, "target", 4,
                         65, 2.0, "ema align"])
        # Non-passing: BREAKOUT + long
        for i in range(3):
            rows.append(["TSLA", "BREAKOUT", "long",
                         f"2026-04-{i+1:02d}", f"2026-04-{i+5:02d}",
                         300.0, 305.0, 295.0, 310.0, 1.5, "target", 4,
                         55, 2.0, "resistance break"])
        _write_csv(p, self.HEADER, rows)
        return bt_dir

    def test_writer_builds_valid_artifact(self):
        import titan.confidence_writer_validated as mod
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_titan_validated_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "titan_validated")
        self.assertEqual(data["disposition"]["status"], "scope_down")
        self.assertIsNotNone(data.get("drawdown"))

    def test_dry_run_does_not_write(self):
        import titan.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = self._stub_csv_dir(tdp)
            out_path = tdp / "titan_validated.json"
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(mod, "ARTIFACT_PATH", out_path):
                result = mod.write_titan_validated_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_filter_reduces_sample(self):
        """17 rows total, 10 pass strategy==TREND_FOLLOW AND direction==long."""
        import titan.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_titan_validated_artifact()
        self.assertEqual(data["n_total"], 10)
        self.assertEqual(data["bt_trades"], 10)


# ---------------------------------------------------------------------------
# Hermes Validated
# ---------------------------------------------------------------------------
class TestHermesValidated(unittest.TestCase):
    HEADER = ["symbol", "date", "gap_type", "direction", "gap_pct",
              "entry_price", "exit_price", "target_price", "stop_price",
              "pnl_pct", "exit_reason", "days_held", "score",
              "volume_ratio", "rsi"]

    def _stub_csv_dir(self, td: Path) -> Path:
        bt_dir = td / "hermes" / "data" / "backtest_results"
        bt_dir.mkdir(parents=True)
        p = bt_dir / "trades_20260101T000000.csv"
        rows = []
        # Passing: score>=80, long, GAP_DOWN — 10 rows
        for i in range(7):
            rows.append(["AAPL", f"2026-01-{i+1:02d}", "GAP_DOWN", "long",
                         -2.5, 180.0, 185.0, 190.0, 178.0, 2.8, "target", 2,
                         85, 1.5, 40.0])
        for i in range(3):
            rows.append(["NVDA", f"2026-02-{i+1:02d}", "GAP_DOWN", "long",
                         -3.0, 400.0, 392.0, 410.0, 390.0, -2.0, "stop", 2,
                         82, 1.4, 35.0])
        # Non-passing: score < 80
        for i in range(3):
            rows.append(["META", f"2026-03-{i+1:02d}", "GAP_DOWN", "long",
                         -2.0, 400.0, 410.0, 415.0, 395.0, 2.5, "target", 2,
                         70, 1.2, 45.0])
        # Non-passing: GAP_UP (not GAP_DOWN)
        for i in range(2):
            rows.append(["TSLA", f"2026-04-{i+1:02d}", "GAP_UP", "long",
                         2.0, 300.0, 306.0, 310.0, 295.0, 2.0, "target", 2,
                         85, 1.3, 55.0])
        # Non-passing: short direction
        for i in range(2):
            rows.append(["GOOG", f"2026-04-{10+i:02d}", "GAP_DOWN", "short",
                         -2.0, 140.0, 137.0, 135.0, 142.0, 2.1, "target", 2,
                         85, 1.4, 40.0])
        _write_csv(p, self.HEADER, rows)
        return bt_dir

    def test_writer_builds_valid_artifact(self):
        import hermes.confidence_writer_validated as mod
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_hermes_validated_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "hermes_validated")
        self.assertEqual(data["disposition"]["status"], "scope_down")
        self.assertIsNotNone(data.get("drawdown"))

    def test_dry_run_does_not_write(self):
        import hermes.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bt_dir = self._stub_csv_dir(tdp)
            out_path = tdp / "hermes_validated.json"
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir), \
                 mock.patch.object(mod, "ARTIFACT_PATH", out_path):
                result = mod.write_hermes_validated_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_filter_reduces_sample(self):
        """17 rows total, 10 pass score>=80 AND long AND GAP_DOWN."""
        import hermes.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            bt_dir = self._stub_csv_dir(Path(td))
            with mock.patch.object(mod, "_BACKTEST_DIR", bt_dir):
                data = mod.build_hermes_validated_artifact()
        self.assertEqual(data["n_total"], 10)
        self.assertEqual(data["bt_trades"], 10)


# ---------------------------------------------------------------------------
# Cue Banks Validated
# ---------------------------------------------------------------------------
class TestCueBanksValidated(unittest.TestCase):
    HEADER = ["date", "time", "direction", "entry", "stop", "tp1", "tp2",
              "tp3", "confluence_score", "factors", "exit_price",
              "exit_reason", "pnl_points", "pnl_usd", "rr_achieved",
              "contracts", "risk_usd", "equity_after"]

    def _stub_csv(self, path: Path):
        rows = []
        # Passing: factors contains "S/D supply zone" — 10 rows (7 wins, 3 losses)
        passing_factor = ("S/R @ 38000 | S/D supply zone [38050-38100] "
                          "| Structure bearish")
        for i in range(7):
            rows.append(["2026-01-01", "10:00", "SHORT", 38000, 38050,
                         37900, 37800, 37700, 7.0, passing_factor,
                         37900, "tp1", 100, 200.0, 2.0, 2, 100.0, 10200.0])
        for i in range(3):
            rows.append(["2026-02-01", "10:00", "SHORT", 38000, 38050,
                         37900, 37800, 37700, 7.0, passing_factor,
                         38050, "stopped", -50, -100.0, -1.0, 2, 100.0,
                         9900.0])
        # Non-passing: demand zone only
        failing_factor = ("S/R @ 38000 | S/D demand zone [37900-37950] "
                          "| Structure bullish")
        for i in range(5):
            rows.append(["2026-03-01", "10:00", "LONG", 38000, 37950,
                         38100, 38200, 38300, 6.0, failing_factor,
                         37950, "stopped", -50, -100.0, -1.0, 2, 100.0,
                         9900.0])
        _write_csv(path, self.HEADER, rows)

    def test_writer_builds_valid_artifact(self):
        import forge.cuebanks.confidence_writer_validated as mod
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "cuebanks_backtest_trades.csv"
            self._stub_csv(csv_path)
            with mock.patch.object(mod, "BACKTEST_CSV", csv_path):
                data = mod.build_cuebanks_validated_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "cue_banks_validated")
        self.assertEqual(data["disposition"]["status"], "scope_down")
        self.assertIsNotNone(data.get("drawdown"))

    def test_dry_run_does_not_write(self):
        import forge.cuebanks.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = tdp / "cuebanks_backtest_trades.csv"
            self._stub_csv(csv_path)
            out_path = tdp / "cue_banks_validated.json"
            with mock.patch.object(mod, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(mod, "ARTIFACT_PATH", out_path):
                result = mod.write_cuebanks_validated_artifact(dry_run=True)
            self.assertFalse(out_path.exists())
            self.assertIsInstance(result, dict)

    def test_filter_reduces_sample(self):
        """15 rows total, 10 have factors containing 'S/D supply zone'."""
        import forge.cuebanks.confidence_writer_validated as mod
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "cuebanks_backtest_trades.csv"
            self._stub_csv(csv_path)
            with mock.patch.object(mod, "BACKTEST_CSV", csv_path):
                data = mod.build_cuebanks_validated_artifact()
        self.assertEqual(data["n_total"], 10)
        self.assertEqual(data["bt_trades"], 10)


if __name__ == "__main__":
    unittest.main()
