"""Tests for helio.fleet_state — the Phase 1 read-model aggregator.

These tests fence three properties that must hold for fleet_state.json to
be safe as an additive view:

  1. Missing source files must NOT crash the build — they degrade to None
     slots. The nightly may occasionally run before all reports exist.
  2. Per-strategy roll-up must merge correctly across kill_watchdog +
     reconciliation + promotion_readiness shapes.
  3. is_healthy must flag kill-candidate AND reconciliation-DRIFT.
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


class TestFleetStateBuild(unittest.TestCase):
    def test_build_does_not_crash_when_all_sources_missing(self):
        """Every report file returns None — aggregator must still emit a
        minimally-valid state."""
        with tempfile.TemporaryDirectory() as td:
            fake_logs = Path(td) / "logs"
            fake_logs.mkdir()
            with mock.patch("helio.fleet_state.LOGS", fake_logs), \
                 mock.patch("helio.fleet_state._FLEET_STATUS", fake_logs / "fleet_status.json"), \
                 mock.patch("helio.fleet_state._KILL_WATCHDOG", fake_logs / "kill_watchdog_report.json"), \
                 mock.patch("helio.fleet_state._RECON", fake_logs / "reconciliation_report.json"), \
                 mock.patch("helio.fleet_state._RISK_OVERSIGHT", fake_logs / "risk_oversight_report.json"), \
                 mock.patch("helio.promotion_readiness.build_report",
                            return_value={"strategies": [], "summary": {}}):
                from helio.fleet_state import build_fleet_state
                state = build_fleet_state()
        self.assertIn("generated_at", state)
        self.assertEqual(state["source_of_truth_status"], "READ_MODEL_ONLY")
        self.assertIn("strategies", state)
        self.assertIn("fleet", state)

    def test_rolls_up_kill_and_prom_into_per_strategy_block(self):
        with tempfile.TemporaryDirectory() as td:
            fake_logs = Path(td) / "logs"
            fake_logs.mkdir()
            (fake_logs / "kill_watchdog_report.json").write_text(json.dumps({
                "summary": {"kill_candidates": []},
                "strategies": [{
                    "strategy": "forge_test",
                    "status": "OK",
                    "stats": {"trades": 12, "profit_factor": 1.35, "pnl_usd": 400},
                    "triggered_rules": [],
                }],
            }), encoding="utf-8")
            (fake_logs / "reconciliation_report.json").write_text(json.dumps({
                "summary": {"drift_strategies": []},
                "strategies": [{
                    "strategy": "forge_test",
                    "status": "OK",
                    "csv_row_count": 12,
                    "canonical_row_count": 12,
                    "pnl_drift_usd": 0.0,
                    "drift_flags": [],
                }],
            }), encoding="utf-8")
            fake_prom = {
                "strategies": [{
                    "strategy": "forge_test",
                    "review_gate_passed": False,
                    "canonical_gate_passed": False,
                    "next_action": "OBSERVE_MORE",
                    "blockers": ["need 30 trades, have 12"],
                }],
                "summary": {},
            }
            with mock.patch("helio.fleet_state._FLEET_STATUS", fake_logs / "fleet_status.json"), \
                 mock.patch("helio.fleet_state._KILL_WATCHDOG", fake_logs / "kill_watchdog_report.json"), \
                 mock.patch("helio.fleet_state._RECON", fake_logs / "reconciliation_report.json"), \
                 mock.patch("helio.fleet_state._RISK_OVERSIGHT", fake_logs / "risk_oversight_report.json"), \
                 mock.patch("helio.promotion_readiness.build_report", return_value=fake_prom):
                from helio.fleet_state import build_fleet_state
                state = build_fleet_state()
        self.assertIn("forge_test", state["strategies"])
        block = state["strategies"]["forge_test"]
        self.assertEqual(block["performance"]["trades"], 12)
        self.assertAlmostEqual(block["performance"]["profit_factor"], 1.35)
        self.assertEqual(block["gates"]["next_action"], "OBSERVE_MORE")
        self.assertEqual(block["gates"]["blockers"], ["need 30 trades, have 12"])
        self.assertEqual(block["watchdog"]["status"], "OK")
        self.assertEqual(block["reconciliation"]["status"], "OK")
        self.assertTrue(block["is_healthy"])


class TestCanonicalFillsSummary(unittest.TestCase):
    """The canonical_fills summary block is the 4th Fill consumer."""

    def test_empty_fills_yields_zero_summary(self):
        from helio.fleet_state import _fills_summary
        s = _fills_summary([])
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["pnl_usd_total"], 0.0)
        self.assertIsNone(s["last_fill_ts"])

    def test_summary_splits_backfill_from_live(self):
        from helio.domain import Fill
        from helio.fleet_state import _fills_summary
        fills = [
            Fill(strategy="x", pnl_usd=10, ts="2026-04-18T10:00:00+00:00",
                 source="backfill_from_trade_csv"),
            Fill(strategy="x", pnl_usd=-5, ts="2026-04-17T10:00:00+00:00",
                 source="backfill_from_trade_csv"),
            Fill(strategy="x", pnl_usd=20, ts="2026-04-19T10:00:00+00:00",
                 source=None),  # live write
        ]
        s = _fills_summary(fills)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["backfill_count"], 2)
        self.assertEqual(s["live_count"], 1)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["pnl_usd_total"], 25.0)

    def test_summary_respects_pnl_is_none(self):
        """Fill rows with null pnl_usd (backfilled Argus timeouts) are counted
        but not summed."""
        from helio.domain import Fill
        from helio.fleet_state import _fills_summary
        fills = [
            Fill(strategy="x", pnl_usd=None, ts="2026-04-18T10:00:00+00:00"),
            Fill(strategy="x", pnl_usd=100, ts="2026-04-19T10:00:00+00:00"),
        ]
        s = _fills_summary(fills)
        self.assertEqual(s["count"], 2)
        self.assertAlmostEqual(s["pnl_usd_total"], 100.0)

    def test_fleet_state_block_contains_canonical_fills(self):
        """Integration: the per-strategy block inside fleet_state.json
        includes the canonical_fills summary."""
        with tempfile.TemporaryDirectory() as td:
            fake_logs = Path(td) / "logs"
            fake_logs.mkdir()
            with mock.patch("helio.fleet_state._FLEET_STATUS", fake_logs / "fleet_status.json"), \
                 mock.patch("helio.fleet_state._KILL_WATCHDOG", fake_logs / "kill_watchdog_report.json"), \
                 mock.patch("helio.fleet_state._RECON", fake_logs / "reconciliation_report.json"), \
                 mock.patch("helio.fleet_state._RISK_OVERSIGHT", fake_logs / "risk_oversight_report.json"), \
                 mock.patch("helio.fleet_state._canonical_fills_by_strategy",
                            return_value={}), \
                 mock.patch("helio.promotion_readiness.build_report",
                            return_value={"strategies": [{"strategy": "X", "next_action": "OBSERVE"}],
                                          "summary": {}}):
                from helio.fleet_state import build_fleet_state
                state = build_fleet_state()
        self.assertIn("X", state["strategies"])
        self.assertIn("canonical_fills", state["strategies"]["X"])


class TestFleetStateHistory(unittest.TestCase):
    """append_history writes a compact snapshot per build to JSONL."""

    def _sample_state(self) -> dict:
        return {
            "generated_at": "2026-04-19T14:30:00+00:00",
            "strategies": {
                "forge_x": {
                    "performance": {"trades": 12, "profit_factor": 1.35, "pnl_usd": 400},
                    "gates": {"next_action": "OBSERVE_MORE"},
                    "canonical_fills": {"count": 12},
                    "is_healthy": True,
                },
            },
            "fleet": {
                "broker_equity_usd": 10500,
                "broker_connected": True,
                "risk_level": "GREEN",
                "kill_summary": {"kill_candidates": [], "drift_warnings": []},
                "reconciliation_summary": {"drift_strategies": []},
            },
        }

    def test_history_row_compact_shape(self):
        from helio.fleet_state import _history_row
        row = _history_row(self._sample_state())
        self.assertEqual(row["ts"], "2026-04-19T14:30:00+00:00")
        self.assertEqual(row["broker_equity_usd"], 10500)
        self.assertEqual(row["risk_level"], "GREEN")
        self.assertIn("forge_x", row["strategies"])
        self.assertEqual(row["strategies"]["forge_x"]["trades"], 12)
        self.assertAlmostEqual(row["strategies"]["forge_x"]["profit_factor"], 1.35)
        self.assertEqual(row["strategies"]["forge_x"]["canonical_fills_count"], 12)

    def test_append_history_creates_file_and_appends(self):
        import helio.fleet_state as fs
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "fleet_state_history.jsonl"
            with mock.patch.object(fs, "HISTORY_PATH", hist):
                fs.append_history(self._sample_state())
                fs.append_history(self._sample_state())
            lines = hist.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_append_history_never_raises(self):
        """Broken history path must not crash the snapshot write flow."""
        import helio.fleet_state as fs
        # Point at a path whose parent is a FILE (can't mkdir), and open() will
        # also fail. append_history must swallow both.
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("im a file", encoding="utf-8")
            bad = blocker / "inside" / "fleet_state_history.jsonl"
            with mock.patch.object(fs, "HISTORY_PATH", bad):
                # Must not raise
                fs.append_history(self._sample_state())

    def test_write_fleet_state_appends_to_history_by_default(self):
        import helio.fleet_state as fs
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "fleet_state.json"
            hist = tdp / "fleet_state_history.jsonl"
            with mock.patch.object(fs, "OUT_PATH", out), \
                 mock.patch.object(fs, "HISTORY_PATH", hist):
                fs.write_fleet_state(self._sample_state())
            self.assertTrue(out.exists())
            self.assertTrue(hist.exists())
            self.assertGreater(len(hist.read_text().splitlines()), 0)

    def test_write_fleet_state_skips_history_when_disabled(self):
        import helio.fleet_state as fs
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            out = tdp / "fleet_state.json"
            hist = tdp / "fleet_state_history.jsonl"
            with mock.patch.object(fs, "OUT_PATH", out), \
                 mock.patch.object(fs, "HISTORY_PATH", hist):
                fs.write_fleet_state(self._sample_state(), append_to_history=False)
            self.assertFalse(hist.exists(),
                "history file should not be created when append_to_history=False")


class TestIsHealthy(unittest.TestCase):
    def test_kill_candidate_marks_unhealthy(self):
        from helio.fleet_state import _compute_is_healthy
        self.assertFalse(_compute_is_healthy(
            kill={"status": "KILL_CANDIDATE", "triggered_rules": ["pf_below_1"]},
            recon={},
            prom={},
        ))

    def test_reconciliation_drift_marks_unhealthy(self):
        from helio.fleet_state import _compute_is_healthy
        self.assertFalse(_compute_is_healthy(
            kill={"status": "OK", "triggered_rules": []},
            recon={"status": "DRIFT"},
            prom={},
        ))

    def test_all_ok_is_healthy(self):
        from helio.fleet_state import _compute_is_healthy
        self.assertTrue(_compute_is_healthy(
            kill={"status": "OK", "triggered_rules": []},
            recon={"status": "OK"},
            prom={},
        ))

    def test_empty_reports_are_healthy(self):
        """Cold-start with no reports yet: don't panic, report healthy."""
        from helio.fleet_state import _compute_is_healthy
        self.assertTrue(_compute_is_healthy(kill={}, recon={}, prom={}))


class TestSafeGet(unittest.TestCase):
    def test_safe_get_walks_nested_keys(self):
        from helio.fleet_state import _safe_get
        d = {"a": {"b": {"c": 42}}}
        self.assertEqual(_safe_get(d, "a", "b", "c"), 42)

    def test_safe_get_returns_default_on_missing(self):
        from helio.fleet_state import _safe_get
        self.assertEqual(_safe_get({"a": 1}, "b", default="X"), "X")

    def test_safe_get_handles_none_intermediate(self):
        from helio.fleet_state import _safe_get
        self.assertIsNone(_safe_get(None, "a", "b"))


class TestBrokerConnectedRollup(unittest.TestCase):
    """Fleet-level broker_connected must roll up from per-runner flags in
    fleet_status — risk_oversight_report has no top-level 'connected' bool
    today, so the old read-path silently returned None."""

    def test_derive_from_runner_flags(self):
        from helio.fleet_state import _derive_broker_connected, _derive_broker_runners
        fleet_status = {"systems": {"argus": {"runtime": {
            "usdjpy": {"broker_connected": True},
            "gbpusd": {"broker_connected": True},
            "cadjpy": {"broker_connected": False},
        }}}}
        runners = _derive_broker_runners(fleet_status)
        self.assertEqual(runners["argus.usdjpy"], True)
        self.assertEqual(runners["argus.cadjpy"], False)
        # Any runner connected → fleet connected
        self.assertTrue(_derive_broker_connected({}, fleet_status))

    def test_all_disconnected_returns_false(self):
        from helio.fleet_state import _derive_broker_connected
        fs = {"systems": {"argus": {"runtime": {"usdjpy": {"broker_connected": False}}}}}
        self.assertFalse(_derive_broker_connected({}, fs))

    def test_no_runner_info_returns_none(self):
        from helio.fleet_state import _derive_broker_connected
        self.assertIsNone(_derive_broker_connected({}, {"systems": {}}))

    def test_risk_oversight_explicit_wins(self):
        """If a future risk_oversight schema adds broker_truth.connected,
        it takes precedence over the per-runner rollup."""
        from helio.fleet_state import _derive_broker_connected
        ro = {"broker_truth": {"connected": True}}
        fs = {"systems": {"argus": {"runtime": {"usdjpy": {"broker_connected": False}}}}}
        self.assertTrue(_derive_broker_connected(ro, fs))


class TestConfidence(unittest.TestCase):
    """Per-strategy + fleet-level confidence scoreboard.

    Target from project_testing_framework_20260419 is P(expectancy>0) >= 0.90,
    not win rate. These tests pin the bootstrap + evidence-bar contract so the
    scoreboard gives the same answer across runs and can be trusted as an
    input to the promotion decision.
    """

    def _fill(self, pnl, *, source=None, risk=None, ts="2026-04-19T12:00:00+00:00"):
        from helio.domain import Fill
        return Fill(strategy="s", pnl_usd=pnl, risk_usd=risk, source=source, ts=ts)

    def test_evidence_bar_thresholds(self):
        from helio.fleet_state import _evidence_bar
        self.assertEqual(_evidence_bar(0), "insufficient")
        self.assertEqual(_evidence_bar(9), "insufficient")
        self.assertEqual(_evidence_bar(10), "sanity")
        self.assertEqual(_evidence_bar(29), "sanity")
        self.assertEqual(_evidence_bar(30), "review")
        self.assertEqual(_evidence_bar(59), "review")
        self.assertEqual(_evidence_bar(60), "promotion")
        self.assertEqual(_evidence_bar(500), "promotion")

    def test_bootstrap_returns_none_under_sanity_bar(self):
        from helio.fleet_state import _bootstrap_p_positive
        # 9 trades, all winners — still None because sample too small to trust
        self.assertIsNone(_bootstrap_p_positive([10.0] * 9))

    def test_bootstrap_near_one_for_all_positive_sample(self):
        from helio.fleet_state import _bootstrap_p_positive
        p = _bootstrap_p_positive([10.0] * 20)
        # All-positive sample → every resample mean is positive → p == 1.0
        self.assertEqual(p, 1.0)

    def test_bootstrap_near_zero_for_all_negative_sample(self):
        from helio.fleet_state import _bootstrap_p_positive
        p = _bootstrap_p_positive([-5.0] * 20)
        self.assertEqual(p, 0.0)

    def test_bootstrap_is_deterministic(self):
        from helio.fleet_state import _bootstrap_p_positive
        # Seeded RNG — identical inputs must yield identical outputs across runs
        pnls = [1.0, -2.0, 3.0, -1.0, 4.0, -3.0, 2.0, 1.0, -1.0, 5.0,
                -2.0, 3.0, 1.0, -1.0, 2.0]
        self.assertEqual(_bootstrap_p_positive(list(pnls)),
                         _bootstrap_p_positive(list(pnls)))

    def test_confidence_splits_live_vs_paper(self):
        from helio.fleet_state import _confidence_summary
        fills = (
            [self._fill(5.0, source="backfill_from_trade_csv")] * 3
            + [self._fill(10.0, source=None)] * 2
        )
        c = _confidence_summary(fills)
        self.assertEqual(c["n_total"], 5)
        self.assertEqual(c["n_paper"], 3)
        self.assertEqual(c["n_live"], 2)

    def test_confidence_expectancy_r_uses_risk_usd(self):
        from helio.fleet_state import _confidence_summary
        # 2R win + 1R loss + 2R win = avg 1R
        fills = [
            self._fill(200.0, risk=100.0),
            self._fill(-100.0, risk=100.0),
            self._fill(200.0, risk=100.0),
        ]
        c = _confidence_summary(fills)
        self.assertAlmostEqual(c["expectancy_r"], 1.0, places=2)

    def test_confidence_warns_on_small_sample(self):
        from helio.fleet_state import _confidence_summary
        fills = [self._fill(10.0) for _ in range(3)]
        c = _confidence_summary(fills)
        self.assertEqual(c["evidence_bar"], "insufficient")
        self.assertIsNone(c["p_expectancy_positive"])
        self.assertIn("3 trades", c["sample_warning"])

    def test_confidence_warns_when_backfill_dominates(self):
        from helio.fleet_state import _confidence_summary
        fills = (
            [self._fill(5.0, source="backfill_from_trade_csv")] * 10
            + [self._fill(10.0, source=None)]
        )
        c = _confidence_summary(fills)
        self.assertIsNotNone(c["sample_warning"])
        self.assertIn("backfill", c["sample_warning"])

    def test_confidence_empty_fills_block(self):
        from helio.fleet_state import _confidence_summary
        c = _confidence_summary([])
        self.assertEqual(c["n_total"], 0)
        self.assertIsNone(c["expectancy_usd"])
        self.assertIsNone(c["p_expectancy_positive"])
        self.assertEqual(c["evidence_bar"], "insufficient")
        self.assertEqual(c["sample_warning"], "no trades recorded")

    def test_rollup_counts_evidence_bars_and_positive_expectancy(self):
        from helio.fleet_state import _confidence_rollup
        strategies = {
            "a": {"confidence": {"expectancy_usd": 5.0, "evidence_bar": "promotion",
                                  "p_expectancy_positive": 0.95, "n_total": 60}},
            "b": {"confidence": {"expectancy_usd": 2.0, "evidence_bar": "review",
                                  "p_expectancy_positive": 0.55, "n_total": 35}},
            "c": {"confidence": {"expectancy_usd": -1.0, "evidence_bar": "sanity",
                                  "p_expectancy_positive": 0.20, "n_total": 12}},
            "d": {"confidence": {"expectancy_usd": None, "evidence_bar": "insufficient",
                                  "p_expectancy_positive": None, "n_total": 3}},
        }
        r = _confidence_rollup(strategies)
        self.assertEqual(r["total_strategies"], 4)
        self.assertEqual(r["strategies_with_positive_expectancy"], 2)
        self.assertEqual(r["strategies_at_sanity_bar"], 3)     # a,b,c
        self.assertEqual(r["strategies_at_review_bar"], 2)     # a,b
        self.assertEqual(r["strategies_at_promotion_bar"], 1)  # a
        self.assertEqual(r["strategies_p_positive_gte_50pct"], 2)  # a,b
        self.assertEqual(r["strategies_p_positive_gte_90pct"], 1)  # a
        leaders = r["leaders_by_p_positive"]
        self.assertEqual(leaders[0]["strategy"], "a")

    def test_build_fleet_state_includes_confidence_block(self):
        """Integration: confidence block appears in the per-strategy dict
        and the fleet rollup."""
        with tempfile.TemporaryDirectory() as td:
            fake_logs = Path(td) / "logs"
            fake_logs.mkdir()
            from helio.domain import Fill
            fills_stub = {
                "forge_x": [
                    Fill(strategy="forge_x", pnl_usd=10.0, source=None,
                         ts="2026-04-19T10:00:00+00:00"),
                ] * 15
            }
            with mock.patch("helio.fleet_state._FLEET_STATUS", fake_logs / "fleet_status.json"), \
                 mock.patch("helio.fleet_state._KILL_WATCHDOG", fake_logs / "kill_watchdog_report.json"), \
                 mock.patch("helio.fleet_state._RECON", fake_logs / "reconciliation_report.json"), \
                 mock.patch("helio.fleet_state._RISK_OVERSIGHT", fake_logs / "risk_oversight_report.json"), \
                 mock.patch("helio.fleet_state._canonical_fills_by_strategy",
                            return_value=fills_stub), \
                 mock.patch("helio.promotion_readiness.build_report",
                            return_value={"strategies": [{"strategy": "forge_x",
                                                           "next_action": "OBSERVE"}],
                                          "summary": {}}):
                from helio.fleet_state import build_fleet_state
                state = build_fleet_state()
        self.assertIn("confidence", state["strategies"]["forge_x"])
        c = state["strategies"]["forge_x"]["confidence"]
        self.assertEqual(c["n_total"], 15)
        self.assertEqual(c["n_live"], 15)
        self.assertEqual(c["evidence_bar"], "sanity")
        self.assertEqual(c["p_expectancy_positive"], 1.0)  # all wins
        self.assertIn("confidence_rollup", state["fleet"])
        self.assertEqual(state["fleet"]["confidence_rollup"]["strategies_with_positive_expectancy"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
