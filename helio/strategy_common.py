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


def safe_position_size(
    risk_usd: float,
    entry_px: float,
    stop_px: float,
    *,
    atr: float | None = None,
    sizing_floor_atr_mult: float = 1.0,
    abs_floor_per_unit: float = 0.01,
    max_notional_usd: float | None = None,
    max_size: int | None = None,
    point_value_usd: float = 1.0,
) -> tuple[int, str]:
    """Compute a position size that is bounded by a stop-distance floor.

    Background (2026-05-07 audit): the naive formula
        shares = int(risk_usd / abs(entry_px - stop_px))
    blows up when the stop is unusually tight relative to ATR. On 5/5
    multi_orb produced sizes 681-1419 QQQ shares ($500K-$965K notional
    on a $33K account) because stop_distance was $0.21 on a $680 stock.
    Same bug in nq_london_close produced 417 NQ contracts ($11.7M
    notional). All paper-side phantoms; broker rejected them. But the
    underlying math is broken.

    Fix: cap sizing at a per-unit risk no smaller than `sizing_floor_atr_mult * atr`
    (default 1.0× — at least a "normal" 1-ATR move worth of risk per unit).
    The actual execution stop can still be tighter (good R:R), but the SIZING
    math doesn't get to ride a temporarily-compressed stop into 1400 shares.

    Args:
        risk_usd: dollar risk budget for this trade
        entry_px: planned entry price
        stop_px: planned stop price (the actual execution stop)
        atr: ATR value at entry; if None, sizing_floor_atr_mult is ignored
        sizing_floor_atr_mult: minimum atr-multiple for sizing (NOT the
            execution stop). Default 1.0 — never size as if stop were
            less than 1 ATR away. Use 0.5 if your strategy genuinely
            needs tighter sizing.
        abs_floor_per_unit: absolute floor per unit (e.g., $0.01 stocks,
            0.0001 forex pip). Catches weird zero-ATR cases.
        max_notional_usd: optional notional cap; size clamped if breached
        max_size: optional hard size cap
        point_value_usd: dollar value per point per unit (1.0 stocks/ETF,
            2.0 MNQ, 5.0 MES, etc.)

    Returns (size, sizing_policy_label). Always returns size >= 0.
    """
    actual_dist = abs(float(entry_px) - float(stop_px))
    floor_dist = 0.0
    if atr is not None and atr > 0 and sizing_floor_atr_mult > 0:
        floor_dist = float(atr) * float(sizing_floor_atr_mult)
    sizing_dist = max(actual_dist, floor_dist, abs_floor_per_unit)

    per_unit_risk = sizing_dist * point_value_usd
    if per_unit_risk <= 0:
        return 0, "zero_per_unit_risk"

    raw = float(risk_usd) / per_unit_risk
    size = max(0, int(raw))

    if floor_dist > actual_dist:
        policy = "atr_sizing_floor"
    elif sizing_dist == abs_floor_per_unit:
        policy = "abs_floor"
    else:
        policy = "actual_stop"

    if max_notional_usd is not None and max_notional_usd > 0 and entry_px > 0:
        # Notional per unit = entry_px * point_value_usd (for futures, point
        # value matters; for stocks point_value_usd=1.0 collapses to entry_px).
        notional_per_unit = float(entry_px) * float(point_value_usd)
        if notional_per_unit > 0:
            cap_size = int(max_notional_usd / notional_per_unit)
            if size > cap_size:
                size = cap_size
                policy = "notional_cap"

    if max_size is not None and max_size > 0 and size > max_size:
        size = max_size
        policy = "max_size_cap"

    return max(0, size), policy
