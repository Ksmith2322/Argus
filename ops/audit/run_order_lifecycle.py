"""Run the per-lineage order lifecycle audit (Codex X7).

Produces a JSON + markdown report of every distinct strategy intent
in canonical_fills.jsonl, classified as:

  COMPLETE        — 1 ENTRY + matching EXIT(s) with sizes that net to zero
  ORPHAN_ENTRY    — ENTRY without a matching EXIT (open position OR lost)
  ORPHAN_EXIT     — EXIT without a matching ENTRY (broker fill leaked in
                    without a prior intent — INVESTIGATE)
  DUPLICATE_ENTRY — >1 ENTRY rows under one lineage (impossible by
                    design; if seen, the writer wrote twice)
  PARTIAL_EXIT    — ENTRY size != sum of EXIT sizes (still open in
                    part, or exit attribution wrong)
  LINEAGE_MISSING — fills without lineage_id (legacy rows before
                    commit 1d25487)

Exit codes:
  0 — only COMPLETE / LINEAGE_MISSING (clean operational state)
  1 — ORPHAN_ENTRY present (review — could be in-flight positions)
  2 — ORPHAN_EXIT or DUPLICATE_ENTRY or PARTIAL_EXIT present (real bug)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _render_markdown(summary: dict) -> str:
    out = [
        "# Order lifecycle audit (Codex X7)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Total lineages: {summary['n_lineages']}",
        "",
        "## Verdict counts",
        "",
    ]
    counts = summary["verdict_counts"]
    for verdict in (
        "COMPLETE", "ORPHAN_ENTRY", "ORPHAN_EXIT",
        "DUPLICATE_ENTRY", "PARTIAL_EXIT", "LINEAGE_MISSING",
    ):
        n = counts.get(verdict, 0)
        if n:
            out.append(f"  - {verdict}: {n}")
    out.append("")

    if summary.get("per_strategy"):
        out.append("## Per strategy")
        out.append("")
        out.append("| Strategy | COMPLETE | ORPH_ENT | ORPH_EXT | DUP | PARTIAL | LEGACY |")
        out.append("|---|---|---|---|---|---|---|")
        for s, b in sorted(summary["per_strategy"].items()):
            out.append(
                f"| {s} | "
                f"{b.get('COMPLETE', 0)} | "
                f"{b.get('ORPHAN_ENTRY', 0)} | "
                f"{b.get('ORPHAN_EXIT', 0)} | "
                f"{b.get('DUPLICATE_ENTRY', 0)} | "
                f"{b.get('PARTIAL_EXIT', 0)} | "
                f"{b.get('LINEAGE_MISSING', 0)} |"
            )
        out.append("")

    # Highlight every non-COMPLETE lineage at the bottom
    anomalies = [lc for lc in summary.get("lifecycles", [])
                 if lc["verdict"] not in ("COMPLETE",)]
    if anomalies:
        out.append("## Anomalies (full detail)")
        out.append("")
        for lc in anomalies:
            out.append(f"### {lc['lineage_id']} ({lc['verdict']})")
            out.append(f"strategy={lc['strategy']}  "
                       f"entries={lc['n_entries']} exits={lc['n_exits']}  "
                       f"realized_pnl=${lc['realized_pnl_usd']:.2f}")
            for r in lc.get("reasons", []):
                out.append(f"  - {r}")
            out.append("")
    return "\n".join(out)


def _exit_code(summary: dict) -> int:
    counts = summary.get("verdict_counts", {})
    if counts.get("ORPHAN_EXIT", 0) \
       or counts.get("DUPLICATE_ENTRY", 0) \
       or counts.get("PARTIAL_EXIT", 0):
        return 2
    if counts.get("ORPHAN_ENTRY", 0):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    from helio.order_lifecycle import reconcile_all
    summary = reconcile_all()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "order_lifecycle.json"
    json_path.write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_render_markdown(summary))
        print(f"\nPersisted: {json_path}")

    return _exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
