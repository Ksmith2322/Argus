"""Passive coin rotation helpers used by the dashboard.

This module is intentionally read-only. It ranks coins from the persisted
coin pool registry and exposes lightweight status for operator review.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
POOL_FILE = REPO / "ops" / "coin_pool.json"

DEFAULT_POOL = {
    "max_active": 2,
    "active": ["ETH", "BTC"],
    "coins": {
        "ETH": {"status": "active", "config": {}, "metrics": {}},
        "BTC": {"status": "active", "config": {}, "metrics": {}},
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_coin_pool() -> dict:
    """Load the persisted coin pool registry with safe defaults."""
    try:
        if POOL_FILE.exists():
            data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return dict(DEFAULT_POOL)


def _score_coin(coin: str, payload: dict, active: list[str]) -> dict:
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    status = str(payload.get("status", "unknown")) if isinstance(payload, dict) else "unknown"

    pf = _safe_float(metrics.get("rolling_pf", metrics.get("profit_factor")))
    wr = _safe_float(metrics.get("rolling_wr", metrics.get("win_rate")))
    expectancy = _safe_float(metrics.get("expectancy_usd", metrics.get("expectancy")))
    trades = int(_safe_float(metrics.get("trades", metrics.get("trade_count")), 0))
    degraded = bool(metrics.get("degraded", False))

    score = pf * 100.0 + wr * 25.0 + expectancy * 5.0 + min(trades, 50)
    if degraded:
        score -= 100.0
    if status.lower() not in {"active", "candidate", "ready"}:
        score -= 25.0

    return {
        "coin": coin,
        "status": status,
        "active": coin in active,
        "score": round(score, 4),
        "profit_factor": round(pf, 4),
        "win_rate": round(wr, 4),
        "expectancy": round(expectancy, 4),
        "trades": trades,
        "degraded": degraded,
    }


def check_rotation_candidates(pool: dict | None = None, max_candidates: int = 5) -> list[dict]:
    """Return ranked coin candidates for operator review.

    This function never mutates the pool and does not activate or deactivate
    coins automatically.
    """
    pool = pool or read_coin_pool()
    active = list(pool.get("active", []))
    coins = pool.get("coins", {})
    if not isinstance(coins, dict):
        return []

    ranked = [_score_coin(coin, payload, active) for coin, payload in coins.items()]
    ranked.sort(key=lambda row: (row["degraded"], -row["score"], row["coin"]))
    return ranked[:max_candidates]


def get_rotation_status() -> dict:
    """Return the current pool plus ranked candidates."""
    pool = read_coin_pool()
    return {
        "pool": pool,
        "active": list(pool.get("active", [])),
        "candidates": check_rotation_candidates(pool),
    }
