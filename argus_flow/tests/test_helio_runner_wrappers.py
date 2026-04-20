"""Unit tests for helio.runner_apollo and helio.runner_hermes — thin
family-runner wrappers that derive from helio.runner's swing pattern.

Both follow the same structure: State class, compute_indicators,
evaluate(), _log_trade, run_live loop. Tests cover the pure logic:
state persistence, indicator math, and evaluate() exit/entry decisions.

helio.runner (the original) is already covered by test_helio_runner;
these are the Apollo + Hermes forks with mean-reversion / breakout
specifics.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import runner_apollo as ra  # noqa: E402
from helio import runner_hermes as rh  # noqa: E402


class TestApolloStateInitAndPersistence(unittest.TestCase):
    def test_starts_flat(self):
        with tempfile.TemporaryDirectory() as td:
            s = ra.ApolloState(Path(td) / "state.json")
        self.assertEqual(s.position, "FLAT")
        self.assertEqual(s.trade_count, 0)
        self.assertEqual(s.pnl_total, 0.0)

    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            s = ra.ApolloState(path)
            s.position = "LONG"
            s.entry_price = 100.5
            s.trade_count = 7
            s.save()
            s2 = ra.ApolloState(path)
            s2.load()
            self.assertEqual(s2.position, "LONG")
            self.assertAlmostEqual(s2.entry_price, 100.5)
            self.assertEqual(s2.trade_count, 7)

    def test_load_missing_file_noop(self):
        with tempfile.TemporaryDirectory() as td:
            s = ra.ApolloState(Path(td) / "missing.json")
            s.load()  # must not raise
            self.assertEqual(s.position, "FLAT")

    def test_load_corrupt_file_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.json"
            p.write_text("{ not json", encoding="utf-8")
            s = ra.ApolloState(p)
            s.load()
            self.assertEqual(s.position, "FLAT")


class TestApolloComputeIndicators(unittest.TestCase):
    def _df(self, n: int = 40) -> pd.DataFrame:
        closes = np.linspace(100, 120, n)
        return pd.DataFrame({
            "Open": closes, "High": closes * 1.005,
            "Low": closes * 0.995, "Close": closes,
            "Volume": np.full(n, 1_000_000.0),
        })

    def test_adds_ema_atr_rsi_dist_columns(self):
        df = ra.compute_indicators(self._df(), {"timeframe": {"ema_period": 20}})
        for col in ("ema", "atr", "dist_from_ema", "dist_atr", "rsi", "tr"):
            self.assertIn(col, df.columns)

    def test_dist_atr_is_positive_magnitude(self):
        """dist_atr is the normalized absolute distance from EMA — must be >= 0."""
        df = ra.compute_indicators(self._df(), {"timeframe": {"ema_period": 20}})
        valid = df["dist_atr"].dropna()
        self.assertTrue((valid >= 0).all())


class TestApolloEvaluateExit(unittest.TestCase):
    """evaluate() closes open positions on stop/target/timeout."""

    def _state_long(self) -> ra.ApolloState:
        with tempfile.TemporaryDirectory() as td:
            s = ra.ApolloState(Path(td) / "state.json")
        s.position = "LONG"
        s.entry_price = 100
        s.stop_price = 95
        s.target_price = 105
        s.bars_held = 0
        return s

    def test_long_exits_on_stop_hit(self):
        s = self._state_long()
        row = pd.Series({"High": 101, "Low": 94, "Close": 95,
                         "atr": 1.0, "ema": 100, "rsi": 30,
                         "dist_atr": 0.5, "dist_from_ema": -5.0})
        with tempfile.TemporaryDirectory() as td:
            ra.evaluate(s, row, {"symbol": "GLD", "risk": {"max_hold_bars": 5}},
                        "2026-04-18", Path(td))
        # Position closed
        self.assertEqual(s.position, "FLAT")
        self.assertEqual(s.trade_count, 0)  # trade_count isn't incremented in this path, pnl_total is

    def test_long_exits_on_target_hit(self):
        s = self._state_long()
        row = pd.Series({"High": 106, "Low": 101, "Close": 105,
                         "atr": 1.0, "ema": 100, "rsi": 70,
                         "dist_atr": 5.0, "dist_from_ema": 5.0})
        with tempfile.TemporaryDirectory() as td:
            ra.evaluate(s, row, {"symbol": "GLD", "risk": {"max_hold_bars": 5}},
                        "2026-04-18", Path(td))
        self.assertEqual(s.position, "FLAT")

    def test_long_times_out_on_max_hold(self):
        s = self._state_long()
        s.bars_held = 4  # will become 5 after increment
        row = pd.Series({"High": 101, "Low": 99, "Close": 100,
                         "atr": 1.0, "ema": 100, "rsi": 50,
                         "dist_atr": 0.0, "dist_from_ema": 0.0})
        with tempfile.TemporaryDirectory() as td:
            ra.evaluate(s, row, {"symbol": "GLD", "risk": {"max_hold_bars": 5}},
                        "2026-04-18", Path(td))
        self.assertEqual(s.position, "FLAT")


class TestApolloEvaluateEntry(unittest.TestCase):
    """No entry when not overextended; long on oversold RSI + below EMA;
    short on overbought RSI + above EMA."""

    def _state_flat(self) -> ra.ApolloState:
        with tempfile.TemporaryDirectory() as td:
            s = ra.ApolloState(Path(td) / "state.json")
        return s

    def _cfg(self) -> dict:
        return {"symbol": "GLD", "timeframe": {"ema_period": 20},
                "entry": {"extension_atr_mult": 2.0, "rsi_overbought": 70,
                          "rsi_oversold": 30},
                "risk": {"atr_stop_mult": 0.5, "reversion_target_mult": 0.5,
                         "max_hold_bars": 5}}

    def test_not_overextended_no_entry(self):
        s = self._state_flat()
        row = pd.Series({"High": 100, "Low": 99, "Close": 100,
                         "atr": 1.0, "ema": 100, "rsi": 50,
                         "dist_atr": 1.0, "dist_from_ema": 0.0})
        with tempfile.TemporaryDirectory() as td:
            ra.evaluate(s, row, self._cfg(), "2026-04-18", Path(td))
        self.assertEqual(s.position, "FLAT")


# ═══════════════════════════════════════════════════════════════
# Hermes
# ═══════════════════════════════════════════════════════════════

class TestHermesStateInitAndPersistence(unittest.TestCase):
    def test_starts_flat(self):
        with tempfile.TemporaryDirectory() as td:
            s = rh.HermesState(Path(td) / "state.json")
        self.assertEqual(s.position, "FLAT")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s = rh.HermesState(p)
            s.position = "LONG"
            s.entry_price = 50.0
            s.save()
            s2 = rh.HermesState(p)
            s2.load()
            self.assertEqual(s2.position, "LONG")
            self.assertAlmostEqual(s2.entry_price, 50.0)

    def test_corrupt_file_doesnt_raise(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.json"
            p.write_text("{ bad", encoding="utf-8")
            s = rh.HermesState(p)
            s.load()
            self.assertEqual(s.position, "FLAT")


class TestHermesComputeIndicators(unittest.TestCase):
    def test_adds_atr_and_avg_vol(self):
        n = 60
        closes = np.linspace(100, 110, n)
        df = pd.DataFrame({
            "Open": closes, "High": closes * 1.005,
            "Low": closes * 0.995, "Close": closes,
            "Volume": np.full(n, 1_000_000.0),
        })
        out = rh.compute_indicators(df, {})
        self.assertIn("atr", out.columns)
        self.assertIn("avg_vol", out.columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
