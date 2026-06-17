"""Order lifecycle audit (Codex X7).

Walks the codebase, finds every `ib.placeOrder(...)` call site, and classifies
each by:
  - Is the call inside a try/except (network/IBKR error handling)?
  - Is there a pre-allocate-orderId pattern (assigns order.orderId before
    placeOrder, then writes state, then calls placeOrder) — eliminates the
    placeOrder→state-write race window per project_2026_05_20.
  - Is there a state-write (save / write_pending) before the placeOrder?
  - Is the trade reference captured for later monitoring?

The audit is HEURISTIC — it inspects ~30 lines of context around each
placeOrder call. Classification is conservative: when uncertain, flags as
"UNVERIFIED" rather than "PROTECTED."

CLI:
    python -m ops.audit.order_lifecycle_audit

Output:
    argus_flow/logs/order_lifecycle_audit.json
    argus_flow/logs/order_lifecycle_audit.md  (operator-readable)

Exit codes:
    0  no UNPROTECTED sites
    1  some UNPROTECTED sites (review needed)
    2  errors during audit (file not readable, etc.)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "argus_flow" / "logs"
DEFAULT_JSON = LOG_DIR / "order_lifecycle_audit.json"
DEFAULT_MD = LOG_DIR / "order_lifecycle_audit.md"

PLACE_ORDER_RE = re.compile(r"\b(ib|self\._ib|self\.ib)\.placeOrder\b")

# Patterns that indicate protective measures
PRE_ALLOCATE_HINTS = (
    "_pre_allocate_order_id",
    "pre_allocate_order_id",
    "order.orderId =",
    "order.orderId=",
)
STATE_WRITE_HINTS = (
    "s.save()", "state.save()", "self.state.save()",
    "write_pending", "save_state",
)
TRY_HINT = "try:"
EXCEPT_HINT = "except"

# Directories to skip (vendored / archived code)
SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "archive",
             "_research_archive", "data_yfinance", "data_massive",
             "node_modules"}


@dataclass
class CallSite:
    file: str
    line: int
    function: str
    context_excerpt: str
    has_try_except: bool
    has_pre_allocate: bool
    has_state_write_before: bool
    has_trade_capture: bool
    classification: str  # PROTECTED / PARTIAL / UNPROTECTED / UNVERIFIED
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        # Skip vendored / cache dirs
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _enclosing_function(source_lines: list[str], call_line_idx: int) -> str:
    """Walk backward from the call line to find the def-line."""
    for i in range(call_line_idx, -1, -1):
        line = source_lines[i]
        m = re.match(r"\s*(async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            return m.group(2)
    return "<module-level>"


def classify_call_site(
    source_lines: list[str],
    call_line_idx: int,
    window_before: int = 25,
    window_after: int = 5,
) -> CallSite:
    """Inspect ~30 lines around a placeOrder call and classify."""
    start = max(0, call_line_idx - window_before)
    end = min(len(source_lines), call_line_idx + window_after + 1)
    context = source_lines[start:end]
    excerpt = "".join(f"  {start + i + 1:>4} | {line}"
                       for i, line in enumerate(context))
    text_before = "".join(source_lines[start:call_line_idx])
    text_after = "".join(source_lines[call_line_idx + 1:end])

    has_try = TRY_HINT in text_before and any(
        l.lstrip().startswith(EXCEPT_HINT)
        for l in source_lines[call_line_idx:min(len(source_lines), call_line_idx + 30)]
    )
    has_pre_alloc = any(hint in text_before for hint in PRE_ALLOCATE_HINTS)
    has_state_write = any(hint in text_before for hint in STATE_WRITE_HINTS)
    has_trade_capture = bool(re.search(r"=\s*(ib|self\._ib|self\.ib)\.placeOrder",
                                        source_lines[call_line_idx]))

    if has_pre_alloc and has_state_write and has_try:
        classification = "PROTECTED"
        notes = "Pre-alloc orderId + state-write before placeOrder + try/except"
    elif has_pre_alloc and has_try:
        classification = "PARTIAL"
        notes = "Pre-alloc orderId + try/except; state-write ordering uncertain"
    elif has_try:
        classification = "UNVERIFIED"
        notes = "Has try/except but no clear pre-alloc pattern visible"
    else:
        classification = "UNPROTECTED"
        notes = "No try/except or pre-alloc pattern detected in surrounding 25 lines"

    return CallSite(
        file="", line=call_line_idx + 1,
        function="",  # caller fills in
        context_excerpt=excerpt,
        has_try_except=has_try,
        has_pre_allocate=has_pre_alloc,
        has_state_write_before=has_state_write,
        has_trade_capture=has_trade_capture,
        classification=classification,
        notes=notes,
    )


def audit_file(path: Path) -> list[CallSite]:
    """Find all placeOrder calls in one file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines(keepends=True)
    sites = []
    for i, line in enumerate(lines):
        if PLACE_ORDER_RE.search(line):
            site = classify_call_site(lines, i)
            site.file = str(path.relative_to(REPO)).replace("\\", "/")
            site.function = _enclosing_function(lines, i)
            sites.append(site)
    return sites


def audit_repo(root: Path = REPO) -> dict:
    """Run the full audit. Returns dict suitable for JSON output."""
    all_sites: list[CallSite] = []
    files_with_calls: set[str] = set()
    for path in _iter_python_files(root):
        sites = audit_file(path)
        if sites:
            files_with_calls.add(str(path.relative_to(root)).replace("\\", "/"))
            all_sites.extend(sites)

    counts: dict[str, int] = {}
    for s in all_sites:
        counts[s.classification] = counts.get(s.classification, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_kind": "order_lifecycle (Codex X7)",
        "total_call_sites": len(all_sites),
        "files_with_calls": sorted(files_with_calls),
        "classification_counts": counts,
        "sites": [s.to_dict() for s in all_sites],
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Order Lifecycle Audit Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Total call sites: **{report['total_call_sites']}**",
        f"Files: {len(report['files_with_calls'])}",
        "",
        "## Classification counts",
        "",
    ]
    for cls in ("PROTECTED", "PARTIAL", "UNVERIFIED", "UNPROTECTED"):
        n = report["classification_counts"].get(cls, 0)
        lines.append(f"- **{cls}**: {n}")
    lines.append("")
    lines.append("## Per-file breakdown")
    lines.append("")

    by_file: dict[str, list[dict]] = {}
    for s in report["sites"]:
        by_file.setdefault(s["file"], []).append(s)
    for f in sorted(by_file):
        sites = by_file[f]
        lines.append(f"### {f}")
        lines.append(f"  {len(sites)} call site(s)")
        for s in sites:
            sym = {"PROTECTED": "+", "PARTIAL": "~", "UNVERIFIED": "?",
                   "UNPROTECTED": "!"}.get(s["classification"], "?")
            lines.append(f"  - [{sym}] L{s['line']} `{s['function']}()` — "
                         f"{s['classification']}: {s['notes']}")
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
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--md", default=str(DEFAULT_MD))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_repo()
    except Exception as e:
        print(f"FAIL: audit exploded: {e}", file=sys.stderr)
        return 2
    write_outputs(report, json_path=Path(args.json), md_path=Path(args.md))
    counts = report["classification_counts"]
    if not args.quiet:
        print(f"Order lifecycle audit:")
        print(f"  Total call sites: {report['total_call_sites']}")
        print(f"  Files: {len(report['files_with_calls'])}")
        print(f"  Classification: {counts}")
        print(f"  JSON: {args.json}")
        print(f"  Markdown: {args.md}")
    return 1 if counts.get("UNPROTECTED", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
