"""Tests for argus_flow.ops.refresh_managed_truth — the first step of
the nightly cohort pipeline.

If this fails, the entire nightly pipeline bails (it's `fatal=True` in
both the PS script and `cohort_run.py`). Which means morning brief,
reconciliation, kill watchdog etc. are all skipped for the day.

Status-field vocabulary in this module:
  - timestamp: ISO-8601 UTC of last refresh (field name: "timestamp")
  - step status values: "OK" | "SKIPPED_FRESH" | "FAILED" | "TIMEOUT"
  - overall status: "OK" | "FAILED"
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

from argus_flow.ops import refresh_managed_truth as rmt  # noqa: E402


class TestNowIso(unittest.TestCase):
    def test_returns_parseable_iso_with_tz(self):
        s = rmt._now_iso()
        dt = datetime.fromisoformat(s)
        self.assertIsNotNone(dt.tzinfo)


class TestStatusAge(unittest.TestCase):
    def test_none_status_returns_none(self):
        self.assertIsNone(rmt._status_age_s(None))

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(rmt._status_age_s({}))

    def test_fresh_timestamp_returns_small_age(self):
        recent = datetime.now(timezone.utc).isoformat()
        age = rmt._status_age_s({"timestamp": recent})
        self.assertIsNotNone(age)
        self.assertLess(age, 60)

    def test_z_suffix_iso_parsed(self):
        """'...Z' is a common ISO variant — must not return None."""
        age = rmt._status_age_s({"timestamp": "2026-04-01T12:00:00Z"})
        self.assertIsNotNone(age)

    def test_malformed_timestamp_returns_none(self):
        self.assertIsNone(rmt._status_age_s({"timestamp": "not a date"}))


class TestPreviousStepMap(unittest.TestCase):
    def test_none_status_returns_empty_map(self):
        self.assertEqual(rmt._previous_step_map(None), {})

    def test_missing_steps_returns_empty_map(self):
        self.assertEqual(rmt._previous_step_map({"timestamp": "x"}), {})

    def test_extracts_step_entries_by_id(self):
        status = {"steps": [
            {"id": "stepA", "status": "OK"},
            {"id": "stepB", "status": "FAILED"},
        ]}
        result = rmt._previous_step_map(status)
        self.assertIn("stepA", result)
        self.assertIn("stepB", result)

    def test_skips_entries_missing_id(self):
        status = {"steps": [{"status": "OK"}, {"id": "x", "status": "OK"}]}
        result = rmt._previous_step_map(status)
        self.assertEqual(len(result), 1)


class TestReuseStep(unittest.TestCase):
    """_reuse_step builds a reused-step payload. Status is flipped to
    SKIPPED_FRESH and the original status is recorded in source_status."""

    def test_reused_step_marks_skipped_fresh(self):
        step = {"id": "stepA", "module": "x.y", "critical": False}
        previous = {"status": "OK", "returncode": 0, "duration_s": 1.0, "critical": False}
        result = rmt._reuse_step(step, previous, age_s=100)
        self.assertEqual(result["status"], "SKIPPED_FRESH")
        self.assertTrue(result.get("reused"))

    def test_source_status_captures_prior_result(self):
        step = {"id": "stepA", "module": "x.y", "critical": False}
        previous = {"status": "OK", "critical": False}
        result = rmt._reuse_step(step, previous, age_s=100)
        self.assertEqual(result["source_status"], "OK")

    def test_duration_reset_to_zero(self):
        """Reused steps didn't actually run this cycle — duration=0."""
        step = {"id": "stepA", "module": "x.y", "critical": False}
        previous = {"status": "OK", "duration_s": 12.3, "critical": False}
        result = rmt._reuse_step(step, previous, age_s=100)
        self.assertEqual(result["duration_s"], 0.0)


class TestRefreshManagedTruthIntegration(unittest.TestCase):
    """Smoke refresh_managed_truth with fake STEPS + mocked _run_step."""

    def _make_lock_context_manager(self):
        """ProcessLock is used as both an object (acquire/release) AND a
        context manager in places. Mock accordingly."""
        lock = mock.MagicMock()
        lock.acquire.return_value = None
        lock.release.return_value = None
        return lock

    def test_all_ok_returns_0(self):
        fake_steps = [
            {"id": "stepA", "module": "unused.a", "critical": True},
            {"id": "stepB", "module": "unused.b", "critical": False},
        ]
        with tempfile.TemporaryDirectory() as td:
            status_path = Path(td) / "status.json"
            log_path = Path(td) / "refresh.log"
            lock = self._make_lock_context_manager()

            def fake_run(step):
                return {"id": step["id"], "status": "OK", "returncode": 0,
                        "duration_s": 0.01, "critical": step.get("critical", False),
                        "stdout_tail": [], "stderr_tail": []}

            with mock.patch.object(rmt, "STEPS", fake_steps), \
                 mock.patch.object(rmt, "STATUS_PATH", status_path), \
                 mock.patch.object(rmt, "LOG_PATH", log_path), \
                 mock.patch.object(rmt, "_run_step", side_effect=fake_run), \
                 mock.patch.object(rmt, "ProcessLock", return_value=lock):
                exit_code = rmt.refresh_managed_truth(
                    include_summary=False, accept_existing_age_s=0)
            self.assertEqual(exit_code, 0)
            self.assertTrue(status_path.exists())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "OK")
            self.assertEqual(len(status["steps"]), 2)

    def test_critical_failure_returns_1(self):
        fake_steps = [
            {"id": "stepA", "module": "unused.a", "critical": True},
        ]
        with tempfile.TemporaryDirectory() as td:
            status_path = Path(td) / "status.json"
            log_path = Path(td) / "refresh.log"
            lock = self._make_lock_context_manager()

            def fake_run(step):
                return {"id": step["id"], "status": "FAILED", "returncode": 2,
                        "duration_s": 0.01, "critical": True,
                        "stdout_tail": [], "stderr_tail": ["boom"]}

            with mock.patch.object(rmt, "STEPS", fake_steps), \
                 mock.patch.object(rmt, "STATUS_PATH", status_path), \
                 mock.patch.object(rmt, "LOG_PATH", log_path), \
                 mock.patch.object(rmt, "_run_step", side_effect=fake_run), \
                 mock.patch.object(rmt, "ProcessLock", return_value=lock):
                exit_code = rmt.refresh_managed_truth(
                    include_summary=False, accept_existing_age_s=0)
            self.assertEqual(exit_code, 1)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "FAILED")
            self.assertIn("stepA", status["failed_critical"])

    def test_noncritical_failure_returns_0_but_lists_failures(self):
        fake_steps = [
            {"id": "noncrit", "module": "unused.a", "critical": False},
        ]
        with tempfile.TemporaryDirectory() as td:
            status_path = Path(td) / "status.json"
            log_path = Path(td) / "refresh.log"
            lock = self._make_lock_context_manager()

            def fake_run(step):
                return {"id": step["id"], "status": "FAILED", "returncode": 2,
                        "duration_s": 0.01, "critical": False,
                        "stdout_tail": [], "stderr_tail": ["fail"]}

            with mock.patch.object(rmt, "STEPS", fake_steps), \
                 mock.patch.object(rmt, "STATUS_PATH", status_path), \
                 mock.patch.object(rmt, "LOG_PATH", log_path), \
                 mock.patch.object(rmt, "_run_step", side_effect=fake_run), \
                 mock.patch.object(rmt, "ProcessLock", return_value=lock):
                exit_code = rmt.refresh_managed_truth(
                    include_summary=False, accept_existing_age_s=0)
            # Non-critical failure -> exit 0, but listed in failed_noncritical
            self.assertEqual(exit_code, 0)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "OK")
            self.assertIn("noncrit", status["failed_noncritical"])


class TestStepsConfig(unittest.TestCase):
    """STEPS config must keep the expected entries — if someone removes
    one, nightly truth goes stale on that domain."""

    def test_all_steps_have_required_fields(self):
        for step in rmt.STEPS:
            self.assertIn("id", step)
            self.assertIn("module", step)

    def test_step_ids_are_unique(self):
        ids = [s["id"] for s in rmt.STEPS]
        self.assertEqual(len(ids), len(set(ids)),
            f"duplicate step ids: {ids}")

    def test_every_step_module_is_importable(self):
        """Regression: if a module rename drops a STEP's module, the
        nightly reports silently stop being refreshed."""
        import importlib
        missing = []
        for step in rmt.STEPS:
            try:
                importlib.import_module(step["module"])
            except ImportError as e:
                missing.append(f"{step['id']} ({step['module']}): {e}")
        self.assertEqual(missing, [],
            f"steps reference un-importable modules:\n" + "\n".join(missing))

    def test_critical_flag_present_on_every_step(self):
        for step in rmt.STEPS:
            self.assertIn("critical", step,
                f"step {step['id']} missing 'critical' flag — fatal-vs-noncritical "
                f"distinction is ambiguous")


if __name__ == "__main__":
    unittest.main(verbosity=2)
