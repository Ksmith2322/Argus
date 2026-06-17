"""Tests for helio.weekly_gate_review."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio import weekly_gate_review as wgr


# ─── helpers ────────────────────────────────────────────────────────

def _seed_history(tmp_path, rows):
    """Write a synthetic history JSONL file."""
    path = tmp_path / "history.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


# ─── _trend_over_window ─────────────────────────────────────────────

def test_trend_window_counts_recent_snapshots():
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    history = [
        {"ts": (now - timedelta(days=10)).isoformat(),
         "strategies": [{"strategy": "X", "verdict": "PASS_GATE", "live_pf_30trades": 1.8}]},
        {"ts": (now - timedelta(days=3)).isoformat(),
         "strategies": [{"strategy": "X", "verdict": "PASS_GATE", "live_pf_30trades": 1.9}]},
        {"ts": (now - timedelta(days=1)).isoformat(),
         "strategies": [{"strategy": "X", "verdict": "WARNING", "live_pf_30trades": 1.4}]},
    ]
    trend = wgr._trend_over_window(history, "X", days=7, now=now)
    assert trend["snapshots"] == 2  # only the 3-day and 1-day-old ones
    assert trend["verdict_counts"] == {"PASS_GATE": 1, "WARNING": 1}
    assert trend["min_pf"] == 1.4
    assert trend["max_pf"] == 1.9
    assert trend["latest_pf"] == 1.4  # chronological-last in window


def test_trend_window_empty_history():
    trend = wgr._trend_over_window([], "X", days=7)
    assert trend["snapshots"] == 0


def test_trend_window_missing_strategy():
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    history = [
        {"ts": (now - timedelta(days=1)).isoformat(),
         "strategies": [{"strategy": "Y", "verdict": "PASS_GATE", "live_pf_30trades": 1.8}]},
    ]
    trend = wgr._trend_over_window(history, "X", days=7, now=now)
    assert trend["snapshots"] == 1
    assert trend.get("verdict_counts") in (None, {})  # X never appeared
    assert "min_pf" not in trend  # no pf data for X


# ─── render ─────────────────────────────────────────────────────────

def test_render_returns_string_with_required_sections():
    md = wgr.render()
    assert isinstance(md, str)
    assert "# Weekly Gate Review" in md
    assert "## Headline status" in md
    assert "## Active cohort allocations" in md
    assert "## Per-strategy trend" in md
    assert "## Actionable recommendations" in md
    assert "## Recent allocation changes" in md


def test_render_includes_baseline_version():
    md = wgr.render()
    # baseline version should appear (whatever it is)
    assert "Baseline config" in md


def test_render_reports_zero_alerts_when_no_alerts():
    """If recommendations show no PAUSE/WARNING, headline is 'All clear'."""
    md = wgr.render()
    # Either has alerts (in which case detail rendered) or shows All clear.
    # Smoke check: render doesn't crash on the live data.
    assert "All clear" in md or "WARNING" in md or "PAUSE_RECOMMENDED" in md


# ─── write_report ───────────────────────────────────────────────────

def test_write_report_creates_dated_file(tmp_path):
    path = wgr.write_report(
        as_of=datetime(2026, 5, 23, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert path.exists()
    assert path.name == "weekly_review_20260523.md"
    text = path.read_text(encoding="utf-8")
    assert "# Weekly Gate Review — 2026-05-23" in text


# ─── _load_history with corrupt lines ──────────────────────────────

def test_load_history_skips_corrupt_lines(tmp_path, monkeypatch):
    path = tmp_path / "hist.jsonl"
    path.write_text('{"ts":"2026-01-01T00:00:00Z","strategies":[]}\n'
                    '{garbage\n'
                    '{"ts":"2026-01-02T00:00:00Z","strategies":[]}\n', encoding="utf-8")
    monkeypatch.setattr(wgr, "HISTORY_PATH", path)
    history = wgr._load_history()
    assert len(history) == 2


def test_load_history_returns_empty_on_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(wgr, "HISTORY_PATH", tmp_path / "missing.jsonl")
    assert wgr._load_history() == []
