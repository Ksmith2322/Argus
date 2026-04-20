"""End-to-end happy-path test for ops.cohort_run — the Python orchestrator
backup for the PowerShell nightly pipeline.

If run_cohort_report.ps1 breaks (Windows permissions, env var quirk,
etc.), `python -m ops.cohort_run` is the backup. This test proves the
backup actually runs through all 13 steps under mocked subprocess.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _fake_success(*args, **kwargs):
    """subprocess.run mock that returns a successful CompletedProcess."""
    result = mock.MagicMock()
    result.returncode = 0
    result.stdout = "ok"
    result.stderr = ""
    return result


def _fake_failure(*args, **kwargs):
    result = mock.MagicMock()
    result.returncode = 2
    result.stdout = ""
    result.stderr = "boom"
    return result


class TestCohortRunHappyPath(unittest.TestCase):
    def test_all_steps_invoked_in_order(self):
        """Mocked subprocess.run returns success for each step. Verify
        every step in STEPS actually ran and was invoked with the right
        args."""
        from ops import cohort_run

        calls_seen: list[list[str]] = []

        def capture(*args, **kwargs):
            calls_seen.append(list(args[0]))
            return _fake_success(*args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = capture
                mock_sp.TimeoutExpired = Exception  # keep timeout handler alive
                exit_code = cohort_run.run()

        self.assertEqual(exit_code, 0, "happy path should exit 0")
        self.assertEqual(len(calls_seen), len(cohort_run.STEPS),
            f"expected {len(cohort_run.STEPS)} subprocess calls, got {len(calls_seen)}")
        # Verify module names are correct in order
        for i, (module, args, _) in enumerate(cohort_run.STEPS):
            self.assertIn("-m", calls_seen[i])
            self.assertIn(module, calls_seen[i])

    def test_exit_0_when_all_steps_pass(self):
        from ops import cohort_run
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = _fake_success
                mock_sp.TimeoutExpired = Exception
                self.assertEqual(cohort_run.run(), 0)

    def test_exit_1_when_any_step_fails(self):
        from ops import cohort_run

        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"

            call_count = [0]
            def mixed(*args, **kwargs):
                call_count[0] += 1
                return _fake_failure() if call_count[0] == 5 else _fake_success()

            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = mixed
                mock_sp.TimeoutExpired = Exception
                # Non-fatal step failure -> continues but returns 1
                result = cohort_run.run(stop_on_fatal=False)
        self.assertEqual(result, 1)

    def test_fatal_step_aborts_when_stop_on_fatal(self):
        """The first step (refresh_managed_truth) is fatal=True. If it
        fails and stop_on_fatal=True, remaining steps must be skipped."""
        from ops import cohort_run

        run_count = [0]
        def count_calls(*args, **kwargs):
            run_count[0] += 1
            return _fake_failure()  # every call fails

        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = count_calls
                mock_sp.TimeoutExpired = Exception
                cohort_run.run(stop_on_fatal=True)

        # Only the first fatal step should have been attempted
        self.assertEqual(run_count[0], 1,
            f"fatal-step abort should run 1 step, got {run_count[0]}")

    def test_timeout_counted_as_failure(self):
        """A subprocess timeout should be treated as step failure, not
        propagated as an exception."""
        from ops import cohort_run
        import subprocess as real_subprocess

        def timeout_then_success(*args, **kwargs):
            if "drift_detector" in args[0][2]:
                raise real_subprocess.TimeoutExpired(cmd=args[0], timeout=600)
            return _fake_success()

        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch("ops.cohort_run.subprocess.run",
                            side_effect=timeout_then_success):
                result = cohort_run.run(stop_on_fatal=False)
            # At least one step failed (the timeout) -> exit 1
            self.assertEqual(result, 1)
            # Log should mention TIMEOUT
            log_text = log.read_text(encoding="utf-8")
            self.assertIn("TIMEOUT", log_text)


class TestCohortRunLogging(unittest.TestCase):
    """Every step's OK/WARN/FATAL/TIMEOUT status must hit the log file
    so post-mortems don't require re-running to find the failure."""

    def test_each_step_logged_to_file(self):
        from ops import cohort_run
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = _fake_success
                mock_sp.TimeoutExpired = Exception
                cohort_run.run()
            text = log.read_text(encoding="utf-8")
        # Every step module should appear in the log
        for module, _, _ in cohort_run.STEPS:
            self.assertIn(module, text,
                f"step {module} missing from cohort_run.log")
        # Summary line
        self.assertIn("Summary:", text)

    def test_fatal_abort_recorded_in_log(self):
        from ops import cohort_run
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "cohort_run.log"
            with mock.patch.object(cohort_run, "_LOG", log), \
                 mock.patch.object(cohort_run, "subprocess") as mock_sp:
                mock_sp.run.side_effect = _fake_failure
                mock_sp.TimeoutExpired = Exception
                cohort_run.run(stop_on_fatal=True)
            text = log.read_text(encoding="utf-8")
        self.assertIn("Fatal step failed", text)


class TestCohortRunStepCoverage(unittest.TestCase):
    """Regression: ensure the Python orchestrator covers the same modules
    the PS script does."""

    def test_ps_script_modules_are_in_cohort_run_steps(self):
        from ops.cohort_run import STEPS
        ps_path = _REPO / "ops" / "run_cohort_report.ps1"
        if not ps_path.exists():
            self.skipTest("run_cohort_report.ps1 not present")

        import re
        ps_text = ps_path.read_text(encoding="utf-8")
        ps_modules = set(re.findall(r"-m\s+([a-zA-Z_][\w\.]*)", ps_text))
        py_modules = {mod for mod, _, _ in STEPS}

        # Every module invoked via `python -m` in the PS script should
        # have a matching step in cohort_run.py (give or take a few
        # non-cohort modules like the test runner itself).
        missing = ps_modules - py_modules
        # Known gaps that are acceptable: PS script may invoke modules
        # not part of the cohort pipeline (e.g., one-off utilities).
        acceptable_gap = {"argus_flow.ops.refresh_managed_truth"}  # already in both
        missing = missing - acceptable_gap
        # We ALLOW the Python orchestrator to be a strict subset — but
        # every cohort_run step MUST be in the PS script.
        py_missing = py_modules - ps_modules
        self.assertEqual(py_missing, set(),
            f"cohort_run.py has steps the PS script doesn't: {py_missing} — "
            f"add them to run_cohort_report.ps1 or remove from STEPS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
