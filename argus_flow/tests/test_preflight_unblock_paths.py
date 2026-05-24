"""Tests for the preflight unblock-recipe attachment (Codex gap #4).

Every RED/YELLOW check result must carry a specific `unblock` string
that tells the operator how to flip it GREEN. GREEN results carry no
recipe (none needed).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.real_money_preflight import (
    CheckResult,
    Verdict,
    _attach_unblock,
    _UNBLOCK_RECIPES,
    evaluate_strategy,
    check_evidence_quality,
)


# ─── _attach_unblock ─────────────────────────────────────────────────

def test_green_result_gets_no_unblock():
    r = CheckResult(name="in_active_roster", verdict=Verdict.GREEN,
                    value=True, reason="in roster")
    out = _attach_unblock(r)
    assert out.unblock == ""


def test_red_result_gets_unblock_recipe():
    r = CheckResult(name="real_money_allowlist", verdict=Verdict.RED,
                    value={"global_enabled": False},
                    reason="global_enabled=false")
    out = _attach_unblock(r)
    assert out.unblock != ""
    assert "global_enabled" in out.unblock


def test_yellow_result_also_gets_unblock_recipe():
    """YELLOW means review-before-scale; still wants an unblock path."""
    r = CheckResult(name="capacity_headroom_2x", verdict=Verdict.YELLOW,
                    value=1.0, reason="below 2x")
    out = _attach_unblock(r)
    assert "PER_CLUSTER_CAP_X" in out.unblock


def test_unknown_check_name_gets_empty_unblock():
    """A check name not in _UNBLOCK_RECIPES gets empty string, not crash."""
    r = CheckResult(name="some_future_check", verdict=Verdict.RED,
                    value=None, reason="?")
    out = _attach_unblock(r)
    assert out.unblock == ""


def test_all_known_check_names_have_unblock_recipes():
    """Every check name returned by evaluate_strategy must have a
    recipe. Otherwise a new check will silently ship without an
    unblock path."""
    p = evaluate_strategy("forge_does_not_exist")
    check_names = {c.name for c in p.checks}
    missing = check_names - set(_UNBLOCK_RECIPES.keys())
    assert not missing, (
        f"Check(s) without unblock recipe: {missing}. Add entries to "
        f"_UNBLOCK_RECIPES in helio/real_money_preflight.py."
    )


# ─── check_evidence_quality (Codex gap #5) ───────────────────────────

def test_evidence_quality_yellow_when_no_canonical_fills(monkeypatch, tmp_path):
    """Empty canonical_fills.jsonl after a fresh epoch reset → YELLOW
    (clean epoch but no fills yet)."""
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    # Create the directory but no canonical_fills file
    (tmp_path / "argus_flow" / "logs").mkdir(parents=True, exist_ok=True)
    result = check_evidence_quality("forge_xs_momentum")
    assert result.verdict == Verdict.YELLOW
    assert "no canonical_fills" in result.reason.lower()


def test_evidence_quality_red_when_exits_exceed_entries(monkeypatch, tmp_path):
    """If a runner dropped ENTRY writes, EXIT rows will outnumber ENTRY
    rows → RED."""
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    fills = tmp_path / "argus_flow" / "logs" / "canonical_fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    # 1 ENTRY + 3 EXITs — clearly broken
    rows = [
        {"strategy": "forge_xs_momentum", "side": "ENTRY", "ts": "2026-06-01T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "EXIT", "ts": "2026-06-02T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "EXIT", "ts": "2026-06-03T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "EXIT", "ts": "2026-06-04T20:00:00Z"},
    ]
    fills.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    # Mock the evidence epoch to a date before all rows
    class _Epoch:
        @property
        def started_at(self):
            from datetime import datetime as _dt
            return _dt.fromisoformat("2026-05-22T00:00:00")
    monkeypatch.setattr(
        "helio.evidence_epoch.current_epoch", lambda: _Epoch())
    result = check_evidence_quality("forge_xs_momentum")
    assert result.verdict == Verdict.RED
    assert "EXIT-only" in result.reason


def test_evidence_quality_green_when_balanced_post_epoch(monkeypatch, tmp_path):
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    fills = tmp_path / "argus_flow" / "logs" / "canonical_fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"strategy": "forge_xs_momentum", "side": "ENTRY", "ts": "2026-06-01T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "EXIT", "ts": "2026-06-02T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "ENTRY", "ts": "2026-07-01T20:00:00Z"},
        {"strategy": "forge_xs_momentum", "side": "EXIT", "ts": "2026-07-02T20:00:00Z"},
    ]
    fills.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    class _Epoch:
        @property
        def started_at(self):
            from datetime import datetime as _dt
            return _dt.fromisoformat("2026-05-22T00:00:00")
    monkeypatch.setattr(
        "helio.evidence_epoch.current_epoch", lambda: _Epoch())
    result = check_evidence_quality("forge_xs_momentum")
    assert result.verdict == Verdict.GREEN
    assert result.value["entries"] == 2
    assert result.value["exits"] == 2


def test_evidence_quality_red_when_pre_epoch_rows_present(monkeypatch, tmp_path):
    """A row dated BEFORE epoch start → contaminated evidence → RED."""
    import helio.real_money_preflight as p
    monkeypatch.setattr(p, "REPO", tmp_path)
    fills = tmp_path / "argus_flow" / "logs" / "canonical_fills.jsonl"
    fills.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"strategy": "forge_xs_momentum", "side": "ENTRY",
         "ts": "2026-04-15T20:00:00Z"},   # pre-epoch
        {"strategy": "forge_xs_momentum", "side": "EXIT",
         "ts": "2026-06-02T20:00:00Z"},
    ]
    fills.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    class _Epoch:
        @property
        def started_at(self):
            from datetime import datetime as _dt
            return _dt.fromisoformat("2026-05-22T00:00:00")
    monkeypatch.setattr(
        "helio.evidence_epoch.current_epoch", lambda: _Epoch())
    result = check_evidence_quality("forge_xs_momentum")
    assert result.verdict == Verdict.RED
    assert "pre-epoch" in result.reason.lower()
