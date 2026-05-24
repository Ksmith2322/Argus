"""Tests for ops.daily_health_check.

Each component is exercised with mocks so the test doesn't depend on
the live filesystem state. The combined `evaluate()` is the unit
under test for status aggregation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── _run_auto_pause / _run_orphan_check / _run_preflight / _run_flag_check ─

def test_run_auto_pause_returns_green_on_rc_0(monkeypatch, tmp_path):
    import ops.daily_health_check as dhc
    from helio import auto_pause as ap
    monkeypatch.setattr(ap, "run", lambda **kw: 0)
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", tmp_path / "rec.json")
    (tmp_path / "rec.json").write_text(json.dumps({
        "alert_tier_counts": {"OK": 3}, "alerts": [],
    }), encoding="utf-8")
    result = dhc._run_auto_pause()
    assert result["status"] == "GREEN"
    assert result["exit_code"] == 0


def test_run_auto_pause_returns_red_on_rc_2(monkeypatch, tmp_path):
    import ops.daily_health_check as dhc
    from helio import auto_pause as ap
    monkeypatch.setattr(ap, "run", lambda **kw: 2)
    monkeypatch.setattr(ap, "RECOMMENDATIONS_PATH", tmp_path / "rec.json")
    (tmp_path / "rec.json").write_text(json.dumps({
        "alert_tier_counts": {"PAUSE_RECOMMENDED": 1, "OK": 4}, "alerts": [],
    }), encoding="utf-8")
    result = dhc._run_auto_pause()
    assert result["status"] == "RED"


def test_run_flag_check_red_when_halt_flag_present(monkeypatch, tmp_path):
    import ops.daily_health_check as dhc
    halt = tmp_path / "HALT.flag"
    halt.write_text("manual halt", encoding="utf-8")
    monkeypatch.setattr(dhc, "HALT_FLAG", halt)
    monkeypatch.setattr(dhc, "FLATTEN_FLAG", tmp_path / "FLATTEN_EOD.flag")
    result = dhc._run_flag_check()
    assert result["status"] == "RED"
    assert result["halt_flag"] is True


def test_run_flag_check_green_when_no_flags(monkeypatch, tmp_path):
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "HALT_FLAG", tmp_path / "absent_halt")
    monkeypatch.setattr(dhc, "FLATTEN_FLAG", tmp_path / "absent_flatten")
    result = dhc._run_flag_check()
    assert result["status"] == "GREEN"


def test_run_flag_check_red_when_flatten_flag_present(monkeypatch, tmp_path):
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "HALT_FLAG", tmp_path / "absent_halt")
    flatten = tmp_path / "FLATTEN_EOD.flag"
    flatten.write_text("emergency flatten", encoding="utf-8")
    monkeypatch.setattr(dhc, "FLATTEN_FLAG", flatten)
    result = dhc._run_flag_check()
    assert result["status"] == "RED"
    assert result["flatten_flag"] is True


# ─── evaluate() aggregation ──────────────────────────────────────────

def test_evaluate_returns_red_when_any_component_red(monkeypatch):
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "_run_auto_pause", lambda: {
        "component": "auto_pause", "status": "GREEN", "alert_tier_counts": {}})
    monkeypatch.setattr(dhc, "_run_orphan_check", lambda: {
        "component": "orphan_phantom", "status": "RED", "phantoms": [{}]})
    monkeypatch.setattr(dhc, "_run_preflight", lambda: {
        "component": "preflight", "status": "GREEN", "strategies": []})
    monkeypatch.setattr(dhc, "_run_flag_check", lambda: {
        "component": "flag_check", "status": "GREEN"})
    result = dhc.evaluate(post_discord=False)
    assert result["worst_status"] == "RED"


def test_evaluate_returns_yellow_when_only_yellow(monkeypatch):
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "_run_auto_pause", lambda: {
        "component": "auto_pause", "status": "YELLOW",
        "alert_tier_counts": {"WARNING": 1}})
    monkeypatch.setattr(dhc, "_run_orphan_check", lambda: {
        "component": "orphan_phantom", "status": "GREEN", "phantoms": []})
    monkeypatch.setattr(dhc, "_run_preflight", lambda: {
        "component": "preflight", "status": "GREEN", "strategies": []})
    monkeypatch.setattr(dhc, "_run_flag_check", lambda: {
        "component": "flag_check", "status": "GREEN"})
    result = dhc.evaluate(post_discord=False)
    assert result["worst_status"] == "YELLOW"


def test_evaluate_returns_green_when_all_green(monkeypatch):
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "_run_auto_pause", lambda: {
        "component": "auto_pause", "status": "GREEN",
        "alert_tier_counts": {"OK": 5}})
    monkeypatch.setattr(dhc, "_run_orphan_check", lambda: {
        "component": "orphan_phantom", "status": "GREEN", "phantoms": []})
    monkeypatch.setattr(dhc, "_run_preflight", lambda: {
        "component": "preflight", "status": "GREEN", "strategies": []})
    monkeypatch.setattr(dhc, "_run_flag_check", lambda: {
        "component": "flag_check", "status": "GREEN"})
    result = dhc.evaluate(post_discord=False)
    assert result["worst_status"] == "GREEN"


def test_evaluate_error_treated_as_red(monkeypatch):
    """A component that throws still counts as RED — fail-closed."""
    import ops.daily_health_check as dhc
    monkeypatch.setattr(dhc, "_run_auto_pause", lambda: {
        "component": "auto_pause", "status": "ERROR", "error": "boom"})
    monkeypatch.setattr(dhc, "_run_orphan_check", lambda: {
        "component": "orphan_phantom", "status": "GREEN", "phantoms": []})
    monkeypatch.setattr(dhc, "_run_preflight", lambda: {
        "component": "preflight", "status": "GREEN", "strategies": []})
    monkeypatch.setattr(dhc, "_run_flag_check", lambda: {
        "component": "flag_check", "status": "GREEN"})
    result = dhc.evaluate(post_discord=False)
    assert result["worst_status"] == "RED"


def test_evaluate_skips_discord_when_all_green(monkeypatch):
    """No Discord spam when everything is GREEN."""
    import ops.daily_health_check as dhc
    posted = []
    monkeypatch.setattr(dhc, "_run_auto_pause", lambda: {
        "component": "auto_pause", "status": "GREEN",
        "alert_tier_counts": {}})
    monkeypatch.setattr(dhc, "_run_orphan_check", lambda: {
        "component": "orphan_phantom", "status": "GREEN", "phantoms": []})
    monkeypatch.setattr(dhc, "_run_preflight", lambda: {
        "component": "preflight", "status": "GREEN", "strategies": []})
    monkeypatch.setattr(dhc, "_run_flag_check", lambda: {
        "component": "flag_check", "status": "GREEN"})

    def _fake_post_discord(reports):
        posted.append(reports)
        return False
    monkeypatch.setattr(dhc, "_post_discord", _fake_post_discord)
    result = dhc.evaluate(post_discord=True)
    # The _post_discord helper IS called but emits nothing internally
    # when all reports are GREEN. The wrapping evaluate() doesn't skip
    # the call — it's the _post_discord that returns early. So `posted`
    # has one entry, but with all-GREEN reports.
    assert len(posted) == 1
    statuses = {r.get("status") for r in posted[0]}
    assert statuses == {"GREEN"}
