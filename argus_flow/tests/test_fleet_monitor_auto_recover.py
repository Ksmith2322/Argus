"""Auto-recover path tests for helio.fleet_monitor.

The auto-recover mechanism unblocks Argus trading when a gateway-supervision
PAUSE_ENTRIES pause is no longer justified. This is a safety-critical
decision: clear too eagerly and a broken gateway keeps placing orders;
clear too late and a recovered gateway sits idle for no reason.

Covered:
  - `_argus_all_brokers_healthy` requires EVERY active pair to be fresh +
    connected + error-free
  - `_maybe_auto_clear_pause_entries` requires all gates: present,
    age > 60s, gateway_supervision marker, brokers healthy
  - An operator-created pause (no gateway_supervision marker) is NEVER
    auto-cleared
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import fleet_monitor as fm  # noqa: E402


def _make_healthy_hb(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "broker_connected": True,
        "consecutive_errors": 0,
    }), encoding="utf-8")


def _make_unhealthy_hb(path: Path, *, broker_connected: bool = False,
                      consecutive_errors: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "broker_connected": broker_connected,
        "consecutive_errors": consecutive_errors,
    }), encoding="utf-8")


class TestArgusAllBrokersHealthy(unittest.TestCase):
    """_argus_all_brokers_healthy: True iff every active pair has
    fresh + connected + no-error heartbeats."""

    def test_all_healthy_returns_true(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            hb_paths = [tdp / f"hb_{p}.json" for p in ("usdjpy", "gbpusd", "cadjpy")]
            for p in hb_paths:
                _make_healthy_hb(p)
            fake_cfg = {"argus": {"heartbeats": hb_paths}}
            with mock.patch.object(fm, "SYSTEMS", fake_cfg):
                self.assertTrue(fm._argus_all_brokers_healthy())

    def test_any_missing_heartbeat_returns_false(self):
        """Missing file == unhealthy. A killed pair with no heartbeat at all
        must not block auto-clear — BUT this function reads SYSTEMS which
        already filters those out. So here the concern is: a pair we EXPECT
        to be live doesn't have a heartbeat file yet."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            usdjpy_hb = tdp / "hb_usdjpy.json"
            _make_healthy_hb(usdjpy_hb)
            # gbpusd and cadjpy hb files don't exist
            fake_cfg = {"argus": {"heartbeats": [
                usdjpy_hb, tdp / "hb_gbpusd.json", tdp / "hb_cadjpy.json",
            ]}}
            with mock.patch.object(fm, "SYSTEMS", fake_cfg):
                self.assertFalse(fm._argus_all_brokers_healthy())

    def test_stale_heartbeat_returns_false(self):
        """Heartbeat older than 5 minutes (300s) = unhealthy."""
        import os
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "hb_usdjpy.json"
            _make_healthy_hb(p)
            old_mtime = time.time() - 600  # 10 minutes old
            os.utime(p, (old_mtime, old_mtime))
            with mock.patch.object(fm, "SYSTEMS", {"argus": {"heartbeats": [p]}}):
                self.assertFalse(fm._argus_all_brokers_healthy())

    def test_broker_disconnected_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "hb_usdjpy.json"
            _make_unhealthy_hb(p, broker_connected=False)
            with mock.patch.object(fm, "SYSTEMS", {"argus": {"heartbeats": [p]}}):
                self.assertFalse(fm._argus_all_brokers_healthy())

    def test_consecutive_errors_nonzero_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            p = tdp / "hb_usdjpy.json"
            _make_unhealthy_hb(p, broker_connected=True, consecutive_errors=5)
            with mock.patch.object(fm, "SYSTEMS", {"argus": {"heartbeats": [p]}}):
                self.assertFalse(fm._argus_all_brokers_healthy())

    def test_empty_heartbeats_list_returns_false(self):
        """If SYSTEMS['argus']['heartbeats'] is empty, we have no evidence
        of health — conservative answer is False."""
        with mock.patch.object(fm, "SYSTEMS", {"argus": {"heartbeats": []}}):
            self.assertFalse(fm._argus_all_brokers_healthy())

    def test_missing_argus_config_returns_false(self):
        with mock.patch.object(fm, "SYSTEMS", {}):
            self.assertFalse(fm._argus_all_brokers_healthy())


class TestMaybeAutoClearPauseEntries(unittest.TestCase):
    """Four gates must ALL pass before we auto-clear the pause file:
      1. PAUSE_ENTRIES file is present
      2. File is > 60s old (avoid watchdog race)
      3. Content starts with 'gateway_supervision' (operator pauses stay)
      4. Every Argus broker is healthy now
    """

    def _make_control_state(self, *, present: bool = True, age_s: int = 120,
                            content: str = "gateway_supervision\nIBKR disconnected"
                            ) -> dict:
        return {"PAUSE_ENTRIES": {
            "present": present, "age_s": age_s, "content": content,
        }}

    def test_happy_path_clears_and_returns_reason(self):
        with tempfile.TemporaryDirectory() as td:
            pause_file = Path(td) / "PAUSE_ENTRIES"
            pause_file.write_text("gateway_supervision", encoding="utf-8")
            with mock.patch.object(fm, "PAUSE_ENTRIES_FILE", pause_file), \
                 mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=True):
                reason = fm._maybe_auto_clear_pause_entries(self._make_control_state())
        self.assertIsNotNone(reason)
        self.assertIn("auto-cleared", reason.lower())
        self.assertFalse(pause_file.exists(), "pause file should be deleted")

    def test_not_present_returns_none(self):
        with mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=True):
            result = fm._maybe_auto_clear_pause_entries(
                self._make_control_state(present=False))
        self.assertIsNone(result)

    def test_age_under_60s_does_not_clear(self):
        """Race guard: don't clear within a minute of the watchdog creating it."""
        with tempfile.TemporaryDirectory() as td:
            pause_file = Path(td) / "PAUSE_ENTRIES"
            pause_file.write_text("gateway_supervision", encoding="utf-8")
            with mock.patch.object(fm, "PAUSE_ENTRIES_FILE", pause_file), \
                 mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=True):
                reason = fm._maybe_auto_clear_pause_entries(
                    self._make_control_state(age_s=30))
            self.assertIsNone(reason)
            self.assertTrue(pause_file.exists(), "pause file must not be deleted")

    def test_operator_created_pause_never_cleared(self):
        """If content does NOT contain 'gateway_supervision', assume it came
        from an operator and leave it alone. This is the most safety-critical
        rule in this module — deleting an operator pause could re-enable
        trading against intent."""
        with tempfile.TemporaryDirectory() as td:
            pause_file = Path(td) / "PAUSE_ENTRIES"
            pause_file.write_text("manual operator pause", encoding="utf-8")
            with mock.patch.object(fm, "PAUSE_ENTRIES_FILE", pause_file), \
                 mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=True):
                reason = fm._maybe_auto_clear_pause_entries(
                    self._make_control_state(content="Paused manually by operator"))
            self.assertIsNone(reason, "operator pause must never be auto-cleared")
            self.assertTrue(pause_file.exists())

    def test_brokers_unhealthy_does_not_clear(self):
        """Even if everything else is right, if brokers aren't healthy we
        must NOT clear the pause."""
        with tempfile.TemporaryDirectory() as td:
            pause_file = Path(td) / "PAUSE_ENTRIES"
            pause_file.write_text("gateway_supervision", encoding="utf-8")
            with mock.patch.object(fm, "PAUSE_ENTRIES_FILE", pause_file), \
                 mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=False):
                reason = fm._maybe_auto_clear_pause_entries(self._make_control_state())
            self.assertIsNone(reason)
            self.assertTrue(pause_file.exists())

    def test_unlink_failure_returns_none(self):
        """Windows may hold the file open; the unlink OSError must be
        swallowed so the caller doesn't crash the check cycle."""
        with tempfile.TemporaryDirectory() as td:
            pause_file = Path(td) / "PAUSE_ENTRIES"
            pause_file.write_text("gateway_supervision", encoding="utf-8")
            with mock.patch.object(fm, "PAUSE_ENTRIES_FILE", pause_file), \
                 mock.patch.object(fm, "_argus_all_brokers_healthy", return_value=True), \
                 mock.patch.object(type(pause_file), "unlink",
                                   side_effect=OSError("locked")):
                reason = fm._maybe_auto_clear_pause_entries(self._make_control_state())
        self.assertIsNone(reason)


class TestControlFileStateShape(unittest.TestCase):
    """_maybe_auto_clear_pause_entries reads a nested dict; the shape
    coming from _check_control_files must match."""

    def test_check_control_files_returns_expected_keys(self):
        result = fm._check_control_files()
        # Whatever the content, the shape should include PAUSE_ENTRIES
        self.assertIn("PAUSE_ENTRIES", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
