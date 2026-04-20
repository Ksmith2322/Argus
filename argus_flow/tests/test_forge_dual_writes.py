"""Regression tests for the forge dual-writes to canonical_fills.

Phase 2 coverage: wick_gbpusd and gdx_gld_runner now write a Fill to
canonical_fills in addition to their per-strategy trades.csv. This file
pins each new path:

  1. One canonical row per close / per trade_row append
  2. Canonical write failure does NOT break local writes
  3. gdx_gld carries the GLD leg + z-scores in the extra dict
     (critical for auditing pair trades after the fact)
  4. Direction normalization: LONG_SPREAD -> "long", SHORT_SPREAD -> "short"
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestWickGbpusdDualWrite(unittest.TestCase):
    def _stub_state(self) -> dict:
        return {
            "open_trade": {
                "entry_idx": 0, "entry_px": 1.2500,
                "position_size": 10000, "risk_usd": 50.0,
                "atr_entry": 0.004, "stop_px": 1.246, "target_px": 1.258,
                "session_id": "sess", "config_hash": "h", "git_sha": "s",
            },
            "trade_count": 0,
            "runtime_start": time.time(),
            "session_id": "sess",
        }

    def _stub_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"Close": [1.25, 1.255]},
            index=pd.date_range("2026-04-18", periods=2, tz="UTC"),
        )

    def test_close_emits_canonical_row(self):
        import helio.canonical_fills as cf
        from forge.wick_gbpusd import runner as wr
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(wr, "_append_trade"):
                wr._close_paper_trade(
                    self._stub_state(), self._stub_df(),
                    exit_idx=1, exit_px=1.258, reason="target",
                )
            rows = [json.loads(l) for l in out.read_text().splitlines() if l]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "forge_wick_gbpusd")
        self.assertEqual(row["symbol"], "GBPUSD")
        self.assertEqual(row["direction"], "long")
        self.assertEqual(row["side"], "EXIT")
        self.assertEqual(row["exit_reason"], "target")
        self.assertAlmostEqual(row["entry_px"], 1.25)
        self.assertAlmostEqual(row["exit_px"], 1.258)

    def test_canonical_failure_does_not_raise(self):
        from forge.wick_gbpusd import runner as wr
        with mock.patch.object(wr, "_append_trade"), \
             mock.patch("helio.canonical_fills.write_fill_typed",
                        side_effect=RuntimeError("boom")):
            # Must not raise
            wr._close_paper_trade(
                self._stub_state(), self._stub_df(),
                exit_idx=1, exit_px=1.258, reason="target",
            )


class TestGdxGldDualWrite(unittest.TestCase):
    def test_append_trade_row_emits_canonical_row(self):
        import helio.canonical_fills as cf
        import forge.gdx_gld_runner as gr
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(gr, "TRADES_CSV", tdp / "trades.csv"), \
                 mock.patch.object(gr, "LOG_DIR", tdp):
                gr.append_trade_row(
                    entry_date="2026-04-15", exit_date="2026-04-18",
                    direction="LONG_SPREAD",
                    entry_z=-2.1, exit_z=0.1,
                    gdx_entry=38.0, gld_entry=150.0,
                    gdx_exit=38.5, gld_exit=150.0,
                    pnl_pct=0.01, exit_reason="mean_revert",
                    equity_usd=10000,
                    emit_canonical=True,
                )
            rows = [json.loads(l) for l in out.read_text().splitlines() if l]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "forge_gdx_gld")
        self.assertEqual(row["symbol"], "GDX/GLD")
        self.assertEqual(row["direction"], "long")  # LONG_SPREAD → long
        self.assertEqual(row["exit_reason"], "mean_revert")

    def test_backtest_append_does_not_emit_canonical(self):
        # run_backtest() replays historical data and must NEVER pollute
        # canonical_fills.jsonl. Default emit_canonical=False enforces that.
        import helio.canonical_fills as cf
        import forge.gdx_gld_runner as gr
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(gr, "TRADES_CSV", tdp / "trades.csv"), \
                 mock.patch.object(gr, "LOG_DIR", tdp):
                gr.append_trade_row(
                    entry_date="2008-03-15", exit_date="2008-04-18",
                    direction="LONG_SPREAD",
                    entry_z=-2.1, exit_z=0.1,
                    gdx_entry=38.0, gld_entry=150.0,
                    gdx_exit=38.5, gld_exit=150.0,
                    pnl_pct=0.01, exit_reason="mean_revert",
                    equity_usd=10000,
                )
            self.assertFalse(out.exists(), "backtest must not write canonical fills")
            self.assertTrue((tdp / "trades.csv").exists(), "csv must still be written")

    def test_short_spread_normalizes_to_short(self):
        import helio.canonical_fills as cf
        import forge.gdx_gld_runner as gr
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(gr, "TRADES_CSV", tdp / "trades.csv"), \
                 mock.patch.object(gr, "LOG_DIR", tdp):
                gr.append_trade_row(
                    entry_date="2026-04-15", exit_date="2026-04-18",
                    direction="SHORT_SPREAD",
                    entry_z=2.1, exit_z=-0.1,
                    gdx_entry=38.0, gld_entry=150.0,
                    gdx_exit=37.5, gld_exit=150.0,
                    pnl_pct=0.01, exit_reason="mean_revert",
                    equity_usd=10000,
                    emit_canonical=True,
                )
            row = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(row["direction"], "short")

    def test_extra_dict_captures_gld_leg_and_zscores(self):
        """The pair-trade audit trail depends on this — without the extra
        dict we'd lose the GLD leg data entirely."""
        import helio.canonical_fills as cf
        import forge.gdx_gld_runner as gr
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(gr, "TRADES_CSV", tdp / "trades.csv"), \
                 mock.patch.object(gr, "LOG_DIR", tdp):
                gr.append_trade_row(
                    entry_date="2026-04-15", exit_date="2026-04-18",
                    direction="LONG_SPREAD",
                    entry_z=-2.1, exit_z=0.1,
                    gdx_entry=38.0, gld_entry=150.0,
                    gdx_exit=38.5, gld_exit=151.0,
                    pnl_pct=0.01, exit_reason="mean_revert",
                    equity_usd=10000,
                    emit_canonical=True,
                )
            row = json.loads(out.read_text().splitlines()[0])
        extra = row["extra"]
        self.assertAlmostEqual(extra["gld_entry"], 150.0)
        self.assertAlmostEqual(extra["gld_exit"], 151.0)
        self.assertAlmostEqual(extra["entry_z"], -2.1)
        self.assertAlmostEqual(extra["exit_z"], 0.1)
        self.assertGreater(extra["gld_shares"], 0)

    def test_canonical_failure_does_not_break_csv_write(self):
        """Local trades.csv write must still happen even if canonical blows up."""
        import forge.gdx_gld_runner as gr
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with mock.patch.object(gr, "TRADES_CSV", tdp / "trades.csv"), \
                 mock.patch.object(gr, "LOG_DIR", tdp), \
                 mock.patch("helio.canonical_fills.write_fill_typed",
                            side_effect=RuntimeError("canonical broken")):
                gr.append_trade_row(
                    entry_date="2026-04-15", exit_date="2026-04-18",
                    direction="LONG_SPREAD",
                    entry_z=-2.1, exit_z=0.1,
                    gdx_entry=38.0, gld_entry=150.0,
                    gdx_exit=38.5, gld_exit=150.0,
                    pnl_pct=0.01, exit_reason="mean_revert",
                    equity_usd=10000,
                )
            # The CSV still got written
            csv_text = (tdp / "trades.csv").read_text()
        self.assertIn("LONG_SPREAD", csv_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
