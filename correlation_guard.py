#!/usr/bin/env python3
"""correlation_guard.py — Cross-coin correlation guard.

Reads sibling runtime state JSON files to count how many coins currently
have open positions.  Blocks new entries when too many correlated assets
are already in play.

Usage (from engine.py entry path):
    from correlation_guard import can_enter_cross_coin
    allowed, reason = can_enter_cross_coin(current_symbol="ETH-USD", cfg=cfg)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _state_dir(cfg: Dict) -> Path:
    return Path(str(cfg.get("STATE_DIR", "./state")))


def _symbols_from_cfg(cfg: Dict) -> List[str]:
    raw = str(cfg.get("CROSS_COIN_SYMBOLS", "ETH-USD,BTC-USD,SOL-USD"))
    return [s.strip() for s in raw.split(",") if s.strip()]


def count_open_positions(
    current_symbol: str,
    cfg: Dict,
) -> Tuple[int, List[str]]:
    """Return (count_of_other_open, list_of_open_symbols) excluding current_symbol."""
    state_dir = _state_dir(cfg)
    symbols = _symbols_from_cfg(cfg)
    current_norm = current_symbol.replace("-", "_").upper()

    open_symbols: List[str] = []

    for sym in symbols:
        safe = sym.replace("-", "_").replace("/", "_").upper()
        if safe == current_norm:
            continue

        state_path = state_dir / f"runtime_state_{safe}.json"
        if not state_path.exists():
            continue

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            bot_state = str(data.get("bot_state", "")).upper()
            pos_qty = str(data.get("position_qty", "0")).strip()
            if bot_state in ("OPEN", "EXITING") or (pos_qty and pos_qty != "0" and pos_qty != "0.00000000"):
                open_symbols.append(sym)
        except Exception:
            pass

    return len(open_symbols), open_symbols


def can_enter_cross_coin(
    current_symbol: str,
    cfg: Dict,
) -> Tuple[bool, str]:
    """Check if a new entry is allowed given cross-coin exposure.

    Returns (allowed, reason).

    NOTE: guard is disabled in backtest mode (ARGUS_MODE=bt) because each
    backtest runs in isolation and the live state files reflect the live
    runner's position, not the backtest's in-memory state.
    """
    if not _as_bool(cfg.get("USE_CROSS_COIN_GUARD", False)):
        return True, ""

    # Disable in backtest — live state files are irrelevant to BT isolation
    mode = os.environ.get("ARGUS_MODE", "").strip().lower()
    if mode in ("bt", "backtest"):
        return True, ""

    max_open = int(cfg.get("CROSS_COIN_MAX_OPEN", 2))
    if max_open <= 0:
        return True, ""

    n_open, open_syms = count_open_positions(current_symbol, cfg)

    if n_open >= max_open:
        return False, f"CROSS_COIN_LIMIT({n_open}>={max_open})|open={','.join(open_syms)}"

    return True, ""