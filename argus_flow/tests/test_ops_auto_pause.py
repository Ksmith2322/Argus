"""Tests for ops.auto_pause — operator-gated apply path.

The recommendation side (helio.auto_pause) is alert-only and covered
in test_auto_pause.py. This file tests the destructive --apply --confirm
path that turns PAUSE_RECOMMENDED recommendations into allocation
flips.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_recs_file(path: Path, alerts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": "test",
        "alert_tier_counts": {},
        "alerts": alerts,
    }, default=str), encoding="utf-8")


def _write_factors_file(path: Path, factors: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "factors": factors, "version": "test", "_kill_log": [],
    }, default=str), encoding="utf-8")


def _pause_alert(strategy: str, days: int = 5, threshold: int = 5) -> dict:
    return {
        "strategy": strategy,
        "alert_tier": "PAUSE_RECOMMENDED",
        "current_verdict": "FAIL",
        "consecutive_days_at_verdict": days,
        "threshold_days": threshold,
        "last_live_pf": 0.5,
        "baseline_ci_lower": 1.2,
    }


# ─── dry-run preserves the factors file ──────────────────────────────

def test_apply_dry_run_does_not_write_factors(monkeypatch, tmp_path):
    from ops import auto_pause as ap
    recs_path = tmp_path / "recs.json"
    factors_path = tmp_path / "factors.json"
    events_path = tmp_path / "events.jsonl"
    _write_recs_file(recs_path, [_pause_alert("forge_x")])
    _write_factors_file(factors_path, {"forge_x": 0.5})
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr(ap, "ALLOCATION_FACTORS_PATH", factors_path)
    monkeypatch.setattr(ap, "APPLY_EVENTS_PATH", events_path)

    result = ap.apply_recommendations(confirm=False)
    cfg = json.loads(factors_path.read_text())
    assert cfg["factors"]["forge_x"] == 0.5
    assert result["n_dry_run"] == 1
    assert result["n_paused"] == 0
    # Event still logged so we have an audit trail
    assert events_path.exists()


# ─── --confirm flips + appends _kill_log ─────────────────────────────

def test_apply_confirm_flips_to_zero_and_logs(monkeypatch, tmp_path):
    from ops import auto_pause as ap
    recs_path = tmp_path / "recs.json"
    factors_path = tmp_path / "factors.json"
    events_path = tmp_path / "events.jsonl"
    _write_recs_file(recs_path, [_pause_alert("forge_x")])
    _write_factors_file(factors_path, {"forge_x": 0.5, "forge_y": 1.0})
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr(ap, "ALLOCATION_FACTORS_PATH", factors_path)
    monkeypatch.setattr(ap, "APPLY_EVENTS_PATH", events_path)

    result = ap.apply_recommendations(confirm=True)
    cfg = json.loads(factors_path.read_text())
    assert cfg["factors"]["forge_x"] == 0.0
    assert cfg["factors"]["forge_y"] == 1.0  # untouched
    log = cfg.get("_kill_log") or []
    assert any("AUTO-PAUSE-APPLIED" in s for s in log)
    assert any("forge_x" in s for s in log)
    events = events_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    assert json.loads(events[0])["action"] == "PAUSED"
    assert result["n_paused"] == 1


# ─── already-zero is a no-op ─────────────────────────────────────────

def test_apply_skips_already_zero(monkeypatch, tmp_path):
    """If allocation is already 0.0, no flip happens even with --confirm,
    and _kill_log isn't appended (avoids spamming the audit log with
    no-ops after a strategy stays paused for days)."""
    from ops import auto_pause as ap
    recs_path = tmp_path / "recs.json"
    factors_path = tmp_path / "factors.json"
    events_path = tmp_path / "events.jsonl"
    _write_recs_file(recs_path, [_pause_alert("forge_x")])
    _write_factors_file(factors_path, {"forge_x": 0.0})
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr(ap, "ALLOCATION_FACTORS_PATH", factors_path)
    monkeypatch.setattr(ap, "APPLY_EVENTS_PATH", events_path)

    result = ap.apply_recommendations(confirm=True)
    assert result["n_already_zero"] == 1
    assert result["n_paused"] == 0
    cfg = json.loads(factors_path.read_text())
    log = cfg.get("_kill_log") or []
    assert not any("AUTO-PAUSE-APPLIED" in s for s in log)


# ─── only PAUSE_RECOMMENDED is acted on; WARNING / OK ignored ────────

def test_apply_ignores_non_pause_alerts(monkeypatch, tmp_path):
    from ops import auto_pause as ap
    recs_path = tmp_path / "recs.json"
    factors_path = tmp_path / "factors.json"
    events_path = tmp_path / "events.jsonl"
    _write_recs_file(recs_path, [
        {"strategy": "forge_x", "alert_tier": "WARNING",
         "current_verdict": "FAIL",
         "consecutive_days_at_verdict": 2, "threshold_days": 5},
        {"strategy": "forge_y", "alert_tier": "OK",
         "current_verdict": "PASS_GATE",
         "consecutive_days_at_verdict": 0, "threshold_days": 0},
    ])
    _write_factors_file(factors_path, {"forge_x": 0.5, "forge_y": 1.0})
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr(ap, "ALLOCATION_FACTORS_PATH", factors_path)
    monkeypatch.setattr(ap, "APPLY_EVENTS_PATH", events_path)

    result = ap.apply_recommendations(confirm=True)
    assert result["n_candidates"] == 0
    cfg = json.loads(factors_path.read_text())
    assert cfg["factors"] == {"forge_x": 0.5, "forge_y": 1.0}


# ─── missing recommendations file raises ─────────────────────────────

def test_apply_missing_recommendations_file_raises(monkeypatch, tmp_path):
    from ops import auto_pause as ap
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError, match="recommendations"):
        ap.apply_recommendations(confirm=False)


# ─── multi-strategy: pauses each in one transaction ──────────────────

def test_apply_handles_multiple_candidates(monkeypatch, tmp_path):
    from ops import auto_pause as ap
    recs_path = tmp_path / "recs.json"
    factors_path = tmp_path / "factors.json"
    events_path = tmp_path / "events.jsonl"
    _write_recs_file(recs_path, [
        _pause_alert("forge_x"),
        _pause_alert("forge_y", days=7),
        {"strategy": "forge_z", "alert_tier": "OK",
         "current_verdict": "PASS_GATE",
         "consecutive_days_at_verdict": 0, "threshold_days": 0},
    ])
    _write_factors_file(factors_path, {
        "forge_x": 0.5, "forge_y": 1.0, "forge_z": 1.0,
    })
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", recs_path)
    monkeypatch.setattr(ap, "ALLOCATION_FACTORS_PATH", factors_path)
    monkeypatch.setattr(ap, "APPLY_EVENTS_PATH", events_path)

    result = ap.apply_recommendations(confirm=True)
    cfg = json.loads(factors_path.read_text())
    assert cfg["factors"]["forge_x"] == 0.0
    assert cfg["factors"]["forge_y"] == 0.0
    assert cfg["factors"]["forge_z"] == 1.0
    assert result["n_paused"] == 2
    # Single _kill_log entry mentioning both
    log = cfg.get("_kill_log") or []
    last = log[-1]
    assert "forge_x" in last and "forge_y" in last
