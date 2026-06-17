"""Tests for ops.audit.run_pre_market_check.

Focused on the verdict-aggregation logic + the operator-facing
expected_actions calendar. The composition layer is integration-tested
implicitly by the existing daily_health_check + data_feed_contract
test suites.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ops.audit.run_pre_market_check import (
    _aggregate_verdict,
    _expected_actions,
    _exit_code,
    _trading_days_from,
)


# ─── Trading-day helper ──────────────────────────────────────────────

def test_trading_days_skips_weekends():
    # Sat 2026-05-23 -> next 5 weekdays start Mon 5-25 ... Fri 5-29
    days = _trading_days_from(datetime(2026, 5, 23), 5)
    assert [d.weekday() for d in days] == [0, 1, 2, 3, 4]
    assert days[0].day == 25
    assert days[-1].day == 29


def test_trading_days_from_weekday_includes_today():
    days = _trading_days_from(datetime(2026, 5, 26), 3)
    assert days[0].day == 26
    assert all(d.weekday() < 5 for d in days)


# ─── Expected-actions calendar ──────────────────────────────────────

def test_expected_actions_xs_momentum_fires_first_week_of_month():
    # 2026-06-01 = Mon, day 1 -> rebalance window
    actions = _expected_actions(datetime(2026, 6, 1))
    xs = next(a for a in actions if a["strategy"] == "forge_xs_momentum")
    assert xs["action_in_next_5_days"] is True


def test_expected_actions_xs_momentum_idle_mid_month():
    # 2026-06-15 = Mon, day 15 -> NOT in first 7 days
    actions = _expected_actions(datetime(2026, 6, 15))
    xs = next(a for a in actions if a["strategy"] == "forge_xs_momentum")
    assert xs["action_in_next_5_days"] is False


def test_expected_actions_tom_spy_fires_at_eom():
    # 2026-05-26 = Tue, last week of May -> TOM window
    actions = _expected_actions(datetime(2026, 5, 26))
    tom = next(a for a in actions if a["strategy"] == "forge_tom_spy")
    assert tom["action_in_next_5_days"] is True
    # Note must flag PENDING_OPT_IN so operator doesn't expect fills
    assert "PENDING_OPT_IN" in tom.get("note", "")


def test_expected_actions_nov_spy_silent_outside_november():
    actions = _expected_actions(datetime(2026, 5, 26))
    nov = next(a for a in actions if a["strategy"] == "forge_nov_spy")
    assert nov["action_in_next_5_days"] is False


def test_expected_actions_gld_fires_every_weekday():
    actions = _expected_actions(datetime(2026, 6, 15))
    gld = next(a for a in actions if a["strategy"] == "forge_gld_pm_long")
    assert gld["action_in_next_5_days"] is True
    assert len(gld["expected_dates"]) == 5


# ─── Verdict aggregation ─────────────────────────────────────────────

def _green_daily():
    return {
        "worst_status": "GREEN",
        "reports": [
            {"component": "auto_pause", "status": "GREEN", "summary": ""},
            {"component": "orphan_phantom", "status": "GREEN", "summary": ""},
            {"component": "data_feed", "status": "GREEN", "summary": ""},
            {"component": "preflight", "status": "GREEN", "summary": ""},
            {"component": "roster_state", "status": "GREEN", "summary": ""},
            {"component": "flag_check", "status": "GREEN", "summary": ""},
        ],
    }


def _green_feeds():
    return {"refreshed": False, "contracts": {
        "forge_xs_momentum": {"verdict": "GREEN", "reasons": []},
    }}


def _green_restart():
    return {"head_sha": "abc", "rows": [], "n_need_restart": 0}


def _green_heartbeats():
    return {"max_age_hours": 6, "stale": []}


def _green_flags():
    return {"halt_flag_present": False, "flatten_flag_present": False}


def test_ready_when_everything_green():
    v, r = _aggregate_verdict(
        _green_daily(), _green_feeds(), _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "READY"
    assert r == []


def test_not_ready_when_halt_flag_present():
    flags = _green_flags()
    flags["halt_flag_present"] = True
    v, r = _aggregate_verdict(
        _green_daily(), _green_feeds(), _green_restart(),
        _green_heartbeats(), flags,
    )
    assert v == "NOT_READY"
    assert any("HALT" in x for x in r)


def test_not_ready_when_non_preflight_component_is_red():
    daily = _green_daily()
    daily["reports"][2]["status"] = "RED"  # data_feed component RED
    daily["reports"][2]["summary"] = "all tickers missing"
    v, r = _aggregate_verdict(
        daily, _green_feeds(), _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "NOT_READY"
    assert any("data_feed RED" in x for x in r)


def test_preflight_red_alone_does_not_block_ready():
    """preflight RED is expected behavior — strategies BLOCKED on
    real_money_allowlist by design until operator opts in. Must NOT
    flip pre-market verdict to NOT_READY."""
    daily = _green_daily()
    daily["reports"][3]["status"] = "RED"
    daily["reports"][3]["summary"] = "preflight: BLOCKED on allowlist"
    daily["worst_status"] = "RED"
    v, r = _aggregate_verdict(
        daily, _green_feeds(), _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "READY", f"expected READY, got {v} with reasons {r}"


def test_review_when_runner_on_old_sha():
    restart = {
        "head_sha": "abc",
        "n_need_restart": 1,
        "rows": [{"strategy": "forge_xs_momentum", "needs_restart": True}],
    }
    v, r = _aggregate_verdict(
        _green_daily(), _green_feeds(), restart,
        _green_heartbeats(), _green_flags(),
    )
    assert v == "REVIEW"
    assert any("old SHA" in x for x in r)


def test_review_when_data_feed_yellow():
    feeds = {"refreshed": False, "contracts": {
        "forge_x": {"verdict": "YELLOW", "reasons": ["stale"]},
    }}
    v, r = _aggregate_verdict(
        _green_daily(), feeds, _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "REVIEW"
    assert any("YELLOW" in x for x in r)


def test_not_ready_when_contract_red_and_refresh_failed():
    feeds = {"refreshed": False, "contracts": {
        "forge_x": {"verdict": "RED", "reasons": ["missing"]},
    }}
    v, r = _aggregate_verdict(
        _green_daily(), feeds, _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "NOT_READY"


def test_review_when_contract_red_after_refresh():
    feeds = {"refreshed": True, "contracts": {
        "forge_x": {"verdict": "RED", "reasons": ["missing"]},
    }}
    v, r = _aggregate_verdict(
        _green_daily(), feeds, _green_restart(),
        _green_heartbeats(), _green_flags(),
    )
    assert v == "REVIEW"


def test_review_when_heartbeats_stale():
    hb = {"max_age_hours": 6, "stale": [
        {"strategy": "forge_xs_momentum", "age_hours": 12, "status": "STALE"},
    ]}
    v, r = _aggregate_verdict(
        _green_daily(), _green_feeds(), _green_restart(),
        hb, _green_flags(),
    )
    assert v == "REVIEW"
    assert any("stale" in x.lower() for x in r)


def test_exit_code_mapping():
    assert _exit_code("READY") == 0
    assert _exit_code("REVIEW") == 1
    assert _exit_code("NOT_READY") == 2
    assert _exit_code("nonsense") == 2  # fail-closed
