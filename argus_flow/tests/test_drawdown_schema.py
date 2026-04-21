"""Tests for the Drawdown pydantic model and _max_drawdown helper.

Fenced properties:
  1. _max_drawdown returns None on empty input.
  2. Monotonically rising pnls → zero drawdown.
  3. A dip after a peak yields the expected magnitude + pct.
  4. Without starting_equity, pct is peak-relative.
  5. With starting_equity, pct is anchored to capital.
  6. Drawdown schema validates a minimal-but-complete artifact.
  7. Drawdown schema rejects a negative max_drawdown_usd (field has ge=0).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestMaxDrawdownHelper(unittest.TestCase):
    def test_helper_empty_returns_none(self):
        from helio.fleet_state import _max_drawdown
        self.assertIsNone(_max_drawdown([]))

    def test_helper_all_wins(self):
        """Monotonically rising equity → max_dd = 0."""
        from helio.fleet_state import _max_drawdown
        out = _max_drawdown([100.0, 50.0, 200.0, 25.0])
        self.assertIsNotNone(out)
        self.assertEqual(out["max_drawdown_usd"], 0.0)
        self.assertEqual(out["max_drawdown_pct"], 0.0)

    def test_helper_simple_dd(self):
        """pnls=[100, -50, 30, -20]:
          equity path: 0 -> 100(peak) -> 50 -> 80 -> 60
          DD1 after first peak: 100 - 50 = 50 (trough at 50 before a new high)
          No new peak surpasses 100 → max_dd stays 50.
        """
        from helio.fleet_state import _max_drawdown
        out = _max_drawdown([100.0, -50.0, 30.0, -20.0])
        self.assertIsNotNone(out)
        self.assertEqual(out["max_drawdown_usd"], 50.0)
        self.assertEqual(out["peak_equity_usd"], 100.0)
        self.assertEqual(out["trough_equity_usd"], 50.0)

    def test_helper_peak_relative_pct(self):
        """pnls=[100, -20] with no starting_equity:
          equity: 0 -> 100(peak) -> 80
          dd_usd = 20, pct basis = peak_relative → 20/100 * 100 = 20.0
        """
        from helio.fleet_state import _max_drawdown
        out = _max_drawdown([100.0, -20.0])
        self.assertIsNotNone(out)
        self.assertEqual(out["max_drawdown_usd"], 20.0)
        self.assertEqual(out["max_drawdown_pct"], 20.0)
        self.assertEqual(out["pct_basis"], "peak_relative")

    def test_helper_starting_equity_basis(self):
        """pnls=[100, -20] with starting_equity_usd=1000:
          equity: 1000 -> 1100(peak) -> 1080
          dd_usd = 20, pct basis = starting_equity → 20/1000 * 100 = 2.0
        """
        from helio.fleet_state import _max_drawdown
        out = _max_drawdown([100.0, -20.0], starting_equity_usd=1000.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["max_drawdown_usd"], 20.0)
        self.assertEqual(out["max_drawdown_pct"], 2.0)
        self.assertEqual(out["pct_basis"], "starting_equity")


def _minimal_artifact_dict(drawdown_block: dict | None = None) -> dict:
    """Build a minimal valid StrategyConfidenceArtifact dict with
    n_total >= 10 (so p_expectancy_positive is allowed nonzero if set)."""
    data = {
        "schema_version": 1,
        "strategy": "test_strategy",
        "source": "unit_test",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_total": 10,
        "n_live": 0,
        "n_paper": 10,
        "evidence_bar": "sanity",
    }
    if drawdown_block is not None:
        data["drawdown"] = drawdown_block
    return data


class TestDrawdownSchema(unittest.TestCase):
    def test_schema_accepts_drawdown(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        dd = {
            "max_drawdown_usd": 50.0,
            "max_drawdown_pct": 5.0,
            "peak_equity_usd": 1000.0,
            "trough_equity_usd": 950.0,
            "pct_basis": "starting_equity",
        }
        artifact = StrategyConfidenceArtifact.model_validate(
            _minimal_artifact_dict(drawdown_block=dd)
        )
        self.assertIsNotNone(artifact.drawdown)
        self.assertEqual(artifact.drawdown.max_drawdown_usd, 50.0)
        self.assertEqual(artifact.drawdown.pct_basis, "starting_equity")

    def test_schema_accepts_peak_relative_basis(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        dd = {
            "max_drawdown_usd": 20.0,
            "max_drawdown_pct": 20.0,
            "peak_equity_usd": 100.0,
            "trough_equity_usd": 80.0,
            "pct_basis": "peak_relative",
        }
        artifact = StrategyConfidenceArtifact.model_validate(
            _minimal_artifact_dict(drawdown_block=dd)
        )
        self.assertEqual(artifact.drawdown.pct_basis, "peak_relative")

    def test_schema_rejects_negative_drawdown(self):
        """max_drawdown_usd has ge=0 — negative value must raise."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        dd = {
            "max_drawdown_usd": -10.0,
            "max_drawdown_pct": 1.0,
            "peak_equity_usd": 1000.0,
            "trough_equity_usd": 990.0,
            "pct_basis": "starting_equity",
        }
        with self.assertRaises(ValidationError):
            StrategyConfidenceArtifact.model_validate(
                _minimal_artifact_dict(drawdown_block=dd)
            )

    def test_schema_rejects_negative_drawdown_pct(self):
        """max_drawdown_pct has ge=0 — negative value must also raise."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        dd = {
            "max_drawdown_usd": 10.0,
            "max_drawdown_pct": -1.0,
            "peak_equity_usd": 1000.0,
            "trough_equity_usd": 990.0,
            "pct_basis": "starting_equity",
        }
        with self.assertRaises(ValidationError):
            StrategyConfidenceArtifact.model_validate(
                _minimal_artifact_dict(drawdown_block=dd)
            )


if __name__ == "__main__":
    unittest.main()
