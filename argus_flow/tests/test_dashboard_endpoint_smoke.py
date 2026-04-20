"""Dashboard endpoint shape-smoke audit.

The dashboard exposes 65+ GET endpoints. Most are JSON passthroughs, but
some do real computation (notional math, direction normalization, time
windowing) and we've already caught NameErrors + silent crashes in a
couple via targeted testing.

This file runs a GET against every endpoint that doesn't require a path
parameter or POST body, and asserts:
  1. HTTP 200 (no unhandled exception crashing the handler)
  2. Parseable JSON response body
  3. Not an empty string

It does NOT validate semantic correctness — that's covered by
per-endpoint unit tests. This is the "will the dashboard even load?"
safety net before going into monitor mode.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# Endpoints that need special handling and get skipped from the generic smoke:
#   - /api/runner_chart/{symbol}  — requires a path parameter
#   - /api/stage_action           — POST only
#   - /api/stream, /api/ibkr_stream — may be Server-Sent Events
#   - /api/events                  — may be SSE
_SKIP_ENDPOINTS = {
    "/api/runner_chart/{symbol}",
    "/api/stage_action",
    "/api/stream",
    "/api/ibkr_stream",
    "/api/events",  # SSE
}


def _client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi.testclient not available")
    from ops.dashboard import app
    return TestClient(app)


def _get_endpoints() -> list[str]:
    """Enumerate GET /api/* routes that aren't in _SKIP_ENDPOINTS."""
    from ops.dashboard import app
    routes = []
    for r in app.routes:
        if not hasattr(r, "path"):
            continue
        if not r.path.startswith("/api/"):
            continue
        if r.path in _SKIP_ENDPOINTS:
            continue
        methods = set(r.methods or [])
        if "GET" not in methods:
            continue
        # Skip routes with path parameters (fastapi marks them with {})
        if "{" in r.path:
            continue
        routes.append(r.path)
    return sorted(routes)


class TestEndpointSmoke(unittest.TestCase):
    """Every GET endpoint must return 200 + parseable JSON."""

    def test_all_endpoints_return_200_and_valid_json(self):
        client = _client()
        endpoints = _get_endpoints()
        self.assertGreater(len(endpoints), 30,
            "enumeration found <30 endpoints — route discovery may be broken")

        failures = []
        for path in endpoints:
            try:
                response = client.get(path, timeout=10)
            except Exception as e:
                failures.append((path, f"request raised {type(e).__name__}: {e}"))
                continue
            if response.status_code != 200:
                failures.append((path, f"HTTP {response.status_code}: "
                                         f"{response.text[:200]}"))
                continue
            try:
                data = response.json()
            except Exception as e:
                failures.append((path, f"JSON parse: {e}"))
                continue
            if data is None:
                failures.append((path, "response body is None"))
                continue

        if failures:
            msg = f"\n{len(failures)} endpoint(s) failed smoke:\n"
            for path, reason in failures:
                msg += f"  {path}: {reason}\n"
            self.fail(msg)


class TestHealthEndpointComplete(unittest.TestCase):
    """Our /api/health endpoint should itself be healthy (i.e., all its
    sub-surfaces report OK or have an explicit non-OK reason)."""

    def test_health_endpoint_returns_complete_shape(self):
        client = _client()
        data = client.get("/api/health").json()
        self.assertIn("overall", data)
        self.assertIn("surfaces", data)
        self.assertIn("generated_at", data)
        # Every surface has a status field
        for s in data["surfaces"]:
            self.assertIn("surface", s)
            self.assertIn("status", s)


class TestEndpointErrorsReturnJsonNotRawExceptions(unittest.TestCase):
    """Even when source data is missing or corrupt, endpoints return
    structured JSON (usually {"error": "..."}), not a raw 500."""

    def test_no_endpoint_returns_500(self):
        client = _client()
        endpoints = _get_endpoints()
        fives = []
        for path in endpoints:
            try:
                response = client.get(path, timeout=10)
            except Exception:
                continue
            if response.status_code >= 500:
                fives.append((path, response.status_code,
                               response.text[:120]))
        self.assertEqual(fives, [],
            f"{len(fives)} endpoints returned 5xx:\n" +
            "\n".join(f"  {p}: {s} {t}" for p, s, t in fives))


class TestPathParameterRoute(unittest.TestCase):
    """/api/runner_chart/{symbol} takes a path parameter. Test it with
    a known symbol and with a bogus one."""

    def test_runner_chart_known_symbol(self):
        client = _client()
        r = client.get("/api/runner_chart/usdjpy")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json())

    def test_runner_chart_unknown_symbol_returns_structured_empty(self):
        """A bogus symbol must not 500 — it should return an empty-data
        structure with a shape the UI can render."""
        client = _client()
        r = client.get("/api/runner_chart/nonexistent_bogus_symbol_xyz")
        self.assertLess(r.status_code, 500,
            f"unknown symbol 500'd: {r.status_code} {r.text[:200]}")


class TestPostEndpoint(unittest.TestCase):
    """/api/stage_action is the only POST — verify it accepts a minimal
    valid body without 500'ing, even if the action is rejected."""

    def test_stage_action_rejects_malformed_body_cleanly(self):
        client = _client()
        r = client.post("/api/stage_action", json={})
        # Either 200 (accepted empty) or 4xx (validation error) — NOT 5xx
        self.assertLess(r.status_code, 500,
            f"/api/stage_action returned 5xx on empty body: {r.status_code}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
