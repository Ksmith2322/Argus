"""Log Rotation — archive and compress old CSV/JSONL logs.

Rotates signals.csv, trades.csv, and opportunities.jsonl per runner when they
exceed a configurable line threshold. Archived files are compressed with gzip
and stored in an 'archive/' subdirectory.

Usage:
    python -m argus_flow.ops.log_rotation
    python -m argus_flow.ops.log_rotation --dry-run
    python -m argus_flow.ops.log_rotation --max-lines 5000
"""
from __future__ import annotations

import argparse
import csv
import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"

# Files eligible for rotation (per-runner log dirs)
ROTATABLE_FILES = ["signals.csv", "trades.csv", "opportunities.jsonl"]

# Default rotation threshold
DEFAULT_MAX_LINES = 10000


def _count_lines(path: Path) -> int:
    """Count lines efficiently without loading the whole file."""
    if not path.exists():
        return 0
    count = 0
    with open(path, "rb") as f:
        for _ in f:
            count += 1
    return count


def _rotate_file(path: Path, max_lines: int, dry_run: bool = False) -> dict | None:
    """Rotate a file if it exceeds max_lines.

    Keeps the header + last max_lines/2 lines in the active file.
    Archives the older lines to a timestamped gzip file.
    """
    line_count = _count_lines(path)
    if line_count <= max_lines:
        return None

    is_csv = path.suffix == ".csv"
    keep_lines = max_lines // 2  # keep recent half after rotation

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = path.parent / "archive"
    archive_name = f"{path.stem}_{ts}{path.suffix}.gz"
    archive_path = archive_dir / archive_name

    if dry_run:
        return {
            "file": str(path),
            "lines": line_count,
            "action": "would rotate",
            "archive": str(archive_path),
            "keep": keep_lines,
        }

    # Read all lines
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    header = all_lines[0] if is_csv and all_lines else None
    data_lines = all_lines[1:] if is_csv else all_lines

    # Split: archive older lines, keep recent lines
    archive_lines = data_lines[:-keep_lines]
    keep_data = data_lines[-keep_lines:]

    # Write archive (compressed)
    archive_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive_path, "wt", encoding="utf-8") as gz:
        if header:
            gz.write(header)
        gz.writelines(archive_lines)

    # Rewrite active file with header + recent data
    with open(path, "w", encoding="utf-8", newline="") as f:
        if header:
            f.write(header)
        f.writelines(keep_data)

    return {
        "file": str(path),
        "lines_before": line_count,
        "lines_after": len(keep_data) + (1 if header else 0),
        "archived": len(archive_lines),
        "archive_path": str(archive_path),
    }


def rotate_all(max_lines: int = DEFAULT_MAX_LINES, dry_run: bool = False) -> list[dict]:
    """Scan all runner log dirs and rotate eligible files."""
    results = []

    # Find all runner log dirs (immediate subdirs of LOGS_ROOT that have CSV files)
    if not LOGS_ROOT.exists():
        return results

    for log_dir in sorted(LOGS_ROOT.iterdir()):
        if not log_dir.is_dir():
            continue
        if log_dir.name.startswith("_"):
            continue  # skip internal dirs like _locks, _risk, _broker

        for filename in ROTATABLE_FILES:
            filepath = log_dir / filename
            if filepath.exists():
                result = _rotate_file(filepath, max_lines, dry_run)
                if result:
                    results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(description="Rotate Argus log files")
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                        help=f"Rotate files exceeding this line count (default: {DEFAULT_MAX_LINES})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be rotated without making changes")
    args = parser.parse_args()

    print("=" * 55)
    print(f"  Log Rotation — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Threshold: {args.max_lines} lines | {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 55)

    results = rotate_all(max_lines=args.max_lines, dry_run=args.dry_run)

    if not results:
        print("\n  No files need rotation.")
    else:
        for r in results:
            if args.dry_run:
                print(f"\n  {r['file']}")
                print(f"    {r['lines']} lines → would archive, keep {r['keep']}")
            else:
                print(f"\n  {r['file']}")
                print(f"    {r['lines_before']} → {r['lines_after']} lines")
                print(f"    Archived {r['archived']} lines → {r['archive_path']}")

    print(f"\n  Total: {len(results)} file(s) {'would be' if args.dry_run else ''} rotated")


if __name__ == "__main__":
    main()
