"""Tests for forge.tail_hedge.runner — defensive sleeve strategy.

The runner's job: monthly check SPY 200dma. If bullish, sit flat. If
bearish, hold 50/50 GLD+TLT. Per the 5/25 backtest (PF 2.91, n=28
over 20y), this is the first HEDGE-role strategy to ship.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect state/heartbeat/trades to tmp so tests don't touch
    live state files."""
    from forge.tail_hedge import runner as th
    monkeypatch.setattr(th, "LOG_DIR", tmp_path)
    monkeypatch.setattr(th, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(th, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(th, "TRADES_PATH", tmp_path / "trades.csv")
    yield


# ─── Registry consistency ───────────────────────────────────────────

def test_tail_hedge_in_allocation_factors():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    cfg = json.loads(
        (repo / "argus_flow" / "configs" / "allocation_factors.json").read_text()
    )
    assert "forge_tail_hedge" in cfg["factors"]
    assert cfg["factors"]["forge_tail_hedge"] > 0.0


def test_tail_hedge_in_active_roster():
    from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
    assert "forge_tail_hedge" in ACTIVE_ROSTER


def test_tail_hedge_role_is_hedge():
    """The strategy must be tagged HEDGE (0.80 PF floor) not OFFENSE
    (1.20). The whole point of the role registry is to apply the
    appropriate gate floor per strategy type."""
    from helio.strategy_roles import STRATEGY_ROLES, HEDGE, get_pf_floor
    assert STRATEGY_ROLES["forge_tail_hedge"] == HEDGE
    assert get_pf_floor("forge_tail_hedge") == 0.80


def test_tail_hedge_in_data_feed_contract():
    from helio.data_feed_contract import CONTRACTS
    c = CONTRACTS.get("forge_tail_hedge")
    assert c is not None
    # Must include SPY (for 200dma) + GLD + TLT (the hedge basket)
    assert "SPY" in c.universe
    assert "GLD" in c.universe
    assert "TLT" in c.universe


# ─── Regime check ───────────────────────────────────────────────────

def _build_spy_series(prices: list[float]) -> pd.Series:
    """Build a date-indexed SPY closes series ending today."""
    return pd.Series(
        prices,
        index=pd.date_range(end=pd.Timestamp.now(), periods=len(prices)),
    )


def test_regime_check_returns_bullish_when_spy_above_200dma(monkeypatch):
    """Rising series ending high -> is_bullish True."""
    from forge.tail_hedge import runner as th
    series = _build_spy_series([100.0 + i * 0.5 for i in range(250)])
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    result = th._check_regime()
    assert result["is_bullish"] is True
    assert result["latest_close"] > result["sma_200"]


def test_regime_check_returns_bearish_when_spy_below_200dma(monkeypatch):
    """Falling series ending low -> is_bullish False."""
    from forge.tail_hedge import runner as th
    series = _build_spy_series([200.0 - i * 0.5 for i in range(250)])
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    result = th._check_regime()
    assert result["is_bullish"] is False


def test_regime_check_fails_closed_on_missing_data(monkeypatch):
    """SPY data missing -> is_bullish=None so caller refuses to trade."""
    from forge.tail_hedge import runner as th
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {},
    )
    result = th._check_regime()
    assert result["is_bullish"] is None
    assert "error" in result


def test_regime_check_fails_closed_on_stale_data(monkeypatch):
    """Data older than 4 days -> refused."""
    from forge.tail_hedge import runner as th
    old_dt = pd.Timestamp.now() - pd.Timedelta(days=10)
    series = pd.Series([400.0] * 250,
                       index=pd.date_range(end=old_dt, periods=250))
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    result = th._check_regime()
    assert result["is_bullish"] is None
    assert "stale" in result.get("error", "").lower()


def test_regime_check_fails_closed_on_insufficient_history(monkeypatch):
    """< 200 bars -> refused."""
    from forge.tail_hedge import runner as th
    series = _build_spy_series([400.0] * 50)
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    result = th._check_regime()
    assert result["is_bullish"] is None


# ─── evaluate_once decisions ────────────────────────────────────────

def test_bullish_regime_exits_open_picks(monkeypatch):
    """If we're holding positions and regime flips bullish, exit them."""
    from forge.tail_hedge import runner as th
    # Pre-populate state with held positions
    state = {
        "current_picks": {
            "GLD": {"entry_ts": "x", "entry_px": 300.0, "qty": 10, "weight": 0.5},
            "TLT": {"entry_ts": "x", "entry_px": 100.0, "qty": 30, "weight": 0.5},
        },
        "trade_count": 2,
        "session_id": "test",
        "last_rebalance_month": "",  # force rebalance
    }
    th.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    series = _build_spy_series([100.0 + i * 0.5 for i in range(250)])
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    monkeypatch.setattr(
        "forge.tail_hedge.runner.get_allocation_factor",
        lambda label: 0.1,
    )

    summary = th.evaluate_once(force=True)
    assert summary["action"] == "bullish_flat"
    # State should show no positions
    state_after = json.loads(th.STATE_PATH.read_text())
    assert state_after["current_picks"] == {}


def test_evaluate_blocks_on_regime_failure(monkeypatch):
    """If regime data is missing, refuse to trade."""
    from forge.tail_hedge import runner as th
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {},
    )
    monkeypatch.setattr(
        "forge.tail_hedge.runner.get_allocation_factor",
        lambda label: 0.1,
    )
    summary = th.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "regime_check_failed" in summary["reason"]


def test_evaluate_blocks_when_allocation_zero(monkeypatch):
    """allocation_factor=0 -> blocked even with valid regime."""
    from forge.tail_hedge import runner as th
    series = _build_spy_series([100.0 + i * 0.5 for i in range(250)])
    monkeypatch.setattr(
        "helio.regime.fetch_regime_data",
        lambda **_: {"SPY_closes": series},
    )
    monkeypatch.setattr(
        "forge.tail_hedge.runner.get_allocation_factor",
        lambda label: 0.0,
    )
    summary = th.evaluate_once(force=True)
    assert summary["action"] == "blocked"
    assert "alloc_factor" in summary["reason"]


def test_not_due_skips_when_not_force(monkeypatch):
    """If we already rebalanced this month, skip without checking regime."""
    from forge.tail_hedge import runner as th
    now = datetime.now(timezone.utc)
    this_month = now.strftime("%Y-%m")
    state = {
        "current_picks": {},
        "trade_count": 0,
        "session_id": "test",
        "last_rebalance_month": this_month,
    }
    th.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    # Don't even need to mock regime — we should skip before checking
    summary = th.evaluate_once(force=False)
    assert summary["action"] == "noop"
    assert "not_due" in summary["reason"]


# ─── Rebalance-due gating ──────────────────────────────────────────

def test_is_rebalance_due_first_week_weekday():
    """Rebalance fires for first Mon-Fri of month, no prior fire."""
    from forge.tail_hedge.runner import _is_rebalance_due
    state = {"last_rebalance_month": ""}
    mon = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)  # Mon
    assert _is_rebalance_due(state, mon) is True


def test_is_rebalance_due_skips_weekends():
    from forge.tail_hedge.runner import _is_rebalance_due
    state = {"last_rebalance_month": ""}
    sat = datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc)  # Sat
    assert _is_rebalance_due(state, sat) is False


def test_is_rebalance_due_skips_after_day_7():
    """Mid-month dates don't fire even on a Monday."""
    from forge.tail_hedge.runner import _is_rebalance_due
    state = {"last_rebalance_month": ""}
    mid = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert _is_rebalance_due(state, mid) is False


def test_is_rebalance_due_skips_if_already_rebalanced():
    from forge.tail_hedge.runner import _is_rebalance_due
    state = {"last_rebalance_month": "2026-06"}
    mon = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    assert _is_rebalance_due(state, mon) is False
