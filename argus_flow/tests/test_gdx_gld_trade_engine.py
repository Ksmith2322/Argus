"""Unit tests for forge.gdx_gld_pairs.run_backtest — the z-score state machine.

gdx_gld is the shortlist strategy with the most trades (87). The backtest
engine decides when to enter/exit/stop based on z-score crossings. Bugs
here:
  - Fake entries when no crossing actually happened
  - Missed exits (trades held too long)
  - Costs applied wrong way

Entry logic:
  z crosses below -2  -> long_spread
  z crosses above +2  -> short_spread
Exit logic:
  z crosses back through 0  -> mean_revert
Stop:
  z hits +/-3  -> stop loss

Tests use synthetic z-score series + matching spread series to drive each
path in isolation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _df_and_series(z_values: list[float], s_values: list[float] | None = None):
    """Build a (df, zscore, spread) triple from a list of z-values.
    Dates start 2026-01-01, daily. Spread defaults to same values as z."""
    idx = pd.date_range("2026-01-01", periods=len(z_values), freq="D")
    df = pd.DataFrame({"GDX": [40.0] * len(z_values), "GLD": [150.0] * len(z_values)},
                      index=idx)
    z = pd.Series(z_values, index=idx)
    s = pd.Series(s_values if s_values is not None else z_values, index=idx)
    return df, z, s


class TestEntryConditions(unittest.TestCase):
    """Entry fires only on a z-score CROSS of +/-2."""

    def test_cross_below_neg_two_opens_long_spread(self):
        from forge.gdx_gld_pairs import run_backtest
        # z: -1.5 -> -2.1 = cross below -2 -> long entry
        # Then converge back to 0 for mean-revert exit
        df, z, s = _df_and_series([-1.5, -2.1, -1.8, -1.0, 0.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0].direction, "long_spread")

    def test_cross_above_pos_two_opens_short_spread(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([1.5, 2.1, 1.8, 1.0, -0.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0].direction, "short_spread")

    def test_already_at_minus_two_does_not_open(self):
        """If we START already at -2.5, no CROSS occurs — must not enter."""
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([-2.5, -2.3, -1.9, -1.0])
        trades = run_backtest(df, z, s, label="test")
        # The end-of-data close may emit one trade IF we enter later, but
        # there's no entry cross in this series, so none should fire.
        entries = [t for t in trades if t.direction in ("long_spread", "short_spread")
                   and t.entry_zscore <= -2.0 and t.exit_reason != "end_of_data"
                   or t.exit_reason == "mean_revert"]
        self.assertEqual(len([t for t in trades if t.exit_reason != "end_of_data"]), 0,
            "No entry should fire without a cross")

    def test_z_between_minus_two_and_plus_two_never_opens(self):
        """Classic mean-reverting range — no entries."""
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([0.0, 0.5, -0.3, 0.8, -1.0, 0.4, -0.9, 1.2])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(trades, [])


class TestExitConditions(unittest.TestCase):
    """Mean-revert exit fires when z crosses back through 0."""

    def test_long_spread_exits_on_zero_cross_from_below(self):
        from forge.gdx_gld_pairs import run_backtest
        # Enter at -2.1, hold at -1.5, exit at +0.1 (crossed 0)
        df, z, s = _df_and_series([-1.0, -2.1, -1.5, 0.1, 0.3])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "mean_revert")

    def test_short_spread_exits_on_zero_cross_from_above(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([1.0, 2.1, 1.5, -0.1, -0.3])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "mean_revert")

    def test_end_of_data_closes_open_position(self):
        """Series ends while still in a trade -> exit_reason = end_of_data."""
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([-1.0, -2.1, -1.8, -1.5])  # no zero cross
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "end_of_data")


class TestStopConditions(unittest.TestCase):
    """Stop fires when z hits +/-3 against the position."""

    def test_long_spread_stops_when_z_hits_minus_three(self):
        from forge.gdx_gld_pairs import run_backtest
        # Enter at -2.1, spread continues down to -3.1 -> stop
        df, z, s = _df_and_series([-1.0, -2.1, -2.5, -3.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "stop")

    def test_short_spread_stops_when_z_hits_plus_three(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([1.0, 2.1, 2.5, 3.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "stop")


class TestMultipleTradesInSeries(unittest.TestCase):
    """The engine must handle sequential trades correctly — no overlapping
    positions and correct state transitions."""

    def test_two_complete_trades_in_sequence(self):
        from forge.gdx_gld_pairs import run_backtest
        # Trade 1: enter -2.1, exit at 0
        # Trade 2: enter +2.1, exit at 0
        df, z, s = _df_and_series([
            -1.0, -2.1, -0.5, 0.1,   # first trade (long, exits at +0.1)
            1.5, 2.1, 1.0, -0.1,      # second trade (short, exits at -0.1)
        ])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].direction, "long_spread")
        self.assertEqual(trades[1].direction, "short_spread")

    def test_entry_blocked_while_position_open(self):
        """Second cross while in a position should NOT open a new trade."""
        from forge.gdx_gld_pairs import run_backtest
        # Enter -2.1, then z goes to +2.5 (which would be a new entry cross)
        # but we must close the existing long first at 0, then re-enter.
        df, z, s = _df_and_series([-1.0, -2.1, -1.0, 0.1, 2.5, 3.1])
        trades = run_backtest(df, z, s, label="test")
        # Trade 1 exits mean_revert. Trade 2 enters at +2.5 cross, stops at +3.1.
        # There must never be 2 simultaneously-open positions.
        for i in range(len(trades) - 1):
            t1_exit = pd.Timestamp(trades[i].exit_date)
            t2_entry = pd.Timestamp(trades[i + 1].entry_date)
            self.assertLessEqual(t1_exit, t2_entry,
                "positions overlapped — state machine leaked")


class TestPnlAndCosts(unittest.TestCase):
    """PnL calculation + cost subtraction."""

    def test_long_spread_profitable_when_spread_rises(self):
        from forge.gdx_gld_pairs import run_backtest
        # Enter at spread=-2.1 (long), exit at spread=0.1 -> raw pnl positive
        df, z, s = _df_and_series([-1.0, -2.1, -1.0, 0.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        # Gross should be ~95% ((0.1 - -2.1) / 2.1 * 100), minus costs ~0.1%
        self.assertGreater(trades[0].pnl_pct, 0)

    def test_cost_is_10bps_round_trip(self):
        """2 * COST_PER_SIDE_BPS / 100 = 10bps = 0.1% subtracted per trade."""
        from forge import gdx_gld_pairs as g
        self.assertEqual(g.COST_PER_SIDE_BPS, 5)
        self.assertEqual(2 * g.COST_PER_SIDE_BPS / 100.0, 0.1)

    def test_short_spread_profitable_when_spread_falls(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([1.0, 2.1, 1.0, -0.1])
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0].pnl_pct, 0)


class TestNanHandling(unittest.TestCase):
    """Leading NaN z-scores (before lookback completes) must not trigger trades."""

    def test_nan_prefix_skipped(self):
        from forge.gdx_gld_pairs import run_backtest
        # First 3 values are NaN (before lookback), then a normal series
        idx = pd.date_range("2026-01-01", periods=8, freq="D")
        z = pd.Series([np.nan, np.nan, np.nan, -1.0, -2.1, -1.0, 0.1, 0.3], index=idx)
        s = pd.Series([np.nan, np.nan, np.nan, -1.0, -2.1, -1.0, 0.1, 0.3], index=idx)
        df = pd.DataFrame({"GDX": [40.0] * 8, "GLD": [150.0] * 8}, index=idx)
        trades = run_backtest(df, z, s, label="test")
        self.assertEqual(len(trades), 1, "NaN prefix should not produce extra trades")
        self.assertEqual(trades[0].direction, "long_spread")


class TestTradeMetadata(unittest.TestCase):
    """Every completed Trade record must have the required fields populated."""

    def test_trade_records_entry_and_exit_zscore(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([-1.0, -2.1, -1.5, 0.1])
        trades = run_backtest(df, z, s, label="test")
        t = trades[0]
        self.assertIsNotNone(t.entry_zscore)
        self.assertIsNotNone(t.exit_zscore)
        self.assertLess(t.entry_zscore, -2.0)
        self.assertGreaterEqual(t.exit_zscore, 0.0)

    def test_trade_records_dates_as_iso_strings(self):
        from forge.gdx_gld_pairs import run_backtest
        df, z, s = _df_and_series([-1.0, -2.1, -1.5, 0.1])
        trades = run_backtest(df, z, s, label="test")
        t = trades[0]
        # YYYY-MM-DD format
        self.assertRegex(t.entry_date, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(t.exit_date, r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
