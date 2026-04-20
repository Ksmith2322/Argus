"""Tests for /api/health — the dashboard self-healthcheck endpoint.

The health endpoint surfaces silent data failures (stale report files,
broken registry, empty canonical_fills). If this endpoint itself breaks,
the operator has no early-warning signal, so the tests keep its shape
stable.

We exercise the handler logic by calling it synchronously through
TestClient. All surface checks are exercised against live files when
present and mocked when we need to force a degradation path.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _call_health():
    """Invoke the /api/health route via FastAPI's TestClient."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi.testclient not available")
    from ops.dashboard import app
    client = TestClient(app)
    response = client.get("/api/health")
    return response.json()


class TestHealthEndpointShape(unittest.TestCase):
    def test_returns_overall_and_surfaces(self):
        data = _call_health()
        self.assertIn("overall", data)
        self.assertIn("surfaces", data)
        self.assertIn("generated_at", data)
        self.assertIsInstance(data["surfaces"], list)

    def test_overall_is_ok_or_degraded(self):
        data = _call_health()
        self.assertIn(data["overall"], ("OK", "DEGRADED"))

    def test_every_surface_has_status(self):
        data = _call_health()
        for s in data["surfaces"]:
            self.assertIn("surface", s)
            self.assertIn("status", s)

    def test_all_expected_surfaces_appear(self):
        """Surface names are a stable contract — the operator may alert
        on specific names."""
        data = _call_health()
        names = {s["surface"] for s in data["surfaces"]}
        expected = {"fleet_status", "fleet_state", "kill_watchdog",
                     "reconciliation", "fleet_perf_summary",
                     "strategy_registry", "canonical_fills", "disk_space"}
        missing = expected - names
        self.assertEqual(missing, set(),
            f"health endpoint dropped expected surfaces: {missing}")


class TestDiskSpaceSurface(unittest.TestCase):
    """disk_space surface warns below 5 GB free, critical below 1 GB.
    If disk fills up during the week, writes would fail silently; this
    surface makes it visible."""

    def test_normal_disk_reports_ok(self):
        """At current workstation free-space (hundreds of GB), surface
        is OK with free_gb populated."""
        data = _call_health()
        ds = next(s for s in data["surfaces"] if s["surface"] == "disk_space")
        self.assertIn("free_gb", ds)
        # On any real dev machine this is >>5
        if ds["free_gb"] >= 5.0:
            self.assertEqual(ds["status"], "OK")

    def test_below_5gb_reports_warn(self):
        """Simulate low disk — health flips to DEGRADED."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        from ops.dashboard import app
        fake_usage = mock.MagicMock()
        fake_usage.free = 3 * (1024**3)  # 3 GB
        fake_usage.total = 500 * (1024**3)
        with mock.patch("shutil.disk_usage", return_value=fake_usage):
            client = TestClient(app)
            data = client.get("/api/health").json()
        ds = next(s for s in data["surfaces"] if s["surface"] == "disk_space")
        self.assertEqual(ds["status"], "WARN")
        self.assertEqual(data["overall"], "DEGRADED")

    def test_below_1gb_reports_critical(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        from ops.dashboard import app
        fake_usage = mock.MagicMock()
        fake_usage.free = 500 * (1024**2)  # 500 MB
        fake_usage.total = 500 * (1024**3)
        with mock.patch("shutil.disk_usage", return_value=fake_usage):
            client = TestClient(app)
            data = client.get("/api/health").json()
        ds = next(s for s in data["surfaces"] if s["surface"] == "disk_space")
        self.assertEqual(ds["status"], "CRITICAL")
        self.assertEqual(data["overall"], "DEGRADED")

    def test_disk_usage_exception_does_not_crash_endpoint(self):
        """If shutil.disk_usage raises (e.g., on a locked mount), surface
        reports UNKNOWN but overall endpoint still returns 200."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        from ops.dashboard import app
        with mock.patch("shutil.disk_usage",
                        side_effect=OSError("mount error")):
            client = TestClient(app)
            r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        ds = next(s for s in data["surfaces"] if s["surface"] == "disk_space")
        self.assertEqual(ds["status"], "UNKNOWN")


class TestHealthSurfaceStatuses(unittest.TestCase):
    def test_registry_surface_reports_version(self):
        data = _call_health()
        reg = next(s for s in data["surfaces"] if s["surface"] == "strategy_registry")
        if reg["status"] == "OK":
            self.assertIn("version", reg)
            self.assertIn("shortlist_count", reg)

    def test_canonical_fills_surface_reports_latest_ts_when_ok(self):
        data = _call_health()
        cf = next(s for s in data["surfaces"] if s["surface"] == "canonical_fills")
        if cf["status"] == "OK":
            self.assertIn("latest_ts", cf)

    def test_broken_registry_flagged_as_broken(self):
        """If load_registry raises, the surface must be flagged BROKEN and
        overall must be DEGRADED."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi.testclient not available")
        from ops.dashboard import app
        with mock.patch("helio.strategy_registry.load_registry",
                        side_effect=RuntimeError("registry busted")):
            client = TestClient(app)
            data = client.get("/api/health").json()
        reg = next(s for s in data["surfaces"] if s["surface"] == "strategy_registry")
        self.assertEqual(reg["status"], "BROKEN")
        self.assertEqual(data["overall"], "DEGRADED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
