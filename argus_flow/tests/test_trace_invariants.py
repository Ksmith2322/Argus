"""Tests for helio.trace_invariants — cross-event invariant checks
and for the extended anomaly patterns in helio.trace_replay.

Plus tests for the ops.trace_inspect CLI tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.trace_invariants import InvariantViolation, verify_invariants
from helio.trace_replay import find_anomalies, load_trace
from ops import trace_inspect


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASCADE_RACE = FIXTURES / "cascade_race_20260519.jsonl"


# ─── ORDER_LIFECYCLE_VALID ────────────────────────────────────────────────

def test_lifecycle_passes_clean_sequence():
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "PendingSubmit"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "ORDER_LIFECYCLE_VALID"]
    assert out == []


def test_lifecycle_detects_regression_filled_then_submitted():
    """The textbook nasty: status appears to go backward."""
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "ORDER_LIFECYCLE_VALID"]
    assert len(out) == 1
    assert out[0].order_id == 1
    assert "Filled" in out[0].summary and "Submitted" in out[0].summary


def test_lifecycle_allows_repeat_of_same_status():
    """Broker can repeat the same status without it being a violation."""
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "ORDER_LIFECYCLE_VALID"]
    assert out == []


# ─── NO_FILL_WITHOUT_ORDER ────────────────────────────────────────────────

def test_no_fill_without_order_pass():
    trace = [
        {"event": "newOrder", "data": {"order_id": 42, "action": "BUY"}},
        {"event": "execDetails", "data": {"order_id": 42, "shares": 100, "price": 1.0}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "NO_FILL_WITHOUT_ORDER"]
    assert out == []


def test_no_fill_without_order_detects_orphan_fill():
    """Fill arrives for an order_id we never saw — runner missed the
    newOrder/orderStatus callback OR data corruption."""
    trace = [
        {"event": "execDetails", "data": {"order_id": 99, "shares": 100, "price": 1.0}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "NO_FILL_WITHOUT_ORDER"]
    assert len(out) == 1
    assert out[0].order_id == 99


# ─── NO_CANCEL_AFTER_FILL ─────────────────────────────────────────────────

def test_cancel_after_fill_detected_in_cascade_fixture():
    """The 5/19 fixture has exactly this signature — broker reports
    Cancelled on an already-Filled order during the cancel-sleep window."""
    events = load_trace(CASCADE_RACE)
    out = [v for v in verify_invariants(events) if v.invariant == "NO_CANCEL_AFTER_FILL"]
    assert len(out) >= 1
    assert all(v.order_id == 7421 for v in out)


def test_no_cancel_after_fill_passes_clean_cancellation():
    """Cancelled BEFORE Filled is fine — that's normal cancellation."""
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Cancelled"}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "NO_CANCEL_AFTER_FILL"]
    assert out == []


# ─── NO_DUPLICATE_EXEC_ID ─────────────────────────────────────────────────

def test_duplicate_exec_id_detected():
    trace = [
        {"event": "execDetails", "data": {"order_id": 1, "exec_id": "abc.001"}},
        {"event": "execDetails", "data": {"order_id": 2, "exec_id": "abc.001"}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "NO_DUPLICATE_EXEC_ID"]
    assert len(out) == 1
    assert "abc.001" in out[0].summary


def test_missing_exec_id_does_not_trigger():
    """Some broker variants omit exec_id; missing != duplicate."""
    trace = [
        {"event": "execDetails", "data": {"order_id": 1, "exec_id": None}},
        {"event": "execDetails", "data": {"order_id": 2, "exec_id": None}},
    ]
    out = [v for v in verify_invariants(trace) if v.invariant == "NO_DUPLICATE_EXEC_ID"]
    assert out == []


# ─── new anomaly patterns ─────────────────────────────────────────────────

def test_status_regression_detected():
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
    ]
    out = [a for a in find_anomalies(trace) if a.kind == "STATUS_REGRESSION"]
    assert len(out) == 1


def test_orphan_position_change_detected():
    """Position changes by 1000 but no execDetails between updates."""
    trace = [
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 1000}},
        # NO execDetails here
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 5000}},
    ]
    out = [a for a in find_anomalies(trace) if a.kind == "ORPHAN_POSITION_CHANGE"]
    assert len(out) == 1
    assert "EUR.USD" in out[0].summary


def test_orphan_position_change_NOT_triggered_when_fill_present():
    trace = [
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 1000}},
        {"event": "execDetails", "data": {"order_id": 1, "symbol": "EUR",
                                          "shares": 4000, "side": "BOT"}},
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 5000}},
    ]
    out = [a for a in find_anomalies(trace) if a.kind == "ORPHAN_POSITION_CHANGE"]
    assert out == []


def test_rapid_reversal_long_to_short():
    trace = [
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 10000}},
        {"event": "execDetails", "data": {"order_id": 1, "symbol": "EUR",
                                          "shares": 20000, "side": "SLD"}},
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": -10000}},
    ]
    out = [a for a in find_anomalies(trace) if a.kind == "RAPID_REVERSAL"]
    assert len(out) == 1
    assert "long to short" in out[0].summary


def test_rapid_reversal_short_to_long():
    trace = [
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": -5000}},
        {"event": "execDetails", "data": {"order_id": 1, "symbol": "EUR",
                                          "shares": 10000, "side": "BOT"}},
        {"event": "position", "data": {"symbol": "EUR", "currency": "USD",
                                       "local_symbol": "EUR.USD", "position": 5000}},
    ]
    out = [a for a in find_anomalies(trace) if a.kind == "RAPID_REVERSAL"]
    assert len(out) == 1
    assert "short to long" in out[0].summary


# ─── ops.trace_inspect CLI ────────────────────────────────────────────────

def test_inspect_returns_structured_report():
    report = trace_inspect.inspect(CASCADE_RACE)
    assert report["event_count"] == 9
    assert report["by_kind"]["orderStatus"] == 4
    assert report["by_kind"]["position"] == 2
    # Cascade fixture has NO_CANCEL_AFTER_FILL violation
    invs = report["invariant_violations"]
    assert any(v["invariant"] == "NO_CANCEL_AFTER_FILL" for v in invs)


def test_inspect_clean_trace_returns_empty_violations(tmp_path):
    clean = tmp_path / "clean.jsonl"
    clean.write_text(
        '{"event": "connected", "data": {}}\n'
        '{"event": "newOrder", "data": {"order_id": 1, "action": "BUY"}}\n'
        '{"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}}\n'
        '{"event": "execDetails", "data": {"order_id": 1, "shares": 100, "price": 1.0, "exec_id": "ex1"}}\n'
        '{"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}}\n',
        encoding="utf-8",
    )
    report = trace_inspect.inspect(clean)
    assert report["invariant_violations"] == []


def test_cli_exits_1_on_invariant_violation(capsys):
    rc = trace_inspect.main([str(CASCADE_RACE)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "INVARIANT VIOLATIONS" in captured.out
    assert "NO_CANCEL_AFTER_FILL" in captured.out


def test_cli_exits_0_on_clean_trace(tmp_path, capsys):
    clean = tmp_path / "clean.jsonl"
    clean.write_text(
        '{"event": "newOrder", "data": {"order_id": 1, "action": "BUY"}}\n'
        '{"event": "execDetails", "data": {"order_id": 1, "shares": 100, "exec_id": "ex1"}}\n',
        encoding="utf-8",
    )
    rc = trace_inspect.main([str(clean)])
    assert rc == 0


def test_cli_exits_2_on_missing_file(capsys):
    rc = trace_inspect.main(["does_not_exist.jsonl"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_cli_json_mode_emits_valid_json(capsys):
    rc = trace_inspect.main([str(CASCADE_RACE), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event_count"] == 9
    assert isinstance(parsed["invariant_violations"], list)
    # Exit code reflects the violation, regardless of output mode
    assert rc == 1


def test_inspect_handles_empty_trace(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    report = trace_inspect.inspect(empty)
    assert report["event_count"] == 0
    assert report["first_ts"] is None
    assert report["invariant_violations"] == []
