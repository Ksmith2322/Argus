"""Capacity / margin stress test harness (Codex audit 2026-05-18 fix-by-5/31 #8).

Each strategy gets simulated at 1×, 2×, 5×, 10× its current notional, and
we report which cap layer trips per multiplier. Promotion review wants
max_safe_multiplier — a strategy already trapped at 1× has no scaling
headroom."""
from __future__ import annotations

import pytest

from helio import capacity_stress as cs


ANCHOR = 30_000.0  # canonical paper anchor


# ── stress_strategy ────────────────────────────────────────────────────────

def test_gld_at_1x_passes():
    """GLD currently sized within per-strategy cap (0.4× = $12K) should
    pass at 1×."""
    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
    )
    level_1x = next(l for l in out["levels"] if l["multiplier"] == 1.0)
    assert level_1x["passed"] is True
    assert level_1x["breaches"] == []


def test_gld_at_2x_trips_per_strategy_cap():
    """GLD at 2× = $20K — exceeds per-strategy cap of $12K (0.4× × $30K)."""
    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
    )
    level_2x = next(l for l in out["levels"] if l["multiplier"] == 2.0)
    assert level_2x["passed"] is False
    cap_layers = [b["cap_layer"] for b in level_2x["breaches"]]
    assert "PER_STRATEGY" in cap_layers, (
        f"At 2× $20K notional, GLD should breach the 0.4× per-strategy cap. "
        f"Breaches found: {cap_layers}"
    )


def test_max_safe_multiplier_reflects_first_breach():
    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
    )
    assert out["max_safe_multiplier"] == 1.0


def test_strategy_with_zero_headroom():
    """If a strategy's current notional already maxes the per-strategy cap,
    max_safe_multiplier should be < 1 (i.e., 0 in our discrete list)."""
    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=15_000.0,  # already over the 12K cap at 1x
        anchor_usd=ANCHOR,
    )
    assert out["max_safe_multiplier"] == 0.0


def test_fx_position_has_more_headroom_than_etf():
    """FX has a 5× single-instrument cap and 2× per-strategy cap. A
    $20K USDJPY position has more scale headroom than the equivalent ETF."""
    fx = cs.stress_strategy(
        strategy_label="argus_usdjpy",
        symbol="USDJPY",
        direction="long",
        current_notional_usd=20_000.0,
        anchor_usd=ANCHOR,
    )
    etf = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=20_000.0,
        anchor_usd=ANCHOR,
    )
    assert fx["max_safe_multiplier"] >= etf["max_safe_multiplier"]


def test_micro_future_uses_higher_single_instrument_cap():
    """MNQ futures single-instrument cap is 2× ($60K @ $30K anchor), but
    per-strategy cap is 1× ($30K) for nq_overnight — the per-strategy cap
    will bite first."""
    out = cs.stress_strategy(
        strategy_label="forge_nq_overnight",
        symbol="MNQ",
        direction="long",
        current_notional_usd=25_000.0,
        anchor_usd=ANCHOR,
    )
    level_1x = next(l for l in out["levels"] if l["multiplier"] == 1.0)
    assert level_1x["passed"] is True
    # 2× = $50K, exceeds per-strategy 1× = $30K
    level_2x = next(l for l in out["levels"] if l["multiplier"] == 2.0)
    assert "PER_STRATEGY" in [b["cap_layer"] for b in level_2x["breaches"]]


def test_existing_cluster_exposure_tightens_headroom():
    """If FX_USD_LONG cluster already has $30K from other FX strategies,
    a USDJPY entry has less headroom than in isolation."""
    out_alone = cs.stress_strategy(
        strategy_label="argus_usdjpy",
        symbol="USDJPY",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
    )
    out_crowded = cs.stress_strategy(
        strategy_label="argus_usdjpy",
        symbol="USDJPY",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
        existing_cluster_exposure_usd={"FX_USD_LONG": 50_000.0},
    )
    assert out_crowded["max_safe_multiplier"] <= out_alone["max_safe_multiplier"]


# ── stress_fleet ───────────────────────────────────────────────────────────

def test_stress_fleet_returns_per_strategy_and_survivors():
    strategies = [
        {"strategy": "forge_gld_pm_long", "symbol": "GLD", "direction": "long",
         "current_notional_usd": 10_000.0},
        {"strategy": "argus_usdjpy", "symbol": "USDJPY", "direction": "long",
         "current_notional_usd": 10_000.0},
        {"strategy": "forge_nq_overnight", "symbol": "MNQ", "direction": "long",
         "current_notional_usd": 20_000.0},
    ]
    out = cs.stress_fleet(strategies=strategies, anchor_usd=ANCHOR)
    assert out["anchor_usd"] == ANCHOR
    assert len(out["per_strategy"]) == 3
    assert "fleet_survivors_by_multiplier" in out
    # At 1× everyone should survive (all sized within their caps)
    survivors_1x = out["fleet_survivors_by_multiplier"][1.0]
    assert set(survivors_1x) == {"forge_gld_pm_long", "argus_usdjpy", "forge_nq_overnight"}
    # At 10× no equity-class strategy should survive against 0.4-0.6× caps
    survivors_10x = out["fleet_survivors_by_multiplier"][10.0]
    assert "forge_gld_pm_long" not in survivors_10x


def test_zero_anchor_yields_no_headroom():
    """Anchor of $0 makes every cap evaluate to $0 — every level fails."""
    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=0.0,
    )
    assert out["max_safe_multiplier"] == 0.0
    for lvl in out["levels"]:
        assert lvl["passed"] is False


# ── Output shape contract ──────────────────────────────────────────────────

def test_stress_strategy_output_shape_is_json_serializable():
    """Downstream writers (promotion review JSON) need the output to JSON
    serialize cleanly. Dataclass breach must round-trip via asdict."""
    import json

    out = cs.stress_strategy(
        strategy_label="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        current_notional_usd=10_000.0,
        anchor_usd=ANCHOR,
    )
    s = json.dumps(out)  # must not raise
    assert "max_safe_multiplier" in s
    assert "cap_layer" in s
