"""Tests for helio.auto_pause — alert-only recommendation engine.

Validates the persistent-FAIL detector + alert classification. No
destructive actions tested; the module is alert-only by design.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.auto_pause import (
    StrategyAlert,
    _consecutive_days_at_verdict,
    append_history,
    compute_alerts,
    load_history,
    write_recommendations,
)


# ─── helpers ────────────────────────────────────────────────────────

def _make_history_row(ts: str, strategies: list[tuple[str, str]],
                       live_pf: float = 1.0, baseline: float = 1.86) -> dict:
    return {
        "ts": ts,
        "strategies": [
            {"strategy": s, "verdict": v, "live_pf_30trades": live_pf,
             "n_live_trades": 30, "baseline_ci_lower": baseline}
            for s, v in strategies
        ],
    }


# ─── _consecutive_days_at_verdict ──────────────────────────────────

def test_consecutive_days_counts_run_correctly():
    history = [
        _make_history_row("2026-05-19T00:00:00Z", [("X", "PASS_GATE")]),
        _make_history_row("2026-05-20T00:00:00Z", [("X", "FAIL")]),
        _make_history_row("2026-05-21T00:00:00Z", [("X", "FAIL")]),
        _make_history_row("2026-05-22T00:00:00Z", [("X", "FAIL")]),
    ]
    count, first = _consecutive_days_at_verdict(history, "X", {"FAIL"})
    assert count == 3
    assert first == "2026-05-20T00:00:00Z"


def test_consecutive_days_breaks_on_different_verdict():
    history = [
        _make_history_row("2026-05-19T00:00:00Z", [("X", "FAIL")]),
        _make_history_row("2026-05-20T00:00:00Z", [("X", "PASS_GATE")]),
        _make_history_row("2026-05-21T00:00:00Z", [("X", "FAIL")]),
        _make_history_row("2026-05-22T00:00:00Z", [("X", "FAIL")]),
    ]
    # Walking back from latest: FAIL FAIL [break PASS_GATE] FAIL
    count, first = _consecutive_days_at_verdict(history, "X", {"FAIL"})
    assert count == 2  # only the latest 2 FAILs (PASS_GATE breaks the streak)


def test_consecutive_days_handles_missing_strategy():
    history = [
        _make_history_row("2026-05-21T00:00:00Z", [("Y", "FAIL")]),
        _make_history_row("2026-05-22T00:00:00Z", [("Y", "FAIL")]),
    ]
    count, _ = _consecutive_days_at_verdict(history, "X", {"FAIL"})
    assert count == 0  # X not in any row → 0 consecutive days


def test_consecutive_days_empty_history():
    count, first = _consecutive_days_at_verdict([], "X", {"FAIL"})
    assert count == 0
    assert first is None


# ─── compute_alerts ────────────────────────────────────────────────

def test_compute_alerts_pause_recommended_after_K_fail_days():
    # 5 consecutive FAIL days → PAUSE_RECOMMENDED with default threshold
    history = [
        _make_history_row(f"2026-05-{20+i:02d}T00:00:00Z", [("X", "FAIL")])
        for i in range(5)
    ]
    alerts = compute_alerts(history=history, fail_days_for_pause=5)
    x_alert = next(a for a in alerts if a.strategy == "X")
    assert x_alert.alert_tier == "PAUSE_RECOMMENDED"
    assert x_alert.consecutive_days_at_verdict == 5


def test_compute_alerts_warning_when_fail_below_threshold():
    history = [
        _make_history_row(f"2026-05-{20+i:02d}T00:00:00Z", [("X", "FAIL")])
        for i in range(2)
    ]
    alerts = compute_alerts(history=history, fail_days_for_pause=5)
    x_alert = next(a for a in alerts if a.strategy == "X")
    assert x_alert.alert_tier == "WARNING"
    assert "Monitor closely" in x_alert.recommendation


def test_compute_alerts_ok_when_passing():
    history = [
        _make_history_row("2026-05-22T00:00:00Z", [("X", "PASS_GATE")]),
    ]
    alerts = compute_alerts(history=history)
    x_alert = next(a for a in alerts if a.strategy == "X")
    assert x_alert.alert_tier == "OK"
    assert "operating as expected" in x_alert.recommendation.lower()


def test_compute_alerts_handles_insufficient_n():
    history = [
        _make_history_row("2026-05-22T00:00:00Z", [("X", "INSUFFICIENT_N")]),
    ]
    alerts = compute_alerts(history=history)
    x_alert = next(a for a in alerts if a.strategy == "X")
    assert x_alert.alert_tier == "OK"
    assert "waiting for n" in x_alert.recommendation.lower()


def test_compute_alerts_warning_persistent_warning_status():
    # 3 consecutive WARNING days → WARNING flag
    history = [
        _make_history_row(f"2026-05-{20+i:02d}T00:00:00Z", [("X", "WARNING")])
        for i in range(3)
    ]
    alerts = compute_alerts(history=history, warning_days_for_flag=3)
    x_alert = next(a for a in alerts if a.strategy == "X")
    assert x_alert.alert_tier == "WARNING"


# ─── history I/O ────────────────────────────────────────────────────

def test_append_and_load_history_roundtrip(tmp_path):
    path = tmp_path / "hist.jsonl"
    report = {
        "generated_at": "2026-05-22T00:00:00Z",
        "baseline_version": "v1",
        "verdict_counts": {"PASS_GATE": 1, "FAIL": 1},
        "strategies": [
            {"strategy": "X", "verdict": "PASS_GATE", "live_pf_30trades": 1.8,
             "n_live_trades": 30, "pct_of_ci_lower": 1.0},
            {"strategy": "Y", "verdict": "FAIL", "live_pf_30trades": 0.5,
             "n_live_trades": 25, "pct_of_ci_lower": 0.3},
        ],
    }
    append_history(report, path=path)
    history = load_history(path=path)
    assert len(history) == 1
    assert history[0]["ts"] == "2026-05-22T00:00:00Z"
    assert len(history[0]["strategies"]) == 2


def test_load_history_returns_empty_on_missing_file(tmp_path):
    history = load_history(path=tmp_path / "does_not_exist.jsonl")
    assert history == []


def test_load_history_skips_corrupt_lines(tmp_path):
    path = tmp_path / "hist.jsonl"
    path.write_text('{"ts":"a","strategies":[]}\n{not json\n{"ts":"b","strategies":[]}\n',
                    encoding="utf-8")
    history = load_history(path=path)
    assert len(history) == 2


# ─── write_recommendations ──────────────────────────────────────────

def test_write_recommendations_produces_valid_json(tmp_path):
    path = tmp_path / "rec.json"
    alerts = [
        StrategyAlert(strategy="X", alert_tier="OK",
                      current_verdict="PASS_GATE",
                      consecutive_days_at_verdict=0,
                      threshold_days=0,
                      first_seen_at_verdict=None,
                      last_live_pf=1.8,
                      baseline_ci_lower=1.86,
                      recommendation="all good"),
    ]
    write_recommendations(alerts, path=path)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["alert_tier_counts"] == {"OK": 1}
    assert len(data["alerts"]) == 1
    assert data["alerts"][0]["strategy"] == "X"


# ─── ALERT-ONLY invariant (no destructive action) ──────────────────

def test_alert_only_no_allocation_factor_writes(tmp_path, monkeypatch):
    """The auto_pause module must NEVER touch allocation_factors.json or
    HALT.flag. This test ensures the import surface doesn't expose any
    mutation function for those files."""
    from helio import auto_pause as ap
    # No function should be named anything that mutates allocation/halt
    bad_names = ["set_allocation_factor", "halt_strategy", "write_halt_flag",
                 "kill_strategy", "set_factor_zero"]
    for name in bad_names:
        assert not hasattr(ap, name), (
            f"auto_pause exports {name} — auto-pause must be ALERT-ONLY")
