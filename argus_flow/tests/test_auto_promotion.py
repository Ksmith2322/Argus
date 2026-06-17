"""Tests for helio.auto_promotion + ops.auto_promotion.

Pin the recommendation logic + dry-run vs confirm gating so the
audit-driven allocation pipeline can't silently flip allocations
without operator confirm.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from helio.auto_promotion import (
    MAX_ALLOCATION_CEILING,
    MIN_LIVE_N_FOR_PROMOTION,
    PROMOTION_QUALIFYING_VERDICTS,
    PROMOTION_STEP,
    Recommendation,
    compute_recommendations,
)


# ─── Helpers ────────────────────────────────────────────────────────

def _write_walk_forward(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "walk_forward_oos.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def _write_allocation(tmp_path: Path, factors: dict) -> Path:
    p = tmp_path / "allocation_factors.json"
    p.write_text(json.dumps({"factors": factors, "version": "v0",
                             "last_updated": "2026-01-01"}),
                 encoding="utf-8")
    return p


# ─── Engine logic ───────────────────────────────────────────────────

def test_promotion_step_and_ceiling_constants():
    """Pin the promotion math so future tuning is visible."""
    assert PROMOTION_STEP == 0.25
    assert MAX_ALLOCATION_CEILING == 1.0
    assert MIN_LIVE_N_FOR_PROMOTION == 20


def test_qualifying_verdicts_set():
    """STRENGTHENED_OOS and MILD_DECAY are the only qualifying
    verdicts (LUCKY_OOS, BOTH_FAIL, OVERFIT_DEGRADED all disqualify)."""
    assert "STRENGTHENED_OOS" in PROMOTION_QUALIFYING_VERDICTS
    assert "MILD_DECAY" in PROMOTION_QUALIFYING_VERDICTS
    assert "LUCKY_OOS" not in PROMOTION_QUALIFYING_VERDICTS
    assert "BOTH_FAIL" not in PROMOTION_QUALIFYING_VERDICTS
    assert "OVERFIT_DEGRADED" not in PROMOTION_QUALIFYING_VERDICTS


def test_skips_strategy_already_at_ceiling(monkeypatch, tmp_path):
    """A strategy already at 1.0× should NOT be recommended for
    promotion (already at ceiling)."""
    wf_rows = [{
        "universe": "broad_8",
        "verdict": "STRENGTHENED_OOS",
        "train": {"sharpe": 1.0},
        "test": {"sharpe": 1.2},
    }]
    monkeypatch.setattr(
        "helio.auto_promotion._read_json",
        lambda p: ({
            "factors": {"forge_xs_momentum": 1.0}
        } if "allocation" in str(p)
            else wf_rows if "walk_forward" in str(p)
            else None),
    )
    monkeypatch.setattr(
        "helio.auto_promotion._read_jsonl", lambda p: []
    )
    result = compute_recommendations()
    # forge_xs_momentum is at ceiling -> NO recommendation
    labels = {r["strategy"] for r in result["recommendations"]}
    assert "forge_xs_momentum" not in labels


def test_proposes_promotion_for_walk_forward_winner(monkeypatch):
    """A strategy at 0.5× with STRENGTHENED_OOS verdict should be
    proposed for promotion to 0.75×."""
    wf_rows = [{
        "universe": "style_factors_8",
        "verdict": "STRENGTHENED_OOS",
        "train": {"sharpe": 1.18},
        "test": {"sharpe": 1.50},
    }]
    monkeypatch.setattr(
        "helio.auto_promotion._read_json",
        lambda p: ({
            "factors": {"forge_xs_momentum_style": 0.5}
        } if "allocation" in str(p)
            else wf_rows if "walk_forward" in str(p)
            else None),
    )
    monkeypatch.setattr(
        "helio.auto_promotion._read_jsonl", lambda p: []
    )
    result = compute_recommendations()
    style_rec = next(
        (r for r in result["recommendations"]
         if r["strategy"] == "forge_xs_momentum_style"),
        None,
    )
    assert style_rec is not None
    assert style_rec["proposed_alloc"] == pytest.approx(0.75)
    assert style_rec["current_alloc"] == pytest.approx(0.5)


def test_no_proposal_for_disqualifying_verdicts(monkeypatch):
    """LUCKY_OOS and BOTH_FAIL should NOT trigger a promotion."""
    wf_rows = [
        {"universe": "sectors_spdr_11", "verdict": "LUCKY_OOS",
         "train": {"sharpe": 0.78}, "test": {"sharpe": 1.35}},
        {"universe": "legacy_15", "verdict": "BOTH_FAIL",
         "train": {"sharpe": 0.65}, "test": {"sharpe": 1.00}},
    ]
    monkeypatch.setattr(
        "helio.auto_promotion._read_json",
        lambda p: ({
            "factors": {
                "forge_xs_momentum_sectors": 0.25,
                "forge_xs_momentum_legacy15": 0.25,
            },
        } if "allocation" in str(p)
            else wf_rows if "walk_forward" in str(p)
            else None),
    )
    monkeypatch.setattr(
        "helio.auto_promotion._read_jsonl", lambda p: []
    )
    result = compute_recommendations()
    labels = {r["strategy"] for r in result["recommendations"]}
    # Disqualifying verdicts -> no recommendations
    assert "forge_xs_momentum_sectors" not in labels
    assert "forge_xs_momentum_legacy15" not in labels


def test_higher_confidence_with_live_evidence(monkeypatch):
    """When live PnL evidence is positive AND walk-forward qualifies,
    confidence should be HIGH."""
    wf_rows = [{
        "universe": "style_factors_8",
        "verdict": "STRENGTHENED_OOS",
        "train": {"sharpe": 1.2},
        "test": {"sharpe": 1.5},
    }]
    fills = [
        {"side": "EXIT", "strategy": "forge_xs_momentum_style",
         "pnl_usd": 100.0}
        for _ in range(MIN_LIVE_N_FOR_PROMOTION)
    ]
    monkeypatch.setattr(
        "helio.auto_promotion._read_json",
        lambda p: ({
            "factors": {"forge_xs_momentum_style": 0.5}
        } if "allocation" in str(p)
            else wf_rows if "walk_forward" in str(p)
            else None),
    )
    monkeypatch.setattr(
        "helio.auto_promotion._read_jsonl", lambda p: fills
    )
    result = compute_recommendations()
    rec = next(r for r in result["recommendations"]
               if r["strategy"] == "forge_xs_momentum_style")
    assert rec["confidence"] == "HIGH"


def test_no_recommendations_with_empty_evidence():
    """If no walk-forward + no live fills + no cohort data, no
    recommendations should fire."""
    with patch("helio.auto_promotion._read_json", return_value=None), \
         patch("helio.auto_promotion._read_jsonl", return_value=[]):
        result = compute_recommendations()
    assert result["n_recommendations"] == 0


# ─── CLI dry-run vs confirm ────────────────────────────────────────

def _make_recs(strategy: str, proposed: float = 0.75) -> dict:
    return {
        "generated_at": "2026-05-25T17:00:00Z",
        "n_recommendations": 1,
        "recommendations": [{
            "strategy": strategy,
            "current_alloc": 0.5,
            "proposed_alloc": proposed,
            "confidence": "MED",
            "reasons": ["walk_forward=STRENGTHENED_OOS"],
            "evidence": {},
        }],
        "thresholds": {},
    }


def test_apply_dry_run_does_not_write_allocation(tmp_path, monkeypatch):
    """--apply without --confirm = DRY RUN. Must NOT write
    allocation_factors.json."""
    recs_path = tmp_path / "recs.json"
    recs_path.write_text(json.dumps(_make_recs("forge_xs_momentum_style")),
                         encoding="utf-8")
    alloc_path = tmp_path / "allocation_factors.json"
    alloc_path.write_text(json.dumps({
        "factors": {"forge_xs_momentum_style": 0.5},
        "version": "v0", "last_updated": "2026-01-01",
    }), encoding="utf-8")

    monkeypatch.setattr("ops.auto_promotion.RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr("ops.auto_promotion.ALLOCATION_FACTORS_PATH", alloc_path)

    from ops.auto_promotion import apply_recommendations
    result = apply_recommendations(confirm=False)

    assert result["confirmed"] is False
    assert result["n_applied"] == 1
    # Check allocation file UNCHANGED
    after = json.loads(alloc_path.read_text())
    assert after["factors"]["forge_xs_momentum_style"] == 0.5


def test_apply_with_confirm_writes_allocation(tmp_path, monkeypatch):
    """--apply --confirm = persist the new allocation."""
    recs_path = tmp_path / "recs.json"
    recs_path.write_text(json.dumps(_make_recs("forge_xs_momentum_style")),
                         encoding="utf-8")
    alloc_path = tmp_path / "allocation_factors.json"
    alloc_path.write_text(json.dumps({
        "factors": {"forge_xs_momentum_style": 0.5},
        "version": "v0", "last_updated": "2026-01-01",
    }), encoding="utf-8")
    events_path = tmp_path / "apply_events.jsonl"

    monkeypatch.setattr("ops.auto_promotion.RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr("ops.auto_promotion.ALLOCATION_FACTORS_PATH", alloc_path)
    monkeypatch.setattr("ops.auto_promotion.APPLY_EVENTS_PATH", events_path)

    from ops.auto_promotion import apply_recommendations
    result = apply_recommendations(confirm=True)

    assert result["confirmed"] is True
    assert result["n_applied"] == 1
    after = json.loads(alloc_path.read_text())
    assert after["factors"]["forge_xs_momentum_style"] == 0.75
    # Kill log appended
    assert any("AUTO-PROMOTION" in line
               for line in after.get("_kill_log", []))
    # Event file written
    assert events_path.exists()


def test_apply_filters_to_specified_strategies(tmp_path, monkeypatch):
    """When --strategy is passed, only those strategies are applied."""
    recs = {
        "generated_at": "x", "n_recommendations": 2,
        "recommendations": [
            {"strategy": "forge_xs_momentum_style", "current_alloc": 0.5,
             "proposed_alloc": 0.75, "confidence": "MED", "reasons": [],
             "evidence": {}},
            {"strategy": "forge_xs_momentum_style_top3",
             "current_alloc": 0.5, "proposed_alloc": 0.75,
             "confidence": "MED", "reasons": [], "evidence": {}},
        ],
        "thresholds": {},
    }
    recs_path = tmp_path / "recs.json"
    recs_path.write_text(json.dumps(recs), encoding="utf-8")
    alloc_path = tmp_path / "allocation_factors.json"
    alloc_path.write_text(json.dumps({
        "factors": {
            "forge_xs_momentum_style": 0.5,
            "forge_xs_momentum_style_top3": 0.5,
        },
        "version": "v0", "last_updated": "x",
    }), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"

    monkeypatch.setattr("ops.auto_promotion.RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr("ops.auto_promotion.ALLOCATION_FACTORS_PATH", alloc_path)
    monkeypatch.setattr("ops.auto_promotion.APPLY_EVENTS_PATH", events_path)

    from ops.auto_promotion import apply_recommendations
    result = apply_recommendations(
        confirm=True,
        strategies=["forge_xs_momentum_style"],  # only ONE
    )
    after = json.loads(alloc_path.read_text())
    assert after["factors"]["forge_xs_momentum_style"] == 0.75
    # The other one should be UNCHANGED
    assert after["factors"]["forge_xs_momentum_style_top3"] == 0.5
    assert result["n_applied"] == 1
