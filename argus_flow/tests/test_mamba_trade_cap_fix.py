"""Regression tests for the 2026-04-19 external audit findings.

1. Mamba MAX_TRADES_PER_DAY bug: the counter was inside the ticker loop,
   so NQ and YM each got an independent daily cap — doubling the effective
   trades per day from the rulebook's stated limit. Counter is now
   strategy-wide.

2. /api/strategy_performance live_confidence split: a 100%-backfill
   strategy must not inflate the fleet live-confidence headline. The
   live_only gate requires n_live >= 10, which today is zero for every
   strategy — the honest signal.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestMambaTradeCapIsStrategyWide(unittest.TestCase):
    """Audit finding #4: daily_trade_count must live OUTSIDE the ticker
    loop so a cap of MAX_TRADES_PER_DAY=2 is a true daily cap, not 2×N
    tickers."""

    def test_daily_counter_declared_outside_ticker_loop(self):
        """Source-level check: the counter must be initialized before
        `for ticker, df in datasets.items():`."""
        src = (Path(_REPO) / "forge" / "mamba" / "runner.py").read_text(encoding="utf-8")
        lines = src.splitlines()

        # Find the ticker loop start
        ticker_loop_line = None
        for i, line in enumerate(lines):
            if "for ticker, df in datasets.items()" in line:
                ticker_loop_line = i
                break
        self.assertIsNotNone(ticker_loop_line, "ticker loop not found in runner")

        # Find the daily_trade_count declaration(s)
        counter_lines = [i for i, line in enumerate(lines)
                         if "daily_trade_count" in line and "=" in line and "defaultdict" in line]
        self.assertEqual(len(counter_lines), 1,
                         f"expected exactly one daily_trade_count declaration, got {len(counter_lines)}")

        # It must come BEFORE the ticker loop
        self.assertLess(counter_lines[0], ticker_loop_line,
                        "daily_trade_count must be declared before the ticker loop so the "
                        "cap is strategy-wide, not per-ticker")

    def test_comment_explains_the_fix(self):
        """The comment tagged '2026-04-19' above the counter must be
        preserved so a future reader knows why it's hoisted."""
        src = (Path(_REPO) / "forge" / "mamba" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("per-day total across instruments", src)
        self.assertIn("external audit", src)


class TestLiveConfidenceSplitInDashboard(unittest.TestCase):
    """Audit finding #6: fleet headline must not be inflated by
    backfill-only strategies. The endpoint now emits live_confidence
    and backfill_dominant_warning."""

    def _client(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi testclient unavailable")
        from ops.dashboard import app
        return TestClient(app)

    def test_endpoint_returns_live_confidence_block(self):
        c = self._client()
        data = c.get("/api/strategy_performance").json()
        self.assertIn("live_confidence", data)
        lc = data["live_confidence"]
        self.assertIn("strategies_at_live_sanity_bar", lc)
        self.assertIn("strategies_passing_90pct_live_only", lc)
        self.assertIn("note", lc)

    def test_live_only_counts_require_n_live_gte_10(self):
        """No strategy has n_live >= 10 yet. The count must therefore be 0."""
        c = self._client()
        data = c.get("/api/strategy_performance").json()
        lc = data["live_confidence"]
        # All current strategies are 100% backfill → live_only counts must be 0
        self.assertEqual(lc["strategies_at_live_sanity_bar"], 0)
        self.assertEqual(lc["strategies_passing_90pct_live_only"], 0)

    def test_backfill_warning_surfaces_when_backfill_strategies_pass_90pct(self):
        """Tori hits P(exp>0)=1.0 on 100% backfill — this should trigger the
        warning, naming Tori (and any other backfill-only 90%+ strategies)."""
        c = self._client()
        data = c.get("/api/strategy_performance").json()
        warning = data.get("backfill_dominant_warning")
        # If any strategy is backfill-only with P>=0.90, warning must exist
        self.assertIsNotNone(warning, "backfill-only 90% strategies exist but no warning fired")
        self.assertIn("backfill", warning.lower())

    def test_warning_is_none_when_no_backfill_only_90pct_strategies(self):
        """Under stubbed empty artifacts, no backfill-only strategy qualifies."""
        import helio.strategy_confidence as sc
        import helio.fleet_state as fs
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(sc, "ARTIFACT_DIR", _P(td)), \
                 mock.patch.object(fs, "_canonical_fills_by_strategy", return_value={}):
                c = self._client()
                data = c.get("/api/strategy_performance").json()
        self.assertIsNone(data.get("backfill_dominant_warning"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
