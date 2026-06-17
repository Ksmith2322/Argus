"""Tests for helio.trace_parity (Codex X1 capture-replay parity) +
ops.trace_parity CLI. Includes self-parity demo: dispatch the cascade
fixture through the harness, capture the subscriber's calls, convert
back to trace form, and verify it matches the original modulo timing
fields."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.event_dispatcher import (
    EventDispatcher, RecordingSubscriber, make_target, subscribe_recording,
)
from helio.trace_parity import (
    DEFAULT_IGNORE_FIELDS, TraceDifference, compare_traces, subscriber_to_trace,
)
from helio.trace_replay import load_trace
from ops import trace_parity


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASCADE_RACE = FIXTURES / "cascade_race_20260519.jsonl"


# ─── compare_traces ──────────────────────────────────────────────────────

def test_identical_traces_have_no_diffs():
    a = [
        {"event": "newOrder", "data": {"order_id": 1, "action": "BUY"}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 100, "price": 1.0}},
    ]
    b = [dict(e) for e in a]
    assert compare_traces(a, b) == []


def test_ts_difference_ignored_by_default():
    a = [{"ts": "2026-05-20T15:30:00Z", "event": "newOrder",
          "data": {"order_id": 1}}]
    b = [{"ts": "2026-05-20T16:30:00Z", "event": "newOrder",
          "data": {"order_id": 1}}]
    # ts is in DEFAULT_IGNORE_FIELDS — no diff expected
    assert compare_traces(a, b) == []


def test_monotonic_ms_ignored_by_default():
    a = [{"monotonic_ms": 100, "event": "newOrder", "data": {"order_id": 1}}]
    b = [{"monotonic_ms": 999, "event": "newOrder", "data": {"order_id": 1}}]
    assert compare_traces(a, b) == []


def test_field_value_mismatch_detected():
    a = [{"event": "orderStatus",
          "data": {"order_id": 1, "status": "Filled"}}]
    b = [{"event": "orderStatus",
          "data": {"order_id": 1, "status": "Cancelled"}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert diffs[0].kind == "VALUE_MISMATCH"
    assert diffs[0].field == "data.status"
    assert "Filled" in diffs[0].summary and "Cancelled" in diffs[0].summary


def test_event_kind_mismatch_detected():
    a = [{"event": "newOrder", "data": {"order_id": 1}}]
    b = [{"event": "execDetails", "data": {"order_id": 1}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert diffs[0].kind == "EVENT_KIND_MISMATCH"


def test_missing_event_in_b_flagged():
    a = [
        {"event": "newOrder", "data": {"order_id": 1}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 100}},
    ]
    b = [{"event": "newOrder", "data": {"order_id": 1}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert diffs[0].kind == "MISSING_IN_B"
    assert diffs[0].at_index == 1
    assert "execDetails" in diffs[0].summary


def test_extra_event_in_b_flagged():
    a = [{"event": "newOrder", "data": {"order_id": 1}}]
    b = [
        {"event": "newOrder", "data": {"order_id": 1}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 100}},
    ]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert diffs[0].kind == "EXTRA_IN_B"
    assert diffs[0].at_index == 1


def test_ignore_fields_override_works():
    a = [{"event": "newOrder", "data": {"order_id": 1, "perm_id": 100}}]
    b = [{"event": "newOrder", "data": {"order_id": 1, "perm_id": 200}}]
    # Default: perm_id is NOT ignored, so a diff would be reported
    assert len(compare_traces(a, b)) == 1
    # Override: ignore perm_id → no diff
    assert compare_traces(a, b, ignore_fields=("ts", "monotonic_ms", "perm_id")) == []


def test_value_mismatch_inside_nested_data():
    """price is inside data.* — strip should preserve it for comparison."""
    a = [{"event": "execDetails", "data": {"order_id": 1, "price": 1.0}}]
    b = [{"event": "execDetails", "data": {"order_id": 1, "price": 2.0}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert diffs[0].field == "data.price"


def test_two_diffs_in_single_event():
    a = [{"event": "execDetails",
          "data": {"order_id": 1, "shares": 100, "price": 1.0}}]
    b = [{"event": "execDetails",
          "data": {"order_id": 1, "shares": 200, "price": 2.0}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 2
    fields = {d.field for d in diffs}
    assert fields == {"data.shares", "data.price"}


def test_field_present_in_a_only_is_diff():
    a = [{"event": "newOrder", "data": {"order_id": 1, "extra": "x"}}]
    b = [{"event": "newOrder", "data": {"order_id": 1}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert "extra" in diffs[0].field


def test_field_present_in_b_only_is_diff():
    a = [{"event": "newOrder", "data": {"order_id": 1}}]
    b = [{"event": "newOrder", "data": {"order_id": 1, "extra": "y"}}]
    diffs = compare_traces(a, b)
    assert len(diffs) == 1
    assert "extra" in diffs[0].field


# ─── self-parity: dispatch + capture + compare ────────────────────────────

def test_self_parity_cascade_fixture_round_trips_through_harness():
    """The key validation: load a real fixture, dispatch through the
    Phase 3 harness, capture what the subscriber receives, convert back
    to trace form, and verify it matches the original modulo timing.

    A mismatch here = bug in the recorder, dispatcher, or reconstruction
    layer. This is the harness self-test."""
    original = load_trace(CASCADE_RACE)
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)
    EventDispatcher(target).dispatch(original, mode="instant")

    replayed = subscriber_to_trace(sub)
    # The RecordingSubscriber only captures a subset of fields per event
    # kind (e.g., for orderStatus: order_id + status), so we constrain the
    # comparison to ignore everything the subscriber doesn't see.
    LOSSY_IGNORE = DEFAULT_IGNORE_FIELDS + (
        "filled", "remaining", "avg_fill_price", "perm_id",
        "currency", "local_symbol", "account", "avg_cost",
        "exec_id", "side", "time",  # execDetails subset that RecordingSubscriber doesn't keep
        "qty", "type", "lmt_price", "tif",  # newOrder subset
        "symbol",  # subscriber's position log keeps symbol but as positional arg
        "recorder_attached",
    )
    diffs = compare_traces(original, replayed, ignore_fields=LOSSY_IGNORE)
    # NB: original has length 9, replayed has length 9 (subscriber gets all events)
    # Field-level diffs will be 0 because everything not captured is in
    # LOSSY_IGNORE. If diffs > 0 there's a real bug in dispatch/reconstruct.
    assert diffs == [], f"unexpected diffs: {diffs}"


def test_chaos_swap_produces_detectable_diff_in_parity_check():
    """Apply chaos, capture, compare. The swap should show as VALUE_MISMATCH
    at the swapped indices."""
    from helio.event_dispatcher import ChaosTransform
    original = load_trace(CASCADE_RACE)

    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)
    EventDispatcher(target).dispatch(
        original, mode="instant",
        chaos=[ChaosTransform(kind="swap", target_idx=5, other_idx=6)],
    )

    replayed = subscriber_to_trace(sub)
    diffs = compare_traces(original, replayed,
                           ignore_fields=DEFAULT_IGNORE_FIELDS + (
                               "filled", "remaining", "avg_fill_price",
                               "perm_id", "currency", "local_symbol", "account",
                               "avg_cost", "exec_id", "side", "time",
                               "qty", "type", "lmt_price", "tif", "symbol",
                               "recorder_attached",
                           ))
    assert len(diffs) > 0  # swap should produce some divergence
    kinds_seen = {d.kind for d in diffs}
    assert "EVENT_KIND_MISMATCH" in kinds_seen


# ─── subscriber_to_trace ──────────────────────────────────────────────────

def test_subscriber_to_trace_handles_all_event_types():
    sub = RecordingSubscriber()
    sub.calls = [
        ("orderStatus", (1, "Filled"), {}),
        ("execDetails", (1, 100.0, 1.5), {}),
        ("error", (42, 110, "test"), {}),
        ("position", ("EUR", 1000), {}),
        ("newOrder", (1, "BUY"), {}),
        ("connected", (), {}),
        ("disconnected", (), {}),
    ]
    trace = subscriber_to_trace(sub)
    kinds = [e["event"] for e in trace]
    assert kinds == ["orderStatus", "execDetails", "error", "position",
                     "newOrder", "connected", "disconnected"]
    assert trace[0]["data"]["status"] == "Filled"
    assert trace[1]["data"]["shares"] == 100.0


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_cli_exits_0_on_identical_traces(tmp_path):
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    content = (
        '{"event": "newOrder", "data": {"order_id": 1}}\n'
        '{"event": "execDetails", "data": {"order_id": 1, "shares": 100}}\n'
    )
    p1.write_text(content, encoding="utf-8")
    p2.write_text(content, encoding="utf-8")
    rc = trace_parity.main(["--a", str(p1), "--b", str(p2)])
    assert rc == 0


def test_cli_exits_1_on_divergent_traces(tmp_path, capsys):
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    p1.write_text('{"event": "newOrder", "data": {"order_id": 1}}\n', encoding="utf-8")
    p2.write_text('{"event": "execDetails", "data": {"order_id": 1}}\n', encoding="utf-8")
    rc = trace_parity.main(["--a", str(p1), "--b", str(p2)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "DIVERGENCES" in captured.out


def test_cli_exits_2_on_missing_file(capsys):
    rc = trace_parity.main(["--a", "missing_a.jsonl", "--b", "missing_b.jsonl"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_cli_ignore_fields_flag_works(tmp_path):
    """perm_id differs between A and B; default would flag. Override
    ignore list to also exclude perm_id and parity should pass."""
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    p1.write_text('{"event": "newOrder", "data": {"order_id": 1, "perm_id": 100}}\n',
                  encoding="utf-8")
    p2.write_text('{"event": "newOrder", "data": {"order_id": 1, "perm_id": 200}}\n',
                  encoding="utf-8")
    rc = trace_parity.main(["--a", str(p1), "--b", str(p2),
                            "--ignore-fields", "ts,monotonic_ms,perm_id"])
    assert rc == 0


def test_cli_json_mode_emits_valid_json(tmp_path, capsys):
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    p1.write_text('{"event": "newOrder", "data": {"order_id": 1}}\n', encoding="utf-8")
    p2.write_text('{"event": "execDetails", "data": {"order_id": 1}}\n', encoding="utf-8")
    rc = trace_parity.main(["--a", str(p1), "--b", str(p2), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["a"] == str(p1)
    assert parsed["b"] == str(p2)
    assert isinstance(parsed["divergences"], list)
    assert len(parsed["divergences"]) == 1
    assert rc == 1
