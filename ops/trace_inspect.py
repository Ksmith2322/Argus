"""Inspect a golden-trace JSONL: summary stats, anomalies, invariant violations.

Operator workflow:
  1. Recorder captures broker events to argus_flow/logs/traces/<file>.jsonl
  2. After an incident (or any time), run:
        python -m ops.trace_inspect <file>.jsonl
  3. Output: counts by event kind, list of anomalies, list of invariant
     violations. Exit code 0 if clean, 1 if invariant violations found
     (anomalies alone are non-blocking heuristics).

Examples:
  python -m ops.trace_inspect argus_flow/logs/traces/argus_2026-05-21.jsonl
  python -m ops.trace_inspect argus_flow/tests/fixtures/cascade_race_20260519.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from helio.trace_invariants import verify_invariants
from helio.trace_replay import find_anomalies, load_trace


def inspect(trace_path: str | Path) -> dict:
    """Return a structured report. Does not print or exit."""
    events = load_trace(trace_path)
    kind_counts = Counter(e.get("event", "?") for e in events)
    anomalies = find_anomalies(events)
    violations = verify_invariants(events)
    return {
        "path": str(trace_path),
        "event_count": len(events),
        "first_ts": events[0].get("ts") if events else None,
        "last_ts": events[-1].get("ts") if events else None,
        "by_kind": dict(kind_counts),
        "anomalies": [asdict(a) for a in anomalies],
        "invariant_violations": [asdict(v) for v in violations],
    }


def _print_human(report: dict) -> None:
    print(f"trace: {report['path']}")
    print(f"events: {report['event_count']}")
    if report["first_ts"]:
        print(f"window: {report['first_ts']} -> {report['last_ts']}")
    print()
    print("by kind:")
    for k, v in sorted(report["by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:18s} {v:>6d}")
    print()

    violations = report["invariant_violations"]
    if violations:
        print(f"INVARIANT VIOLATIONS ({len(violations)}):")
        for v in violations:
            oid = f" order_id={v['order_id']}" if v.get("order_id") else ""
            print(f"  [{v['invariant']}] at idx {v['at_index']}{oid}: {v['summary']}")
    else:
        print("INVARIANT VIOLATIONS: none")
    print()

    anomalies = report["anomalies"]
    if anomalies:
        print(f"anomalies / heuristic patterns ({len(anomalies)}):")
        for a in anomalies:
            print(f"  [{a['kind']}] at idx {a['at_index']}: {a['summary']}")
    else:
        print("anomalies: none")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inspect a golden-trace JSONL file")
    p.add_argument("path", help="path to .jsonl trace file")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable summary")
    args = p.parse_args(argv)

    if not Path(args.path).exists():
        print(f"trace file not found: {args.path}", file=sys.stderr)
        return 2

    report = inspect(args.path)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    return 1 if report["invariant_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
