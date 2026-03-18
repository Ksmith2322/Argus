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
from typing import Any, Dict, List, Optional, Tuple


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
ETH_TREND_FILE = "eth_trend.json"
STALE_THRESHOLD_S = 300  # ignore BTC trend data older than 5 minutes
_LAG_STALE_THRESHOLD_S = 30  # shorter stale window for lag signal

# Rolling price buffers: list of (epoch_float, price_float), max 10 entries
_btc_price_buf: List[Tuple[float, float]] = []
_eth_price_buf: List[Tuple[float, float]] = []


def write_btc_trend_state(cfg: Dict, regime: str, trend_strength: float, vol: float, px: float = 0.0) -> None:
    """Called by BTC runner each tick to publish trend state for other coins."""
    global _btc_price_buf

    state_dir = _state_dir(cfg)
    path = state_dir / BTC_TREND_FILE

    now = time.time()

    # Update rolling price buffer
    if px and float(px) > 0:
        _btc_price_buf.append((now, float(px)))
        _btc_price_buf = _btc_price_buf[-10:]

    # Compute 60s price delta pct
    btc_px_delta_60s_pct: Optional[float] = None
    if _btc_price_buf and float(px) > 0:
        cutoff = now - 60.0
        old_entries = [(t, p) for (t, p) in _btc_price_buf if t <= cutoff]
        if old_entries:
            oldest_px = old_entries[0][1]
            if oldest_px > 0:
                btc_px_delta_60s_pct = (float(px) - oldest_px) / oldest_px * 100.0

    payload: Dict[str, Any] = {
        "ts": int(now),
        "regime": str(regime),
        "trend_strength": float(trend_strength),
        "vol": float(vol),
    }
    if btc_px_delta_60s_pct is not None:
        payload["btc_px_delta_60s_pct"] = btc_px_delta_60s_pct

    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp.replace(path)
    except Exception:
        pass


def read_btc_lag_delta() -> Tuple[Optional[float], float]:
    """Read BTC 60s price delta from shared state file.

    Returns (btc_px_delta_60s_pct, age_seconds).
    Returns (None, 999) if file missing, stale (>30s), or delta not available.
    """
    # Resolve state dir from cwd convention (same as _state_dir with no cfg)
    path = Path("./state") / BTC_TREND_FILE

    if not path.exists():
        return None, 999.0

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, 999.0

    ts = int(data.get("ts", 0))
    age = time.time() - ts

    if age > _LAG_STALE_THRESHOLD_S:
        return None, float(age)

    delta = data.get("btc_px_delta_60s_pct")
    if delta is None:
        return None, float(age)

    return float(delta), float(age)


def write_eth_trend_state(cfg: Dict, regime: str, trend_strength: float, vol: float, px: float = 0.0) -> None:
    """Called by ETH runner each tick to publish trend state for BTC to read."""
    global _eth_price_buf

    state_dir = _state_dir(cfg)
    path = state_dir / ETH_TREND_FILE

    now = time.time()

    # Update rolling price buffer
    if px and float(px) > 0:
        _eth_price_buf.append((now, float(px)))
        _eth_price_buf = _eth_price_buf[-10:]

    # Compute 60s price delta pct
    eth_px_delta_60s_pct: Optional[float] = None
    if _eth_price_buf and float(px) > 0:
        cutoff = now - 60.0
        old_entries = [(t, p) for (t, p) in _eth_price_buf if t <= cutoff]
        if old_entries:
            oldest_px = old_entries[0][1]
            if oldest_px > 0:
                eth_px_delta_60s_pct = (float(px) - oldest_px) / oldest_px * 100.0

    payload: Dict[str, Any] = {
        "ts": int(now),
        "regime": str(regime),
        "trend_strength": float(trend_strength),
        "vol": float(vol),
    }
    if eth_px_delta_60s_pct is not None:
        payload["eth_px_delta_60s_pct"] = eth_px_delta_60s_pct

    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp.replace(path)
    except Exception:
        pass


def read_eth_lag_delta() -> Tuple[Optional[float], float]:
    """Read ETH 60s price delta from shared state file.

    Returns (eth_px_delta_60s_pct, age_seconds).
    Returns (None, 999) if file missing, stale (>30s), or delta not available.
    """
    path = Path("./state") / ETH_TREND_FILE

    if not path.exists():
        return None, 999.0

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, 999.0

    ts = int(data.get("ts", 0))
    age = time.time() - ts

    if age > _LAG_STALE_THRESHOLD_S:
        return None, float(age)

    delta = data.get("eth_px_delta_60s_pct")
    if delta is None:
        return None, float(age)

    return float(delta), float(age)


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