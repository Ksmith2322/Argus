#!/usr/bin/env python3
"""
ops/run_manifest.py  --  Phase 16 Config Freeze & Run Manifests

Every run gets a manifest recording:
  - run_id, start time
  - config hash (SHA-256 of frozen config JSON)
  - code hash (SHA-256 of key source files)
  - initial runtime mode
  - artifact list (populated at end of run)

Config freeze rule: once a run starts, changing config requires a new run.
The manifest is written at run start and updated at run end.
"""

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Config hash
# ---------------------------------------------------------------------------

def _normalize_config_for_hash(cfg: dict) -> str:
    """Produce a deterministic JSON string from config for hashing.

    Excludes volatile keys that change every run (run_id, paths, timestamps).
    """
    exclude_prefixes = (
        "RUNTIME_LOG_DIR", "OPS_LOG_DIR", "LOG_DIR", "STATE_DIR",
        "LIVE_ORDERS_CSV", "LIVE_FILLS_CSV", "LIVE_POSITIONS_CSV",
        "LIVE_ACCOUNT_CSV", "RUNTIME_STATE_PATH",
        "ARGUS_BT_ARTIFACT_DIR",
    )
    filtered = {}
    for k, v in sorted(cfg.items()):
        if k in exclude_prefixes:
            continue
        # Convert Decimal to str for JSON serialization
        try:
            json.dumps(v)
            filtered[k] = v
        except (TypeError, ValueError):
            filtered[k] = str(v)
    return json.dumps(filtered, sort_keys=True, default=str)


def config_hash(cfg: dict) -> str:
    """SHA-256 hex digest of normalized config."""
    norm = _normalize_config_for_hash(cfg)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Code hash
# ---------------------------------------------------------------------------

_KEY_SOURCE_FILES = [
    # Files whose change invalidates a run's reproducibility. Pre-Phase-6
    # legacy files (engine.py, strategy_phase2.py, confluence.py, etc.) were
    # removed in the Greek-family refactor. Current scope reflects active
    # production code: Argus runner, risk mgmt, the central sizing anchor,
    # the fleet monitor, and every live Forge runner. code_hash() is
    # tolerant of missing files (just skips them), so adding new ones here
    # is safe.
    "risk.py",
    "ledger.py",
    "runtime_mode.py",
    "argus_flow/runner_unified.py",
    "argus_flow/ops/promotion_gate_v2.py",
    "argus_flow/ops/fleet_registry.py",
    "argus_flow/configs/fleet_sizing.json",
    "helio/fleet_sizing.py",
    "helio/fleet_monitor.py",
    "helio/fleet_perf_summary.py",
    "forge/gld_pm_long/runner.py",
    "forge/wick_gbpusd/runner.py",
    "forge/nq_overnight/runner.py",
    "forge/jpy_pm_short/runner.py",
    "forge/gdx_gld_runner.py",
    "apollo/runner.py",
    "apollo/ops/backfill_forward_returns.py",
]


def code_hash(repo_root: Optional[str] = None) -> str:
    """SHA-256 hex digest of concatenated key source files."""
    root = repo_root or os.path.dirname(os.path.dirname(__file__))
    h = hashlib.sha256()
    for fname in sorted(_KEY_SOURCE_FILES):
        fpath = os.path.join(root, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "rb") as f:
                    h.update(f.read())
            except Exception:
                pass
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------

def create_manifest(
    *,
    run_id: str,
    cfg: dict,
    repo_root: Optional[str] = None,
    runtime_mode_str: str = "FULL",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a run manifest dict (call at run start)."""
    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "start_ts": time.time(),
        "start_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_hash": config_hash(cfg),
        "code_hash": code_hash(repo_root),
        "initial_runtime_mode": runtime_mode_str,
        "end_ts": None,
        "end_iso": None,
        "artifacts": [],
        "status": "RUNNING",
    }
    if extra:
        manifest["extra"] = extra
    return manifest


def finalize_manifest(
    manifest: Dict[str, Any],
    *,
    artifacts: Optional[List[str]] = None,
    status: str = "COMPLETE",
) -> Dict[str, Any]:
    """Update manifest at run end."""
    manifest["end_ts"] = time.time()
    manifest["end_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if artifacts:
        manifest["artifacts"] = artifacts
    manifest["status"] = status
    return manifest


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _manifest_path(log_dir: str, run_id: str) -> str:
    return os.path.join(log_dir, f"run_manifest_{run_id}.json")


def save_manifest(manifest: Dict[str, Any], log_dir: str) -> str:
    """Write manifest to log_dir. Returns file path."""
    run_id = manifest.get("run_id", "unknown")
    path = _manifest_path(log_dir, run_id)
    os.makedirs(log_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
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
            json.dump(manifest, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def load_manifest(log_dir: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Load a manifest by run_id. Returns None if not found."""
    path = _manifest_path(log_dir, run_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Config drift detection
# ---------------------------------------------------------------------------

def check_config_drift(manifest: Dict[str, Any], cfg: dict) -> bool:
    """Return True if config has changed since manifest was created.

    This is the config-freeze enforcement: if drift detected, the run
    should be halted and a new run started.
    """
    return config_hash(cfg) != manifest.get("config_hash", "")
