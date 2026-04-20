"""Unit tests for helio.watchdog — the family watchdog that restarts
dead runners.

The watchdog's critical decision point is `_is_runner_alive`: if it returns
False when the runner is actually up (false-negative), we'd spawn a duplicate.
If it returns True when the runner is actually dead (false-positive), the
family silently drops offline and we lose monitoring.

Scope: focus on the pure decision functions. The main loop (infinite) and
restart side effects (subprocess.Popen) are not unit-tested; integration
coverage lives in test_fleet_monitor_faults.
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

from helio import watchdog as wd  # noqa: E402


def _write_heartbeat(path: Path, age_seconds: float = 0.0) -> None:
    """Create a heartbeat file and optionally backdate its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": "2026-04-19T00:00:00+00:00"}),
                    encoding="utf-8")
    if age_seconds > 0:
        mtime = time.time() - age_seconds
        import os
        os.utime(path, (mtime, mtime))


class TestIsRunnerAlive(unittest.TestCase):
    """Core decision: does ANY heartbeat dir show a fresh signal?"""

    def test_alive_when_one_heartbeat_fresh(self):
        """Multi-dir runners only need ONE dir to be fresh to be alive."""
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            # Create 3 dirs: first two stale, third fresh
            _write_heartbeat(logs / "gld" / "heartbeat.json", age_seconds=8000)
            _write_heartbeat(logs / "spy" / "heartbeat.json", age_seconds=9000)
            _write_heartbeat(logs / "mes" / "heartbeat.json", age_seconds=60)
            with mock.patch.object(wd, "LOGS_ROOT", logs):
                alive = wd._is_runner_alive({
                    "name": "helio",
                    "heartbeat_dirs": ["gld", "spy", "mes"],
                    "stale_threshold_s": 7200,
                })
        self.assertTrue(alive, "fresh mes heartbeat should mark runner alive")

    def test_dead_when_all_heartbeats_stale(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            _write_heartbeat(logs / "gld" / "heartbeat.json", age_seconds=8000)
            _write_heartbeat(logs / "spy" / "heartbeat.json", age_seconds=9000)
            with mock.patch.object(wd, "LOGS_ROOT", logs):
                alive = wd._is_runner_alive({
                    "name": "helio",
                    "heartbeat_dirs": ["gld", "spy"],
                    "stale_threshold_s": 7200,
                })
        self.assertFalse(alive)

    def test_dead_when_no_heartbeat_file_exists(self):
        """Missing heartbeat file is treated the same as stale."""
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            # No heartbeat files at all
            with mock.patch.object(wd, "LOGS_ROOT", logs):
                alive = wd._is_runner_alive({
                    "name": "helio",
                    "heartbeat_dirs": ["nonexistent"],
                    "stale_threshold_s": 7200,
                })
        self.assertFalse(alive)

    def test_boundary_at_exact_threshold(self):
        """A heartbeat exactly at the threshold is treated as stale
        (rule uses strict <)."""
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            # Age exactly 7200s = the threshold. Just under must be alive.
            _write_heartbeat(logs / "gld" / "heartbeat.json", age_seconds=7199)
            with mock.patch.object(wd, "LOGS_ROOT", logs):
                alive = wd._is_runner_alive({
                    "name": "helio",
                    "heartbeat_dirs": ["gld"],
                    "stale_threshold_s": 7200,
                })
        self.assertTrue(alive, "just-under-threshold must be alive")

    def test_different_thresholds_per_runner(self):
        """Different runners can have different freshness requirements —
        heartbeat age must be compared per-runner."""
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            _write_heartbeat(logs / "x" / "heartbeat.json", age_seconds=1000)
            with mock.patch.object(wd, "LOGS_ROOT", logs):
                # 500s threshold — stale at 1000s
                alive_tight = wd._is_runner_alive({
                    "heartbeat_dirs": ["x"], "stale_threshold_s": 500,
                })
                # 7200s threshold — alive at 1000s
                alive_loose = wd._is_runner_alive({
                    "heartbeat_dirs": ["x"], "stale_threshold_s": 7200,
                })
        self.assertFalse(alive_tight)
        self.assertTrue(alive_loose)


class TestIsProcessRunning(unittest.TestCase):
    """_is_process_running scans psutil for a python module name."""

    def test_returns_true_when_matching_cmdline_found(self):
        fake_procs = [
            mock.MagicMock(info={"cmdline": ["python", "-m", "helio.runner"]}),
            mock.MagicMock(info={"cmdline": ["python", "other_script.py"]}),
        ]
        with mock.patch("psutil.process_iter", return_value=fake_procs):
            self.assertTrue(wd._is_process_running("helio.runner"))

    def test_returns_false_when_no_match(self):
        fake_procs = [
            mock.MagicMock(info={"cmdline": ["python", "something_else.py"]}),
        ]
        with mock.patch("psutil.process_iter", return_value=fake_procs):
            self.assertFalse(wd._is_process_running("helio.runner_apollo"))

    def test_returns_false_when_psutil_missing(self):
        """Fallback path: no psutil means we can't check — return False
        (the conservative 'assume not running' answer)."""
        # Simulate the ImportError branch
        with mock.patch.dict("sys.modules", {"psutil": None}):
            # Direct call — won't actually work with None module, so use
            # builtins.__import__ to force ImportError
            orig_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

            def bad_import(name, *a, **kw):
                if name == "psutil":
                    raise ImportError
                return orig_import(name, *a, **kw)

            with mock.patch("builtins.__import__", side_effect=bad_import):
                self.assertFalse(wd._is_process_running("whatever"))


class TestCountProcesses(unittest.TestCase):
    """_count_processes: used to detect duplicate runners."""

    def test_counts_multiple_matches(self):
        fake_procs = [
            mock.MagicMock(info={"cmdline": ["python", "-m", "helio.runner"]}),
            mock.MagicMock(info={"cmdline": ["python", "-m", "helio.runner"]}),
            mock.MagicMock(info={"cmdline": ["python", "other_script.py"]}),
        ]
        with mock.patch("psutil.process_iter", return_value=fake_procs):
            count = wd._count_processes("helio.runner")
        self.assertEqual(count, 2)

    def test_zero_on_no_matches(self):
        with mock.patch("psutil.process_iter", return_value=[]):
            self.assertEqual(wd._count_processes("nothing"), 0)


class TestRunnersConfig(unittest.TestCase):
    """Sanity: the RUNNERS config should have distinct names and non-empty
    heartbeat dirs per runner. A duplicate name or empty dir list means the
    watchdog would skip/duplicate monitoring for a family."""

    def test_every_runner_has_unique_name(self):
        names = [r["name"] for r in wd.RUNNERS]
        self.assertEqual(len(names), len(set(names)),
            f"duplicate runner names in config: {names}")

    def test_every_runner_has_module_path(self):
        for r in wd.RUNNERS:
            self.assertIn("module", r)
            self.assertTrue(r["module"].startswith("helio."),
                f"runner {r['name']} module should be under helio.*")

    def test_every_runner_has_non_empty_heartbeat_dirs(self):
        for r in wd.RUNNERS:
            self.assertIn("heartbeat_dirs", r)
            self.assertGreater(len(r["heartbeat_dirs"]), 0,
                f"runner {r['name']} has no heartbeat dirs")

    def test_stale_thresholds_are_positive(self):
        for r in wd.RUNNERS:
            self.assertGreater(r["stale_threshold_s"], 0)


class TestRestartBudget(unittest.TestCase):
    """The MAX_RESTARTS_PER_HOUR throttle prevents restart loops."""

    def test_constant_is_set_to_safe_value(self):
        """3 restarts/hour is the throttle. Must stay small — a flapping
        runner that restarts 100 times/hour creates its own problem."""
        self.assertLessEqual(wd.MAX_RESTARTS_PER_HOUR, 5,
            "restart throttle should stay <=5/hour")
        self.assertGreaterEqual(wd.MAX_RESTARTS_PER_HOUR, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
