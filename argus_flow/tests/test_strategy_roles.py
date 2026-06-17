"""Tests for helio.strategy_roles (Codex gap #10).

Validates the role registry + role-specific PF floor logic, and pins
that the active roster's roles match the documented allocation.
"""
from __future__ import annotations

import math

import pytest

from helio.strategy_roles import (
    DEFENSE,
    HEDGE,
    OFFENSE,
    RESEARCH,
    STRATEGY_ROLES,
    VALID_ROLES,
    get_pf_floor,
    get_role,
    role_description,
)


# ─── Registry semantics ──────────────────────────────────────────────

def test_known_strategies_get_their_assigned_role():
    """Active + pending-opt-in roster must have explicit roles."""
    assert get_role("forge_xs_momentum") == OFFENSE
    assert get_role("forge_gld_pm_long") == DEFENSE
    assert get_role("forge_tom_spy") == OFFENSE
    assert get_role("forge_nov_spy") == OFFENSE


def test_unknown_strategy_defaults_to_offense():
    """Default is OFFENSE (highest bar) — a strategy without explicit
    role must not slip through with a lower threshold."""
    assert get_role("forge_does_not_exist") == OFFENSE


def test_all_registered_roles_are_valid():
    for strategy, role in STRATEGY_ROLES.items():
        assert role in VALID_ROLES, (
            f"{strategy} has invalid role {role!r}; must be one of {VALID_ROLES}"
        )


# ─── PF floor per role ───────────────────────────────────────────────

def test_offense_floor_is_120():
    assert get_pf_floor("forge_xs_momentum") == 1.20


def test_defense_floor_is_105():
    """gld_pm_long is DEFENSE → lower bar (1.05). This is what unblocks
    gld_pm_long's disciplined_gate_passes check (CI lower 1.08 < 1.20
    but 1.08 > 1.05)."""
    assert get_pf_floor("forge_gld_pm_long") == 1.05


def test_hedge_floor_is_080():
    """No HEDGE strategy currently in the roster, but the floor must
    still be configured."""
    # Use a synthetic registry entry — direct lookup via _PF_FLOOR_BY_ROLE
    from helio.strategy_roles import _PF_FLOOR_BY_ROLE
    assert _PF_FLOOR_BY_ROLE[HEDGE] == 0.80


def test_research_floor_is_infinity():
    """RESEARCH strategies always fail the gate by design."""
    from helio.strategy_roles import _PF_FLOOR_BY_ROLE
    assert math.isinf(_PF_FLOOR_BY_ROLE[RESEARCH])


# ─── Role description text ───────────────────────────────────────────

def test_every_valid_role_has_a_description():
    for role in VALID_ROLES:
        desc = role_description(role)
        assert desc, f"{role} has no description"
        assert "(unknown role" not in desc


def test_unknown_role_description_is_safe():
    desc = role_description("FROBNICATE")
    assert "unknown" in desc.lower()


# ─── Integration: preflight uses role-aware floor ────────────────────

def test_preflight_gld_pm_long_passes_gate_at_defense_floor(monkeypatch, tmp_path):
    """gld_pm_long has CI lower 1.08 in the baseline. At OFFENSE floor
    (1.20) it FAILS. At DEFENSE floor (1.05) it PASSES."""
    from helio.real_money_preflight import check_disciplined_gate_passes, Verdict
    import json as _json
    # Construct a synthetic baseline matching production
    fake_baseline = {
        "promotion_floor": 1.20,
        "strategies": {
            "forge_gld_pm_long": {
                "ci_95_lower": 1.08,
                "verdict": "FAIL",
            },
        },
    }
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(fake_baseline), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_disciplined_gate_passes("forge_gld_pm_long")
    assert result.verdict == Verdict.GREEN
    assert "DEFENSE floor 1.05" in result.reason


def test_preflight_xs_momentum_passes_at_offense_floor(monkeypatch, tmp_path):
    """xs_momentum has CI lower 1.86 in baseline; PASSES OFFENSE 1.20 floor."""
    from helio.real_money_preflight import check_disciplined_gate_passes, Verdict
    import json as _json
    fake_baseline = {
        "promotion_floor": 1.20,
        "strategies": {
            "forge_xs_momentum": {
                "ci_95_lower": 1.86,
                "verdict": "PASS_DISCIPLINED_GATE",
            },
        },
    }
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(fake_baseline), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_disciplined_gate_passes("forge_xs_momentum")
    assert result.verdict == Verdict.GREEN
    assert "OFFENSE floor 1.20" in result.reason


def test_preflight_research_strategy_always_fails(monkeypatch, tmp_path):
    """A RESEARCH-classified strategy with an arbitrarily high CI
    lower MUST still fail (floor is infinity)."""
    from helio.real_money_preflight import check_disciplined_gate_passes, Verdict
    from helio.strategy_roles import STRATEGY_ROLES, RESEARCH
    import json as _json
    monkeypatch.setitem(STRATEGY_ROLES, "forge_synthetic_research", RESEARCH)
    fake_baseline = {
        "promotion_floor": 1.20,
        "strategies": {
            "forge_synthetic_research": {
                "ci_95_lower": 99.0,  # arbitrarily high
                "verdict": "PASS_DISCIPLINED_GATE",
            },
        },
    }
    target = tmp_path / "argus_flow" / "configs" / "promotion_gate_baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(fake_baseline), encoding="utf-8")
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    result = check_disciplined_gate_passes("forge_synthetic_research")
    assert result.verdict == Verdict.RED
    assert "RESEARCH floor" in result.reason


# ─── ACTIVE_ROSTER strategies must all have explicit roles ───────────

def test_every_active_roster_strategy_has_explicit_role():
    """Every strategy in ACTIVE_ROSTER must appear in STRATEGY_ROLES.
    Default-to-OFFENSE is fine for unknown candidates but a strategy
    that's BEEN ACTIVATED must be explicit about its role."""
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
    except ImportError:
        pytest.skip("ACTIVE_ROSTER not importable")
    missing = ACTIVE_ROSTER - set(STRATEGY_ROLES.keys())
    assert not missing, (
        f"ACTIVE_ROSTER strategies without explicit role assignment: "
        f"{missing}. Add them to STRATEGY_ROLES in helio/strategy_roles.py."
    )
