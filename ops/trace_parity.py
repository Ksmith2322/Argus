"""Compare two golden-trace JSONL files for capture-replay parity.

Closes the Codex X1 deferred audit item. Output names every divergence
between trace A and trace B at the event-and-field level, with default
exclusions for fields that are expected to differ (timestamps, monotonic
counters).

Examples:
  python -m ops.trace_parity --a live.jsonl --b replay.jsonl
  python -m ops.trace_parity --a t1.jsonl --b t2.jsonl --json
  python -m ops.trace_parity --a t1.jsonl --b t2.jsonl \\
      --ignore-fields ts,monotonic_ms,perm_id

Exit codes:
  0 = traces match modulo ignored fields
  1 = one or more divergences
  2 = a file is missing or unreadable
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from helio.trace_parity import DEFAULT_IGNORE_FIELDS, compare_traces
from helio.trace_replay import load_trace


def _print_human(diffs, a_path, b_path, ignore_fields):
    print(f"A: {a_path}")
    print(f"B: {b_path}")
    print(f"ignore_fields: {','.join(ignore_fields) or '(none)'}")
    print()
    if not diffs:
        print("PARITY: clean — traces match")
        return
    print(f"DIVERGENCES ({len(diffs)}):")
    for d in diffs:
        loc = f"idx {d['at_index']}"
        if d.get("field"):
            loc += f" field={d['field']}"
        print(f"  [{d['kind']}] {loc}: {d['summary']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare two JSONL traces for parity")
    p.add_argument("--a", required=True, help="path to trace A (usually live capture)")
    p.add_argument("--b", required=True, help="path to trace B (usually replay or other capture)")
    p.add_argument("--ignore-fields", default=",".join(DEFAULT_IGNORE_FIELDS),
                   help=f"comma-separated field names to ignore "
                        f"(default: {','.join(DEFAULT_IGNORE_FIELDS)})")
    p.add_argument("--json", action="store_true", help="emit JSON instead of human-readable summary")
    args = p.parse_args(argv)

    for path, label in ((args.a, "A"), (args.b, "B")):
        if not Path(path).exists():
            print(f"trace {label} not found: {path}", file=sys.stderr)
            return 2

    ignore_fields = tuple(f.strip() for f in args.ignore_fields.split(",") if f.strip())
    trace_a = load_trace(args.a)
    trace_b = load_trace(args.b)
    diffs = compare_traces(trace_a, trace_b, ignore_fields=ignore_fields)
    diff_dicts = [asdict(d) for d in diffs]

    if args.json:
        print(json.dumps({
            "a": args.a,
            "b": args.b,
            "ignore_fields": list(ignore_fields),
            "event_count_a": len(trace_a),
            "event_count_b": len(trace_b),
            "divergences": diff_dicts,
        }, indent=2, default=str))
    else:
        _print_human(diff_dicts, args.a, args.b, ignore_fields)

    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
