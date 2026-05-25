"""Validation tests for the 2026-05-31 sunset roster.

These tests pin the post-reset surviving fleet so we don't accidentally
re-activate an archived strategy in the future. The sunset decision doc
is project_2026_05_31_sunset_decisions.md in memory; this is the
machine-enforceable contract."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The post-reset surviving fleet — strategies that DO have allocation
# (or are about to have, post-reset) AND should still be running.
SURVIVING_FLEET = {
    "forge_gld_pm_long",
    "forge_nq_overnight",
    "forge_pead",              # paper-candidate post-reset
    "forge_xs_momentum",       # paper-candidate post-reset
    "forge_spy_trend_follower",  # passive beta benchmark
}

# Strategies that should be archived (allocation 0 + no_restart=True)
ARCHIVED_FLEET = {
    "forge_multi_orb",
    "forge_spy_mean_rev",
    "forge_vix_intraday",
    "forge_nq_london_close",
    "forge_vix_carry",         # research-only, allocation 0
    "forge_aud_asian_breakout",
    "forge_wick_gbpusd",
    "forge_jpy_pm_short",
    "forge_mamba",
    "forge_tori",
    "forge_cuebanks",
    "forge_vix_revert",
    "forge_fomc_drift",
    "forge_tom_international",
    "forge_rebalance",
    "forge_gdx_gld",           # shadow
    "forge_atlas",
    "forge_themis",
    "apollo",
    "hermes",
    "titan",
}

ALLOCATION_FACTORS_PATH = REPO / "argus_flow" / "configs" / "allocation_factors.json"


def _load_factors() -> dict:
    return json.loads(ALLOCATION_FACTORS_PATH.read_text(encoding="utf-8"))


def test_all_archived_strategies_have_zero_allocation():
    """Every archived strategy must have factor=0.0 in allocation_factors.json."""
    factors = _load_factors().get("factors", {})
    offenders: list[str] = []
    for strat in ARCHIVED_FLEET:
        f = factors.get(strat)
        if f is None:
            offenders.append(f"{strat}: missing from allocation_factors.factors")
        elif float(f) != 0.0:
            offenders.append(f"{strat}: factor={f} != 0.0 (archived strategy must be 0)")
    assert not offenders, (
        "Archived strategies must have factor=0.0:\n  " + "\n  ".join(offenders)
    )


def test_argus_fx_pairs_are_archived():
    """The 3 argus FX strategies must be in the archived list with factor=0,
    EXCEPT argus_usdjpy which may be temporarily activated during the
    pre-5/31-reset exercise window (commit bc92b27, 2026-05-21). The
    exercise window allows USDJPY to fire real fills so the activity-
    multiplication toolkit (paper_stress + recorder + invariants) gets
    real data to chew on. The 5/31 reset runbook returns it to 0.0
    along with the rest of the archived fleet."""
    factors = _load_factors().get("factors", {})
    for pair in ("argus_gbpusd", "argus_cadjpy"):
        assert pair in factors, f"{pair} missing from allocation_factors"
        assert float(factors[pair]) == 0.0, (
            f"{pair} has factor={factors[pair]} — must be 0.0 per sunset doc"
        )
    # argus_usdjpy: allow up to 0.5 during pre-reset exercise; assert
    # not above 0.5 (which would exceed the documented exercise budget)
    # and not negative.
    assert "argus_usdjpy" in factors, "argus_usdjpy missing from allocation_factors"
    usdjpy = float(factors["argus_usdjpy"])
    assert 0.0 <= usdjpy <= 0.5, (
        f"argus_usdjpy factor={usdjpy} outside exercise-window bounds "
        f"[0.0, 0.5]. Either revert to 0.0 OR raise the bound here with "
        f"explicit operator note in the commit."
    )


def test_fleet_monitor_no_restart_on_archived_strategies():
    """Every archived strategy that's in fleet_monitor.SYSTEMS must have
    no_restart=True. Strategies not in SYSTEMS (e.g., argus pairs which
    use a different mechanism) are exempt."""
    from helio.fleet_monitor import SYSTEMS
    offenders: list[str] = []
    for strat in ARCHIVED_FLEET:
        if strat not in SYSTEMS:
            continue  # argus pairs etc. — not managed by fleet_monitor
        cfg = SYSTEMS[strat]
        if not cfg.get("no_restart"):
            offenders.append(f"{strat}: SYSTEMS entry missing no_restart=True")
    assert not offenders, (
        "Archived strategies must have no_restart=True in fleet_monitor.SYSTEMS:\n  "
        + "\n  ".join(offenders)
    )


def test_surviving_fleet_can_still_be_started():
    """Surviving strategies should NOT have no_restart=True — they need
    to actually be runnable. (Unless they're scheduled externally like
    forge_gld_pm_long, which has no_restart=True for that reason.)"""
    from helio.fleet_monitor import SYSTEMS

    externally_scheduled = {
        "forge_gld_pm_long",   # scheduled externally per comment in SYSTEMS
        "forge_nq_overnight",  # same
        "forge_pead",          # not yet a fleet_monitor entry; runs daily
        "forge_xs_momentum",   # not yet a fleet_monitor entry; runs monthly
        "forge_spy_trend_follower",  # not yet in SYSTEMS
    }
    for strat in SURVIVING_FLEET:
        if strat in externally_scheduled:
            continue  # ok — scheduled outside fleet_monitor's restart loop
        if strat not in SYSTEMS:
            continue  # not in SYSTEMS at all
        cfg = SYSTEMS[strat]
        assert not cfg.get("no_restart"), (
            f"{strat} is on the SURVIVING_FLEET list but has no_restart=True. "
            f"Either remove no_restart or move it to ARCHIVED_FLEET."
        )


def test_no_overlap_between_surviving_and_archived():
    """Sanity check: a strategy can't be both surviving and archived."""
    overlap = SURVIVING_FLEET & ARCHIVED_FLEET
    assert not overlap, f"Strategy in both lists: {overlap}"


def test_post_reset_target_roster_size():
    """The sunset doc target is 4-6 surviving strategies (down from 22).
    This test pins the target so future cleanups don't accidentally
    shrink or grow it without a deliberate decision."""
    assert 4 <= len(SURVIVING_FLEET) <= 6, (
        f"Surviving fleet has {len(SURVIVING_FLEET)} strategies. The "
        f"sunset doc target is 4-6. Update the doc + this test if changing."
    )


# 2026-05-23 rigor sprint: PEAD and NQ overnight failed the disciplined
# gate at realistic slippage (PEAD 1.025 @ 40bps, NQ 0.526 @ 7bps), so
# both were flipped to 0.0×. The ACTIVE roster (factor > 0) is now just
# the two strategies that survive realistic execution costs.
#
# 2026-05-24 universe-expansion sprint: 3 xs_momentum variants survived
# the 20y disciplined gate on alternative universes (sectors_spdr_11,
# style_factors_8, legacy_sectors_countries_15). Each runs the SAME
# engine via the new --variant flag, with its own state dir + client_id.
# Activated at 0.5x for paper-evidence accumulation through 6/30.
ACTIVE_ROSTER = {
    "forge_xs_momentum",
    "forge_gld_pm_long",
    "forge_xs_momentum_sectors",
    "forge_xs_momentum_style",
    "forge_xs_momentum_legacy15",
}


def test_active_roster_is_exactly_xs_momentum_and_gld_pm_long():
    """Pin the post-slippage-recalibration active roster. Any future
    re-activation of PEAD or NQ overnight requires a deliberate
    operator decision (and a new line in _kill_log explaining what
    changed about the edge). Other strategies can be added only after
    they pass the disciplined gate at realistic slippage."""
    factors = _load_factors().get("factors", {})
    active = {k for k, v in factors.items() if float(v) > 0.0}
    extras = active - ACTIVE_ROSTER
    missing = ACTIVE_ROSTER - active
    assert not extras, (
        f"Unexpected strategies with allocation > 0: {extras}. "
        f"To re-activate a sunset strategy, add it to ACTIVE_ROSTER here "
        f"AND append a justification line to allocation_factors._kill_log."
    )
    assert not missing, (
        f"Expected active strategies missing or zeroed: {missing}. "
        f"To deactivate, remove from ACTIVE_ROSTER here AND append a "
        f"justification line to allocation_factors._kill_log."
    )
