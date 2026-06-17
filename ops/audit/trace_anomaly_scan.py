"""Trace anomaly scanner — apply find_anomalies to archived golden traces.

Built 2026-05-23. Walks a directory of captured trace JSONLs, runs
helio.trace_replay.find_anomalies on each, and produces a report.

Each anomaly is a SIGNAL TO INVESTIGATE (not necessarily a bug). For
hard invariant violations the operator should use trace_invariants.

CLI:
    python -m ops.audit.trace_anomaly_scan
    python -m ops.audit.trace_anomaly_scan --dir argus_flow/logs/traces
    python -m ops.audit.trace_anomaly_scan --json

Output:
    argus_flow/logs/trace_anomaly_scan.json   (machine-readable)
    argus_flow/logs/trace_anomaly_scan.md     (operator-readable)

Exit codes:
    0  no traces with anomalies
    1  one or more traces have anomalies
    2  error (couldn't find any traces, all unreadable, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helio.trace_replay import find_anomalies, load_trace


_REPO = Path(__file__).resolve().parents[2]
LOG_DIR = _REPO / "argus_flow" / "logs"
DEFAULT_JSON = LOG_DIR / "trace_anomaly_scan.json"
DEFAULT_MD = LOG_DIR / "trace_anomaly_scan.md"
DEFAULT_TRACE_DIR = LOG_DIR / "traces"
# Common fixture directory used by tests
FIXTURE_DIR = _REPO / "argus_flow" / "tests" / "fixtures"


@dataclass
class TraceScanResult:
    file: str
    n_events: int
    n_anomalies: int
    anomaly_kinds: dict          # {kind: count}
    sample_anomalies: list       # first 3 anomalies for context

    def to_dict(self) -> dict:
        return asdict(self)


def _find_trace_files(search_dirs: list[Path]) -> list[Path]:
    """Find all JSONL files in given dirs. Order: most-recent mtime first."""
    found: set[Path] = set()
    for d in search_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.jsonl"):
            # Filter to plausible traces — skip canonical_fills, history files, etc.
            name = p.name.lower()
            if any(skip in name for skip in ("canonical_fills", "history",
                                              "alert_events", "broker_equity",
                                              "opportunities", "signals")):
                continue
            found.add(p)
    return sorted(found, key=lambda p: -p.stat().st_mtime)


def _rel_path(path: Path) -> str:
    """Best-effort repo-relative path. Falls back to absolute if outside repo
    (e.g. tmp_path during tests)."""
    try:
        return str(path.relative_to(_REPO)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def scan_trace(path: Path) -> TraceScanResult:
    """Load + scan one trace file."""
    try:
        events = load_trace(path)
    except Exception as exc:
        return TraceScanResult(
            file=_rel_path(path),
            n_events=0, n_anomalies=0, anomaly_kinds={},
            sample_anomalies=[{"error": str(exc)}],
        )
    anomalies = find_anomalies(events)
    kinds: dict[str, int] = {}
    for a in anomalies:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    sample = [
        {"kind": a.kind, "at_index": a.at_index, "summary": a.summary}
        for a in anomalies[:3]
    ]
    return TraceScanResult(
        file=_rel_path(path),
        n_events=len(events),
        n_anomalies=len(anomalies),
        anomaly_kinds=kinds,
        sample_anomalies=sample,
    )


def scan_repo(dirs: Optional[list[Path]] = None) -> dict:
    """Scan all known trace directories. Returns dict suitable for JSON."""
    if dirs is None:
        dirs = [DEFAULT_TRACE_DIR, FIXTURE_DIR]
    trace_files = _find_trace_files(dirs)
    results = [scan_trace(p) for p in trace_files]
    total_anomalies = sum(r.n_anomalies for r in results)
    by_kind: dict[str, int] = {}
    for r in results:
        for kind, n in r.anomaly_kinds.items():
            by_kind[kind] = by_kind.get(kind, 0) + n
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_kind": "trace_anomaly_scan",
        "trace_dirs": [_rel_path(d) for d in dirs if d.exists()],
        "n_traces_scanned": len(results),
        "n_traces_with_anomalies": sum(1 for r in results if r.n_anomalies > 0),
        "total_anomalies": total_anomalies,
        "anomaly_kind_counts": by_kind,
        "traces": [r.to_dict() for r in results],
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Trace Anomaly Scan Report")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Trace dirs: {report['trace_dirs']}")
    lines.append(f"Traces scanned: **{report['n_traces_scanned']}**")
    lines.append(f"Traces with anomalies: **{report['n_traces_with_anomalies']}**")
    lines.append(f"Total anomalies found: **{report['total_anomalies']}**")
    lines.append("")
    if report["anomaly_kind_counts"]:
        lines.append("## Anomaly kinds (total)")
        lines.append("")
        for kind, n in sorted(report["anomaly_kind_counts"].items(),
                                key=lambda x: -x[1]):
            lines.append(f"- **{kind}**: {n}")
        lines.append("")
    lines.append("## Per-trace results")
    lines.append("")
    if not report["traces"]:
        lines.append("No traces found in any search directory.")
        return "\n".join(lines)
    for r in report["traces"]:
        if r["n_anomalies"] == 0:
            continue  # don't clutter operator review with clean traces
        lines.append(f"### `{r['file']}`")
        lines.append(f"  Events: {r['n_events']}, Anomalies: **{r['n_anomalies']}**")
        for kind, n in r["anomaly_kinds"].items():
            lines.append(f"  - {kind}: {n}")
        lines.append("  Sample:")
        for a in r["sample_anomalies"]:
            lines.append(f"    - [{a.get('kind', '?')}] at index {a.get('at_index', '?')}: "
                          f"{a.get('summary', a.get('error', ''))}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(
    report: dict,
    *,
    json_path: Path = DEFAULT_JSON,
    md_path: Path = DEFAULT_MD,
) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return (json_path, md_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", action="append", default=None,
                        help="Directory to scan (can pass multiple times)")
    parser.add_argument("--json-out", default=str(DEFAULT_JSON))
    parser.add_argument("--md-out", default=str(DEFAULT_MD))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.dir:
        dirs = [Path(d) for d in args.dir]
    else:
        dirs = [DEFAULT_TRACE_DIR, FIXTURE_DIR]
    try:
        report = scan_repo(dirs)
    except Exception as exc:
        print(f"FAIL: scan exploded: {exc}", file=sys.stderr)
        return 2

    write_outputs(report, json_path=Path(args.json_out), md_path=Path(args.md_out))

    if not args.quiet:
        print(f"Trace anomaly scan:")
        print(f"  Traces scanned: {report['n_traces_scanned']}")
        print(f"  Traces with anomalies: {report['n_traces_with_anomalies']}")
        print(f"  Total anomalies: {report['total_anomalies']}")
        if report["anomaly_kind_counts"]:
            print(f"  Kinds: {report['anomaly_kind_counts']}")
        print(f"  JSON: {args.json_out}")
        print(f"  Markdown: {args.md_out}")

    return 1 if report["total_anomalies"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
