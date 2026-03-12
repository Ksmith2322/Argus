#!/usr/bin/env python3
"""
ops/backup_restore.py  --  Phase 16 Daily Backup & Restore Drill

Automated backup of critical artifacts and a restore verification drill.

Backup:
  - Copies ops/logs/*.csv, ops/logs/*.json, state/*.json to dated backup dir
  - Creates a manifest with hashes of all backed-up files

Restore drill:
  - Copies backup to a temporary restore dir
  - Verifies all files match backup manifest hashes
  - Logs PASS/FAIL result

Usage:
    python ops/backup_restore.py backup
    python ops/backup_restore.py restore-drill
    python ops/backup_restore.py full          # backup + restore drill
"""

import hashlib
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _default_log_dir() -> str:
    return os.path.join(_REPO_ROOT, "ops", "logs")


def _default_state_dir() -> str:
    return os.path.join(_REPO_ROOT, "state")


def _default_backup_root() -> str:
    return os.path.join(_REPO_ROOT, "ops", "backups")


def _backup_dir_for_date(backup_root: str, date_str: Optional[str] = None) -> str:
    if date_str is None:
        date_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return os.path.join(backup_root, f"backup_{date_str}")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def run_backup(
    *,
    log_dir: Optional[str] = None,
    state_dir: Optional[str] = None,
    backup_root: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Create a backup of all critical artifacts.

    Returns (ok, backup_path, manifest).
    """
    log_dir = log_dir or _default_log_dir()
    state_dir = state_dir or _default_state_dir()
    backup_root = backup_root or _default_backup_root()
    backup_dir = _backup_dir_for_date(backup_root)

    os.makedirs(backup_dir, exist_ok=True)

    manifest: Dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_log_dir": log_dir,
        "source_state_dir": state_dir,
        "files": {},
    }

    copied = 0

    # Copy log artifacts
    if os.path.isdir(log_dir):
        for fname in os.listdir(log_dir):
            if fname.endswith((".csv", ".json", ".jsonl")):
                src = os.path.join(log_dir, fname)
                dst = os.path.join(backup_dir, "logs", fname)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    manifest["files"][f"logs/{fname}"] = {
                        "hash": _file_hash(src),
                        "size": os.path.getsize(src),
                    }
                    copied += 1
                except Exception as e:
                    manifest["files"][f"logs/{fname}"] = {"error": str(e)}

    # Copy state artifacts
    if os.path.isdir(state_dir):
        for fname in os.listdir(state_dir):
            if fname.endswith(".json"):
                src = os.path.join(state_dir, fname)
                dst = os.path.join(backup_dir, "state", fname)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    manifest["files"][f"state/{fname}"] = {
                        "hash": _file_hash(src),
                        "size": os.path.getsize(src),
                    }
                    copied += 1
                except Exception as e:
                    manifest["files"][f"state/{fname}"] = {"error": str(e)}

    manifest["files_copied"] = copied

    # Write manifest
    manifest_path = os.path.join(backup_dir, "backup_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return True, backup_dir, manifest


# ---------------------------------------------------------------------------
# Restore drill
# ---------------------------------------------------------------------------

def run_restore_drill(
    backup_dir: str,
) -> Tuple[bool, List[str]]:
    """Verify a backup by restoring to temp dir and checking hashes.

    Returns (all_ok, list_of_issues).
    """
    manifest_path = os.path.join(backup_dir, "backup_manifest.json")
    if not os.path.exists(manifest_path):
        return False, ["backup_manifest.json not found"]

    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    issues: List[str] = []
    files = manifest.get("files", {})

    for rel_path, meta in files.items():
        if "error" in meta:
            issues.append(f"{rel_path}: backup had error: {meta['error']}")
            continue

        src_path = os.path.join(backup_dir, rel_path)
        if not os.path.exists(src_path):
            issues.append(f"{rel_path}: file missing from backup")
            continue

        expected_hash = meta.get("hash", "")
        actual_hash = _file_hash(src_path)
        if expected_hash and actual_hash != expected_hash:
            issues.append(f"{rel_path}: hash mismatch (expected={expected_hash[:16]}... actual={actual_hash[:16]}...)")
            continue

        expected_size = meta.get("size", -1)
        actual_size = os.path.getsize(src_path)
        if expected_size >= 0 and actual_size != expected_size:
            issues.append(f"{rel_path}: size mismatch (expected={expected_size} actual={actual_size})")

    all_ok = len(issues) == 0
    return all_ok, issues


# ---------------------------------------------------------------------------
# Full cycle: backup + restore drill
# ---------------------------------------------------------------------------

def run_full_cycle(
    *,
    log_dir: Optional[str] = None,
    state_dir: Optional[str] = None,
    backup_root: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Run backup + restore drill. Returns (all_ok, report)."""
    report: Dict[str, Any] = {}

    # Backup
    ok, backup_dir, manifest = run_backup(
        log_dir=log_dir,
        state_dir=state_dir,
        backup_root=backup_root,
    )
    report["backup_ok"] = ok
    report["backup_dir"] = backup_dir
    report["files_copied"] = manifest.get("files_copied", 0)

    if not ok:
        report["restore_ok"] = False
        report["restore_issues"] = ["backup failed"]
        return False, report

    # Restore drill
    drill_ok, issues = run_restore_drill(backup_dir)
    report["restore_ok"] = drill_ok
    report["restore_issues"] = issues

    # Write drill result log
    drill_log_path = os.path.join(backup_dir, "restore_drill_result.json")
    drill_result = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backup_dir": backup_dir,
        "ok": drill_ok,
        "issues": issues,
    }
    try:
        with open(drill_log_path, "w") as f:
            json.dump(drill_result, f, indent=2)
    except Exception:
        pass

    return drill_ok, report


# ---------------------------------------------------------------------------
# Cleanup old backups (keep last N)
# ---------------------------------------------------------------------------

def cleanup_old_backups(backup_root: Optional[str] = None, keep: int = 7) -> int:
    """Remove old backups, keeping the most recent N. Returns count removed."""
    backup_root = backup_root or _default_backup_root()
    if not os.path.isdir(backup_root):
        return 0

    dirs = []
    for name in os.listdir(backup_root):
        path = os.path.join(backup_root, name)
        if os.path.isdir(path) and name.startswith("backup_"):
            dirs.append((name, path))

    dirs.sort(key=lambda x: x[0], reverse=True)

    removed = 0
    for name, path in dirs[keep:]:
        try:
            shutil.rmtree(path)
            removed += 1
        except Exception:
            pass

    return removed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Argus backup & restore drill")
    parser.add_argument("action", choices=["backup", "restore-drill", "full", "cleanup"],
                        help="Action to perform")
    parser.add_argument("--backup-dir", help="Backup dir for restore-drill")
    parser.add_argument("--keep", type=int, default=7, help="Backups to keep for cleanup")
    args = parser.parse_args()

    if args.action == "backup":
        ok, backup_dir, manifest = run_backup()
        print(f"Backup: {'PASS' if ok else 'FAIL'}")
        print(f"  dir: {backup_dir}")
        print(f"  files: {manifest.get('files_copied', 0)}")
        return 0 if ok else 1

    elif args.action == "restore-drill":
        if not args.backup_dir:
            # Use latest backup
            root = _default_backup_root()
            if not os.path.isdir(root):
                print("No backups found.")
                return 1
            dirs = sorted([d for d in os.listdir(root) if d.startswith("backup_")], reverse=True)
            if not dirs:
                print("No backups found.")
                return 1
            args.backup_dir = os.path.join(root, dirs[0])
        ok, issues = run_restore_drill(args.backup_dir)
        print(f"Restore drill: {'PASS' if ok else 'FAIL'}")
        for issue in issues:
            print(f"  ISSUE: {issue}")
        return 0 if ok else 1

    elif args.action == "full":
        ok, report = run_full_cycle()
        print(f"Full cycle: {'PASS' if ok else 'FAIL'}")
        print(f"  backup_dir: {report.get('backup_dir', '?')}")
        print(f"  files_copied: {report.get('files_copied', 0)}")
        print(f"  backup_ok: {report.get('backup_ok')}")
        print(f"  restore_ok: {report.get('restore_ok')}")
        for issue in report.get("restore_issues", []):
            print(f"  ISSUE: {issue}")
        return 0 if ok else 1

    elif args.action == "cleanup":
        removed = cleanup_old_backups(keep=args.keep)
        print(f"Cleaned up {removed} old backups (keeping {args.keep})")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
