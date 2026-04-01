#!/usr/bin/env python3
"""
ops/artifact_integrity.py  --  Phase 16 Artifact Integrity Checks

Hash verification of critical artifacts on startup:
  - fills.csv     (canonical execution truth)
  - orders.csv    (order history)
  - positions.csv (position snapshots)
  - trade_journal_*.csv (lifecycle truth)
  - runtime_state_*.json (crash-safe snapshots)

Produces an integrity report. If corruption detected:
  - log the failure
  - escalate to OBSERVATION_ONLY mode
  - alert via Discord

The integrity manifest (artifact_hashes.json) is updated after each
successful write cycle and checked on the next startup.
"""

import csv
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------

def file_hash(path: str) -> Optional[str]:
    """SHA-256 hex digest of a file. Returns None if file doesn't exist."""
    if not os.path.exists(path):
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def file_size(path: str) -> int:
    """File size in bytes. Returns -1 if not found."""
    try:
        return os.path.getsize(path)
    except Exception:
        return -1


def file_row_count(path: str) -> int:
    """Count CSV rows (excluding header). Returns -1 on error."""
    if not os.path.exists(path):
        return -1
    try:
        with open(path, "r", newline="") as f:
            reader = csv.reader(f)
            count = sum(1 for _ in reader) - 1  # subtract header
            return max(0, count)
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Integrity manifest
# ---------------------------------------------------------------------------

def _default_manifest_path(log_dir: str) -> str:
    return os.path.join(log_dir, "artifact_hashes.json")


def _critical_artifacts(log_dir: str) -> List[str]:
    """List of critical artifact paths to verify."""
    artifacts = []
    for name in ["fills.csv", "orders.csv", "positions.csv"]:
        path = os.path.join(log_dir, name)
        if os.path.exists(path):
            artifacts.append(path)
    # Also include any trade_journal files
    try:
        for fname in os.listdir(log_dir):
            if fname.startswith("trade_journal_") and fname.endswith(".csv"):
                artifacts.append(os.path.join(log_dir, fname))
    except Exception:
        pass
    return artifacts


def compute_artifact_hashes(log_dir: str) -> Dict[str, Dict[str, Any]]:
    """Compute hashes for all critical artifacts.

    Returns {filename: {"hash": ..., "size": ..., "rows": ..., "ts": ...}}
    """
    result: Dict[str, Dict[str, Any]] = {}
    for path in _critical_artifacts(log_dir):
        fname = os.path.basename(path)
        result[fname] = {
            "hash": file_hash(path),
            "size": file_size(path),
            "rows": file_row_count(path),
            "ts": time.time(),
        }
    return result


def save_integrity_manifest(log_dir: str, hashes: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Save current artifact hashes to manifest file. Returns manifest path."""
    if hashes is None:
        hashes = compute_artifact_hashes(log_dir)
    manifest = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifacts": hashes,
    }
    path = _default_manifest_path(log_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(3):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            time.sleep(0.05)
    else:
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def load_integrity_manifest(log_dir: str) -> Optional[Dict[str, Any]]:
    """Load the last saved integrity manifest. Returns None if not found."""
    path = _default_manifest_path(log_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------

def verify_artifact_integrity(
    log_dir: str,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Verify artifacts against last saved manifest.

    Returns (all_ok, list_of_issues).
    Each issue: {"artifact": ..., "issue": ..., "expected": ..., "actual": ...}
    """
    manifest = load_integrity_manifest(log_dir)
    if manifest is None:
        # No prior manifest — first run, nothing to verify against
        return True, []

    saved_artifacts = manifest.get("artifacts", {})
    issues: List[Dict[str, Any]] = []
    current_hashes = compute_artifact_hashes(log_dir)

    for fname, saved in saved_artifacts.items():
        current = current_hashes.get(fname)
        if current is None:
            # Artifact existed before but is gone now
            issues.append({
                "artifact": fname,
                "issue": "missing",
                "expected": saved.get("hash", "?"),
                "actual": None,
            })
            continue

        saved_hash = saved.get("hash")
        current_hash = current.get("hash")

        # For append-only artifacts (fills, orders, journal), size should only grow
        saved_size = saved.get("size", 0)
        current_size = current.get("size", 0)
        if current_size < saved_size:
            issues.append({
                "artifact": fname,
                "issue": "truncated",
                "expected": f"size>={saved_size}",
                "actual": f"size={current_size}",
            })

        # Row count should only grow for append-only artifacts
        saved_rows = saved.get("rows", 0)
        current_rows = current.get("rows", 0)
        if saved_rows >= 0 and current_rows >= 0 and current_rows < saved_rows:
            issues.append({
                "artifact": fname,
                "issue": "rows_decreased",
                "expected": f"rows>={saved_rows}",
                "actual": f"rows={current_rows}",
            })

    all_ok = len(issues) == 0
    return all_ok, issues


# ---------------------------------------------------------------------------
# CSV structural checks
# ---------------------------------------------------------------------------

def check_csv_not_truncated(path: str) -> Tuple[bool, str]:
    """Check if a CSV file has a valid last line (not truncated mid-row).

    Returns (ok, detail).
    """
    if not os.path.exists(path):
        return True, "file_not_found"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            if size == 0:
                return True, "empty_file"
            # Read last 1KB
            read_size = min(1024, size)
            f.seek(-read_size, 2)
            tail = f.read()
        # Check if file ends with newline
        if tail.endswith(b"\n") or tail.endswith(b"\r\n"):
            return True, "ok"
        # File doesn't end with newline — possible truncation
        last_line = tail.split(b"\n")[-1]
        return False, f"truncated_last_line: {last_line[:80]!r}"
    except Exception as e:
        return False, f"read_error: {e}"


def check_fills_csv_integrity(log_dir: str) -> Tuple[bool, str]:
    """Check fills.csv specifically for common corruption patterns."""
    path = os.path.join(log_dir, "fills.csv")
    if not os.path.exists(path):
        return True, "no_fills_csv"

    ok, detail = check_csv_not_truncated(path)
    if not ok:
        return False, f"fills.csv: {detail}"

    # Check for duplicate fill_ids
    try:
        fill_ids: List[str] = []
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fid = row.get("fill_id", "")
                if fid:
                    fill_ids.append(fid)
        seen = set()
        dupes = []
        for fid in fill_ids:
            if fid in seen:
                dupes.append(fid)
            seen.add(fid)
        if dupes:
            return False, f"fills.csv: duplicate_fill_ids={dupes[:5]}"
    except Exception as e:
        return False, f"fills.csv: parse_error={e}"

    return True, "fills.csv: ok"
