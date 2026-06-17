"""Multi-trace summary — point at a directory of golden-trace JSONLs,
get one row per file with event counts, invariant violations, and
top anomaly kind. The operator's weekly review goes from "open each
trace individually" to one command.

Builds on ops.trace_inspect.inspect() to score each trace; this layer
aggregates + presents.

Examples:
  python -m ops.trace_summary --dir argus_flow/logs/traces
  python -m ops.trace_summary --dir argus_flow/logs/traces --days 7
  python -m ops.trace_summary --dir argus_flow/logs/traces --filter-symbol CAD
  python -m ops.trace_summary --dir argus_flow/logs/traces --json

Exit codes (mirror trace_inspect contract aggregated across files):
  0 = all traces clean
  1 = at least one trace has invariant violations
  2 = directory missing or no .jsonl files found
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from helio.trace_invariants import verify_invariants
from helio.trace_replay import find_anomalies, load_trace


@dataclass
class FileSummary:
    path: str
    event_count: int
    by_kind: dict = field(default_factory=dict)
    invariant_violations: int = 0
    anomalies: int = 0
    top_violation_kind: str = ""
    top_anomaly_kind: str = ""
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    status: str = "CLEAN"  # CLEAN | VIOLATIONS | EMPTY | ERROR
    error: str = ""


def _filter_by_symbol(events: list[dict], symbol: str) -> list[dict]:
    """Keep only events that touch a given base symbol (case-insensitive).

    Matches the event's `data.symbol` field. Events without a symbol
    field (e.g. connected, disconnected) are kept so structural events
    aren't lost. This is a coarse filter — useful for per-pair view
    when a single runner handles multiple symbols."""
    sym = symbol.upper()
    out: list[dict] = []
    for e in events:
        data = e.get("data") or {}
        ev_sym = (data.get("symbol") or "").upper()
        if not ev_sym:  # structural — keep
            out.append(e)
            continue
        if ev_sym == sym:
            out.append(e)
    return out


def summarize_file(path: Path, symbol_filter: Optional[str] = None) -> FileSummary:
    try:
        events = load_trace(path)
    except Exception as e:
        return FileSummary(path=str(path), event_count=0, status="ERROR", error=str(e))

    if symbol_filter:
        events = _filter_by_symbol(events, symbol_filter)

    if not events:
        return FileSummary(path=str(path), event_count=0, status="EMPTY")

    from collections import Counter
    kind_counts = Counter(e.get("event", "?") for e in events)

    violations = verify_invariants(events)
    anomalies = find_anomalies(events)

    top_violation = ""
    if violations:
        vc = Counter(v.invariant for v in violations)
        top_violation = vc.most_common(1)[0][0]

    top_anomaly = ""
    if anomalies:
        ac = Counter(a.kind for a in anomalies)
        top_anomaly = ac.most_common(1)[0][0]

    return FileSummary(
        path=str(path),
        event_count=len(events),
        by_kind=dict(kind_counts),
        invariant_violations=len(violations),
        anomalies=len(anomalies),
        top_violation_kind=top_violation,
        top_anomaly_kind=top_anomaly,
        first_ts=events[0].get("ts"),
        last_ts=events[-1].get("ts"),
        status="VIOLATIONS" if violations else "CLEAN",
    )


def summarize_directory(
    directory: Path,
    *,
    days: Optional[int] = None,
    symbol_filter: Optional[str] = None,
) -> list[FileSummary]:
    files = sorted(directory.glob("*.jsonl"))
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        files = [
            p for p in files
            if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= cutoff
        ]
    return [summarize_file(p, symbol_filter=symbol_filter) for p in files]


def _print_human(summaries: list[FileSummary]) -> None:
    if not summaries:
        print("no .jsonl traces found in directory")
        return

    # Compute column widths
    name_w = max(len(Path(s.path).name) for s in summaries)
    name_w = max(name_w, len("file"))

    header = f"  {'file':<{name_w}}  {'events':>6}  {'status':<10}  {'inv':>4}  {'anom':>4}  top-violation"
    print(header)
    print(f"  {'-' * name_w}  {'-' * 6}  {'-' * 10}  {'-' * 4}  {'-' * 4}  {'-' * 25}")

    total_violations = 0
    total_anomalies = 0
    total_events = 0
    worst_status = "CLEAN"
    status_order = {"ERROR": 3, "VIOLATIONS": 2, "EMPTY": 1, "CLEAN": 0}

    for s in summaries:
        name = Path(s.path).name
        top = s.top_violation_kind or "-"
        print(f"  {name:<{name_w}}  {s.event_count:>6d}  {s.status:<10}  "
              f"{s.invariant_violations:>4d}  {s.anomalies:>4d}  {top}")
        total_violations += s.invariant_violations
        total_anomalies += s.anomalies
        total_events += s.event_count
        if status_order.get(s.status, 0) > status_order.get(worst_status, 0):
            worst_status = s.status

    print()
    print(f"totals: {len(summaries)} traces, {total_events} events, "
          f"{total_violations} invariant violation(s), {total_anomalies} anomaly pattern(s)")
    print(f"worst-status: {worst_status}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aggregate a directory of golden-trace JSONLs into one report")
    p.add_argument("--dir", required=True, help="directory containing .jsonl trace files")
    p.add_argument("--days", type=int, default=None,
                   help="only files modified within last N days")
    p.add_argument("--filter-symbol", default=None,
                   help="keep only events touching this base symbol (e.g. USD, CAD)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable table")
    args = p.parse_args(argv)

    directory = Path(args.dir)
    if not directory.exists():
        print(f"directory not found: {args.dir}", file=sys.stderr)
        return 2

    summaries = summarize_directory(
        directory, days=args.days, symbol_filter=args.filter_symbol,
    )
    if not summaries:
        print(f"no .jsonl traces found in {args.dir}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "directory": str(directory),
            "days_filter": args.days,
            "symbol_filter": args.filter_symbol,
            "summaries": [asdict(s) for s in summaries],
        }, indent=2, default=str))
    else:
        _print_human(summaries)

    return 1 if any(s.status == "VIOLATIONS" for s in summaries) else 0


if __name__ == "__main__":
    sys.exit(main())
