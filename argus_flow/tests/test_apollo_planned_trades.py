"""Unit tests for apollo.execution.planned_trades — ticket + maturation.

The planned-trade layer is Apollo's execution scaffolding: each scan with
score >= 75 becomes a ticket, and each ticket matures against T+N forward
returns into a counterfactual PnL. These tests pin:

  - Dedup: same (symbol, earnings_date) never double-tickets.
  - Score gate: rows below SCORE_FLOOR are skipped.
  - Maturation: matching a ticket to forward_returns picks the right horizon.
  - Counterfactual formula: deployed_usd = risk_usd / 0.02 (assumed stop).

This is the layer that will flip to real paper orders when the edge is
proven, so the math here must be pinned before that happens.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _write_scan(scan_path: Path, rows: list[dict]) -> None:
    scan_path.write_text(json.dumps(rows), encoding="utf-8")


class TestPlanNewTickets(unittest.TestCase):
    """plan_new_tickets emits tickets only for score >= SCORE_FLOOR."""

    def test_rows_below_score_floor_are_skipped(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            scan = logs / "scan_20260415.json"
            _write_scan(scan, [
                {"symbol": "AAPL", "score": 74, "direction": "long",
                 "earnings_date": "2026-04-20"},  # below floor
                {"symbol": "MSFT", "score": 75, "direction": "long",
                 "earnings_date": "2026-04-21"},  # exactly at floor
                {"symbol": "GOOG", "score": 90, "direction": "long",
                 "earnings_date": "2026-04-22"},  # above floor
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                new = pt.plan_new_tickets()
        symbols = [t["symbol"] for t in new]
        self.assertIn("MSFT", symbols)
        self.assertIn("GOOG", symbols)
        self.assertNotIn("AAPL", symbols)

    def test_exact_score_floor_75_accepted(self):
        """SCORE_FLOOR is >= not >, so 75 must be accepted."""
        import apollo.execution.planned_trades as pt
        self.assertEqual(pt.SCORE_FLOOR, 75)
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            _write_scan(logs / "scan_20260415.json", [
                {"symbol": "MSFT", "score": 75, "direction": "long",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                new = pt.plan_new_tickets()
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["symbol"], "MSFT")

    def test_dedup_by_symbol_and_earnings_date(self):
        """Running twice with the same scan must NOT double-ticket."""
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            _write_scan(logs / "scan_20260415.json", [
                {"symbol": "MSFT", "score": 80, "direction": "long",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                first = pt.plan_new_tickets()
                second = pt.plan_new_tickets()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0, "second run must skip existing ticket")

    def test_invalid_direction_is_skipped(self):
        """Direction must be long or short; anything else is dropped."""
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            _write_scan(logs / "scan_20260415.json", [
                {"symbol": "MSFT", "score": 90, "direction": "neutral",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                new = pt.plan_new_tickets()
        self.assertEqual(new, [])

    def test_missing_symbol_is_skipped(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            _write_scan(logs / "scan_20260415.json", [
                {"symbol": "", "score": 90, "direction": "long",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                new = pt.plan_new_tickets()
        self.assertEqual(new, [])

    def test_records_anchor_and_horizon(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            _write_scan(logs / "scan_20260415.json", [
                {"symbol": "MSFT", "score": 90, "direction": "long",
                 "earnings_date": "2026-04-21"},
            ])
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=12345):
                new = pt.plan_new_tickets()
        self.assertEqual(new[0]["anchor_at_plan_usd"], 12345)
        self.assertEqual(new[0]["target_horizon_td"], pt.HORIZON_TRADING_DAYS)
        self.assertEqual(new[0]["status"], "planned")


class TestMatureTickets(unittest.TestCase):
    """mature_tickets matches planned -> forward_returns -> counterfactual PnL."""

    def _setup(self, tmpdir: Path, *, planned_rows: list[dict],
               forward_rows: list[dict]):
        logs = tmpdir / "logs"
        logs.mkdir()
        with open(logs / "planned_trades.jsonl", "w", encoding="utf-8") as f:
            for r in planned_rows:
                f.write(json.dumps(r) + "\n")
        with open(logs / "forward_returns.jsonl", "w", encoding="utf-8") as f:
            for r in forward_rows:
                f.write(json.dumps(r) + "\n")
        return logs

    def test_matures_ticket_when_forward_returns_exist(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(
                Path(td),
                planned_rows=[{
                    "ticket_id": "APL-2026-04-15-MSFT", "symbol": "MSFT",
                    "scan_date": "2026-04-15", "direction": "long",
                    "score": 80, "earnings_date": "2026-04-21",
                    "anchor_at_plan_usd": 10000, "target_horizon_td": 3,
                    "status": "planned", "mode": "research_only",
                }],
                forward_rows=[{
                    "symbol": "MSFT", "scan_date": "2026-04-15",
                    "forward_returns_pct": {
                        "T+3": {"return_pct": 2.5, "date": "2026-04-18"},
                    },
                }],
            )
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                matured = pt.mature_tickets()
        self.assertEqual(len(matured), 1)
        m = matured[0]
        self.assertEqual(m["symbol"], "MSFT")
        self.assertEqual(m["return_pct"], 2.5)
        # Counterfactual: deployed = risk_usd / 0.02 = 50/0.02 = $2500
        # pnl = 2500 * 0.025 = $62.50
        self.assertAlmostEqual(m["counterfactual_pnl_usd"], 62.5, places=2)

    def test_ticket_without_forward_returns_stays_planned(self):
        """No matching forward_return -> ticket remains 'planned', no maturation."""
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(
                Path(td),
                planned_rows=[{
                    "ticket_id": "APL-2026-04-15-MSFT", "symbol": "MSFT",
                    "scan_date": "2026-04-15", "direction": "long",
                    "score": 80, "earnings_date": "2026-04-21",
                    "anchor_at_plan_usd": 10000, "target_horizon_td": 3,
                    "status": "planned", "mode": "research_only",
                }],
                forward_rows=[],  # no data yet
            )
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                matured = pt.mature_tickets()
        self.assertEqual(matured, [])

    def test_counterfactual_pnl_is_negative_on_losing_return(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(
                Path(td),
                planned_rows=[{
                    "ticket_id": "APL-2026-04-15-MSFT", "symbol": "MSFT",
                    "scan_date": "2026-04-15", "direction": "long",
                    "score": 80, "earnings_date": "2026-04-21",
                    "anchor_at_plan_usd": 10000, "target_horizon_td": 3,
                    "status": "planned", "mode": "research_only",
                }],
                forward_rows=[{
                    "symbol": "MSFT", "scan_date": "2026-04-15",
                    "forward_returns_pct": {
                        "T+3": {"return_pct": -3.0, "date": "2026-04-18"},
                    },
                }],
            )
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                matured = pt.mature_tickets()
        self.assertEqual(len(matured), 1)
        # -3% on $2500 deployed = -$75
        self.assertAlmostEqual(matured[0]["counterfactual_pnl_usd"], -75.0, places=2)

    def test_matured_tickets_are_not_re_matured(self):
        """Running mature twice on the same ticket must not double-mature."""
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(
                Path(td),
                planned_rows=[{
                    "ticket_id": "APL-2026-04-15-MSFT", "symbol": "MSFT",
                    "scan_date": "2026-04-15", "direction": "long",
                    "score": 80, "earnings_date": "2026-04-21",
                    "anchor_at_plan_usd": 10000, "target_horizon_td": 3,
                    "status": "planned", "mode": "research_only",
                }],
                forward_rows=[{
                    "symbol": "MSFT", "scan_date": "2026-04-15",
                    "forward_returns_pct": {
                        "T+3": {"return_pct": 2.0, "date": "2026-04-18"},
                    },
                }],
            )
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                first = pt.mature_tickets()
                second = pt.mature_tickets()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)


class TestCounterfactualLabeling(unittest.TestCase):
    """Every matured row must carry `counterfactual: true` and
    `math_assumptions` so downstream consumers (dashboard, morning brief)
    label the numbers as hypothetical. This is the honest-math fix from
    RESTRUCTURE_GUIDE §9."""

    def _setup(self, tmpdir: Path):
        logs = tmpdir / "logs"
        logs.mkdir()
        with open(logs / "planned_trades.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "ticket_id": "APL-2026-04-15-MSFT", "symbol": "MSFT",
                "scan_date": "2026-04-15", "direction": "long",
                "score": 80, "earnings_date": "2026-04-21",
                "anchor_at_plan_usd": 10000, "target_horizon_td": 3,
                "status": "planned", "mode": "research_only",
            }) + "\n")
        with open(logs / "forward_returns.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "symbol": "MSFT", "scan_date": "2026-04-15",
                "forward_returns_pct": {
                    "T+3": {"return_pct": 2.5, "date": "2026-04-18"},
                },
            }) + "\n")
        return logs

    def test_matured_row_marked_counterfactual(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(Path(td))
            with mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                matured = pt.mature_tickets()
            self.assertEqual(len(matured), 1)
            self.assertTrue(matured[0]["counterfactual"],
                "matured row must carry counterfactual=True so consumers label it honestly")

    def test_math_assumptions_recorded(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = self._setup(Path(td))
            with mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "FORWARD_RETURNS_PATH", logs / "forward_returns.jsonl"), \
                 mock.patch.object(pt, "compute_risk_usd", return_value=50.0), \
                 mock.patch.object(pt, "get_sizing_anchor_usd", return_value=10000):
                matured = pt.mature_tickets()
            ma = matured[0]["math_assumptions"]
            self.assertEqual(ma["risk_pct_of_anchor"], 0.005)
            self.assertEqual(ma["assumed_stop_pct"], 0.02)
            self.assertEqual(ma["slippage_bps_per_side"], 0)
            self.assertEqual(ma["commission_usd"], 0)
            self.assertIn("counterfactual", ma["notes"].lower())

    def test_summary_labels_pnl_as_counterfactual_in_research_mode(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "MODE", "research_only"):
                s = pt.summary()
            self.assertTrue(s["is_counterfactual"])
            self.assertIn("hypothetical", s["pnl_label"].lower())

    def test_summary_labels_pnl_as_realised_when_paper(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"), \
                 mock.patch.object(pt, "MODE", "paper"):
                s = pt.summary()
            self.assertFalse(s["is_counterfactual"])
            self.assertIn("realised", s["pnl_label"].lower())


class TestSummary(unittest.TestCase):
    """summary() aggregates active, matured, counterfactual PnL, win rate."""

    def test_empty_state_returns_zeros(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"):
                s = pt.summary()
        self.assertEqual(s["planned_active"], 0)
        self.assertEqual(s["matured_count"], 0)
        self.assertEqual(s["counterfactual_pnl_usd_total"], 0)
        self.assertIsNone(s["matured_win_rate_pct"])

    def test_win_rate_counts_only_positive_pnl(self):
        import apollo.execution.planned_trades as pt
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            with open(logs / "matured_trades.jsonl", "w", encoding="utf-8") as f:
                for pnl in [50, -30, 25, -10]:
                    f.write(json.dumps({"counterfactual_pnl_usd": pnl}) + "\n")
            (logs / "planned_trades.jsonl").write_text("", encoding="utf-8")
            with mock.patch.object(pt, "APOLLO_LOGS", logs), \
                 mock.patch.object(pt, "PLANNED_PATH", logs / "planned_trades.jsonl"), \
                 mock.patch.object(pt, "MATURED_PATH", logs / "matured_trades.jsonl"):
                s = pt.summary()
        # 2 wins of 4 = 50%
        self.assertEqual(s["matured_count"], 4)
        self.assertEqual(s["matured_win_rate_pct"], 50.0)
        # 50 - 30 + 25 - 10 = $35
        self.assertEqual(s["counterfactual_pnl_usd_total"], 35.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
