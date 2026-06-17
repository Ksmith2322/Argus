"""Cross-event invariant checks for golden-trace JSONL.

Where helio.trace_replay.find_anomalies looks for known anti-patterns
(specific race signatures we've already seen), the invariants here
encode rules that MUST always be true for a well-behaved trace. A
violation = a real bug worth investigating, not just a suspicious
pattern.

Each invariant returns a list of InvariantViolation. Empty list = pass.

Invariants currently checked:

  1. ORDER_LIFECYCLE_VALID
     orderStatus progresses through plausible states:
     PendingSubmit -> Submitted -> Filled OR Cancelled.
     No regressions (Filled -> Submitted) and no skipping to terminal.

  2. NO_FILL_WITHOUT_ORDER
     Every execDetails for an order_id is preceded somewhere in the
     trace by a newOrder or orderStatus event for that order_id.

  3. NO_CANCEL_AFTER_FILL
     Once orderStatus reaches "Filled" for an order_id, no subsequent
     status of "Cancelled" or "PendingCancel" on that same order_id.
     (The 2026-05-19 cascade race produced exactly this signature on
     the broker side — IBKR reports Cancelled on an already-Filled
     order during the cancel-sleep window.)

  4. NO_DUPLICATE_EXEC_ID
     Each execution should have a unique exec_id. Duplicates =
     double-counted fills.
"""
from __future__ import annotations

from dataclasses import dataclass


# Status progression: from -> set of valid next statuses
_VALID_NEXT: dict[str, set[str]] = {
    "PendingSubmit": {"PreSubmitted", "Submitted", "Cancelled", "Inactive", "ApiCancelled"},
    "PreSubmitted": {"Submitted", "Cancelled", "ApiCancelled", "Inactive", "Filled"},
    "Submitted": {"Filled", "PendingCancel", "Cancelled", "ApiCancelled", "Inactive"},
    "PendingCancel": {"Cancelled", "ApiCancelled", "Filled", "Inactive"},
    "Filled": set(),       # terminal — no further transitions allowed
    "Cancelled": set(),    # terminal
    "ApiCancelled": set(), # terminal
    "Inactive": {"Submitted", "Cancelled"},  # may revive
}


@dataclass
class InvariantViolation:
    invariant: str
    at_index: int
    order_id: int | None
    summary: str


def _check_order_lifecycle(trace: list[dict]) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    last_status: dict[int, tuple[str, int]] = {}  # order_id -> (status, idx)
    for i, evt in enumerate(trace):
        if evt.get("event") != "orderStatus":
            continue
        data = evt.get("data") or {}
        oid = data.get("order_id")
        status = data.get("status")
        if oid is None or status is None:
            continue
        if oid in last_status:
            prev_status, prev_idx = last_status[oid]
            if prev_status == status:
                # Same status reported twice — not a violation, just noisy
                last_status[oid] = (status, i)
                continue
            allowed = _VALID_NEXT.get(prev_status, set())
            if status not in allowed:
                out.append(InvariantViolation(
                    invariant="ORDER_LIFECYCLE_VALID",
                    at_index=i,
                    order_id=oid,
                    summary=f"order_id {oid}: invalid transition "
                            f"{prev_status!r} -> {status!r} "
                            f"(prev at idx {prev_idx})",
                ))
        last_status[oid] = (status, i)
    return out


def _check_fill_after_order(trace: list[dict]) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    seen_order_ids: set[int] = set()
    for i, evt in enumerate(trace):
        kind = evt.get("event")
        data = evt.get("data") or {}
        oid = data.get("order_id")
        if oid is None:
            continue
        if kind in ("newOrder", "orderStatus"):
            seen_order_ids.add(oid)
        elif kind == "execDetails":
            if oid not in seen_order_ids:
                out.append(InvariantViolation(
                    invariant="NO_FILL_WITHOUT_ORDER",
                    at_index=i,
                    order_id=oid,
                    summary=f"execDetails for order_id {oid} arrived with no "
                            f"prior newOrder/orderStatus in trace",
                ))
    return out


def _check_no_cancel_after_fill(trace: list[dict]) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    filled: dict[int, int] = {}  # order_id -> idx where Filled was reached
    for i, evt in enumerate(trace):
        if evt.get("event") != "orderStatus":
            continue
        data = evt.get("data") or {}
        oid = data.get("order_id")
        status = data.get("status")
        if oid is None:
            continue
        if status == "Filled" and oid not in filled:
            filled[oid] = i
        elif status in ("Cancelled", "PendingCancel", "ApiCancelled") and oid in filled:
            out.append(InvariantViolation(
                invariant="NO_CANCEL_AFTER_FILL",
                at_index=i,
                order_id=oid,
                summary=f"order_id {oid}: status -> {status!r} after Filled at "
                        f"idx {filled[oid]}. Broker reporting cancel on "
                        f"already-filled order = cascade-race signature.",
            ))
    return out


def _check_no_duplicate_exec_id(trace: list[dict]) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    seen: dict[str, int] = {}  # exec_id -> idx where first seen
    for i, evt in enumerate(trace):
        if evt.get("event") != "execDetails":
            continue
        data = evt.get("data") or {}
        exec_id = data.get("exec_id")
        if not exec_id:
            continue
        if exec_id in seen:
            out.append(InvariantViolation(
                invariant="NO_DUPLICATE_EXEC_ID",
                at_index=i,
                order_id=data.get("order_id"),
                summary=f"exec_id {exec_id!r} repeats (first at idx {seen[exec_id]})",
            ))
        else:
            seen[exec_id] = i
    return out


_INVARIANTS = [
    _check_order_lifecycle,
    _check_fill_after_order,
    _check_no_cancel_after_fill,
    _check_no_duplicate_exec_id,
]


def verify_invariants(trace: list[dict]) -> list[InvariantViolation]:
    """Run all invariants, return aggregated violations."""
    out: list[InvariantViolation] = []
    for check in _INVARIANTS:
        out.extend(check(trace))
    return out
