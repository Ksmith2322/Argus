"""Root-level pytest configuration.

Session-scoped fixtures + global test-environment setup. Lives at the
repo root so pytest auto-loads it regardless of which test directory
is invoked.
"""
from __future__ import annotations

import time
import pytest


# ─── Broker-equity anchor for tests ──────────────────────────────────
#
# Many tests touch code paths that call helio.fleet_sizing.get_sizing_anchor_usd()
# (cluster_exposure cap checks, position sizing, dual-write trade rows,
# dashboard /api/strategy_tiers, etc.). On a developer machine without
# TWS running, those tests blow up with BrokerEquityUnavailableError.
#
# This fixture seeds the in-process last-known-good cache with a fake
# $250K (matching the post-5/22 paper anchor) at session start. Tests
# that specifically need to exercise the failure path call
# helio.fleet_sizing.invalidate_cache() in their own setUp.
#
# This does NOT touch the on-disk risk_oversight_report.json — only the
# in-memory cache — so production code paths that read the JSON
# directly (e.g., risk_oversight CLI) still see real broker state.

_TEST_BROKER_EQUITY_USD = 250_000.0


@pytest.fixture(autouse=True)
def _seed_broker_equity_cache_for_tests(request):
    """Prime helio.fleet_sizing._cached_anchor with a known test value
    before EVERY test so we don't get cross-test contamination when
    test_fleet_sizing.py calls invalidate_cache() (which empties the
    cache for the rest of the run).

    Function-scoped so subsequent tests get a fresh prime. Adds ~1ms
    per test (~2-3 seconds across the full 2000-test suite).

    Opt out by adding `@pytest.mark.no_broker_seed` to the test —
    used by test_fleet_sizing.py to exercise the no-cache failure path."""
    if request.node.get_closest_marker("no_broker_seed"):
        yield
        return
    try:
        from helio import fleet_sizing as fs
    except ImportError:
        yield
        return
    fs._cached_anchor = (_TEST_BROKER_EQUITY_USD, time.time())
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_broker_seed: skip the broker-equity-cache prime for this test "
        "(used by test_fleet_sizing.py to verify the no-cache failure path)",
    )
