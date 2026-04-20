"""Shared helpers every strategy runner duplicates.

Each forge strategy (gld_pm_long, wick_gbpusd, nq_overnight, jpy_pm_short,
gdx_gld_runner) and each Argus pair (via runner_unified) reimplements the
same small set of helpers:

  - `_config_hash(params)` — SHA-256 slice of sorted-json params
  - `_git_sha()` — current repo HEAD short hash, or "unknown"
  - `atomic_save_json(path, data)` — write-to-tmp, then os.replace
  - `load_json(path, default=None)` — json.loads with missing/corrupt fallback
  - `ensure_parent_dir(path)` — mkdir parents=True, exist_ok=True

This module consolidates them with no caller changes required — each
runner keeps its local helper for now. Phase 2 runner migrations can
progressively delete locals in favor of these imports.

None of these touch state, broker connections, or any global side-effect
beyond the file they operate on. Safe to import from anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def config_hash(params: dict) -> str:
    """SHA-256 of `json.dumps(params, sort_keys=True)`, first 16 hex chars.

    Every strategy uses this to stamp trade/signal rows with the exact
    config they ran under. A bug here means trades from different PARAMS
    versions could collide in analysis; a rename without aligning the
    implementations would silently split historical data.

    Deterministic: same input dict → same hash, regardless of dict
    insertion order.
    """
    blob = json.dumps(params, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def git_sha(repo_root: Path | None = None) -> str:
    """Current git HEAD short hash, or 'unknown' on any failure.

    Never raises. Strategies stamp every trade with this so the
    post-mortem can locate the exact code that was running.
    """
    try:
        cwd = str(repo_root) if repo_root else None
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return "unknown"


def atomic_save_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write `data` as JSON to `path` atomically.

    Write-to-tmp-and-replace semantics: an interrupted write can never
    leave `path` partially updated. Readers either see the old content or
    the new content, never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    """Read JSON from `path`, returning `default` on missing file or
    parse error. Never raises. Matches the tolerance pattern used
    throughout the codebase for optional config / state files."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def ensure_parent_dir(path: Path) -> Path:
    """mkdir parents=True, exist_ok=True on `path.parent`. Returns
    the path for chaining. A tiny helper, but every runner re-invents it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
