"""End-to-end live-vs-replay parity orchestrator.

Built 2026-05-23. Closes the long-deferred "actually run a trace through
the harness" item from Codex X1 + 5/20 audit. Composes the existing
trace_replay + event_dispatcher + RecordingSubscriber + trace_parity
modules into a single end-to-end check:

    1. Load a captured live trace (JSONL)
    2. Build a SimpleNamespace event target + attach RecordingSubscriber
    3. EventDispatcher fires every event at the target
    4. RecordingSubscriber logs every callback it received
    5. Convert RecordingSubscriber.calls back to JSONL events
    6. compare_traces(original, replayed) — diff at the event-and-field level
    7. Emit a structured report (PASS / DIVERGENCE / ERROR)

USE CASES:
  - **Harness self-test**: prove that recorder + dispatcher + recorder
    round-trip cleanly (this is the cheapest assertion of correctness)
  - **Code-change regression**: capture a trace before a change, replay
    after, verify the dispatched events match (caller is responsible
    for any state-machine differences vs original)
  - **Cross-host comparison**: PC1 captured T1, PC2 captured T2 of the
    same scenario; compare and find host-divergent behaviors

LIMITATIONS (intentional):
  - Does NOT drive a real runner instance. Replay is at the
    dispatcher-and-subscriber level. A bug in a runner's exec_details
    handler (where state writes happen) is NOT caught here.
  - For full runner replay we'd need state-machine checkpointing
    (deferred to a future session).

CLI:
    python -m ops.replay_parity --trace argus_flow/logs/traces/foo.jsonl
    python -m ops.replay_parity --trace foo.jsonl --json --output out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


from helio.event_dispatcher import (
    EventDispatcher, RecordingSubscriber, make_target, subscribe_recording,
)
from helio.trace_parity import compare_traces, DEFAULT_IGNORE_FIELDS
from helio.trace_replay import load_trace


@dataclass
class ReplayParityResult:
    trace_path: str
    n_events_original: int
    n_events_replayed: int
    n_dispatched_ok: int
    n_dispatched_failed: int
    n_differences: int
    verdict: str            # PASS / DIVERGENCE / LOAD_ERROR
    differences_sample: list[dict]
    failure_reasons: list[str]
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── conversion: RecordingSubscriber.calls -> JSONL events ─────────

def _calls_to_events(calls: list[tuple[str, tuple, dict]]) -> list[dict]:
    """Reformat a RecordingSubscriber.calls list into JSONL event dicts
    in the same schema as load_trace()'s output. Used to compare an
    original trace against a replay-captured trace via compare_traces."""
    events = []
    for kind, args, _kw in calls:
        if kind == "orderStatus":
            order_id, status = args
            events.append({"event": "orderStatus",
                            "data": {"order_id": order_id, "status": status}})
        elif kind == "execDetails":
            order_id, shares, price = args
            events.append({"event": "execDetails",
                            "data": {"order_id": order_id, "shares": shares,
                                     "price": price}})
        elif kind == "error":
            req_id, code, msg = args
            events.append({"event": "error",
                            "data": {"req_id": req_id, "code": code, "msg": msg}})
        elif kind == "position":
            symbol, position = args
            events.append({"event": "position",
                            "data": {"symbol": symbol, "position": position}})
        elif kind == "newOrder":
            order_id, action = args
            events.append({"event": "newOrder",
                            "data": {"order_id": order_id, "action": action}})
        elif kind == "disconnected":
            events.append({"event": "disconnected", "data": {}})
        elif kind == "connected":
            events.append({"event": "connected", "data": {}})
        elif kind == "updatePortfolio":
            symbol, position, unrealized = args
            events.append({"event": "updatePortfolio",
                            "data": {"symbol": symbol, "position": position,
                                     "unrealizedPNL": unrealized}})
        else:
            events.append({"event": kind, "data": {"raw_args": list(args)}})
    return events


def _project_events_for_comparison(events: list[dict]) -> list[dict]:
    """Project a full captured trace to the subset of fields that the
    RecordingSubscriber-replay produces. Compares apples to apples.
    The recorder captures rich data; the subscriber captures a subset.
    Both projections must be the SAME subset for the diff to be meaningful."""
    projected = []
    for evt in events:
        kind = evt.get("event")
        data = evt.get("data") or {}
        if kind == "orderStatus":
            projected.append({"event": "orderStatus",
                              "data": {"order_id": data.get("order_id"),
                                       "status": data.get("status")}})
        elif kind == "execDetails":
            projected.append({"event": "execDetails",
                              "data": {"order_id": data.get("order_id"),
                                       "shares": data.get("shares"),
                                       "price": data.get("price")}})
        elif kind == "error":
            projected.append({"event": "error",
                              "data": {"req_id": data.get("req_id"),
                                       "code": data.get("code"),
                                       "msg": data.get("msg")}})
        elif kind == "position":
            projected.append({"event": "position",
                              "data": {"symbol": data.get("symbol"),
                                       "position": data.get("position")}})
        elif kind == "newOrder":
            projected.append({"event": "newOrder",
                              "data": {"order_id": data.get("order_id"),
                                       "action": data.get("action")}})
        elif kind == "disconnected":
            projected.append({"event": "disconnected", "data": {}})
        elif kind == "connected":
            projected.append({"event": "connected", "data": {}})
        elif kind == "updatePortfolio":
            # Recorder writes snake_case (unrealized_pnl); RecordingSubscriber
            # captures the reconstructed item's camelCase (unrealizedPNL).
            # Project original to the same shape as the replayed events.
            unrealized = data.get("unrealizedPNL")
            if unrealized is None:
                unrealized = data.get("unrealized_pnl")
            projected.append({"event": "updatePortfolio",
                              "data": {"symbol": data.get("symbol"),
                                       "position": data.get("position"),
                                       "unrealizedPNL": unrealized}})
        # Drop kinds the dispatcher doesn't handle (attached, snapshot, etc.)
    return projected


# ─── orchestration ────────────────────────────────────────────────

def run_parity_check(trace_path: Path | str) -> ReplayParityResult:
    """Self-test pipeline: load trace -> dispatch -> capture -> diff."""
    path = Path(trace_path)
    try:
        original = load_trace(path)
    except Exception as exc:
        return ReplayParityResult(
            trace_path=str(path),
            n_events_original=0, n_events_replayed=0,
            n_dispatched_ok=0, n_dispatched_failed=0,
            n_differences=0, verdict="LOAD_ERROR",
            differences_sample=[],
            failure_reasons=[f"load_trace failed: {exc}"],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    # Build dispatcher + recording subscriber pipeline
    target = make_target()
    subscriber = RecordingSubscriber()
    subscribe_recording(target, subscriber)
    dispatcher = EventDispatcher(target)
    records = dispatcher.dispatch(original, mode="instant")

    n_ok = sum(1 for r in records if r.fired)
    n_fail = sum(1 for r in records if not r.fired)
    failure_reasons = [
        f"event #{r.event_index} ({r.event_kind}): {r.reason}"
        for r in records if not r.fired
    ][:20]  # cap to first 20 failures

    replayed_events = _calls_to_events(subscriber.calls)
    original_projected = _project_events_for_comparison(original)

    differences = compare_traces(
        original_projected, replayed_events,
        ignore_fields=DEFAULT_IGNORE_FIELDS,
    )
    sample_diffs = [
        {"kind": d.kind, "at_index": d.at_index, "summary": d.summary,
         "field": d.field}
        for d in differences[:10]
    ]

    verdict = "PASS" if not differences else "DIVERGENCE"
    return ReplayParityResult(
        trace_path=str(path),
        n_events_original=len(original_projected),
        n_events_replayed=len(replayed_events),
        n_dispatched_ok=n_ok,
        n_dispatched_failed=n_fail,
        n_differences=len(differences),
        verdict=verdict,
        differences_sample=sample_diffs,
        failure_reasons=failure_reasons,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def render_text(result: ReplayParityResult) -> str:
    lines = []
    lines.append(f"=== Replay parity check: {result.trace_path} ===")
    lines.append(f"Generated: {result.generated_at}")
    lines.append(f"Verdict: {result.verdict}")
    lines.append("")
    lines.append(f"Original events (post-projection): {result.n_events_original}")
    lines.append(f"Replayed events:                   {result.n_events_replayed}")
    lines.append(f"Dispatched OK / Failed:            {result.n_dispatched_ok} / {result.n_dispatched_failed}")
    lines.append(f"Differences:                       {result.n_differences}")
    if result.failure_reasons:
        lines.append("")
        lines.append("Dispatch failures (first 20):")
        for fr in result.failure_reasons:
            lines.append(f"  - {fr}")
    if result.differences_sample:
        lines.append("")
        lines.append("Differences (first 10):")
        for d in result.differences_sample:
            lines.append(f"  - [{d['kind']}] at index {d['at_index']}: {d['summary']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, help="Path to trace JSONL")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of text")
    parser.add_argument("--output", help="Write report to this file (default: stdout)")
    args = parser.parse_args(argv)

    result = run_parity_check(args.trace)
    if args.json:
        rendered = json.dumps(result.to_dict(), indent=2, default=str)
    else:
        rendered = render_text(result)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"Report written to: {args.output}")
    else:
        print(rendered)

    if result.verdict == "PASS":
        return 0
    if result.verdict == "DIVERGENCE":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
