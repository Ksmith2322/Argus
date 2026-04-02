"""Trade artifact schema maintenance for managed runner logs.

Ensures trades.csv files match the current canonical schema so governance
checks read the same columns the runner writes.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import discover_managed_runners
from argus_flow.schemas import trade_header

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _infer_uses_pips(trade_path: Path) -> bool:
    try:
        with open(trade_path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
    except OSError:
        return True
    return "pnl_pts" not in header


def _default_value(field: str) -> str:
    defaults = {
        "experiment_valid": "true",
        "invalid_reason": "",
    }
    return defaults.get(field, "")


def ensure_trade_csv_schema(trade_path: Path, uses_pips: bool) -> dict:
    """Ensure a single trades.csv file matches the canonical schema."""
    target_header = trade_header(uses_pips)
    trade_path.parent.mkdir(parents=True, exist_ok=True)

    if not trade_path.exists():
        with open(trade_path, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(target_header)
        return {
            "path": str(trade_path),
            "status": "CREATED",
            "rows": 0,
        }

    try:
        with open(trade_path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, [])
    except OSError as exc:
        return {
            "path": str(trade_path),
            "status": "ERROR",
            "reason": str(exc),
        }

    if existing_header == target_header:
        with open(trade_path, "r", encoding="utf-8") as handle:
            row_count = max(sum(1 for _ in handle) - 1, 0)
        return {
            "path": str(trade_path),
            "status": "CURRENT",
            "rows": row_count,
        }

    backup_path = trade_path.with_name(f"{trade_path.stem}.schema_backup_{_now_compact()}{trade_path.suffix}")
    shutil.copy2(trade_path, backup_path)

    with open(trade_path, "r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    tmp_path = trade_path.with_name(trade_path.name + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=target_header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            migrated = {field: row.get(field, _default_value(field)) for field in target_header}
            writer.writerow(migrated)

    tmp_path.replace(trade_path)
    return {
        "path": str(trade_path),
        "status": "MIGRATED",
        "rows": len(rows),
        "backup": str(backup_path),
    }


def migrate_managed_trade_artifacts() -> dict:
    """Migrate all managed runner trade journals to the current schema."""
    results: list[dict] = []
    seen: set[Path] = set()

    for runner in discover_managed_runners():
        trade_path = REPO / runner["log_dir"] / "trades.csv"
        if trade_path in seen:
            continue
        seen.add(trade_path)
        results.append(ensure_trade_csv_schema(trade_path, runner["unit"] == "pips"))

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checked": len(results),
        "created": sum(1 for item in results if item["status"] == "CREATED"),
        "migrated": sum(1 for item in results if item["status"] == "MIGRATED"),
        "current": sum(1 for item in results if item["status"] == "CURRENT"),
        "errors": [item for item in results if item["status"] == "ERROR"],
        "results": results,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate managed trades.csv files to the current schema")
    parser.parse_args()
    summary = migrate_managed_trade_artifacts()
    print(
        f"trade_artifact_schema: checked={summary['checked']} "
        f"created={summary['created']} migrated={summary['migrated']} current={summary['current']}"
    )
    if summary["errors"]:
        for item in summary["errors"][:10]:
            print(f"  ERROR {item['path']}: {item.get('reason', 'unknown')}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
