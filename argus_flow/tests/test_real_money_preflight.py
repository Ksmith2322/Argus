"""Tests for helio.real_money_preflight.

Each test mocks the relevant data source so checks are deterministic
without depending on the actual repo state (active roster, allocation
factors, heartbeats, etc.). The combined evaluate_strategy() integration
is tested separately at the end.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio.real_money_preflight import (
    CheckResult,
    StrategyPreflight,
    Verdict,
    check_allocation_factor_positive,
    check_capacity_headroom,
    check_disciplined_gate_passes,
    check_evidence_epoch_clean,
    check_halt_flag_absent,
    check_heartbeat_fresh,
    check_in_active_roster,
    check_killed_strategy_invariant,
    check_real_money_allowlist,
    evaluate_strategy,
)


# ─── check_in_active_roster ──────────────────────────────────────────

def test_active_roster_green_when_member():
    """A strategy that IS in ACTIVE_ROSTER returns GREEN."""
    # xs_momentum is currently in ACTIVE_ROSTER per test_sunset_roster
    result = check_in_active_roster("forge_xs_momentum")
    assert result.verdict == Verdict.GREEN
    assert result.value is True


def test_active_roster_red_when_missing():
    result = check_in_active_roster("forge_does_not_exist_xyz")
    assert result.verdict == Verdict.RED
    assert "ACTIVE_ROSTER" in result.reason


# ─── check_allocation_factor_positive ────────────────────────────────

def test_allocation_factor_positive_green(monkeypatch):
    import helio.fleet_sizing as fs
    monkeypatch.setattr(fs, "get_allocation_factor", lambda s: 0.5)
    result = check_allocation_factor_positive("forge_x")
    assert result.verdict == Verdict.GREEN
    assert result.value == 0.5


def test_allocation_factor_zero_red(monkeypatch):
    import helio.fleet_sizing as fs
    monkeypatch.setattr(fs, "get_allocation_factor", lambda s: 0.0)
    result = check_allocation_factor_positive("forge_x")
    assert result.verdict == Verdict.RED
    assert result.value == 0.0


# ─── check_disciplined_gate_passes ───────────────────────────────────

def test_disciplined_gate_uses_recalibrated_when_present(monkeypatch, tmp_path):
    fake = {
        "promotion_floor": 1.20,
        "strategies": {
            "forge_x": {
                "ci_95_lower": 2.00,
                "recalibrated_ci_95_lower_at_realistic": 0.80,
                "verdict": "FAIL",
            },
        },
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(fake), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path.parent)
    # The check builds the path from REPO; redirect to our fake
    monkeypatch.setattr(
        p, "REPO",
        tmp_path,  # so REPO/"argus_flow"/"configs"/"promotion_gate_baseline.json"
                   # would be wrong — work around by setting a sentinel
    )
    # Easier: monkeypatch the inner function. Use the file-read path:
    # The check reads `REPO / "argus_flow" / "configs" /
    # "promotion_gate_baseline.json"`. Mock by creating that path.
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fake), encoding="utf-8")

    result = check_disciplined_gate_passes("forge_x")
    # Recalibrated value 0.80 < 1.20 floor → RED, NOT the rosier 2.00
    assert result.verdict == Verdict.RED
    assert result.value["ci_lower"] == pytest.approx(0.80)


def test_disciplined_gate_green_when_above_floor(monkeypatch, tmp_path):
    fake = {
        "promotion_floor": 1.20,
        "strategies": {
            "forge_x": {
                "ci_95_lower": 1.86,
                "verdict": "PASS_DISCIPLINED_GATE",
            },
        },
    }
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fake), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)

    result = check_disciplined_gate_passes("forge_x")
    assert result.verdict == Verdict.GREEN
    assert result.value["ci_lower"] == pytest.approx(1.86)


def test_disciplined_gate_red_when_strategy_missing(monkeypatch, tmp_path):
    fake = {"promotion_floor": 1.20, "strategies": {}}
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fake), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_disciplined_gate_passes("forge_x")
    assert result.verdict == Verdict.RED
    assert "missing" in result.reason.lower()


# ─── check_capacity_headroom ─────────────────────────────────────────

def test_capacity_headroom_yellow_when_artifact_missing(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "CAPACITY_STRESS_ARTIFACT", tmp_path / "missing.json")
    result = check_capacity_headroom("forge_x")
    assert result.verdict == Verdict.YELLOW
    assert "no capacity_stress.json" in result.reason


def test_capacity_headroom_green_when_above_target(monkeypatch, tmp_path):
    artifact = tmp_path / "capacity_stress.json"
    artifact.write_text(json.dumps({
        "per_strategy": [
            {"strategy": "forge_x", "max_safe_multiplier": 5.0},
        ],
    }), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "CAPACITY_STRESS_ARTIFACT", artifact)
    result = check_capacity_headroom("forge_x", multiplier=2.0)
    assert result.verdict == Verdict.GREEN
    assert result.value == 5.0


def test_capacity_headroom_yellow_when_below_target(monkeypatch, tmp_path):
    artifact = tmp_path / "capacity_stress.json"
    artifact.write_text(json.dumps({
        "per_strategy": [
            {"strategy": "forge_x", "max_safe_multiplier": 1.0},
        ],
    }), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "CAPACITY_STRESS_ARTIFACT", artifact)
    result = check_capacity_headroom("forge_x", multiplier=2.0)
    assert result.verdict == Verdict.YELLOW
    assert "max_safe_multiplier=1.0" in result.reason


# ─── check_real_money_allowlist ──────────────────────────────────────

def test_allowlist_red_when_global_disabled(monkeypatch):
    from helio import real_money
    class _Allow:
        global_enabled = False
        strategies = ()
        def validate_self(self): return []
    monkeypatch.setattr(real_money, "load_allowlist", lambda: _Allow())
    result = check_real_money_allowlist("forge_x")
    assert result.verdict == Verdict.RED
    assert "global_enabled=false" in result.reason


def test_allowlist_green_when_enabled_and_in_list(monkeypatch):
    from helio import real_money
    class _Allow:
        global_enabled = True
        strategies = ("forge_x",)
        def validate_self(self): return []
    monkeypatch.setattr(real_money, "load_allowlist", lambda: _Allow())
    result = check_real_money_allowlist("forge_x")
    assert result.verdict == Verdict.GREEN


def test_allowlist_red_when_enabled_but_strategy_missing(monkeypatch):
    from helio import real_money
    class _Allow:
        global_enabled = True
        strategies = ("forge_y",)
        def validate_self(self): return []
    monkeypatch.setattr(real_money, "load_allowlist", lambda: _Allow())
    result = check_real_money_allowlist("forge_x")
    assert result.verdict == Verdict.RED


def test_allowlist_yellow_when_validation_problems(monkeypatch):
    """If allowlist is internally inconsistent (e.g. missing approver),
    we YELLOW even if the strategy is in the list."""
    from helio import real_money
    class _Allow:
        global_enabled = True
        strategies = ("forge_x",)
        def validate_self(self): return ["missing approver"]
    monkeypatch.setattr(real_money, "load_allowlist", lambda: _Allow())
    result = check_real_money_allowlist("forge_x")
    assert result.verdict == Verdict.YELLOW
    assert "approver" in result.reason.lower()


# ─── check_killed_strategy_invariant ─────────────────────────────────

def test_kill_invariant_red_when_killed(monkeypatch):
    from helio import roi_filter
    monkeypatch.setattr(roi_filter, "KILLED_STRATEGY_CUTOFFS",
                          {"forge_x": "2026-01-01"})
    result = check_killed_strategy_invariant("forge_x")
    assert result.verdict == Verdict.RED
    assert "2026-01-01" in result.value


def test_kill_invariant_green_when_not_killed(monkeypatch):
    from helio import roi_filter
    monkeypatch.setattr(roi_filter, "KILLED_STRATEGY_CUTOFFS", {})
    result = check_killed_strategy_invariant("forge_x")
    assert result.verdict == Verdict.GREEN


# ─── check_heartbeat_fresh ───────────────────────────────────────────

def test_heartbeat_fresh_green_when_recent(monkeypatch, tmp_path):
    """heartbeat.json that's 1 hour old → GREEN."""
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    hb = tmp_path / "forge" / "logs" / "x" / "heartbeat.json"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("{}", encoding="utf-8")
    # Touch it to 1 hour ago
    import os
    mtime = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(hb, (mtime, mtime))
    result = check_heartbeat_fresh("forge_x", max_age_hours=24)
    assert result.verdict == Verdict.GREEN


def test_heartbeat_fresh_red_when_stale(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    hb = tmp_path / "forge" / "logs" / "x" / "heartbeat.json"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("{}", encoding="utf-8")
    import os
    mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    os.utime(hb, (mtime, mtime))
    result = check_heartbeat_fresh("forge_x", max_age_hours=24)
    assert result.verdict == Verdict.RED
    assert "old" in result.reason.lower()


def test_heartbeat_fresh_red_when_missing(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_heartbeat_fresh("forge_x")
    assert result.verdict == Verdict.RED
    assert "no heartbeat" in result.reason.lower()


# ─── check_halt_flag_absent ──────────────────────────────────────────

def test_halt_flag_absent_green_when_no_file(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_halt_flag_absent()
    assert result.verdict == Verdict.GREEN


def test_halt_flag_absent_red_when_file_present(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    halt = tmp_path / "argus_flow" / "logs" / "HALT.flag"
    halt.parent.mkdir(parents=True, exist_ok=True)
    halt.write_text("manual halt at 2026-05-24", encoding="utf-8")
    result = check_halt_flag_absent()
    assert result.verdict == Verdict.RED


# ─── evaluate_strategy integration ───────────────────────────────────

def test_evaluate_strategy_returns_13_checks():
    """Every call to evaluate_strategy must produce exactly 13 check
    results: the 12 promotion-grade points + evidence_quality (added
    2026-05-24 per Codex gap #5)."""
    p = evaluate_strategy("forge_does_not_exist")
    assert len(p.checks) == 13


def test_evaluate_strategy_verdict_is_blocked_when_any_red():
    p = evaluate_strategy("forge_does_not_exist")
    # The non-existent strategy will produce many REDs
    assert p.n_red > 0
    assert p.verdict == "BLOCKED"


def test_evaluate_strategy_to_dict_serializable():
    """The returned StrategyPreflight should round-trip through JSON."""
    p = evaluate_strategy("forge_does_not_exist")
    d = p.to_dict()
    json.dumps(d)  # should not raise
    assert d["strategy"] == "forge_does_not_exist"
    assert d["verdict"] == "BLOCKED"
    assert len(d["checks"]) == 13
