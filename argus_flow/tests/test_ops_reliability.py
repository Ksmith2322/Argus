"""Tests for helio.ops_reliability.

These cover the pure reducers that turn noisy operational evidence into the
capital-ladder fields.
"""
from __future__ import annotations

from datetime import datetime, timezone

from helio import ops_reliability as ops


NOW = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)


def test_phantom_counter_uses_annotation_and_window():
    rows = [
        {"annotation": "PHANTOM", "ts": "2026-05-11T12:00:00+00:00", "strategy": "a"},
        {"annotation": "OK", "ts": "2026-05-11T12:00:00+00:00", "strategy": "b"},
        {"annotation": "PHANTOM", "ts": "2026-03-01T12:00:00+00:00", "strategy": "c"},
        {"annotation": "phantom-sized", "ts": "", "strategy": "d"},
    ]

    count, examples, resolved = ops._count_phantom_rows(rows, NOW, 30)

    assert count == 2
    assert resolved == 0
    assert [e["strategy"] for e in examples] == ["a", "d"]


def test_phantom_counter_excludes_resolved_rows_before_resolution_time():
    rows = [
        {
            "annotation": "PHANTOM",
            "ts": "2026-05-05T12:00:00+00:00",
            "strategy": "forge_multi_orb",
            "source_file": "forge/logs/multi_orb/trades.csv",
        },
        {
            "annotation": "PHANTOM",
            "ts": "2026-05-09T12:00:00+00:00",
            "strategy": "forge_multi_orb",
            "source_file": "forge/logs/multi_orb/trades.csv",
        },
    ]
    resolutions = [
        {
            "strategy": "forge_multi_orb",
            "source_file": "forge/logs/multi_orb/trades.csv",
            "resolved_after": "2026-05-07T00:00:00+00:00",
        }
    ]

    count, examples, resolved = ops._count_phantom_rows(rows, NOW, 30, resolutions)

    assert count == 1
    assert resolved == 1
    assert examples[0]["timestamp"] == "2026-05-09T12:00:00+00:00"


def test_watchdog_counter_dedupes_same_day_kind():
    lines = [
        "2026-05-10T12:00:00Z HEARTBEAT | Paper=DOWN | Gateway=UP | Port=False",
        "2026-05-10T12:05:00Z HEARTBEAT | Paper=DOWN | Gateway=UP | Port=False",
        "2026-05-10T12:10:00Z MAX RESTARTS reached for paper session",
        "2026-05-11T12:00:00Z IB Gateway/TWS not running",
        "2026-03-01T12:00:00Z MAX RESTARTS reached for paper session",
    ]

    count, examples = ops._count_watchdog_events(lines, NOW, 30)

    assert count == 3
    assert [e["kind"] for e in examples] == ["max_restarts", "tws_api_port_false", "tws_down"]


def test_consecutive_true_days_uses_latest_record_per_day():
    history = [
        {"generated_at": "2026-05-10T10:00:00+00:00", "clean_current": True},
        {"generated_at": "2026-05-11T10:00:00+00:00", "clean_current": False},
        {"generated_at": "2026-05-11T11:00:00+00:00", "clean_current": True},
    ]
    current = {"generated_at": NOW.isoformat(), "clean_current": True}

    assert ops._consecutive_true_days(history, "clean_current", current, NOW) == 3


def test_consecutive_true_days_stops_on_gap():
    history = [
        {"generated_at": "2026-05-09T10:00:00+00:00", "broker_reconcile_passed": True},
        {"generated_at": "2026-05-10T10:00:00+00:00", "broker_reconcile_passed": True},
    ]
    current = {"generated_at": NOW.isoformat(), "broker_reconcile_passed": True}

    assert ops._consecutive_true_days(history, "broker_reconcile_passed", current, NOW) == 1


def test_margin_counter_reads_risk_report_and_watchdog_lines():
    risk = {
        "timestamp": "2026-05-11T12:00:00+00:00",
        "status": "RED",
        "recommendations": ["margin alert"],
    }
    lines = [
        "2026-05-11T12:10:00Z MARGIN_CAP_BREACH account warning",
        "2026-05-11T12:11:00Z MARGIN_CAP_BREACH account warning",
    ]

    count, examples = ops._count_margin_alerts(
        risk_report=risk,
        risk_ts=ops._risk_timestamp(risk),
        watchdog_lines=lines,
        now=NOW,
        window_days=30,
    )

    assert count == 2
    assert {e["kind"] for e in examples} == {"risk_oversight_margin", "watchdog_margin"}


def test_latest_ts_from_lines_picks_max_parseable():
    lines = [
        "[2026-04-20T14:15:42Z] HEARTBEAT | something",
        "noise that has no timestamp",
        "[2026-04-10T09:00:00Z] earlier line",
    ]

    latest = ops._latest_ts_from_lines(lines)

    assert latest is not None
    assert latest.isoformat() == "2026-04-20T14:15:42+00:00"


def test_latest_ts_from_lines_returns_none_when_no_timestamps():
    assert ops._latest_ts_from_lines(["no ts here", "still none"]) is None


def test_drill_record_passed_uses_pass_field_over_substring():
    warn_record = (
        '{"drill": "KILL_SWITCH", "status": "WARN", "pass": false, '
        '"note": "PASS requires KILL_SWITCH detection and fresh heartbeat."}'
    )
    assert ops._drill_record_passed(warn_record) is False


def test_drill_record_passed_accepts_explicit_pass_true():
    record = '{"drill": "KILL_SWITCH", "status": "PASS", "pass": true}'
    assert ops._drill_record_passed(record) is True


def test_drill_record_passed_plain_log_requires_explicit_marker():
    assert ops._drill_record_passed("drill completed; result: PASS") is True
    assert ops._drill_record_passed("description says PASS requires X") is False


def test_margin_counter_ignores_net_liquidation_balance_lines():
    count, examples = ops._count_margin_alerts(
        risk_report={},
        risk_ts=None,
        watchdog_lines=[
            "2026-05-11T12:10:00Z [OK] NetLiquidation: 1002591.71 USD",
        ],
        now=NOW,
        window_days=30,
    )

    assert count == 0
    assert examples == []
