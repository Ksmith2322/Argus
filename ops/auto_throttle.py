#!/usr/bin/env python3
"""
ops/auto_throttle.py  --  Phase 16 Auto-Throttle

Reduces position size on consecutive losses or anomalous slippage.
Returns a size multiplier (0.0 to 1.0) applied to execution_qty
before order submission.

Throttle rules:
  - consecutive_losses >= threshold  -> reduce by step per loss beyond threshold
  - last_slippage_bps > p99          -> halve size for next trade
  - both conditions                  -> minimum of both multipliers

Persistence: crash-safe JSON file (ops/logs/throttle_state.json)
"""

import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "THROTTLE_ENABLED": True,
    "THROTTLE_LOSS_THRESHOLD": 3,       # start throttling after N consecutive losses
    "THROTTLE_LOSS_STEP": 0.25,         # reduce by 25% per loss beyond threshold
    "THROTTLE_MIN_MULTIPLIER": 0.25,    # never go below 25% of base size
    "THROTTLE_SLIPPAGE_P99_BPS": 50.0,  # slippage anomaly threshold
    "THROTTLE_SLIPPAGE_MULTIPLIER": 0.5,  # halve size on slippage anomaly
    "THROTTLE_RECOVERY_WINS": 2,        # wins needed to recover one step
}


def _get_cfg(cfg: dict, key: str) -> Any:
    """Get throttle config with fallback to defaults."""
    env_key = f"ARGUS_{key}"
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        default = _DEFAULTS.get(key)
        if isinstance(default, bool):
            return env_val.lower() in ("1", "true", "yes")
        if isinstance(default, int):
            return int(env_val)
        if isinstance(default, float):
            return float(env_val)
        return env_val
    return cfg.get(key, _DEFAULTS.get(key))


# ---------------------------------------------------------------------------
# Throttle state
# ---------------------------------------------------------------------------

def _default_state_file() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ops", "logs", "throttle_state.json",
    )


def load_throttle_state(state_file: Optional[str] = None) -> Dict[str, Any]:
    """Load throttle state from file."""
    path = state_file or _default_state_file()
    if not os.path.exists(path):
        return {
            "consecutive_losses": 0,
            "consecutive_wins_since_throttle": 0,
            "last_slippage_bps": 0.0,
            "slippage_anomaly_active": False,
            "current_multiplier": 1.0,
            "throttle_reason": "",
        }
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "consecutive_losses": 0,
            "consecutive_wins_since_throttle": 0,
            "last_slippage_bps": 0.0,
            "slippage_anomaly_active": False,
            "current_multiplier": 1.0,
            "throttle_reason": "",
        }


def save_throttle_state(state: Dict[str, Any], state_file: Optional[str] = None) -> None:
    """Persist throttle state (crash-safe)."""
    path = state_file or _default_state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["ts"] = time.time()
    state["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Core throttle logic
# ---------------------------------------------------------------------------

def compute_multiplier(
    *,
    consecutive_losses: int = 0,
    last_slippage_bps: float = 0.0,
    cfg: Optional[dict] = None,
) -> Tuple[float, str]:
    """Compute size multiplier based on current conditions.

    Returns (multiplier, reason). multiplier is in [min_multiplier, 1.0].
    """
    if cfg is None:
        cfg = {}

    enabled = _get_cfg(cfg, "THROTTLE_ENABLED")
    if not enabled:
        return 1.0, ""

    loss_threshold = int(_get_cfg(cfg, "THROTTLE_LOSS_THRESHOLD"))
    loss_step = float(_get_cfg(cfg, "THROTTLE_LOSS_STEP"))
    min_mult = float(_get_cfg(cfg, "THROTTLE_MIN_MULTIPLIER"))
    slip_p99 = float(_get_cfg(cfg, "THROTTLE_SLIPPAGE_P99_BPS"))
    slip_mult = float(_get_cfg(cfg, "THROTTLE_SLIPPAGE_MULTIPLIER"))

    multiplier = 1.0
    reasons = []

    # Consecutive loss throttle
    if consecutive_losses >= loss_threshold:
        excess = consecutive_losses - loss_threshold
        loss_mult = max(min_mult, 1.0 - (excess + 1) * loss_step)
        if loss_mult < multiplier:
            multiplier = loss_mult
            reasons.append(f"consecutive_losses={consecutive_losses}(threshold={loss_threshold})")

    # Slippage anomaly throttle
    if last_slippage_bps > slip_p99:
        if slip_mult < multiplier:
            multiplier = slip_mult
            reasons.append(f"slippage_anomaly={last_slippage_bps:.1f}bps(p99={slip_p99})")

    multiplier = max(min_mult, min(1.0, multiplier))
    reason = "; ".join(reasons) if reasons else ""
    return multiplier, reason


def record_trade_result(
    *,
    is_win: bool,
    slippage_bps: float = 0.0,
    cfg: Optional[dict] = None,
    state_file: Optional[str] = None,
) -> Tuple[float, str]:
    """Record a trade result and return updated (multiplier, reason).

    Call this after each closed trade to update throttle state.
    """
    if cfg is None:
        cfg = {}

    ts = load_throttle_state(state_file)

    if is_win:
        ts["consecutive_losses"] = 0
        ts["consecutive_wins_since_throttle"] = ts.get("consecutive_wins_since_throttle", 0) + 1

        recovery_wins = int(_get_cfg(cfg, "THROTTLE_RECOVERY_WINS"))
        if ts["consecutive_wins_since_throttle"] >= recovery_wins:
            ts["slippage_anomaly_active"] = False
    else:
        ts["consecutive_losses"] = ts.get("consecutive_losses", 0) + 1
        ts["consecutive_wins_since_throttle"] = 0

    ts["last_slippage_bps"] = slippage_bps

    slip_p99 = float(_get_cfg(cfg, "THROTTLE_SLIPPAGE_P99_BPS"))
    if abs(slippage_bps) > slip_p99:
        ts["slippage_anomaly_active"] = True

    multiplier, reason = compute_multiplier(
        consecutive_losses=ts.get("consecutive_losses", 0),
        last_slippage_bps=slippage_bps if ts.get("slippage_anomaly_active") else 0.0,
        cfg=cfg,
    )

    ts["current_multiplier"] = multiplier
    ts["throttle_reason"] = reason

    save_throttle_state(ts, state_file)
    return multiplier, reason


def apply_throttle_to_qty(
    qty: Decimal,
    *,
    cfg: Optional[dict] = None,
    state_file: Optional[str] = None,
) -> Tuple[Decimal, float, str]:
    """Apply throttle to execution quantity.

    Returns (adjusted_qty, multiplier, reason).
    """
    if cfg is None:
        cfg = {}

    enabled = _get_cfg(cfg, "THROTTLE_ENABLED")
    if not enabled:
        return qty, 1.0, ""

    ts = load_throttle_state(state_file)

    multiplier, reason = compute_multiplier(
        consecutive_losses=ts.get("consecutive_losses", 0),
        last_slippage_bps=ts.get("last_slippage_bps", 0.0) if ts.get("slippage_anomaly_active") else 0.0,
        cfg=cfg,
    )

    if multiplier >= 1.0:
        return qty, 1.0, ""

    adjusted = (qty * Decimal(str(multiplier))).quantize(Decimal("0.00000001"))
    return adjusted, multiplier, reason
