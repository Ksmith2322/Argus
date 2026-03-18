"""
ops/coin_rotation.py — Coin performance tracking and rotation management.

Reads per-coin trade journals, computes rolling metrics, updates coin_pool.json.
Called hourly from runner_live.py and can be run standalone for inspection.

Usage:
    python ops/coin_rotation.py
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
COIN_POOL_PATH = REPO_ROOT / "ops" / "coin_pool.json"
LOGS_ROOT = REPO_ROOT / "ops" / "logs"

# Rotation thresholds
ROTATION_MIN_TRADES = 10       # minimum trades before evaluating
ROTATION_MIN_PF = 0.80         # flag if rolling PF falls below this
ROTATION_MIN_WR = 0.20         # flag if rolling WR falls below this
ROTATION_ROLLING_N = 20        # trades to include in rolling window


# ---------------------------------------------------------------------------
# Trade journal reading
# ---------------------------------------------------------------------------

def _find_trade_journal(coin: str) -> Optional[Path]:
    """Find the most recently modified trade_journal CSV for a coin."""
    log_dir = LOGS_ROOT / coin.lower()
    if not log_dir.exists():
        return None
    journals = list(log_dir.glob("trade_journal*.csv"))
    if not journals:
        return None
    return max(journals, key=lambda p: p.stat().st_mtime)


def compute_coin_metrics(coin: str, n_trades: int = ROTATION_ROLLING_N) -> dict:
    """
    Read the latest trade journal for a coin and compute rolling metrics.

    Returns dict with: n_trades, rolling_pf, rolling_wr, total_pnl, updated (ISO).
    rolling_pf and rolling_wr are None if fewer than 3 trades available.
    """
    journal_path = _find_trade_journal(coin)
    now_iso = datetime.now(timezone.utc).isoformat()

    empty = {
        "n_trades": 0,
        "rolling_pf": None,
        "rolling_wr": None,
        "total_pnl": 0.0,
        "updated": now_iso,
    }

    if journal_path is None or not journal_path.exists():
        return empty

    try:
        rows = []
        with open(journal_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pnl_str = row.get("realized_pnl") or row.get("pnl") or ""
                if pnl_str.strip():
                    try:
                        rows.append(float(pnl_str))
                    except ValueError:
                        pass
    except Exception:
        return empty

    if len(rows) < 3:
        return {**empty, "n_trades": len(rows)}

    # Use last n_trades rows for rolling window
    window = rows[-n_trades:]
    wins = [p for p in window if p > 0]
    losses = [p for p in window if p <= 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    rolling_pf = (gross_profit / gross_loss) if gross_loss > 0 else None
    rolling_wr = len(wins) / len(window) if window else None
    total_pnl = sum(rows)

    return {
        "n_trades": len(rows),
        "rolling_pf": round(rolling_pf, 3) if rolling_pf is not None else None,
        "rolling_wr": round(rolling_wr, 3) if rolling_wr is not None else None,
        "total_pnl": round(total_pnl, 4),
        "updated": now_iso,
    }


# ---------------------------------------------------------------------------
# Pool update
# ---------------------------------------------------------------------------

def update_coin_pool_metrics(repo_root: Optional[str] = None) -> dict:
    """
    Compute live metrics for all ACTIVE coins and write them to coin_pool.json.
    Returns the updated pool dict.
    """
    pool_path = Path(repo_root) / "ops" / "coin_pool.json" if repo_root else COIN_POOL_PATH

    try:
        with open(pool_path, encoding="utf-8") as f:
            pool = json.load(f)
    except Exception:
        return {}

    coins = pool.get("coins", {})
    for coin, info in coins.items():
        if info.get("status") != "ACTIVE":
            continue
        metrics = compute_coin_metrics(coin)
        coins[coin]["live_metrics"] = metrics

    pool["last_checked"] = datetime.now(timezone.utc).isoformat()

    # Atomic write
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", dir=pool_path.parent, prefix="coin_pool_"
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(pool, f, indent=2)
        os.replace(tmp_path, pool_path)
    except Exception:
        pass

    return pool


# ---------------------------------------------------------------------------
# Rotation candidate detection
# ---------------------------------------------------------------------------

def check_rotation_candidates(pool: dict) -> List[dict]:
    """
    Return list of ACTIVE coins that are underperforming and should be rotated.
    Each entry: {"coin": str, "reason": str, "metrics": dict}
    """
    candidates = []
    for coin, info in pool.get("coins", {}).items():
        if info.get("status") != "ACTIVE":
            continue
        m = info.get("live_metrics", {})
        n = m.get("n_trades", 0)
        if n < ROTATION_MIN_TRADES:
            continue
        pf = m.get("rolling_pf")
        wr = m.get("rolling_wr")
        reasons = []
        if pf is not None and pf < ROTATION_MIN_PF:
            reasons.append(f"PF={pf:.2f} < {ROTATION_MIN_PF}")
        if wr is not None and wr < ROTATION_MIN_WR:
            reasons.append(f"WR={wr:.1%} < {ROTATION_MIN_WR:.0%}")
        if reasons:
            candidates.append({
                "coin": coin,
                "reason": ", ".join(reasons),
                "metrics": m,
            })
    return candidates


# ---------------------------------------------------------------------------
# Combined status
# ---------------------------------------------------------------------------

def get_rotation_status(repo_root: Optional[str] = None) -> dict:
    """Update metrics and return full rotation status."""
    pool = update_coin_pool_metrics(repo_root)
    candidates = check_rotation_candidates(pool)
    return {
        "pool": pool,
        "candidates": candidates,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Argus Coin Rotation Status ===\n")
    status = get_rotation_status()
    pool = status["pool"]

    for coin, info in pool.get("coins", {}).items():
        if info.get("status") != "ACTIVE":
            continue
        m = info.get("live_metrics", {})
        pf = m.get("rolling_pf")
        wr = m.get("rolling_wr")
        n = m.get("n_trades", 0)
        pnl = m.get("total_pnl", 0.0)
        pf_str = f"{pf:.3f}" if pf is not None else "N/A"
        wr_str = f"{wr:.1%}" if wr is not None else "N/A"
        print(f"  {coin:4s} | trades={n:4d} | rolling_pf={pf_str} | rolling_wr={wr_str} | total_pnl=${pnl:.2f}")

    candidates = status["candidates"]
    if candidates:
        print(f"\n  *** ROTATION CANDIDATES ({len(candidates)}) ***")
        for c in candidates:
            print(f"    {c['coin']}: {c['reason']}")
    else:
        print("\n  No rotation candidates — all active coins within thresholds.")

    print(f"\n  Updated: {status['last_updated']}")