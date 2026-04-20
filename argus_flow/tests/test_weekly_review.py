"""Unit tests for helio.weekly_review — Friday fleet digest.

weekly_review is less money-critical than kill_watchdog, but it's the
output the operator reads to decide promotion/kill. Bugs here:
  - Wrong PF stat -> wrong conclusions
  - Silent skip of a system -> blind spot

Tests cover system_stats math, trade loading with window filtering, and
report generation shape.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import weekly_review as wr  # noqa: E402


def _write_trades_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestSystemStats(unittest.TestCase):
    """system_stats converts a trade list to (trades, wins, losses, WR, PF, PnL)."""

    def test_empty_list_returns_zero_stats(self):
        s = wr.system_stats([])
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["wins"], 0)
        self.assertEqual(s["pf"], 0)
        self.assertEqual(s["pnl"], 0)

    def test_all_winners_gives_pf_sentinel(self):
        """No losses -> PF = 999 (sentinel to avoid inf in dashboards)."""
        trades = [{"_pnl": 10}, {"_pnl": 20}, {"_pnl": 5}]
        s = wr.system_stats(trades)
        self.assertEqual(s["trades"], 3)
        self.assertEqual(s["wins"], 3)
        self.assertEqual(s["losses"], 0)
        self.assertEqual(s["wr"], 100.0)
        self.assertEqual(s["pf"], 999)
        self.assertEqual(s["pnl"], 35)

    def test_profit_factor_calculation(self):
        """PF = sum(wins) / |sum(losses)|."""
        trades = [{"_pnl": 100}, {"_pnl": 50}, {"_pnl": -30}, {"_pnl": -20}]
        s = wr.system_stats(trades)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 2)
        self.assertAlmostEqual(s["pf"], 150 / 50, places=2)
        self.assertEqual(s["pnl"], 100)

    def test_win_rate_as_percentage(self):
        trades = [{"_pnl": 10}, {"_pnl": -5}, {"_pnl": 20}, {"_pnl": -2}]
        s = wr.system_stats(trades)
        self.assertEqual(s["wr"], 50.0)

    def test_avg_win_and_loss(self):
        trades = [{"_pnl": 20}, {"_pnl": 40}, {"_pnl": -10}]
        s = wr.system_stats(trades)
        self.assertEqual(s["avg_win"], 30)  # (20 + 40) / 2
        self.assertEqual(s["avg_loss"], -10)

    def test_zero_pnl_counts_as_loss(self):
        """PnL==0 is in the losses bucket (the module treats <=0 as loss).
        This pins the convention so a refactor doesn't silently flip it."""
        trades = [{"_pnl": 10}, {"_pnl": 0}]
        s = wr.system_stats(trades)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 1)


class TestLoadArgusTrades(unittest.TestCase):
    def test_only_experiment_valid_rows_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recent_ts = datetime.now(timezone.utc).isoformat()
            _write_trades_csv(tdp / "argus_flow" / "logs" / "usdjpy" / "trades.csv",
                rows=[
                    {"ts": recent_ts, "experiment_valid": "true", "pnl_pips": "5"},
                    {"ts": recent_ts, "experiment_valid": "false", "pnl_pips": "-99"},
                    {"ts": recent_ts, "experiment_valid": "true", "pnl_pips": "3"},
                ],
                header=["ts", "experiment_valid", "pnl_pips"])
            with mock.patch.object(wr, "REPO", tdp):
                trades = wr.load_argus_trades(days=7)
        self.assertEqual(len(trades), 2)

    def test_window_filter_excludes_old_trades(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recent = datetime.now(timezone.utc).isoformat()
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            _write_trades_csv(tdp / "argus_flow" / "logs" / "usdjpy" / "trades.csv",
                rows=[
                    {"ts": old, "experiment_valid": "true", "pnl_pips": "100"},
                    {"ts": recent, "experiment_valid": "true", "pnl_pips": "10"},
                ],
                header=["ts", "experiment_valid", "pnl_pips"])
            with mock.patch.object(wr, "REPO", tdp):
                trades = wr.load_argus_trades(days=7)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["_pnl"], 10)

    def test_symbol_tag_applied(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recent = datetime.now(timezone.utc).isoformat()
            _write_trades_csv(tdp / "argus_flow" / "logs" / "gbpusd" / "trades.csv",
                rows=[{"ts": recent, "experiment_valid": "true", "pnl_pips": "5"}],
                header=["ts", "experiment_valid", "pnl_pips"])
            with mock.patch.object(wr, "REPO", tdp):
                trades = wr.load_argus_trades(days=7)
        self.assertEqual(trades[0]["_symbol"], "GBPUSD")
        self.assertEqual(trades[0]["_unit"], "pips")


class TestLoadSystemTrades(unittest.TestCase):
    def test_loads_from_system_logs(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recent = datetime.now(timezone.utc).isoformat()
            _write_trades_csv(tdp / "titan" / "logs" / "trades.csv",
                rows=[{"ts": recent, "pnl_pct": "2.5"}],
                header=["ts", "pnl_pct"])
            with mock.patch.object(wr, "REPO", tdp):
                trades = wr.load_system_trades("titan", days=7)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["_pnl"], 2.5)
        self.assertEqual(trades[0]["_unit"], "%")

    def test_missing_csv_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(wr, "REPO", Path(td)):
                trades = wr.load_system_trades("nonexistent", days=7)
        self.assertEqual(trades, [])


class TestGenerateReport(unittest.TestCase):
    def test_report_contains_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with mock.patch.object(wr, "REPO", tdp), \
                 mock.patch.object(wr, "SNAPSHOT_DIR", tdp / "snapshots"):
                report = wr.generate_report(days=7)
        self.assertIn("HELIO FLEET WEEKLY REVIEW", report)
        self.assertIn("SYSTEM PERFORMANCE", report)
        self.assertIn("Argus", report)
        self.assertIn("Titan", report)
        self.assertIn("Hermes", report)
        self.assertIn("Apollo", report)
        self.assertIn("FLEET TOTALS", report)
        self.assertIn("ACTION ITEMS", report)

    def test_no_trades_reports_none_for_each_system(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with mock.patch.object(wr, "REPO", tdp), \
                 mock.patch.object(wr, "SNAPSHOT_DIR", tdp / "snapshots"):
                report = wr.generate_report(days=7)
        # Each system line says "no trades" / "no rebalances" in quiet periods
        self.assertIn("no trades", report)

    def test_broken_strategy_surfaces_action_item(self):
        """A system with 5+ trades and PF < 0.8 should appear in ACTION ITEMS."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recent = datetime.now(timezone.utc).isoformat()
            # Titan with 6 trades, PF well below 0.8 (net losing)
            _write_trades_csv(tdp / "titan" / "logs" / "trades.csv",
                rows=[
                    {"ts": recent, "pnl_pct": "1"},
                    {"ts": recent, "pnl_pct": "-5"},
                    {"ts": recent, "pnl_pct": "-3"},
                    {"ts": recent, "pnl_pct": "-4"},
                    {"ts": recent, "pnl_pct": "1"},
                    {"ts": recent, "pnl_pct": "-2"},
                ],
                header=["ts", "pnl_pct"])
            with mock.patch.object(wr, "REPO", tdp), \
                 mock.patch.object(wr, "SNAPSHOT_DIR", tdp / "snapshots"):
                report = wr.generate_report(days=7)
        self.assertTrue(
            "investigate or kill" in report,
            "broken strategy should surface an action item")
        self.assertIn("Titan", report)


class TestGetSnapshotProgression(unittest.TestCase):
    def test_missing_snapshot_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(wr, "SNAPSHOT_DIR", Path(td) / "nope"):
                snaps = wr.get_snapshot_progression(7)
        self.assertEqual(snaps, [])

    def test_loads_recent_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            snap_dir.mkdir()
            (snap_dir / "snapshot_20260419.json").write_text(
                json.dumps({"systems": {"argus": {"trades": 5}}}), encoding="utf-8")
            with mock.patch.object(wr, "SNAPSHOT_DIR", snap_dir):
                snaps = wr.get_snapshot_progression(7)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["systems"]["argus"]["trades"], 5)


class TestSendDiscordNoWebhook(unittest.TestCase):
    def test_send_returns_false_when_webhook_unset(self):
        with mock.patch.object(wr, "WEBHOOK_URL", ""):
            self.assertFalse(wr.send_discord("test message"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
