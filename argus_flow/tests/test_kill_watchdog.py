"""Unit tests for helio.kill_watchdog — auto-kill rule evaluation.

The watchdog decides whether a strategy is flagged as KILL_CANDIDATE. That
flag drives the operator's kill-discipline review and feeds the morning
brief. A bug here either:
  - Under-flags: loses money on a broken strategy we should have killed.
  - Over-flags: retires a working strategy after a bad streak.

Both are expensive. These tests pin the thresholds and boundaries so a
refactor can't silently alter them.

Universal rules (apply to every strategy):
  - pf_below_1_on_30plus_trades   : trades >= 30 AND 0 < PF < 1.0
  - drawdown_over_2pct_anchor...  : trades >= 20 AND pnl < -0.02 * anchor

Per-card rules (each strategy):
  - forge_gdx_gld     : trades >= 20 AND 0 < PF < 1.0
  - forge_gld_pm_long : trades >= 30 AND 0 < PF < 1.0
  - forge_wick_gbpusd : trades >= 10 AND 0 < PF < 1.0
  - argus_*           : trades >= 30 AND 0 < PF < 0.9  (harsher threshold)

Signal drift: 3 consecutive severe drift tracker runs -> drift_warning.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import kill_watchdog as kw  # noqa: E402


DEFAULT_CTX = {"anchor_usd": 10000}


class TestUniversalPfRule(unittest.TestCase):
    """pf_below_1_on_30plus_trades — fires AT 30 trades with PF < 1.0."""

    def test_fires_at_30_trades_pf_0_90(self):
        stats = {"trades": 30, "profit_factor": 0.90, "pnl_usd": -50}
        hits = kw._universal_rules("any_label", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertIn("pf_below_1_on_30plus_trades", rules)

    def test_does_not_fire_at_29_trades(self):
        """Exactly one trade below threshold -> no kill_candidate from this rule."""
        stats = {"trades": 29, "profit_factor": 0.50, "pnl_usd": -50}
        hits = kw._universal_rules("any_label", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertNotIn("pf_below_1_on_30plus_trades", rules)

    def test_does_not_fire_at_pf_exactly_1_00(self):
        """Edge: PF == 1.0 is NOT below threshold (rule uses < 1.0)."""
        stats = {"trades": 30, "profit_factor": 1.00, "pnl_usd": 0}
        hits = kw._universal_rules("any_label", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertNotIn("pf_below_1_on_30plus_trades", rules)

    def test_does_not_fire_when_pf_is_zero(self):
        """PF == 0 means no winners yet; rule requires 0 < PF < 1.0 because
        PF=0 normally reflects 'not enough data' not 'broken edge'."""
        stats = {"trades": 50, "profit_factor": 0.0, "pnl_usd": 0}
        hits = kw._universal_rules("any_label", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertNotIn("pf_below_1_on_30plus_trades", rules)


class TestUniversalDrawdownRule(unittest.TestCase):
    """drawdown_over_2pct_anchor_on_20plus — fires at 20 trades AND pnl < -2% anchor."""

    def test_fires_at_20_trades_with_loss_over_2pct(self):
        # anchor 10K; -2.01% = -$201
        stats = {"trades": 20, "profit_factor": 0.8, "pnl_usd": -201}
        hits = kw._universal_rules("any_label", stats, {"anchor_usd": 10000})
        rules = [h["rule"] for h in hits]
        self.assertIn("drawdown_over_2pct_anchor_on_20plus", rules)

    def test_does_not_fire_below_20_trades(self):
        stats = {"trades": 19, "profit_factor": 0.8, "pnl_usd": -500}
        hits = kw._universal_rules("any_label", stats, {"anchor_usd": 10000})
        rules = [h["rule"] for h in hits]
        self.assertNotIn("drawdown_over_2pct_anchor_on_20plus", rules)

    def test_does_not_fire_at_exactly_2pct_loss(self):
        """pnl == -0.02 * anchor is the boundary (rule uses strict <)."""
        stats = {"trades": 20, "profit_factor": 0.8, "pnl_usd": -200}
        hits = kw._universal_rules("any_label", stats, {"anchor_usd": 10000})
        rules = [h["rule"] for h in hits]
        self.assertNotIn("drawdown_over_2pct_anchor_on_20plus", rules)

    def test_scales_with_anchor(self):
        """On a $1M anchor, -$201 is nowhere near 2%. Must NOT fire."""
        stats = {"trades": 20, "profit_factor": 0.8, "pnl_usd": -201}
        hits = kw._universal_rules("any_label", stats, {"anchor_usd": 1_000_000})
        rules = [h["rule"] for h in hits]
        self.assertNotIn("drawdown_over_2pct_anchor_on_20plus", rules)


class TestStrategySpecificRules(unittest.TestCase):
    """Per-card thresholds: gdx_gld=20, gld_pm_long=30, wick_gbpusd=10,
    argus_*=30 with harsher PF<0.9."""

    def test_gdx_gld_fires_at_20_trades(self):
        stats = {"trades": 20, "profit_factor": 0.85, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("forge_gdx_gld", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertTrue(any("gdx_gld_pf_below_1_on_20plus" in r for r in rules))

    def test_gdx_gld_does_not_fire_at_19(self):
        stats = {"trades": 19, "profit_factor": 0.50, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("forge_gdx_gld", stats, DEFAULT_CTX)
        self.assertEqual(hits, [])

    def test_wick_gbpusd_fires_at_10_trades(self):
        """Wick GBPUSD is the lowest-threshold card: 10 trades suffices."""
        stats = {"trades": 10, "profit_factor": 0.50, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("forge_wick_gbpusd", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertTrue(any("wick_gbpusd_pf_below_1_on_10plus" in r for r in rules))

    def test_wick_gbpusd_does_not_fire_at_9(self):
        stats = {"trades": 9, "profit_factor": 0.50, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("forge_wick_gbpusd", stats, DEFAULT_CTX)
        self.assertEqual(hits, [])

    def test_gld_pm_long_fires_at_30_trades(self):
        stats = {"trades": 30, "profit_factor": 0.90, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("forge_gld_pm_long", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertTrue(any("gld_pm_long_pf_below_1_on_30plus" in r for r in rules))

    def test_argus_fires_at_pf_0_89_with_30_trades(self):
        """Argus threshold is harsher: PF < 0.9 (not 1.0)."""
        stats = {"trades": 30, "profit_factor": 0.89, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("argus_usdjpy", stats, DEFAULT_CTX)
        rules = [h["rule"] for h in hits]
        self.assertTrue(any("argus_kill" in r for r in rules))

    def test_argus_does_NOT_fire_at_pf_0_95(self):
        """PF 0.95 would fire universal rule, but NOT the argus-specific one."""
        stats = {"trades": 30, "profit_factor": 0.95, "pnl_usd": 0}
        hits = kw._strategy_specific_rules("argus_usdjpy", stats, DEFAULT_CTX)
        self.assertEqual(hits, [])

    def test_unknown_label_has_no_specific_rules(self):
        stats = {"trades": 100, "profit_factor": 0.50, "pnl_usd": -1000}
        hits = kw._strategy_specific_rules("made_up_label", stats, DEFAULT_CTX)
        self.assertEqual(hits, [])


class TestEvaluateStrategyStatus(unittest.TestCase):
    """evaluate_strategy combines rules into a status verdict."""

    def test_clean_stats_yields_ok_status(self):
        clean_stats = {"trades": 10, "profit_factor": 1.25, "pnl_usd": 100,
                       "wins": 6, "losses": 4, "win_rate": 0.6,
                       "peak_pnl_usd": 150, "current_drawdown_usd": 50,
                       "current_drawdown_pct_of_peak": 0.33}
        with mock.patch.object(kw, "compute_strategy_stats", return_value=clean_stats), \
             mock.patch.object(kw, "_signal_drift_rules", return_value=[]):
            result = kw.evaluate_strategy("forge_gld_pm_long", DEFAULT_CTX)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["triggered_rules"], [])

    def test_broken_strategy_yields_kill_candidate(self):
        broken = {"trades": 50, "profit_factor": 0.5, "pnl_usd": -500,
                  "wins": 10, "losses": 40, "win_rate": 0.2}
        with mock.patch.object(kw, "compute_strategy_stats", return_value=broken), \
             mock.patch.object(kw, "_signal_drift_rules", return_value=[]):
            result = kw.evaluate_strategy("forge_gld_pm_long", DEFAULT_CTX)
        self.assertEqual(result["status"], "KILL_CANDIDATE")
        self.assertGreater(len(result["triggered_rules"]), 0)

    def test_drift_warning_alone_does_not_kill(self):
        """Signal drift by itself is a warning, not a kill flag."""
        clean_stats = {"trades": 5, "profit_factor": 1.2, "pnl_usd": 50,
                       "wins": 3, "losses": 2, "win_rate": 0.6,
                       "peak_pnl_usd": 80, "current_drawdown_usd": 30,
                       "current_drawdown_pct_of_peak": 0.375}
        drift = [{"rule": "signal_drift_severe_3_consecutive_days",
                  "severity": "drift_warning", "detail": "..."}]
        with mock.patch.object(kw, "compute_strategy_stats", return_value=clean_stats), \
             mock.patch.object(kw, "_signal_drift_rules", return_value=drift):
            result = kw.evaluate_strategy("forge_gld_pm_long", DEFAULT_CTX)
        self.assertEqual(result["status"], "DRIFT_WARNING")

    def test_kill_takes_priority_over_drift(self):
        """If both kill and drift trigger, KILL_CANDIDATE wins."""
        broken = {"trades": 50, "profit_factor": 0.5, "pnl_usd": -500,
                  "wins": 10, "losses": 40, "win_rate": 0.2}
        drift = [{"rule": "signal_drift_severe_3_consecutive_days",
                  "severity": "drift_warning", "detail": "..."}]
        with mock.patch.object(kw, "compute_strategy_stats", return_value=broken), \
             mock.patch.object(kw, "_signal_drift_rules", return_value=drift):
            result = kw.evaluate_strategy("forge_gld_pm_long", DEFAULT_CTX)
        self.assertEqual(result["status"], "KILL_CANDIDATE")


class TestSignalDriftRule(unittest.TestCase):
    """3 consecutive severe drift runs -> drift_warning."""

    def _write_history(self, tmpdir: Path, rows: list[dict]) -> Path:
        p = tmpdir / "argus_flow" / "logs" / "signal_frequency_history.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return p

    def test_fires_with_3_consecutive_severe(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._write_history(tdp, [
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
            ])
            with mock.patch.object(kw, "_REPO", tdp):
                hits = kw._signal_drift_rules("argus_usdjpy")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "drift_warning")

    def test_does_not_fire_with_2_consecutive_severe(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._write_history(tdp, [
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
            ])
            with mock.patch.object(kw, "_REPO", tdp):
                hits = kw._signal_drift_rules("argus_usdjpy")
        self.assertEqual(hits, [])

    def test_streak_resets_on_ok_run(self):
        """severe, severe, ok, severe — streak is 1, not 3. Must not fire."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._write_history(tdp, [
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
                {"per_strategy": {"argus_usdjpy": {"severity": "severe"}}},
                {"per_strategy": {"argus_usdjpy": {"severity": "ok"}}},
            ])
            with mock.patch.object(kw, "_REPO", tdp):
                hits = kw._signal_drift_rules("argus_usdjpy")
        self.assertEqual(hits, [])

    def test_missing_history_file_returns_no_hits(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(kw, "_REPO", Path(td)):
                hits = kw._signal_drift_rules("argus_usdjpy")
        self.assertEqual(hits, [])

    def test_label_not_in_history_no_hits(self):
        """Rule is per-label; other strategies' severe runs must not trigger us."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._write_history(tdp, [
                {"per_strategy": {"other_strategy": {"severity": "severe"}}},
                {"per_strategy": {"other_strategy": {"severity": "severe"}}},
                {"per_strategy": {"other_strategy": {"severity": "severe"}}},
            ])
            with mock.patch.object(kw, "_REPO", tdp):
                hits = kw._signal_drift_rules("argus_usdjpy")
        self.assertEqual(hits, [])


class TestBuildReport(unittest.TestCase):
    """build_report combines every watched strategy + summary."""

    def test_report_structure(self):
        stats = {"trades": 5, "profit_factor": 1.1, "pnl_usd": 10,
                 "wins": 3, "losses": 2, "win_rate": 0.6,
                 "peak_pnl_usd": 20, "current_drawdown_usd": 10,
                 "current_drawdown_pct_of_peak": 0.5}
        with mock.patch.object(kw, "compute_strategy_stats", return_value=stats), \
             mock.patch.object(kw, "_signal_drift_rules", return_value=[]), \
             mock.patch.object(kw, "get_sizing_anchor_usd", return_value=10000):
            report = kw.build_report()
        self.assertIn("generated_at", report)
        self.assertIn("anchor_at_time_usd", report)
        self.assertIn("strategies", report)
        self.assertIn("summary", report)
        self.assertEqual(report["summary"]["total_evaluated"], len(kw.WATCHED))


if __name__ == "__main__":
    unittest.main(verbosity=2)
