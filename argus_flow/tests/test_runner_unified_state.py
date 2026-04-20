"""Unit tests for argus_flow.runner_unified state-machine logic.

runner_unified is 4641 LOC and owns the 3 Argus paper strategies. Full
integration tests live in test_fx_system, chaos_test, etc. This file
covers the pure decision functions that don't need a live IB connection
or yfinance data:

  - State (the per-instrument state dataclass) — initial values + flags
  - _evaluate_validity — priority-ordered taxonomy that decides whether a
    trade counts toward performance stats
  - _risk_usd_for_size — risk math (pip-based and point-based)

These are the functions whose silent drift leads to wrong performance
numbers (promotion/kill happening at the wrong trade count).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from argus_flow.runner_unified import State, InstrumentRunner  # noqa: E402


class TestStateInitialValues(unittest.TestCase):
    """State starts FLAT with zero PnL and clean validity flags."""

    def test_starts_flat(self):
        with tempfile.TemporaryDirectory() as td:
            s = State(Path(td) / "state.json")
        self.assertEqual(s.position, "FLAT")

    def test_entry_time_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            s = State(Path(td) / "state.json")
        self.assertIsNone(s.entry_time)

    def test_pnl_starts_zero(self):
        with tempfile.TemporaryDirectory() as td:
            s = State(Path(td) / "state.json")
        self.assertEqual(s.pnl_pips, 0.0)
        self.assertEqual(s.pnl_points, 0.0)
        self.assertEqual(s.pnl_usd, 0.0)

    def test_validity_flags_clean_at_start(self):
        """restored_this_session + had_zero_stops must start False —
        otherwise the first trade would be auto-invalidated."""
        with tempfile.TemporaryDirectory() as td:
            s = State(Path(td) / "state.json")
        self.assertFalse(s.restored_this_session)
        self.assertFalse(s.had_zero_stops)

    def test_state_file_parent_dir_created(self):
        """State should materialize its parent dir so the runner can
        persist to it later without an OSError."""
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "nested" / "path" / "state.json"
            s = State(nested)
            self.assertTrue(nested.parent.exists())


class TestEvaluateValidity(unittest.TestCase):
    """_evaluate_validity prioritizes invalidity reasons in a specific order:
    restored > zero_stops > reconnect > repeated_errors.

    Critical: a bug in priority here would mislabel trades. Tests construct
    a thin stand-in for the runner (since the real __init__ needs IB config)
    and exercise each branch."""

    def _make_runner_stub(self, *, restored: bool = False, zero_stops: bool = False,
                          reconnected: bool = False, consecutive_errors: int = 0) -> object:
        """Build a minimal object that has the attrs _evaluate_validity needs."""
        runner = mock.MagicMock(spec=InstrumentRunner)
        runner._reconnected = reconnected
        runner._consecutive_errors = consecutive_errors
        state = mock.MagicMock()
        state.restored_this_session = restored
        state.had_zero_stops = zero_stops
        runner.state = state
        # Bind the real method to our stub
        runner._evaluate_validity = InstrumentRunner._evaluate_validity.__get__(runner)
        return runner

    def test_clean_trade_is_valid(self):
        runner = self._make_runner_stub()
        valid, reason = runner._evaluate_validity()
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_restored_state_invalidates(self):
        runner = self._make_runner_stub(restored=True)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "restored_from_file")

    def test_zero_stops_invalidates(self):
        runner = self._make_runner_stub(zero_stops=True)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "zero_stops_or_timeout_missing")

    def test_reconnect_invalidates(self):
        runner = self._make_runner_stub(reconnected=True)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "reconnect_during_session")

    def test_repeated_errors_invalidate_at_three(self):
        runner = self._make_runner_stub(consecutive_errors=3)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "repeated_tick_failures")

    def test_repeated_errors_below_three_still_valid(self):
        runner = self._make_runner_stub(consecutive_errors=2)
        valid, reason = runner._evaluate_validity()
        self.assertTrue(valid)

    def test_priority_restored_over_zero_stops(self):
        """When multiple invalidity reasons apply, restored wins (listed first)."""
        runner = self._make_runner_stub(restored=True, zero_stops=True, reconnected=True)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "restored_from_file")

    def test_priority_zero_stops_over_reconnect(self):
        runner = self._make_runner_stub(zero_stops=True, reconnected=True,
                                         consecutive_errors=5)
        valid, reason = runner._evaluate_validity()
        self.assertFalse(valid)
        self.assertEqual(reason, "zero_stops_or_timeout_missing")


class TestRiskUsdForSize(unittest.TestCase):
    """_risk_usd_for_size computes USD risk given entry/stop/size.

    Pip path:   risk = |entry - stop| / pip_size * pip_value_per_unit * size
    Point path: risk = |entry - stop| * multiplier * size
    """

    def _make_pip_runner(self, pip_value_per_unit: float = 0.01) -> object:
        runner = mock.MagicMock(spec=InstrumentRunner)
        runner.uses_pips = True
        runner.pip_size = 0.0001
        runner._pip_value_per_unit_usd = mock.MagicMock(return_value=pip_value_per_unit)
        runner._risk_usd_for_size = InstrumentRunner._risk_usd_for_size.__get__(runner)
        return runner

    def _make_future_runner(self, multiplier: float = 5.0) -> object:
        runner = mock.MagicMock(spec=InstrumentRunner)
        runner.uses_pips = False
        runner.multiplier = multiplier
        runner._risk_usd_for_size = InstrumentRunner._risk_usd_for_size.__get__(runner)
        return runner

    def test_pip_risk_math_standard_case(self):
        """GBPUSD, entry 1.2600, stop 1.2580 = 20 pip stop.
        Each pip worth $0.10 per unit, size 10000 -> risk = 20 * 0.10 * 10000 = $20000.
        Wait, no — pip_value_per_unit is typically $0.0001 for small accounts.
        Use $0.0001 per unit, size 10000 -> risk = 20 * 0.0001 * 10000 = $20."""
        runner = self._make_pip_runner(pip_value_per_unit=0.0001)
        risk = runner._risk_usd_for_size(1.2600, 1.2580, 10000)
        self.assertAlmostEqual(risk, 20.0, places=2)

    def test_zero_size_returns_zero_risk(self):
        runner = self._make_pip_runner()
        self.assertEqual(runner._risk_usd_for_size(1.26, 1.258, 0), 0.0)

    def test_negative_size_returns_zero(self):
        """Defensive: negative size (shouldn't happen but guard anyway) returns 0."""
        runner = self._make_pip_runner()
        self.assertEqual(runner._risk_usd_for_size(1.26, 1.258, -5), 0.0)

    def test_point_based_risk_for_futures(self):
        """MES/MNQ use points × multiplier. Entry 5000, stop 4990 = 10 pts.
        multiplier $5, size 2 contracts -> risk = 10 * 5 * 2 = $100."""
        runner = self._make_future_runner(multiplier=5.0)
        risk = runner._risk_usd_for_size(5000.0, 4990.0, 2)
        self.assertAlmostEqual(risk, 100.0)

    def test_risk_uses_absolute_stop_distance(self):
        """Long (entry > stop) and short (entry < stop) must yield the
        same risk for identical distance."""
        runner = self._make_pip_runner(pip_value_per_unit=0.0001)
        long_risk = runner._risk_usd_for_size(1.2600, 1.2580, 10000)  # long
        short_risk = runner._risk_usd_for_size(1.2580, 1.2600, 10000)  # short
        self.assertAlmostEqual(long_risk, short_risk, places=4)


class TestSymbolLabelsMatchCanonicalPattern(unittest.TestCase):
    """Every Argus config defines a symbol. Our dual-write uses
    `argus_{symbol.lower()}` — the canonical_fills backfill SPECS list
    uses the same. This test pins the convention so a rename would be
    visible."""

    def test_expected_argus_labels(self):
        from helio.canonical_fills import backfill_from_trade_csvs
        import inspect
        src = inspect.getsource(backfill_from_trade_csvs)
        for label in ("argus_usdjpy", "argus_gbpusd", "argus_cadjpy"):
            self.assertIn(label, src,
                f"{label} missing from backfill SPECS — reconciliation "
                f"would silently split on a rename")


if __name__ == "__main__":
    unittest.main(verbosity=2)
