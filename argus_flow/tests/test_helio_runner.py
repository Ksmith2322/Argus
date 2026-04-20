"""Unit tests for helio.runner — the swing-trading runner (the original
one before Apollo/Hermes were forked out).

Coverage:
  - SwingState: initial values + atomic save/load round-trip
  - compute_indicators: EMA / ATR / range_expanded / vol_surge / near_ema
  - check_entry: trend + trigger (type A or type B) combinations
  - manage_position: breakeven move, trail activation, time-stop
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

from helio import runner as hr  # noqa: E402


class TestSwingStateInit(unittest.TestCase):
    def test_starts_flat(self):
        with tempfile.TemporaryDirectory() as td:
            s = hr.SwingState(Path(td) / "state.json")
        self.assertEqual(s.position, "FLAT")
        self.assertEqual(s.entry_price, 0.0)
        self.assertEqual(s.trade_count, 0)

    def test_lowest_initialized_high(self):
        """lowest=999999 so min() during shorts works correctly on first bar."""
        with tempfile.TemporaryDirectory() as td:
            s = hr.SwingState(Path(td) / "state.json")
        self.assertGreater(s.lowest, 1000)


class TestSwingStateAtomicPersistence(unittest.TestCase):
    """save() writes to a .tmp then replaces — an interrupted write must not
    corrupt the state file."""

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            s = hr.SwingState(path)
            s.position = "LONG"
            s.entry_price = 100.5
            s.trade_count = 3
            s.save()
            self.assertTrue(path.exists())
            # Load into fresh state
            s2 = hr.SwingState(path)
            s2.load()
        self.assertEqual(s2.position, "LONG")
        self.assertEqual(s2.entry_price, 100.5)
        self.assertEqual(s2.trade_count, 3)

    def test_load_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            s = hr.SwingState(Path(td) / "missing.json")
            s.load()  # must not raise
        self.assertEqual(s.position, "FLAT")

    def test_load_corrupt_file_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "corrupt.json"
            path.write_text("{ not json", encoding="utf-8")
            s = hr.SwingState(path)
            s.load()  # must not raise
        self.assertEqual(s.position, "FLAT")

    def test_save_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "a" / "b" / "state.json"
            s = hr.SwingState(nested)
            s.save()
            self.assertTrue(nested.exists())


class TestComputeIndicators(unittest.TestCase):
    """compute_indicators adds ema, atr, range_pct, range_expanded, vol_surge, near_ema."""

    def _bars(self, n: int = 60) -> pd.DataFrame:
        closes = np.linspace(100, 120, n)
        return pd.DataFrame({
            "Open": closes, "High": closes * 1.005,
            "Low": closes * 0.995, "Close": closes,
            "Volume": np.full(n, 1_000_000.0),
        })

    def _default_cfg(self) -> dict:
        return {
            "timeframe": {"ema_period": 20, "ema_slope_lookback": 3,
                          "ema_slope_threshold": 0.001},
            "entry": {"range_expansion_mult": 1.2, "volume_surge_mult": 1.2,
                      "ema_pullback_atr_mult": 0.5, "ema_slope_pullback_threshold": 0.002},
            "risk": {"atr_period": 14},
        }

    def test_all_expected_columns_added(self):
        df = hr.compute_indicators(self._bars(), self._default_cfg())
        for col in ("ema", "ema_slope", "tr", "atr", "range_pct",
                    "avg_range", "range_expanded", "vol_surge", "near_ema"):
            self.assertIn(col, df.columns, f"missing column: {col}")

    def test_range_expanded_flags_outlier_bars(self):
        """A bar with 3x normal range must be flagged range_expanded=True."""
        df = self._bars(60)
        df.loc[df.index[-1], "High"] = df.loc[df.index[-1], "Close"] * 1.10
        df.loc[df.index[-1], "Low"] = df.loc[df.index[-1], "Close"] * 0.90
        result = hr.compute_indicators(df, self._default_cfg())
        self.assertTrue(bool(result["range_expanded"].iloc[-1]))

    def test_vol_surge_flags_volume_spike(self):
        df = self._bars(60)
        df.loc[df.index[-1], "Volume"] = 3_000_000  # 3x normal
        result = hr.compute_indicators(df, self._default_cfg())
        self.assertTrue(bool(result["vol_surge"].iloc[-1]))


class TestCheckEntry(unittest.TestCase):
    """check_entry returns LONG/SHORT or None based on trend + trigger."""

    def _row(self, **overrides) -> dict:
        base = {
            "Close": 105.0, "ema": 100.0, "ema_slope": 0.005,
            "range_expanded": False, "vol_surge": False,
            "near_ema": False,
        }
        base.update(overrides)
        return base

    def _cfg(self) -> dict:
        return {
            "timeframe": {"ema_slope_threshold": 0.001},
            "entry": {"ema_slope_pullback_threshold": 0.002},
        }

    def test_no_trend_no_trigger_returns_none(self):
        row = self._row(ema_slope=0.0, Close=99.5)  # below EMA
        self.assertIsNone(hr.check_entry(row, self._cfg()))

    def test_long_with_type_a_trigger(self):
        """Trending up + range_expanded + vol_surge -> LONG."""
        row = self._row(range_expanded=True, vol_surge=True)
        self.assertEqual(hr.check_entry(row, self._cfg()), "LONG")

    def test_short_with_type_a_trigger(self):
        """Trending down + range_expanded + vol_surge -> SHORT."""
        row = self._row(Close=95, ema=100, ema_slope=-0.005,
                        range_expanded=True, vol_surge=True)
        self.assertEqual(hr.check_entry(row, self._cfg()), "SHORT")

    def test_long_with_type_b_pullback(self):
        """Trending + near_ema + strong slope -> LONG via type B."""
        row = self._row(near_ema=True, ema_slope=0.005)
        self.assertEqual(hr.check_entry(row, self._cfg()), "LONG")

    def test_trend_alone_not_enough(self):
        """Above EMA with positive slope but no trigger (no expansion,
        no pullback) -> no entry."""
        row = self._row()
        self.assertIsNone(hr.check_entry(row, self._cfg()))

    def test_range_expand_without_vol_surge_no_type_a(self):
        """Type A requires BOTH range_expanded AND vol_surge."""
        row = self._row(range_expanded=True, vol_surge=False)
        # Falls through — no type A. Check type B gate: near_ema=False.
        self.assertIsNone(hr.check_entry(row, self._cfg()))


class TestManagePosition(unittest.TestCase):
    """manage_position updates stops/trail and returns an exit reason when
    price hits a stop or max_hold."""

    def _state_long(self, entry: float = 100, stop: float = 95) -> hr.SwingState:
        with tempfile.TemporaryDirectory() as td:
            s = hr.SwingState(Path(td) / "state.json")
        s.position = "LONG"
        s.entry_price = entry
        s.initial_stop = stop
        s.stop_price = stop
        s.trail_stop = stop
        s.highest = entry
        s.bars_held = 0
        return s

    def _cfg(self) -> dict:
        return {"risk": {"atr_trail_mult": 1.0, "max_hold_days": 5}}

    def test_long_exits_on_stop_hit(self):
        s = self._state_long()
        row = {"High": 102, "Low": 94, "Close": 95, "atr": 1.0}
        reason = hr.manage_position(s, row, self._cfg())
        self.assertEqual(reason, "stop")

    def test_long_times_out_on_max_hold(self):
        s = self._state_long()
        s.bars_held = 4  # Will become 5 after increment
        row = {"High": 101, "Low": 99, "Close": 100, "atr": 1.0}
        reason = hr.manage_position(s, row, self._cfg())
        self.assertEqual(reason, "timeout")

    def test_breakeven_move_after_1r(self):
        """After gain of 1R (risk distance), trail_stop moves to entry."""
        s = self._state_long(entry=100, stop=95)  # risk=5
        row = {"High": 105.5, "Low": 104, "Close": 105, "atr": 1.0}
        hr.manage_position(s, row, self._cfg())
        # Trail stop should now be at or above entry (100)
        self.assertGreaterEqual(s.trail_stop, 100)

    def test_no_exit_when_still_running(self):
        s = self._state_long()
        row = {"High": 101, "Low": 98, "Close": 100, "atr": 1.0}
        reason = hr.manage_position(s, row, self._cfg())
        self.assertIsNone(reason)
        self.assertEqual(s.bars_held, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
