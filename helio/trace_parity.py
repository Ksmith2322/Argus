"""Capture-replay parity — compare two JSONL traces field-by-field.

Closes the Codex X1 deferred audit item ("live-vs-replay parity"). The
plumbing pieces have been built across Phases 2-3: recorder captures
events, dispatcher fires them, replayer mutates state. Parity is the
final composition — it validates that replaying a captured trace
produces the same observable sequence as the live capture.

Three uses:

  1. **Self-test the harness.** Capture a live trace T1. Dispatch T1
     through helio.event_dispatcher to a RecordingSubscriber. Convert
     the subscriber's call log back to JSONL form (T2). compare_traces
     (T1, T2) should return empty — proof that recorder + dispatcher
     don't add/drop/mutate events.

  2. **Code-change validation.** Given a trace T1 captured against
     code commit A, run T1 through code commit B's handler logic, and
     diff the produced trace. Differences = behavioral changes
     between commits, surfaced deterministically.

  3. **Cross-node comparison.** PC1 and PC2 each captured the same
     trade scenario. Compare the two traces to see if behavior
     diverges across hosts.

This module does NOT itself do any capture or replay — those are
helio.event_recorder and helio.event_dispatcher. compare_traces takes
two already-loaded trace lists and emits diffs.
"""
from __future__ import annotations

from dataclasses import dataclass

# Fields that are timestamp / monotonic and shouldn't be compared by
# default — they will always differ between a live capture and a
# replay because the timestamps are set at moment-of-event. Operator
# can override via the `ignore_fields` argument.
DEFAULT_IGNORE_FIELDS = ("ts", "monotonic_ms")


@dataclass
class TraceDifference:
    """One observed mismatch between trace_a and trace_b."""
    kind: str            # "MISSING_IN_B" | "EXTRA_IN_B" | "VALUE_MISMATCH" | "EVENT_KIND_MISMATCH"
    at_index: int
    summary: str
    field: str = ""      # only meaningful for VALUE_MISMATCH


def _strip(event: dict, ignore_fields: tuple[str, ...]) -> dict:
    """Return a copy of event with ignored top-level fields removed AND
    ignored nested 'data' fields removed."""
    out = {k: v for k, v in event.items() if k not in ignore_fields}
    if "data" in out and isinstance(out["data"], dict):
        out["data"] = {k: v for k, v in out["data"].items() if k not in ignore_fields}
    return out


def _compare_events(a: dict, b: dict, *, ignore_fields: tuple[str, ...]) -> list[tuple[str, str]]:
    """Returns list of (field_path, description) for fields that differ
    between a and b. Empty list = match."""
    a_clean = _strip(a, ignore_fields)
    b_clean = _strip(b, ignore_fields)
    diffs: list[tuple[str, str]] = []

    a_kind = a_clean.get("event")
    b_kind = b_clean.get("event")
    if a_kind != b_kind:
        diffs.append(("event", f"kind {a_kind!r} != {b_kind!r}"))
        return diffs  # short-circuit — different event kinds, deeper diff is noise

    a_data = a_clean.get("data") or {}
    b_data = b_clean.get("data") or {}
    all_keys = set(a_data) | set(b_data)
    for k in sorted(all_keys):
        if a_data.get(k) != b_data.get(k):
            diffs.append((f"data.{k}",
                          f"{a_data.get(k)!r} != {b_data.get(k)!r}"))
    return diffs


def compare_traces(
    trace_a: list[dict],
    trace_b: list[dict],
    *,
    ignore_fields: tuple[str, ...] = DEFAULT_IGNORE_FIELDS,
) -> list[TraceDifference]:
    """Compare two traces event-by-event in order.

    Returns differences. Empty list = traces match modulo ignored fields.

    Semantics:
      - Event ordering is strict (no realignment / fuzzy match)
      - If trace_a is longer, extra events show as MISSING_IN_B
      - If trace_b is longer, extra events show as EXTRA_IN_B
      - For each shared index, fields are compared (minus ignore list)
      - VALUE_MISMATCH = same event kind, different field value
      - EVENT_KIND_MISMATCH = different event kind at same index
    """
    diffs: list[TraceDifference] = []
    n = max(len(trace_a), len(trace_b))
    for i in range(n):
        if i >= len(trace_b):
            a = trace_a[i]
            diffs.append(TraceDifference(
                kind="MISSING_IN_B",
                at_index=i,
                summary=f"event {a.get('event')!r} present in A, absent in B",
            ))
            continue
        if i >= len(trace_a):
            b = trace_b[i]
            diffs.append(TraceDifference(
                kind="EXTRA_IN_B",
                at_index=i,
                summary=f"event {b.get('event')!r} present in B, absent in A",
            ))
            continue
        a = trace_a[i]
        b = trace_b[i]
        field_diffs = _compare_events(a, b, ignore_fields=ignore_fields)
        for field, desc in field_diffs:
            kind = "EVENT_KIND_MISMATCH" if field == "event" else "VALUE_MISMATCH"
            diffs.append(TraceDifference(
                kind=kind,
                at_index=i,
                field=field,
                summary=desc,
            ))
    return diffs


# ─── helper: convert a RecordingSubscriber's call log to trace form ──────

def subscriber_to_trace(subscriber) -> list[dict]:
    """Convert a RecordingSubscriber's .calls into a JSONL-shaped trace
    list. Lossy by design — RecordingSubscriber stores only the fields
    it received, so compare_traces with the default ignore list works
    for self-parity checks.

    Mapping:
      ("orderStatus", (order_id, status), {}) -> {"event": "orderStatus",
                                                  "data": {"order_id":..., "status":...}}
      ("execDetails", (order_id, shares, price), {}) -> {"event": "execDetails",
                                                  "data": {"order_id":..., "shares":..., "price":...}}
      ("error", (req_id, code, msg), {}) -> {"event": "error", "data": {...}}
      ("position", (symbol, position), {}) -> {"event": "position", "data": {...}}
      ("newOrder", (order_id, action), {}) -> {"event": "newOrder", "data": {...}}
      ("connected", (), {}) / ("disconnected", (), {}) -> {"event": "connected"/"disconnected", "data": {}}
    """
    out: list[dict] = []
    for call in subscriber.calls:
        kind, args, _kwargs = call
        if kind == "orderStatus":
            out.append({"event": "orderStatus",
                        "data": {"order_id": args[0], "status": args[1]}})
        elif kind == "execDetails":
            out.append({"event": "execDetails",
                        "data": {"order_id": args[0], "shares": args[1], "price": args[2]}})
        elif kind == "error":
            out.append({"event": "error",
                        "data": {"req_id": args[0], "code": args[1], "msg": args[2]}})
        elif kind == "position":
            out.append({"event": "position",
                        "data": {"symbol": args[0], "position": args[1]}})
        elif kind == "newOrder":
            out.append({"event": "newOrder",
                        "data": {"order_id": args[0], "action": args[1]}})
        elif kind in ("connected", "disconnected"):
            out.append({"event": kind, "data": {}})
        else:
            out.append({"event": kind, "data": {"args": list(args)}})
    return out
