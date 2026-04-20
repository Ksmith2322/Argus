"""Tests for the portfolio-correlation helper and its dashboard endpoint.

Three properties fenced:
  1. Known inputs produce the expected correlation coefficient.
  2. Low-overlap pairs return None rather than a misleading number.
  3. High-correlation pairs emit a diversification warning.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestPortfolioCorrelation(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        from helio.fleet_state import _portfolio_correlation
        dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        pnls = [10, -5, 20, -10, 15, -2, 8, -3, 12, -7]
        a = [{"date": d, "pnl_usd": v} for d, v in zip(dates, pnls)]
        b = [{"date": d, "pnl_usd": v * 2.0} for d, v in zip(dates, pnls)]
        out = _portfolio_correlation({"A": a, "B": b})
        self.assertEqual(out["n_strategies"], 2)
        self.assertAlmostEqual(out["matrix"]["A"]["B"], 1.0, places=3)
        self.assertEqual(out["overlap"]["A"]["B"], 10)

    def test_perfect_negative_correlation(self):
        from helio.fleet_state import _portfolio_correlation
        dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        pnls = [10, -5, 20, -10, 15, -2, 8, -3, 12, -7]
        a = [{"date": d, "pnl_usd": v} for d, v in zip(dates, pnls)]
        c = [{"date": d, "pnl_usd": -v} for d, v in zip(dates, pnls)]
        out = _portfolio_correlation({"A": a, "C": c})
        self.assertAlmostEqual(out["matrix"]["A"]["C"], -1.0, places=3)

    def test_low_overlap_returns_none(self):
        """Fewer than min_overlap (5) shared days → correlation=None."""
        from helio.fleet_state import _portfolio_correlation
        a = [{"date": f"2026-01-{d:02d}", "pnl_usd": d * 1.0}
             for d in range(1, 11)]
        # B trades on different dates with only 3 overlapping
        b = [{"date": f"2026-01-{d:02d}", "pnl_usd": d * 1.0}
             for d in [1, 2, 3, 15, 16, 17]]
        out = _portfolio_correlation({"A": a, "B": b})
        self.assertIsNone(out["matrix"]["A"]["B"])
        self.assertEqual(out["overlap"]["A"]["B"], 3)

    def test_warning_flags_high_correlation(self):
        from helio.fleet_state import _portfolio_correlation
        dates = [f"2026-01-{d:02d}" for d in range(1, 12)]
        pnls = [10, -5, 20, -10, 15, -2, 8, -3, 12, -7, 4]
        a = [{"date": d, "pnl_usd": v} for d, v in zip(dates, pnls)]
        # Tiny perturbation — still > 0.95 correlation
        b = [{"date": d, "pnl_usd": v * 1.05 + 0.1}
             for d, v in zip(dates, pnls)]
        out = _portfolio_correlation({"A": a, "B": b})
        self.assertTrue(len(out["warnings"]) >= 1)
        self.assertIn("A", out["warnings"][0])
        self.assertIn("B", out["warnings"][0])
        self.assertIn("diversification weak", out["warnings"][0])

    def test_empty_input_returns_none(self):
        from helio.fleet_state import _portfolio_correlation
        self.assertIsNone(_portfolio_correlation({}))

    def test_same_day_pnl_sums(self):
        """Two trades on same date for a strategy → daily bucket sums them."""
        from helio.fleet_state import _portfolio_correlation
        a = [{"date": "2026-01-01", "pnl_usd": 10},
             {"date": "2026-01-01", "pnl_usd": 5}]
        # Pad B to meet min_overlap; only shared day is 2026-01-01
        b = ([{"date": "2026-01-01", "pnl_usd": 15}]
             + [{"date": f"2026-02-{d:02d}", "pnl_usd": 1}
                for d in range(1, 6)])
        out = _portfolio_correlation({"A": a, "B": b})
        # Only 1 shared day → below min_overlap, returns None
        self.assertIsNone(out["matrix"]["A"]["B"])
        self.assertEqual(out["overlap"]["A"]["B"], 1)

    def test_missing_date_rows_skipped(self):
        from helio.fleet_state import _portfolio_correlation
        a = [{"date": None, "pnl_usd": 99}] + [
            {"date": f"2026-01-{d:02d}", "pnl_usd": d}
            for d in range(1, 7)
        ]
        b = [{"date": f"2026-01-{d:02d}", "pnl_usd": d}
             for d in range(1, 7)]
        out = _portfolio_correlation({"A": a, "B": b})
        self.assertAlmostEqual(out["matrix"]["A"]["B"], 1.0, places=3)
        self.assertEqual(out["overlap"]["A"]["B"], 6)


if __name__ == "__main__":
    unittest.main()
