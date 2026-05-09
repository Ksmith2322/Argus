"""Diagnose Argus pair MTF gate behavior from read-only signal artifacts."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
POST_RESET = datetime(2026, 5, 1, tzinfo=timezone.utc)
PAIR_SIGNAL_FILES = [
    REPO / "argus_flow" / "logs" / "cadjpy" / "signals.csv",
    REPO / "argus_flow" / "logs" / "gbpusd" / "signals.csv",
    REPO / "argus_flow" / "logs" / "usdjpy" / "signals.csv",
]


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def strategy_for(path: Path) -> str:
    return f"argus_{path.parent.name}"


def reason_from_action(action: str) -> str:
    text = (action or "").upper()
    if text.startswith("MTF_BLOCKED_"):
        return text.replace("MTF_BLOCKED_", "")
    if "BLOCKED" in text:
        return text
    if text == "NO_TRIGGER":
        return "NO_TRIGGER"
    return text or "UNKNOWN"


def read_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in PAIR_SIGNAL_FILES:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for line_no, row in enumerate(reader, start=2):
                ts = parse_ts(row.get("ts", ""))
                if ts and ts < POST_RESET:
                    continue
                action = row.get("action", "")
                rows.append(
                    {
                        "strategy": strategy_for(path),
                        "symbol": path.parent.name.upper(),
                        "source_file": path.relative_to(REPO).as_posix(),
                        "source_line": line_no,
                        "ts": ts.isoformat() if ts else row.get("ts", ""),
                        "action": action,
                        "reason": reason_from_action(action),
                        "direction": row.get("direction", ""),
                        "regime": row.get("regime", ""),
                        "hour": row.get("hour", ""),
                        "price": row.get("price", ""),
                        "mtf_confidence": to_float(row.get("mtf_confidence")),
                        "mtf_consensus": to_float(row.get("mtf_consensus")),
                        "mtf_4h": row.get("mtf_4h", ""),
                        "mtf_1h": row.get("mtf_1h", ""),
                        "mtf_rsi": to_float(row.get("mtf_rsi")),
                        "mtf_bb_pos": to_float(row.get("mtf_bb_pos")),
                        "mtf_at_support": row.get("mtf_at_support", ""),
                        "mtf_at_resistance": row.get("mtf_at_resistance", ""),
                        "is_mtf_block": action.upper().startswith("MTF_BLOCKED_"),
                    }
                )
    return rows


def bucket_conf(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.25:
        return "0.00-0.24"
    if value < 0.50:
        return "0.25-0.49"
    if value < 0.75:
        return "0.50-0.74"
    return "0.75-1.00"


def write_detail(rows: list[dict[str, Any]]) -> None:
    fields = [
        "strategy",
        "symbol",
        "source_file",
        "source_line",
        "ts",
        "action",
        "reason",
        "direction",
        "regime",
        "hour",
        "price",
        "mtf_confidence",
        "mtf_consensus",
        "mtf_4h",
        "mtf_1h",
        "mtf_rsi",
        "mtf_bb_pos",
        "mtf_at_support",
        "mtf_at_resistance",
        "is_mtf_block",
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "argus_mtf_gate_diagnosis.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(rows: list[dict[str, Any]]) -> None:
    summary_rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["strategy"], row["reason"])].append(row)
    for (strategy, reason), items in sorted(groups.items()):
        confs = [row["mtf_confidence"] for row in items if row["mtf_confidence"] is not None]
        bb_pos = [row["mtf_bb_pos"] for row in items if row["mtf_bb_pos"] is not None]
        summary_rows.append(
            {
                "strategy": strategy,
                "reason": reason,
                "rows": len(items),
                "directions": ";".join(f"{k}:{v}" for k, v in Counter(row["direction"] or "none" for row in items).most_common()),
                "regimes": ";".join(f"{k}:{v}" for k, v in Counter(row["regime"] or "unknown" for row in items).most_common(5)),
                "hours_utc": ";".join(f"{k}:{v}" for k, v in Counter(row["hour"] or "unknown" for row in items).most_common(8)),
                "avg_mtf_confidence": round(mean(confs), 4) if confs else "",
                "avg_mtf_bb_pos": round(mean(bb_pos), 4) if bb_pos else "",
                "confidence_buckets": ";".join(f"{k}:{v}" for k, v in Counter(bucket_conf(row["mtf_confidence"]) for row in items).most_common()),
                "first_ts": min(str(row["ts"]) for row in items if row["ts"]),
                "last_ts": max(str(row["ts"]) for row in items if row["ts"]),
                "diagnosis": diagnosis(reason, len(items)),
            }
        )
    with (OUT_DIR / "argus_mtf_gate_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = list(summary_rows[0].keys()) if summary_rows else [
            "strategy",
            "reason",
            "rows",
            "directions",
            "regimes",
            "hours_utc",
            "avg_mtf_confidence",
            "avg_mtf_bb_pos",
            "confidence_buckets",
            "first_ts",
            "last_ts",
            "diagnosis",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)
    write_markdown(rows, summary_rows)


def diagnosis(reason: str, count: int) -> str:
    if reason == "NO_TRIGGER":
        return "Baseline market evaluations; not an entry gate failure."
    if reason.startswith("MTF_"):
        return "High-value audit target: compare blocked opportunities to forward outcomes before changing thresholds."
    if "BLOCKED" in reason:
        return "Blocked opportunity source; requires forward outcome scoring."
    if count == 0:
        return "No evidence."
    return "Signal artifact; classify with opportunity ledger."


def write_markdown(rows: list[dict[str, Any]], summary_rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    mtf_blocks = sum(1 for row in rows if row["is_mtf_block"])
    generated = sum(1 for row in rows if row["direction"])
    lines = [
        "# Argus MTF Gate Diagnosis",
        "",
        "Read-only diagnosis of post-reset CADJPY/GBPUSD/USDJPY signal artifacts.",
        "",
        f"- Total post-reset rows: {total}",
        f"- Rows with direction: {generated}",
        f"- MTF hard-blocked rows: {mtf_blocks}",
        f"- MTF block share of all rows: {(mtf_blocks / total * 100):.2f}%" if total else "- MTF block share of all rows: n/a",
        f"- MTF block share of directional rows: {(mtf_blocks / generated * 100):.2f}%" if generated else "- MTF block share of directional rows: n/a",
        "",
        "## Top Block Reasons",
    ]
    reason_counts = Counter(row["reason"] for row in rows if row["reason"] != "NO_TRIGGER")
    for reason, count in reason_counts.most_common(10):
        lines.append(f"- {reason}: {count}")
    lines.extend(
        [
            "",
            "## Interpretation",
            "Argus pairs are not dead from trade PnL evidence; they are blocked systems with no broker fills.",
            "The next valid performance question is whether the MTF hard gate is preventing negative expectancy or blocking winners.",
            "Do not loosen the gate until blocked rows are joined to forward outcome windows.",
            "",
            "## Exact Next Test",
            "Replay every MTF_BLOCKED row through the original stop, target, timeout, spread, and session model.",
            "Pass gate: blocked losers outnumber blocked winners after friction, or the gate improves expectancy versus ungated entries.",
            "Fail gate: blocked winners materially exceed blocked losers after friction across at least 60 post-reset candidates per pair.",
        ]
    )
    lines.extend(["", "## Largest Diagnosis Rows"])
    for row in sorted(summary_rows, key=lambda item: int(item["rows"]), reverse=True)[:12]:
        lines.append(f"- {row['strategy']} {row['reason']}: {row['rows']} rows; {row['diagnosis']}")
    (OUT_DIR / "argus_mtf_gate_diagnosis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Argus MTF hard gate artifacts")
    parser.parse_args()
    rows = read_rows()
    write_detail(rows)
    write_summary(rows)
    print(f"wrote {len(rows)} Argus MTF diagnosis rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
