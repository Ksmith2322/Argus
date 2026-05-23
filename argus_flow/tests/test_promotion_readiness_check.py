"""Tests for helio.promotion_readiness_check.

Validates each precondition's pass/fail logic against synthetic fixtures.
The committed checks against the live repo are smoke tests — they
should produce reasonable verdicts on the current state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio import promotion_readiness_check as prc


# ─── individual check unit tests ───────────────────────────────────

def test_check_allocation_active_when_factor_positive(monkeypatch, tmp_path):
    fake = tmp_path / "alloc.json"
    fake.write_text(json.dumps({"factors": {"X": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOCATION_PATH", fake)
    c = prc.check_allocation("X")
    assert c.passed
    assert "0.5" in c.detail


def test_check_allocation_fails_when_zero(monkeypatch, tmp_path):
    fake = tmp_path / "alloc.json"
    fake.write_text(json.dumps({"factors": {"X": 0.0}}), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOCATION_PATH", fake)
    c = prc.check_allocation("X")
    assert not c.passed


def test_check_allocation_fails_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(prc, "ALLOCATION_PATH", tmp_path / "missing.json")
    c = prc.check_allocation("X")
    assert not c.passed
    assert "missing" in c.detail.lower() or "unreadable" in c.detail.lower()


def test_check_history_depth_passes_at_threshold(monkeypatch, tmp_path):
    fake = tmp_path / "hist.jsonl"
    rows = [
        {"ts": f"2026-05-{i:02d}T00:00:00Z",
         "strategies": [{"strategy": "X", "verdict": "PASS_GATE"}]}
        for i in range(1, 32)  # 31 rows
    ]
    fake.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(prc, "HISTORY_PATH", fake)
    c = prc.check_history_depth("X", min_days=30)
    assert c.passed


def test_check_history_depth_fails_short(monkeypatch, tmp_path):
    fake = tmp_path / "hist.jsonl"
    rows = [{"ts": "2026-05-01T00:00:00Z", "strategies": [{"strategy": "X"}]}] * 5
    fake.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(prc, "HISTORY_PATH", fake)
    c = prc.check_history_depth("X", min_days=30)
    assert not c.passed
    assert "5" in c.detail


def test_check_live_pf_consistency_passes_when_mostly_passing(monkeypatch, tmp_path):
    fake = tmp_path / "hist.jsonl"
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    # 10 recent snapshots, 9 PASS_GATE
    rows = []
    for i in range(10):
        ts = (now - timedelta(days=i)).isoformat()
        verdict = "PASS_GATE" if i != 5 else "WARNING"
        rows.append({"ts": ts, "strategies": [{"strategy": "X", "verdict": verdict}]})
    fake.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(prc, "HISTORY_PATH", fake)
    c = prc.check_live_pf_consistency("X", min_passing_fraction=0.80, now=now)
    assert c.passed
    assert "90%" in c.detail


def test_check_live_pf_consistency_fails_when_inconsistent(monkeypatch, tmp_path):
    fake = tmp_path / "hist.jsonl"
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    # 10 recent snapshots, only 3 PASS_GATE → 30% passing
    rows = []
    for i in range(10):
        ts = (now - timedelta(days=i)).isoformat()
        verdict = "PASS_GATE" if i < 3 else "WARNING"
        rows.append({"ts": ts, "strategies": [{"strategy": "X", "verdict": verdict}]})
    fake.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(prc, "HISTORY_PATH", fake)
    c = prc.check_live_pf_consistency("X", min_passing_fraction=0.80, now=now)
    assert not c.passed


def test_check_no_pause_alert_fails_on_pause(monkeypatch, tmp_path):
    fake = tmp_path / "rec.json"
    fake.write_text(json.dumps({"alerts": [
        {"strategy": "X", "alert_tier": "PAUSE_RECOMMENDED",
         "recommendation": "test"}
    ]}), encoding="utf-8")
    monkeypatch.setattr(prc, "RECOMMENDATIONS_PATH", fake)
    c = prc.check_no_pause_alert("X")
    assert not c.passed
    assert "PAUSE_RECOMMENDED" in c.detail


def test_check_no_pause_alert_fails_on_warning(monkeypatch, tmp_path):
    fake = tmp_path / "rec.json"
    fake.write_text(json.dumps({"alerts": [
        {"strategy": "X", "alert_tier": "WARNING", "recommendation": "test"}
    ]}), encoding="utf-8")
    monkeypatch.setattr(prc, "RECOMMENDATIONS_PATH", fake)
    c = prc.check_no_pause_alert("X")
    assert not c.passed


def test_check_no_pause_alert_passes_on_ok(monkeypatch, tmp_path):
    fake = tmp_path / "rec.json"
    fake.write_text(json.dumps({"alerts": [
        {"strategy": "X", "alert_tier": "OK", "recommendation": "fine"}
    ]}), encoding="utf-8")
    monkeypatch.setattr(prc, "RECOMMENDATIONS_PATH", fake)
    c = prc.check_no_pause_alert("X")
    assert c.passed


def test_check_no_halt_flag_passes_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(prc, "HALT_FLAG_PATH", tmp_path / "no_such_halt.flag")
    c = prc.check_no_halt_flag()
    assert c.passed


def test_check_no_halt_flag_fails_when_present(monkeypatch, tmp_path):
    halt = tmp_path / "HALT.flag"
    halt.write_text("emergency stop", encoding="utf-8")
    monkeypatch.setattr(prc, "HALT_FLAG_PATH", halt)
    c = prc.check_no_halt_flag()
    assert not c.passed
    assert "emergency stop" in c.detail


def test_check_allowlist_entry_fails_when_empty(monkeypatch, tmp_path):
    fake = tmp_path / "allowlist.json"
    fake.write_text(json.dumps({"strategies": []}), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOWLIST_PATH", fake)
    c = prc.check_allowlist_entry("X")
    assert not c.passed


def test_check_allowlist_entry_passes_when_listed(monkeypatch, tmp_path):
    fake = tmp_path / "allowlist.json"
    fake.write_text(json.dumps({"strategies": ["X", "Y"]}), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOWLIST_PATH", fake)
    c = prc.check_allowlist_entry("X")
    assert c.passed


def test_check_global_enabled_fails_default(monkeypatch, tmp_path):
    fake = tmp_path / "allowlist.json"
    fake.write_text(json.dumps({"global_enabled": False}), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOWLIST_PATH", fake)
    c = prc.check_global_enabled()
    assert not c.passed


def test_check_ledger_fields_fails_when_blank(monkeypatch, tmp_path):
    fake = tmp_path / "allowlist.json"
    fake.write_text(json.dumps({
        "ledger_entry_id": "",
        "approver": "",
        "signed_at": "",
    }), encoding="utf-8")
    monkeypatch.setattr(prc, "ALLOWLIST_PATH", fake)
    c = prc.check_ledger_fields_populated()
    assert not c.passed
    assert "ledger_entry_id" in c.detail


def test_check_baseline_ci_lower_meets_floor():
    """xs_momentum's baseline CI lower is 1.86 per the committed config —
    should pass the 1.20 floor."""
    c = prc.check_baseline_ci_lower("forge_xs_momentum", floor=1.20)
    assert c.passed


def test_check_baseline_ci_lower_fails_for_gld_pm_long():
    """gld_pm_long's baseline CI lower is 1.08 — below the 1.20 floor."""
    c = prc.check_baseline_ci_lower("forge_gld_pm_long", floor=1.20)
    assert not c.passed


# ─── evaluate (orchestration) ──────────────────────────────────────

def test_evaluate_returns_not_ready_on_default_state():
    """Against the actual committed state, no strategy should be READY:
    global_enabled is False, allowlist is empty."""
    report = prc.evaluate("forge_xs_momentum")
    assert report.verdict == "NOT_READY"
    assert len(report.blockers) > 0


def test_evaluate_xs_momentum_passes_some_checks():
    """xs_momentum should pass at least allocation, baseline_ci_floor, and
    capital_ladder — those don't depend on the allowlist flip."""
    report = prc.evaluate("forge_xs_momentum")
    passed_names = {c.name for c in report.checks if c.passed}
    # These three should definitely pass
    assert "allocation_active" in passed_names
    assert "baseline_ci_floor" in passed_names
    assert "capital_ladder" in passed_names


def test_evaluate_returns_structured_report():
    report = prc.evaluate("forge_xs_momentum")
    d = report.to_dict()
    assert d["strategy"] == "forge_xs_momentum"
    assert "verdict" in d
    assert "checks" in d
    assert d["n_checks"] == len(d["checks"])


def test_render_text_returns_string_with_checks():
    report = prc.evaluate("forge_xs_momentum")
    text = prc.render_text(report)
    assert "Promotion readiness" in text
    assert "forge_xs_momentum" in text
    # Every check should appear in the text
    for c in report.checks:
        assert c.name in text
