#!/usr/bin/env python3
"""
ops/rotate_coin.py — Coin rotation monitor for Argus.

Reads live trade journals for each active coin, computes rolling performance,
and recommends rotation in/out. Updates coin_pool.json with latest metrics.

Usage:
    python ops/rotate_coin.py             # report only, no changes
    python ops/rotate_coin.py --apply     # write recommendations to coin_pool.json
    python ops/rotate_coin.py --window 30 # use last 30 trades (default 20)

Exit codes:
    0 = no rotation needed
    1 = rotation recommended (check output)

Rotation rules:
    OUT: active coin rolling PF < 1.0  OR  WR < 25%  over last N live trades
    IN:  pool coin with status CANDIDATE, governor_trained=True, backtest_pf >= 1.3
    Max active coins enforced from coin_pool.json["max_active"] (default 2)

To promote a SCREENED_FAIL coin to CANDIDATE:
    1. Download 90-day candle data for that coin
    2. Run backtest with governor: verify PF >= 1.3
    3. Train governor model (ml/train_governor.py)
    4. Manually set status="CANDIDATE" in coin_pool.json
    The rotation script will then auto-promote it when a slot opens.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POOL_FILE = REPO / "ops" / "coin_pool.json"
OPS_LOGS = REPO / "ops" / "logs"
STATE_DIR = REPO / "state"

# Rotation thresholds
ROTATION_OUT_MIN_PF = 1.0       # rotate out if rolling PF drops below this
ROTATION_OUT_MIN_WR = 0.25      # rotate out if rolling WR drops below this
ROTATION_IN_MIN_BT_PF = 1.3    # minimum backtest PF to promote from pool
ROTATION_MIN_TRADES = 10        # minimum live trades before triggering rotation-out


def load_pool() -> dict:
    if not POOL_FILE.exists():
        print(f"[ERROR] coin_pool.json not found at {POOL_FILE}", file=sys.stderr)
        sys.exit(2)
    return json.loads(POOL_FILE.read_text())


def save_pool(pool: dict) -> None:
    POOL_FILE.write_text(json.dumps(pool, indent=2))


def get_trade_journal_path(coin: str) -> Path:
    """Find trade journal for coin — per-coin log dir first, then root."""
    coin_dir = OPS_LOGS / coin.lower()
    per_coin = coin_dir / "trade_journal.csv"
    if per_coin.exists():
        return per_coin
    # Fallback: root logs dir (legacy location for ETH)
    root = OPS_LOGS / "trade_journal.csv"
    if root.exists():
        return root
    return per_coin  # return per-coin path even if missing (will be detected)


def read_live_trades(coin: str, window: int) -> list[dict]:
    """Read last `window` closed trades from trade_journal.csv for a coin."""
    path = get_trade_journal_path(coin)
    if not path.exists():
        return []
    trades = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter to this coin's trades
                sym = row.get("symbol", row.get("coin", "")).upper()
                if coin.upper() in sym:
                    pnl = row.get("realized_usd", row.get("pnl", ""))
                    try:
                        trades.append({"pnl": float(pnl), "row": row})
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print(f"[WARN] Could not read journal for {coin}: {e}", file=sys.stderr)
    return trades[-window:]  # last N trades


def compute_metrics(trades: list[dict]) -> dict:
    """Compute rolling PF, WR, avg_win, avg_loss from trades list."""
    if not trades:
        return {"n": 0, "pf": None, "wr": None, "avg_win": None, "avg_loss": None, "total_pnl": 0.0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses)) if losses else 0
    pf = round(gross_profit / gross_loss, 3) if gross_loss > 0 else float("inf")
    wr = round(len(wins) / len(trades), 3)
    return {
        "n": len(trades),
        "pf": pf,
        "wr": wr,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
        "total_pnl": round(sum(t["pnl"] for t in trades), 4),
    }


def check_rotation_out(coin: str, metrics: dict) -> tuple[bool, str]:
    """Return (should_rotate_out, reason)."""
    n = metrics["n"]
    if n < ROTATION_MIN_TRADES:
        return False, f"insufficient live trades ({n} < {ROTATION_MIN_TRADES} required)"
    pf = metrics["pf"]
    wr = metrics["wr"]
    reasons = []
    if pf is not None and pf != float("inf") and pf < ROTATION_OUT_MIN_PF:
        reasons.append(f"rolling PF={pf:.3f} < {ROTATION_OUT_MIN_PF}")
    if wr is not None and wr < ROTATION_OUT_MIN_WR:
        reasons.append(f"rolling WR={wr:.1%} < {ROTATION_OUT_MIN_WR:.0%}")
    if reasons:
        return True, "; ".join(reasons)
    return False, "performing within thresholds"


def find_rotation_candidate(pool: dict, active: list[str]) -> tuple[str | None, str]:
    """Find best CANDIDATE coin to rotate in (not already active)."""
    coins = pool.get("coins", {})
    candidates = [
        (coin, info) for coin, info in coins.items()
        if info.get("status") == "CANDIDATE"
        and info.get("governor_trained", False)
        and info.get("backtest_pf", 0) >= ROTATION_IN_MIN_BT_PF
        and coin not in active
    ]
    if not candidates:
        return None, "no CANDIDATE coins available with governor + PF >= 1.3"
    # Sort by backtest PF descending
    candidates.sort(key=lambda x: x[1].get("backtest_pf", 0), reverse=True)
    best_coin, best_info = candidates[0]
    return best_coin, f"best CANDIDATE: PF={best_info.get('backtest_pf')}, WR={best_info.get('backtest_wr_pct')}%"


def run(window: int = 20, apply: bool = False) -> int:
    pool = load_pool()
    active = pool.get("active", ["ETH", "BTC"])
    max_active = pool.get("max_active", 2)
    coins_meta = pool.get("coins", {})
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"=== Argus Coin Rotation Monitor ===")
    print(f"Date: {now_iso}")
    print(f"Active coins: {active} (max {max_active})")
    print(f"Rolling window: last {window} live trades")
    print()

    rotation_needed = False
    coins_to_rotate_out = []
    recommendations = []

    for coin in list(active):
        trades = read_live_trades(coin, window)
        metrics = compute_metrics(trades)
        should_out, reason = check_rotation_out(coin, metrics)

        status_icon = "!!" if should_out else "OK"
        pf_str = f"{metrics['pf']:.3f}" if metrics['pf'] is not None and metrics['pf'] != float('inf') else "inf"
        wr_str = f"{metrics['wr']:.1%}" if metrics['wr'] is not None else "N/A"

        print(f"{status_icon}{coin}: {metrics['n']} trades | PF={pf_str} | WR={wr_str} | PnL=${metrics['total_pnl']:.4f}")
        if metrics['n'] > 0:
            print(f"    AvgWin=${metrics['avg_win']:.4f} | AvgLoss=${metrics['avg_loss']:.4f}")
        print(f"    -> {reason}")
        print()

        # Update pool metrics
        if coin in coins_meta:
            coins_meta[coin]["live_metrics"] = {
                "window": window,
                "n_trades": metrics["n"],
                "rolling_pf": metrics["pf"],
                "rolling_wr": metrics["wr"],
                "total_pnl": metrics["total_pnl"],
                "updated": now_iso,
            }

        if should_out:
            rotation_needed = True
            coins_to_rotate_out.append((coin, reason))

    # Rotation recommendations
    print("-" * 50)
    if not rotation_needed:
        print("OK No rotation needed. All active coins performing within thresholds.")
    else:
        print(f"!! ROTATION RECOMMENDED for: {[c for c,_ in coins_to_rotate_out]}")
        print()
        for coin, reason in coins_to_rotate_out:
            new_coin, candidate_reason = find_rotation_candidate(pool, active)
            if new_coin:
                print(f"  ROTATE OUT: {coin} ({reason})")
                print(f"  ROTATE IN:  {new_coin} ({candidate_reason})")
                recommendations.append(("out", coin))
                recommendations.append(("in", new_coin))
            else:
                print(f"  ROTATE OUT: {coin} ({reason})")
                print(f"  ROTATE IN:  NONE — {candidate_reason}")
                print(f"             -> Promote a coin to CANDIDATE in coin_pool.json to enable rotation")
                recommendations.append(("out", coin))
        print()

    # Pool summary
    print()
    print("-- COIN POOL STATUS ------------------------------")
    for coin, info in sorted(coins_meta.items(), key=lambda x: (x[1].get("status", ""), -x[1].get("backtest_pf", 0))):
        st = info.get("status", "?")
        pf = info.get("backtest_pf", 0)
        wr = info.get("backtest_wr_pct", 0)
        gov = "Y" if info.get("governor_trained") else "N"
        live_m = info.get("live_metrics", {})
        live_str = f" | live_pf={live_m.get('rolling_pf','—')} ({live_m.get('n_trades',0)}t)" if live_m else ""
        print(f"  {coin:<6} [{st:<14}] BT PF={pf:.3f} WR={wr:.0f}% Gov={gov}{live_str}")

    print()
    print("-- TO PROMOTE A COIN TO CANDIDATE ---------------")
    print("  1. Download 90d candles:  python ops/refresh_candles.py --coin AVAX")
    print("  2. Run backtest:          add to backtest_queue.jsonl, run ops/run_queue.ps1")
    print("  3. Train governor:        python ml/train_governor.py --coin AVAX")
    print("  4. Set status in coin_pool.json: 'SCREENED_FAIL' -> 'CANDIDATE'")
    print("  5. Run this script again to confirm eligibility")

    # Apply changes to pool file
    if apply and recommendations:
        print()
        print("Applying recommendations to coin_pool.json...")
        new_active = list(active)
        for action, coin in recommendations:
            if action == "out" and coin in new_active:
                new_active.remove(coin)
                if coin in coins_meta:
                    coins_meta[coin]["status"] = "POOL_PAUSED"
                    coins_meta[coin]["paused_at"] = now_iso
                    coins_meta[coin]["pause_reason"] = "rotation_out"
                print(f"  -> {coin} moved to POOL_PAUSED")
            elif action == "in" and coin not in new_active and len(new_active) < max_active:
                new_active.append(coin)
                if coin in coins_meta:
                    coins_meta[coin]["status"] = "ACTIVE"
                    coins_meta[coin]["activated_at"] = now_iso
                print(f"  -> {coin} moved to ACTIVE")
        pool["active"] = new_active
        pool["coins"] = coins_meta
        pool["last_rotation"] = now_iso
        save_pool(pool)
        print(f"  Saved to {POOL_FILE}")
        print()
        print(f"  !! Restart runners to apply: .\\ops\\launch_multi.ps1 -Coins {' '.join(new_active)}")
    elif apply:
        # Still save live_metrics updates even if no rotation
        pool["coins"] = coins_meta
        pool["last_checked"] = now_iso
        save_pool(pool)
        print(f"\n  Metrics saved to {POOL_FILE}")

    return 1 if rotation_needed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Argus coin rotation monitor")
    parser.add_argument("--window", type=int, default=20, help="Rolling trade window (default 20)")
    parser.add_argument("--apply", action="store_true", help="Apply rotation changes to coin_pool.json")
    args = parser.parse_args()
    sys.exit(run(window=args.window, apply=args.apply))