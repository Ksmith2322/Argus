"""Tests for helio.event_dispatcher — Phase 3 of the golden-trace work.

Phase 2 (TraceFakeIB) mutates state for state-machine regression tests.
Phase 3 fires events to subscribers in controlled order/timing for
callback-ordering regression tests. The two are complementary."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helio.event_dispatcher import (
    ChaosTransform, EventDispatcher, RecordingSubscriber,
    apply_chaos, make_target, subscribe_recording,
)
from helio.trace_replay import load_trace


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASCADE_RACE = FIXTURES / "cascade_race_20260519.jsonl"


# ─── basic dispatch ───────────────────────────────────────────────────────

def test_dispatcher_fires_all_events_in_order():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [
        {"event": "connected", "data": {}},
        {"event": "newOrder", "data": {"order_id": 1, "action": "BUY"}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 1000, "price": 1.0}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    EventDispatcher(target).dispatch(events, mode="instant")

    kinds = [c[0] for c in sub.calls]
    assert kinds == ["connected", "newOrder", "orderStatus", "execDetails", "orderStatus"]


def test_dispatcher_fires_correct_args_for_exec_details():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [{"event": "execDetails",
               "data": {"order_id": 7421, "shares": 41479, "price": 90.451, "side": "SLD"}}]
    EventDispatcher(target).dispatch(events, mode="instant")

    assert sub.calls == [("execDetails", (7421, 41479, 90.451), {})]


def test_dispatcher_returns_records_for_all_events():
    target = make_target()
    events = [
        {"event": "connected", "data": {}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    records = EventDispatcher(target).dispatch(events, mode="instant")
    assert len(records) == 2
    assert all(r.fired for r in records)


def test_dispatcher_records_unknown_event_kind_without_raising():
    target = make_target()
    records = EventDispatcher(target).dispatch(
        [{"event": "weather_report", "data": {}}], mode="instant",
    )
    assert len(records) == 1
    assert records[0].fired is False
    assert "unhandled" in records[0].reason


def test_dispatcher_records_missing_slot_without_raising():
    """If target lacks a slot for some event kind, dispatcher logs
    skip instead of crashing."""
    from types import SimpleNamespace
    target = SimpleNamespace()  # no slots at all
    records = EventDispatcher(target).dispatch(
        [{"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}}],
        mode="instant",
    )
    assert records[0].fired is False
    assert "no orderStatusEvent" in records[0].reason


# ─── chaos transformations ────────────────────────────────────────────────

def test_chaos_swap_swaps_two_events():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 1000, "price": 1.0}},
    ]
    # Swap so execDetails fires BEFORE orderStatus
    EventDispatcher(target).dispatch(
        events, mode="instant",
        chaos=[ChaosTransform(kind="swap", target_idx=0, other_idx=1)],
    )

    assert [c[0] for c in sub.calls] == ["execDetails", "orderStatus"]


def test_chaos_drop_removes_event():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 1000, "price": 1.0}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    # Drop the execDetails — simulates lost callback
    EventDispatcher(target).dispatch(
        events, mode="instant",
        chaos=[ChaosTransform(kind="drop", target_idx=1)],
    )
    assert [c[0] for c in sub.calls] == ["orderStatus", "orderStatus"]


def test_chaos_duplicate_fires_event_twice():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [{"event": "execDetails",
               "data": {"order_id": 1, "shares": 1000, "price": 1.0}}]
    EventDispatcher(target).dispatch(
        events, mode="instant",
        chaos=[ChaosTransform(kind="duplicate", target_idx=0)],
    )
    assert len(sub.calls) == 2
    assert sub.calls[0] == sub.calls[1]


def test_chaos_delay_realtime_mode_actually_sleeps():
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    events = [
        {"event": "orderStatus", "monotonic_ms": 0, "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "orderStatus", "monotonic_ms": 0, "data": {"order_id": 1, "status": "Filled"}},
    ]
    t0 = time.monotonic()
    EventDispatcher(target).dispatch(
        events, mode="realtime",
        chaos=[ChaosTransform(kind="delay_ms", target_idx=1, delay_ms=150)],
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms >= 140  # 150 with small tolerance
    assert elapsed_ms < 600   # but not absurd


def test_apply_chaos_does_not_mutate_input_list():
    original = [
        {"event": "a", "data": {}},
        {"event": "b", "data": {}},
    ]
    transformed = apply_chaos(original, [ChaosTransform(kind="swap", target_idx=0, other_idx=1)])
    assert original[0]["event"] == "a"  # original unchanged
    assert transformed[0]["event"] == "b"  # transformed swapped


def test_apply_chaos_unknown_kind_raises():
    with pytest.raises(ValueError) as exc:
        apply_chaos([{"event": "x", "data": {}}],
                    [ChaosTransform(kind="explode_universe", target_idx=0)])
    assert "explode_universe" in str(exc.value)


def test_apply_chaos_out_of_range_index_is_no_op():
    """Defensive: index past end of list should not crash."""
    events = [{"event": "a", "data": {}}]
    out = apply_chaos(events, [ChaosTransform(kind="drop", target_idx=99)])
    assert len(out) == 1  # nothing dropped


# ─── timing modes ─────────────────────────────────────────────────────────

def test_instant_mode_does_not_sleep_between_events():
    target = make_target()
    events = [
        {"event": "orderStatus", "monotonic_ms": 0, "data": {"order_id": 1, "status": "X"}},
        {"event": "orderStatus", "monotonic_ms": 5000, "data": {"order_id": 1, "status": "Y"}},
    ]
    t0 = time.monotonic()
    EventDispatcher(target).dispatch(events, mode="instant")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1  # should be near-zero despite 5s recorded gap


def test_accelerated_mode_compresses_time():
    target = make_target()
    events = [
        {"event": "orderStatus", "monotonic_ms": 0, "data": {"order_id": 1, "status": "X"}},
        {"event": "orderStatus", "monotonic_ms": 500, "data": {"order_id": 1, "status": "Y"}},
    ]
    t0 = time.monotonic()
    EventDispatcher(target).dispatch(events, mode="accelerated", speedup=10.0)
    elapsed = time.monotonic() - t0
    # 500ms recorded gap, 10x speedup = ~50ms
    assert 0.03 <= elapsed < 0.30


# ─── cascade-race fixture through the dispatcher ──────────────────────────

def test_cascade_fixture_fires_full_sequence_to_subscriber():
    """End-to-end: load the 5/19 cascade fixture, fire all events
    through the dispatcher, verify the subscriber sees them in original
    order with original args."""
    events = load_trace(CASCADE_RACE)
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    records = EventDispatcher(target).dispatch(events, mode="instant")
    assert all(r.fired for r in records), [r for r in records if not r.fired]
    kinds = [c[0] for c in sub.calls]
    assert kinds == [
        "connected", "position", "newOrder", "orderStatus", "execDetails",
        "orderStatus", "position", "orderStatus", "orderStatus",
    ]
    # The execDetails carries the right shares
    exec_call = next(c for c in sub.calls if c[0] == "execDetails")
    assert exec_call[1] == (7421, 41479, 90.451)


def test_cascade_fixture_with_swap_changes_callback_order():
    """Apply chaos: swap the orderStatus-Filled (idx 5) and position-flat
    (idx 6). This simulates the broker reporting position-flat BEFORE
    the order-fill status. Subscribers must be robust to this ordering."""
    events = load_trace(CASCADE_RACE)
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    EventDispatcher(target).dispatch(
        events, mode="instant",
        chaos=[ChaosTransform(kind="swap", target_idx=5, other_idx=6)],
    )
    kinds = [c[0] for c in sub.calls]
    # Original kinds[5:7] = ["orderStatus", "position"]; after swap they
    # are ["position", "orderStatus"].
    assert kinds[5] == "position"
    assert kinds[6] == "orderStatus"


def test_dispatcher_handles_drop_in_cascade_fixture():
    """Dropping the execDetails event (the fill) is the canonical
    callback-loss bug: orderStatus says Filled but no fill record
    arrives. Subscriber should see all events except the dropped one,
    and downstream tests can assert their handler logs an error or
    enters a reconciliation path."""
    events = load_trace(CASCADE_RACE)
    target = make_target()
    sub = RecordingSubscriber()
    subscribe_recording(target, sub)

    # idx 4 is the execDetails fill
    EventDispatcher(target).dispatch(
        events, mode="instant",
        chaos=[ChaosTransform(kind="drop", target_idx=4)],
    )
    assert not any(c[0] == "execDetails" for c in sub.calls)
    # Other events still fire
    assert sum(1 for c in sub.calls if c[0] == "orderStatus") == 4


# ─── slot subscription mechanics ──────────────────────────────────────────

def test_event_slot_supports_unsubscribe():
    """Subscribers can detach via -=, mirroring ib_insync's contract."""
    target = make_target()
    log = []

    def h(*a, **k):
        log.append(a)

    target.orderStatusEvent += h
    target.orderStatusEvent.fire("first")
    target.orderStatusEvent -= h
    target.orderStatusEvent.fire("second")  # should not call h

    assert log == [("first",)]


def test_event_slot_supports_multiple_subscribers():
    target = make_target()
    log_a, log_b = [], []
    target.orderStatusEvent += (lambda *a: log_a.append(a))
    target.orderStatusEvent += (lambda *a: log_b.append(a))
    target.orderStatusEvent.fire(42)
    assert log_a == [(42,)]
    assert log_b == [(42,)]
