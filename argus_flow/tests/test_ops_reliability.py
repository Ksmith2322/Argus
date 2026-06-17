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


def test_drill_record_passed_strips_utf8_bom_from_powershell_output():
    bom_json = "﻿" + '{"drill": "KILL_SWITCH", "status": "PASS", "pass": true}'
    assert ops._drill_record_passed(bom_json) is True
    bom_warn = "﻿" + '{"status": "WARN", "pass": false}'
    assert ops._drill_record_passed(bom_warn) is False


# ---------------------------------------------------------------------------
# Bug 4 (2026-05-13 audit): broker_tripped string-vs-bool parse
# ---------------------------------------------------------------------------


def test_parse_tripped_flag_accepts_real_booleans():
    assert ops._parse_tripped_flag(True) is True
    assert ops._parse_tripped_flag(False) is False


def test_parse_tripped_flag_accepts_none_as_not_tripped():
    assert ops._parse_tripped_flag(None) is False


def test_parse_tripped_flag_string_false_is_not_tripped():
    # The bug: bool("false") is True in Python because non-empty strings are
    # truthy. A manual edit writing the string "false" would silently re-trip
    # the gate. After the fix, it's parsed as a not-tripped indicator.
    assert ops._parse_tripped_flag("false") is False
    assert ops._parse_tripped_flag("False") is False
    assert ops._parse_tripped_flag("  FALSE  ") is False


def test_parse_tripped_flag_string_true_is_tripped():
    assert ops._parse_tripped_flag("true") is True
    assert ops._parse_tripped_flag("True") is True


def test_parse_tripped_flag_unknown_shape_fails_closed_to_tripped():
    # Defensive: anything not in the recognized shapes is treated as tripped
    # so a malformed state file errs toward blocking promotion.
    assert ops._parse_tripped_flag({"tripped": True}) is True  # dict
    assert ops._parse_tripped_flag(["true"]) is True  # list
    assert ops._parse_tripped_flag("garbage") is False  # explicit string that's not "true" stays not-tripped


def test_parse_tripped_flag_numeric():
    assert ops._parse_tripped_flag(1) is True
    assert ops._parse_tripped_flag(0) is False


# ---------------------------------------------------------------------------
# Bug 1 (2026-05-13 audit): clean_current decoupled from 30-day rolling
# ---------------------------------------------------------------------------


def test_watchdog_counter_window_days_1_excludes_old_events():
    """The streak math passes window_days=1 to count today's events only.
    Historical events 4-5 days old should be excluded even if still inside
    the 30-day rolling window."""
    lines = [
        # ~5 days ago — should appear in 30d count, NOT in 1d count
        "2026-05-08T12:10:00Z MAX RESTARTS reached for paper session",
        # ~4 hours ago — should appear in both
        "2026-05-12T08:00:00Z MAX RESTARTS reached for paper session",
    ]

    count_30d, _ = ops._count_watchdog_events(lines, NOW, 30)
    count_1d, _ = ops._count_watchdog_events(lines, NOW, 1)

    assert count_30d == 2
    assert count_1d == 1  # only the recent one


def test_watchdog_counter_window_days_1_empty_when_no_recent_events():
    """With only old events, window_days=1 returns zero. This is the case
    that previously kept clean_current=False forever after a cascade burst."""
    lines = [
        "2026-04-14T23:48:15Z CRITICAL: IB Gateway/TWS process NOT running for 3 checks",
        "2026-04-15T23:48:20Z CRITICAL: IB Gateway/TWS process NOT running for 3 checks",
    ]

    count_30d, _ = ops._count_watchdog_events(lines, NOW, 30)
    count_1d, _ = ops._count_watchdog_events(lines, NOW, 1)

    assert count_30d == 2
    assert count_1d == 0  # streak can advance even though 30d count nonzero


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
