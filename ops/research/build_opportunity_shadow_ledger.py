"""Build a normalized read-only opportunity shadow ledger.

This script reads existing signal and opportunity artifacts and writes an
analytics ledger under ops/reports/system_audit. It does not write into runtime
state and does not change strategy behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
POST_RESET = datetime(2026, 5, 1, tzinfo=timezone.utc)


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def strategy_from_path(path: Path) -> str:
    rel = path.relative_to(REPO)
    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "logs":
        prefix = "argus" if parts[0] == "argus_flow" else parts[0]
        return f"{prefix}_{parts[2]}".replace("-", "_")
    return path.parent.name


def classify_action(action: str, direction: str) -> str:
    text = (action or "").upper()
    if "BLOCKED" in text or "BLACKOUT" in text:
        return "BLOCKED"
    if text in {"BUY", "SELL", "LONG", "SHORT", "TAKE", "ENTER"} or direction:
        return "SIGNAL_GENERATED"
    if text == "NO_TRIGGER":
        return "NO_TRIGGER"
    return "OBSERVED"


def is_current_artifact(path: Path) -> bool:
    lowered = str(path).lower()
    return "_archive" not in lowered and "pre_reset" not in lowered


def read_signal_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(REPO.glob("**/signals*.csv")):
        if not is_current_artifact(path):
            continue
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for line_no, row in enumerate(reader, start=2):
                    ts = parse_ts(row.get("ts", ""))
                    if ts and ts < POST_RESET:
                        continue
                    action = row.get("action", "")
                    direction = row.get("direction", "")
                    rows.append(
                        {
                            "source": "signals_csv",
                            "source_file": path.relative_to(REPO).as_posix(),
                            "source_line": line_no,
                            "strategy": strategy_from_path(path),
                            "symbol": row.get("symbol") or path.parent.name.upper(),
                            "ts": ts.isoformat() if ts else row.get("ts", ""),
                            "ledger_class": classify_action(action, direction),
                            "action": action,
                            "direction": direction,
                            "block_reason": action if "BLOCKED" in action.upper() else "",
                            "price": row.get("price", ""),
                            "regime": row.get("regime", ""),
                            "hour": row.get("hour", ""),
                            "mtf_confidence": row.get("mtf_confidence", ""),
                            "mtf_consensus": row.get("mtf_consensus", ""),
                            "mtf_4h": row.get("mtf_4h", ""),
                            "mtf_1h": row.get("mtf_1h", ""),
                            "mtf_rsi": row.get("mtf_rsi", ""),
                            "mtf_bb_pos": row.get("mtf_bb_pos", ""),
                            "mtf_at_support": row.get("mtf_at_support", ""),
                            "mtf_at_resistance": row.get("mtf_at_resistance", ""),
                            "future_outcome_status": "NOT_COMPUTED",
                            "future_outcome_note": "Needs bar-aligned forward windows before filter ROI can be scored.",
                        }
                    )
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return rows


def read_opportunity_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(REPO.glob("**/opportunities.jsonl")):
        if not is_current_artifact(path):
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(str(payload.get("ts", "")))
                    if ts and ts < POST_RESET:
                        continue
                    rows.append(
                        {
                            "source": "opportunities_jsonl",
                            "source_file": path.relative_to(REPO).as_posix(),
                            "source_line": line_no,
                            "strategy": strategy_from_path(path),
                            "symbol": payload.get("symbol", path.parent.name.upper()),
                            "ts": ts.isoformat() if ts else payload.get("ts", ""),
                            "ledger_class": "BLOCKED",
                            "action": payload.get("block_reason", ""),
                            "direction": payload.get("direction", ""),
                            "block_reason": payload.get("block_reason", ""),
                            "price": payload.get("price", ""),
                            "regime": payload.get("regime", ""),
                            "hour": payload.get("hour", ""),
                            "mtf_confidence": "",
                            "mtf_consensus": "",
                            "mtf_4h": "",
                            "mtf_1h": "",
                            "mtf_rsi": "",
                            "mtf_bb_pos": "",
                            "mtf_at_support": "",
                            "mtf_at_resistance": "",
                            "future_outcome_status": "NOT_COMPUTED",
                            "future_outcome_note": "Needs bar-aligned forward windows before blocked-trade ROI can be scored.",
                        }
                    )
        except OSError:
            continue
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source",
        "source_file",
        "source_line",
        "strategy",
        "symbol",
        "ts",
        "ledger_class",
        "action",
        "direction",
        "block_reason",
        "price",
        "regime",
        "hour",
        "mtf_confidence",
        "mtf_consensus",
        "mtf_4h",
        "mtf_1h",
        "mtf_rsi",
        "mtf_bb_pos",
        "mtf_at_support",
        "mtf_at_resistance",
        "future_outcome_status",
        "future_outcome_note",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    by_class: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    for row in rows:
        by_class[row["ledger_class"]] = by_class.get(row["ledger_class"], 0) + 1
        by_strategy[row["strategy"]] = by_strategy.get(row["strategy"], 0) + 1
    lines = [
        "# Opportunity Shadow Ledger",
        "",
        "This is a read-only normalization of post-reset signal and blocked-opportunity artifacts.",
        "Future outcome fields are intentionally blank until bar-aligned forward windows are wired.",
        "",
        f"- Rows: {len(rows)}",
        "",
        "## By Ledger Class",
    ]
    for key, value in sorted(by_class.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Largest Strategy Sources"])
    for key, value in sorted(by_strategy.items(), key=lambda item: (-item[1], item[0]))[:15]:
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Required Next Step",
            "Join this ledger to bar data and closed-trade rules to calculate false negatives and false positives.",
            "Until that join exists, blocked trade counts are throughput evidence, not expectancy evidence.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Argus opportunity shadow ledger")
    parser.parse_args()
    rows = read_signal_rows() + read_opportunity_rows()
    rows.sort(key=lambda row: (row.get("ts", ""), row.get("strategy", ""), row.get("source_file", "")))
    write_csv(OUT_DIR / "opportunity_shadow_ledger.csv", rows)
    write_summary(OUT_DIR / "opportunity_shadow_ledger.md", rows)
    print(f"wrote {len(rows)} opportunity ledger rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
