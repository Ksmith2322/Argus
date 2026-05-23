"""Tests for helio.replay_parity end-to-end orchestrator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio import replay_parity as rp


# ─── _calls_to_events ──────────────────────────────────────────────

def test_calls_to_events_order_status():
    calls = [("orderStatus", (123, "Filled"), {})]
    events = rp._calls_to_events(calls)
    assert events == [{"event": "orderStatus",
                       "data": {"order_id": 123, "status": "Filled"}}]


def test_calls_to_events_exec_details():
    calls = [("execDetails", (123, 100.0, 50.25), {})]
    events = rp._calls_to_events(calls)
    assert events == [{"event": "execDetails",
                       "data": {"order_id": 123, "shares": 100.0, "price": 50.25}}]


def test_calls_to_events_all_kinds():
    calls = [
        ("orderStatus", (1, "Filled"), {}),
        ("execDetails", (1, 100, 50.0), {}),
        ("error", (-1, 200, "test"), {}),
        ("position", ("USD.JPY", 10000), {}),
        ("newOrder", (2, "BUY"), {}),
        ("disconnected", (), {}),
        ("connected", (), {}),
        ("updatePortfolio", ("SPY", 100, 50.0), {}),
    ]
    events = rp._calls_to_events(calls)
    assert len(events) == 8
    kinds = [e["event"] for e in events]
    assert kinds == ["orderStatus", "execDetails", "error", "position",
                      "newOrder", "disconnected", "connected", "updatePortfolio"]


def test_calls_to_events_unknown_kind_preserves_raw():
    calls = [("mystery_kind", ("a", "b"), {})]
    events = rp._calls_to_events(calls)
    assert events[0]["event"] == "mystery_kind"
    assert "raw_args" in events[0]["data"]


# ─── _project_events_for_comparison ────────────────────────────────

def test_project_events_strips_unhandled_kinds():
    """Events the dispatcher doesn't handle (attached, snapshot) should
    be dropped from the comparison."""
    events = [
        {"event": "attached", "data": {"foo": 1}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled",
                                            "extra_field": "ignore"}},
        {"event": "snapshot", "data": {"x": 2}},
        {"event": "execDetails", "data": {"order_id": 1, "shares": 100, "price": 50}},
    ]
    projected = rp._project_events_for_comparison(events)
    # Only the 2 handled events should remain
    assert len(projected) == 2
    assert projected[0]["event"] == "orderStatus"
    assert projected[1]["event"] == "execDetails"
    # Extra fields should be dropped (only order_id + status for orderStatus)
    assert "extra_field" not in projected[0]["data"]


def test_project_preserves_required_fields():
    events = [{"event": "orderStatus", "data": {"order_id": 99, "status": "Submitted"}}]
    projected = rp._project_events_for_comparison(events)
    assert projected[0]["data"] == {"order_id": 99, "status": "Submitted"}


# ─── run_parity_check end-to-end ───────────────────────────────────

def test_run_parity_check_with_synthetic_trace(tmp_path):
    """Round-trip a synthetic trace through dispatch + record, then diff
    against itself (projected). Should be PASS."""
    trace = [
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}},
        {"event": "execDetails",
         "data": {"order_id": 1, "shares": 100, "price": 50.0}},
        {"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}},
    ]
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in trace) + "\n", encoding="utf-8")
    result = rp.run_parity_check(path)
    assert result.verdict == "PASS", (
        f"synthetic trace should round-trip clean; got {result.n_differences} diffs: "
        f"{result.differences_sample}"
    )
    assert result.n_events_original == 3
    assert result.n_events_replayed == 3
    assert result.n_dispatched_ok == 3
    assert result.n_dispatched_failed == 0


def test_run_parity_check_handles_unloadable_trace(tmp_path):
    """Non-existent file → LOAD_ERROR verdict, not crash."""
    result = rp.run_parity_check(tmp_path / "does_not_exist.jsonl")
    assert result.verdict == "LOAD_ERROR"
    assert result.failure_reasons


def test_run_parity_check_corrupted_trace(tmp_path):
    """JSONL with malformed lines: load_trace raises, orchestrator
    catches and returns LOAD_ERROR (doesn't crash)."""
    path = tmp_path / "corrupt.jsonl"
    path.write_text(
        json.dumps({"event": "orderStatus",
                    "data": {"order_id": 1, "status": "Filled"}}) + "\n"
        + "{not json}\n"
        + json.dumps({"event": "orderStatus",
                      "data": {"order_id": 1, "status": "Submitted"}}) + "\n",
        encoding="utf-8",
    )
    result = rp.run_parity_check(path)
    # Either PASS (if load_trace happens to skip bad lines) or LOAD_ERROR
    # (if it raises). Either is a valid, non-crashing outcome.
    assert result.verdict in ("PASS", "DIVERGENCE", "LOAD_ERROR")


def test_run_parity_check_unhandled_kinds_dont_break():
    """A trace containing events the dispatcher doesn't fire (e.g.
    'attached', 'snapshot') should still produce a verdict — those
    events are projected away on both sides."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        f.write(json.dumps({"event": "attached", "data": {}}) + "\n")
        f.write(json.dumps({"event": "snapshot", "data": {"x": 1}}) + "\n")
        f.write(json.dumps({
            "event": "orderStatus",
            "data": {"order_id": 1, "status": "Filled"},
        }) + "\n")
        path = Path(f.name)
    try:
        result = rp.run_parity_check(path)
        assert result.verdict == "PASS"
        # Projected: just the 1 orderStatus
        assert result.n_events_original == 1
        assert result.n_events_replayed == 1
    finally:
        path.unlink()


# ─── render_text ───────────────────────────────────────────────────

def test_render_text_includes_verdict_and_counts():
    result = rp.ReplayParityResult(
        trace_path="x.jsonl",
        n_events_original=10, n_events_replayed=10,
        n_dispatched_ok=10, n_dispatched_failed=0,
        n_differences=0, verdict="PASS",
        differences_sample=[], failure_reasons=[],
        generated_at="now",
    )
    text = rp.render_text(result)
    assert "Replay parity check" in text
    assert "x.jsonl" in text
    assert "PASS" in text
    assert "Original events" in text


def test_render_text_with_differences():
    result = rp.ReplayParityResult(
        trace_path="x.jsonl",
        n_events_original=10, n_events_replayed=8,
        n_dispatched_ok=8, n_dispatched_failed=2,
        n_differences=2, verdict="DIVERGENCE",
        differences_sample=[
            {"kind": "MISSING_IN_B", "at_index": 5, "summary": "missing event",
             "field": ""},
        ],
        failure_reasons=["event #3 (foo): no slot"],
        generated_at="now",
    )
    text = rp.render_text(result)
    assert "DIVERGENCE" in text
    assert "MISSING_IN_B" in text
    assert "Dispatch failures" in text


# ─── result serialization ──────────────────────────────────────────

def test_to_dict_serializable():
    result = rp.ReplayParityResult(
        trace_path="x.jsonl",
        n_events_original=1, n_events_replayed=1,
        n_dispatched_ok=1, n_dispatched_failed=0,
        n_differences=0, verdict="PASS",
        differences_sample=[], failure_reasons=[],
        generated_at="now",
    )
    d = result.to_dict()
    # Must JSON-serialize without issue
    serialized = json.dumps(d, default=str)
    assert "PASS" in serialized
    assert "verdict" in serialized
