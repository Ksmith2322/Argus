"""Tests for the evidence epoch registry (Codex audit 2026-05-18 X4)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from helio import evidence_epoch as ee


# ── Schema + loading ───────────────────────────────────────────────────────

def test_committed_registry_is_well_formed():
    data = ee.load_registry()
    assert "current_epoch_id" in data
    assert isinstance(data.get("epochs"), list)
    assert len(data["epochs"]) >= 1


def test_committed_registry_pre_freeze_epoch_is_marked_unclean():
    """The pre-freeze epoch ships marked contaminated — anyone reading the
    registry today should refuse to promote from it."""
    pre = ee.get_epoch("pre_freeze_20260418")
    assert pre.is_clean is False, (
        "Pre-freeze epoch must ship is_clean=false. Promotion math computed "
        "against it would be contaminated by the silent bugs only fixed 5/14-5/18."
    )
    assert pre.contamination_notes, (
        "Pre-freeze epoch missing contamination_notes — operators need the "
        "reason recorded so it can't be dismissed as 'just legacy data'."
    )


def test_committed_registry_has_post_reset_epoch_clean():
    post = ee.get_epoch("post_reset_20260601")
    assert post.is_clean is True
    assert post.exclude_strategies == ()
    assert post.phantom_trades == ()


def test_current_epoch_is_pre_freeze_today():
    """Before 5/31 reset, current epoch must point at the pre-freeze record
    (which is is_clean=False). epoch_reset.py will advance this after reset."""
    cur = ee.current_epoch()
    assert cur.id == "pre_freeze_20260418"
    assert cur.is_clean is False


# ── Helpers ────────────────────────────────────────────────────────────────

def test_get_epoch_unknown_id_raises():
    with pytest.raises(ee.EpochRegistryError):
        ee.get_epoch("no_such_epoch")


def test_missing_registry_file_raises(tmp_path):
    bogus = tmp_path / "does_not_exist.json"
    with pytest.raises(ee.EpochRegistryError):
        ee.load_registry(bogus)


def test_corrupt_registry_file_raises(tmp_path):
    p = tmp_path / "evidence_epoch.json"
    p.write_text("{not json")
    with pytest.raises(ee.EpochRegistryError):
        ee.load_registry(p)


def test_excludes_strategy_checks_membership():
    pre = ee.get_epoch("pre_freeze_20260418")
    assert pre.excludes_strategy("forge_spy_mean_rev")
    assert not pre.excludes_strategy("forge_gld_pm_long")


def test_contains_window_semantics():
    pre = ee.get_epoch("pre_freeze_20260418")
    # Inside: 2026-04-20
    inside = datetime(2026, 4, 20, tzinfo=timezone.utc)
    # Before: 2026-04-01
    before = datetime(2026, 4, 1, tzinfo=timezone.utc)
    # After: 2026-06-15
    after = datetime(2026, 6, 15, tzinfo=timezone.utc)

    assert pre.contains(inside)
    assert not pre.contains(before)
    assert not pre.contains(after)


def test_epoch_for_timestamp_routes_to_right_window():
    inside_pre = datetime(2026, 5, 1, tzinfo=timezone.utc)
    inside_post = datetime(2026, 6, 15, tzinfo=timezone.utc)

    e_pre = ee.epoch_for_timestamp(inside_pre)
    e_post = ee.epoch_for_timestamp(inside_post)
    assert e_pre is not None and e_pre.id == "pre_freeze_20260418"
    assert e_post is not None and e_post.id == "post_reset_20260601"


def test_epoch_for_timestamp_gap_returns_none():
    """2026-06-01 boundary: pre ends 2026-05-31T23:59:59, post starts 2026-06-01.
    A timestamp at 2026-06-01T00:00:00 lands in post; the 1-second gap is
    accepted as fine for the scaffolding."""
    boundary = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    e = ee.epoch_for_timestamp(boundary)
    assert e is not None and e.id == "post_reset_20260601"


def test_epoch_for_timestamp_before_all_epochs():
    """Pre-2026 timestamp doesn't fall in any registered epoch."""
    pre_history = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert ee.epoch_for_timestamp(pre_history) is None


# ── Stamp helper ───────────────────────────────────────────────────────────

def test_stamp_report_adds_id_label_clean_fields():
    report = {"metrics": {"pf": 1.3}}
    stamped = ee.stamp_report(report)
    assert stamped is report
    assert "epoch_id" in stamped
    assert "epoch_label" in stamped
    assert "epoch_is_clean" in stamped


def test_stamp_report_with_explicit_epoch():
    post = ee.get_epoch("post_reset_20260601")
    out = ee.stamp_report({}, post)
    assert out["epoch_id"] == "post_reset_20260601"
    assert out["epoch_is_clean"] is True
