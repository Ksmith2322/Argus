"""Smoke tests for fleet_monitor.run_check_cycle — the 5-minute
monitoring cycle that runs all of monitor mode.

During unattended operation this function runs every 5 minutes. If it
raises, the fleet goes blind until someone notices. These tests cover:

  - The happy path produces a well-shaped status dict
  - A broken subsystem (heartbeat missing, restart failure, risk drift)
    does NOT crash the cycle — it logs and continues
  - write_status persists the result to fleet_status.json
  - portfolio_guard refresh failure is swallowed

All filesystem and subprocess calls are mocked so the test is fast and
safe to run in CI.
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

from helio import fleet_monitor as fm  # noqa: E402


class TestRunCheckCycleSmoke(unittest.TestCase):
    """The cycle function must produce a dict with {ts, systems,
    control_files, risk_state} under any error conditions in its
    sub-calls."""

    def test_returns_expected_shape_happy_path(self):
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "OK", "details": []}), \
             mock.patch.object(fm, "_check_control_files",
                               return_value={"PAUSE_ENTRIES": {"present": False}}), \
             mock.patch.object(fm, "_check_risk_state",
                               return_value={"disagreement": None}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch("helio.portfolio_guard.check_current",
                        return_value=None):
            result = fm.run_check_cycle(auto_restart=False)
        for key in ("ts", "systems", "control_files", "risk_state"):
            self.assertIn(key, result)

    def test_down_system_triggers_restart_when_auto_restart(self):
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "DOWN", "details": ["proc dead"]}), \
             mock.patch.object(fm, "_check_control_files",
                               return_value={}), \
             mock.patch.object(fm, "_check_risk_state",
                               return_value={}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch.object(fm, "restart_system") as mock_restart, \
             mock.patch("helio.portfolio_guard.check_current"):
            fm.run_check_cycle(auto_restart=True)
        self.assertGreater(mock_restart.call_count, 0,
            "DOWN system should trigger restart_system")

    def test_auto_restart_false_suppresses_restarts(self):
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "DOWN", "details": []}), \
             mock.patch.object(fm, "_check_control_files",
                               return_value={}), \
             mock.patch.object(fm, "_check_risk_state",
                               return_value={}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch.object(fm, "restart_system") as mock_restart, \
             mock.patch("helio.portfolio_guard.check_current"):
            fm.run_check_cycle(auto_restart=False)
        self.assertEqual(mock_restart.call_count, 0)

    def test_auto_clear_triggers_send_discord(self):
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "OK", "details": []}), \
             mock.patch.object(fm, "_check_control_files") as mock_ctrl, \
             mock.patch.object(fm, "_check_risk_state", return_value={}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value="PAUSE_ENTRIES auto-cleared"), \
             mock.patch.object(fm, "send_discord") as mock_discord, \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch("helio.portfolio_guard.check_current"):
            mock_ctrl.side_effect = [{}, {}]  # called twice
            fm.run_check_cycle(auto_restart=False)
        self.assertGreater(mock_discord.call_count, 0,
            "auto-clear should send a Discord notification")

    def test_portfolio_guard_refresh_failure_swallowed(self):
        """A broken portfolio_guard must not crash the cycle."""
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "OK", "details": []}), \
             mock.patch.object(fm, "_check_control_files", return_value={}), \
             mock.patch.object(fm, "_check_risk_state", return_value={}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch("helio.portfolio_guard.check_current",
                        side_effect=RuntimeError("guard broken")):
            # Must not raise
            result = fm.run_check_cycle(auto_restart=False)
        # Cycle still completed, status dict intact
        self.assertIn("systems", result)

    def test_risk_drift_fires_discord_alert(self):
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "OK", "details": []}), \
             mock.patch.object(fm, "_check_control_files", return_value={}), \
             mock.patch.object(fm, "_check_risk_state",
                               return_value={"disagreement": "broker 1.2% vs paper 0.8%"}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "send_discord") as mock_discord, \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch("helio.portfolio_guard.check_current"):
            fm.run_check_cycle(auto_restart=False)
        # Verify a risk-drift alert fired
        called_with_drift = any(
            "RISK STATE DRIFT" in str(call) for call in mock_discord.call_args_list
        )
        self.assertTrue(called_with_drift,
            "risk_state disagreement must trigger a Discord alert")

    def test_pause_entries_age_alert_fires_above_threshold(self):
        old_age = fm.PAUSE_ENTRIES_ALERT_AGE_S + 600  # well over threshold
        ctrl = {"PAUSE_ENTRIES": {"present": True, "age_s": old_age,
                                    "content": "manual"}}
        with mock.patch.object(fm, "check_system_health",
                               return_value={"status": "OK", "details": []}), \
             mock.patch.object(fm, "_check_control_files", return_value=ctrl), \
             mock.patch.object(fm, "_check_risk_state", return_value={}), \
             mock.patch.object(fm, "_snapshot_broker_equity"), \
             mock.patch.object(fm, "_maybe_auto_clear_pause_entries",
                               return_value=None), \
             mock.patch.object(fm, "send_discord") as mock_discord, \
             mock.patch.object(fm, "maybe_webhook_ping"), \
             mock.patch("helio.portfolio_guard.check_current"):
            fm.run_check_cycle(auto_restart=False)
        # A PAUSE_ENTRIES-age alert must have been dispatched
        fired = any("PAUSE_ENTRIES active" in str(call)
                    for call in mock_discord.call_args_list)
        self.assertTrue(fired)


class TestWriteStatus(unittest.TestCase):
    """write_status persists fleet_status.json."""

    def test_writes_json_to_expected_path(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with mock.patch.object(fm, "REPO", tdp):
                status = {"ts": "2026-04-19T12:00Z", "systems": {}, "control_files": {}}
                fm.write_status(status)
                # File written
                out = tdp / "argus_flow" / "logs" / "fleet_status.json"
                self.assertTrue(out.exists())
                data = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(data["ts"], "2026-04-19T12:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
