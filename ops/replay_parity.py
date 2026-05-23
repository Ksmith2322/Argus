"""ops/replay_parity.py — CLI wrapper for helio.replay_parity.

Adds operator-friendly conveniences over the bare module CLI:
  - Loop over a directory of traces (default: argus_flow/logs/traces)
  - Write a single aggregate report
  - Operator-readable markdown summary

CLI:
    python -m ops.replay_parity                              # scan default dir
    python -m ops.replay_parity --dir path/to/traces
    python -m ops.replay_parity --trace single_file.jsonl
    python -m ops.replay_parity --json                       # JSON output

Output:
    argus_flow/logs/replay_parity_summary.json
    argus_flow/logs/replay_parity_summary.md

Exit codes:
    0  all traces PASS
    1  one or more DIVERGENCE
    2  one or more LOAD_ERROR (or other fatal)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helio.replay_parity import run_parity_check, ReplayParityResult


_REPO = Path(__file__).resolve().parents[1]
LOG_DIR = _REPO / "argus_flow" / "logs"
DEFAULT_TRACE_DIR = LOG_DIR / "traces"
DEFAULT_JSON = LOG_DIR / "replay_parity_summary.json"
DEFAULT_MD = LOG_DIR / "replay_parity_summary.md"


def _find_traces(search_dir: Path) -> list[Path]:
    """All JSONLs in dir, sorted newest-first."""
    if not search_dir.exists():
        return []
    skip_names = ("canonical_fills", "broker_equity", "alert_events",
                   "history", "opportunities", "signals")
    out = []
    for p in search_dir.rglob("*.jsonl"):
        if any(n in p.name.lower() for n in skip_names):
            continue
        out.append(p)
    return sorted(out, key=lambda p: -p.stat().st_mtime)


def render_markdown(results: list[ReplayParityResult]) -> str:
    lines = []
    lines.append("# Replay Parity Summary")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Traces scanned: **{len(results)}**")
    pass_n = sum(1 for r in results if r.verdict == "PASS")
    diverge_n = sum(1 for r in results if r.verdict == "DIVERGENCE")
    error_n = sum(1 for r in results if r.verdict == "LOAD_ERROR")
    lines.append(f"PASS: {pass_n}, DIVERGENCE: {diverge_n}, LOAD_ERROR: {error_n}")
    lines.append("")
    lines.append("| Trace | Verdict | Events | OK / Fail | Diffs |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        name = Path(r.trace_path).name
        lines.append(f"| `{name}` | {r.verdict} | {r.n_events_original} | "
                     f"{r.n_dispatched_ok}/{r.n_dispatched_failed} | "
                     f"{r.n_differences} |")
    # Detail for divergences
    diverged = [r for r in results if r.verdict != "PASS"]
    if diverged:
        lines.append("")
        lines.append("## Detail for non-PASS traces")
        lines.append("")
        for r in diverged:
            lines.append(f"### `{Path(r.trace_path).name}`")
            lines.append(f"Verdict: **{r.verdict}**")
            if r.failure_reasons:
                lines.append("")
                lines.append("Dispatch failures:")
                for fr in r.failure_reasons:
                    lines.append(f"  - {fr}")
            if r.differences_sample:
                lines.append("")
                lines.append("Differences (first 10):")
                for d in r.differences_sample:
                    lines.append(f"  - [{d.get('kind', '?')}] at index "
                                  f"{d.get('at_index', '?')}: {d.get('summary', '')}")
            lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", help="Single trace file to check")
    parser.add_argument("--dir", default=str(DEFAULT_TRACE_DIR),
                        help="Directory to scan (default: argus_flow/logs/traces)")
    parser.add_argument("--json-out", default=str(DEFAULT_JSON))
    parser.add_argument("--md-out", default=str(DEFAULT_MD))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.trace:
        results = [run_parity_check(Path(args.trace))]
    else:
        traces = _find_traces(Path(args.dir))
        results = [run_parity_check(t) for t in traces]

    # Write outputs
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_traces": len(results),
        "verdict_counts": {
            "PASS": sum(1 for r in results if r.verdict == "PASS"),
            "DIVERGENCE": sum(1 for r in results if r.verdict == "DIVERGENCE"),
            "LOAD_ERROR": sum(1 for r in results if r.verdict == "LOAD_ERROR"),
        },
        "results": [r.to_dict() for r in results],
    }
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str),
                                    encoding="utf-8")
    Path(args.md_out).write_text(render_markdown(results), encoding="utf-8")

    if not args.quiet:
        print(f"Replay parity summary:")
        print(f"  Traces scanned: {len(results)}")
        print(f"  Verdicts: {payload['verdict_counts']}")
        print(f"  JSON: {args.json_out}")
        print(f"  Markdown: {args.md_out}")

    # Exit code: 0 if all PASS, 1 if any DIVERGENCE, 2 if any LOAD_ERROR
    if any(r.verdict == "LOAD_ERROR" for r in results):
        return 2
    if any(r.verdict == "DIVERGENCE" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
