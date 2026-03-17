#!/usr/bin/env python3
"""btc_momentum_guard.py — Gate alt-coin entries based on BTC trend.

When BTC is in TREND_DOWN, alt coins tend to follow. This module reads
BTC's regime state from a shared file and blocks entries on other coins
when BTC momentum is bearish.

Usage (from engine.py entry path):
    from btc_momentum_guard import check_btc_momentum
    allowed, reason = check_btc_momentum(current_symbol="ETH-USD", cfg=cfg)

BTC runner writes its trend state via write_btc_trend_state() each tick.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _state_dir(cfg: Dict) -> Path:
    return Path(str(cfg.get("STATE_DIR", "./state")))


BTC_TREND_FILE = "btc_trend.json"
STALE_THRESHOLD_S = 300  # ignore BTC trend data older than 5 minutes


def write_btc_trend_state(cfg: Dict, regime: str, trend_strength: float, vol: float) -> None:
    """Called by BTC runner each tick to publish trend state for other coins."""
    state_dir = _state_dir(cfg)
    path = state_dir / BTC_TREND_FILE

    payload = {
        "ts": int(time.time()),
        "regime": str(regime),
        "trend_strength": float(trend_strength),
        "vol": float(vol),
    }

    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp.replace(path)
    except Exception:
        pass


def check_btc_momentum(
    current_symbol: str,
    cfg: Dict,
) -> Tuple[bool, str]:
    """Check if BTC trend allows entry on this coin.

    Returns (allowed, reason).
    Only gates non-BTC coins.  BTC itself is never blocked by its own trend.
    """
    if not _as_bool(cfg.get("USE_BTC_MOMENTUM_GATE", False)):
        return True, ""

    # Never gate BTC itself
    sym_norm = current_symbol.replace("-", "_").upper()
    if "BTC" in sym_norm:
        return True, ""

    state_dir = _state_dir(cfg)
    path = state_dir / BTC_TREND_FILE

    if not path.exists():
        return True, ""  # no data yet — allow

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return True, ""  # read error — allow

    # Check staleness
    ts = int(data.get("ts", 0))
    age = int(time.time()) - ts
    if age > STALE_THRESHOLD_S:
        return True, ""  # stale data — allow

    regime = str(data.get("regime", "")).upper()
    trend_strength = float(data.get("trend_strength", 0))

    # Block list: which BTC regimes block alt entries
    block_regimes_raw = str(cfg.get("BTC_MOMENTUM_BLOCK_REGIMES", "TREND_DOWN"))
    block_regimes = {r.strip().upper() for r in block_regimes_raw.split(",") if r.strip()}

    if regime in block_regimes:
        return False, f"BTC_TREND_BLOCK(regime={regime},strength={trend_strength:.5f},age={age}s)"

    return True, ""