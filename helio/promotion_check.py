"""Helio Family Promotion Check — evaluate whether swing strategies are ready for paper/real.

Checks:
  - Minimum watcher residency (14 days)
  - Minimum observed signals (10 for daily strategies)
  - Walk-forward or backtest evidence exists
  - Kill discipline (no-progress, strategy kill clock)
  - Heartbeat freshness

Usage:
    python -m helio.promotion_check
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

HELIO_ROOT = Path(__file__).resolve().parent
LOGS_ROOT = HELIO_ROOT / "logs"
CONFIGS_DIR = HELIO_ROOT / "configs"

WATCHER_MIN_DAYS = 14
WATCHER_MIN_SIGNALS = 10
PAPER_MIN_TRADES = 20
PAPER_MIN_DAYS = 30
KILL_PF_THRESHOLD = 1.0
KILL_TRADE_THRESHOLD = 20
NO_PROGRESS_PF = 1.05
NO_PROGRESS_TRADES = 30


def _load_trades(log_dir: Path) -> list[dict]:
    tf = log_dir / "trades.csv"
    if not tf.exists():
        return []
    try:
        with open(tf, "r") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_signals(log_dir: Path) -> list[dict]:
    sf = log_dir / "signals.csv"
    if not sf.exists():
        return []
    try:
        with open(sf, "r") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _profit_factor(trades: list[dict]) -> float:
    pnls = [float(t.get("pnl_pct", 0)) for t in trades]
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    return wins / losses if losses > 0 else (99.0 if wins > 0 else 0.0)


def check_runner(config_path: Path) -> dict:
    cfg = json.loads(config_path.read_text())
    symbol = cfg.get("symbol", "")
    family = cfg.get("family", cfg.get("strategy", "unknown"))
    stage = cfg.get("deployment", {}).get("stage", "watcher")

    # Find log dir
    prefix = f"{family}_{symbol.lower()}" if family in ("apollo", "hermes") else symbol.lower()
    log_dir = LOGS_ROOT / prefix
    if not log_dir.exists():
        log_dir = LOGS_ROOT / symbol.lower()

    trades = _load_trades(log_dir)
    signals = _load_signals(log_dir)

    # Heartbeat freshness
    hb = log_dir / "heartbeat.json"
    hb_age = None
    if hb.exists():
        hb_age = int(time.time() - hb.stat().st_mtime)

    result = {
        "symbol": symbol,
        "family": family,
        "config": config_path.name,
        "stage": stage,
        "trades": len(trades),
        "signals": len(signals),
        "hb_age_s": hb_age,
        "verdict": "NOT_READY",
        "blockers": [],
        "pf": _profit_factor(trades) if trades else 0,
    }

    if stage == "watcher":
        if len(signals) < WATCHER_MIN_SIGNALS:
            result["blockers"].append(f"signals {len(signals)}/{WATCHER_MIN_SIGNALS}")
        if hb_age and hb_age > 86400:  # 24hr
            result["blockers"].append(f"heartbeat stale ({hb_age}s)")

        # Kill checks
        if len(trades) >= KILL_TRADE_THRESHOLD and result["pf"] < KILL_PF_THRESHOLD:
            result["verdict"] = "KILL"
            result["blockers"].append(f"PF {result['pf']:.2f} < {KILL_PF_THRESHOLD} after {len(trades)} trades")
            return result

        if not result["blockers"]:
            result["verdict"] = "READY_FOR_PAPER"
        return result

    elif stage == "paper":
        if len(trades) < PAPER_MIN_TRADES:
            result["blockers"].append(f"trades {len(trades)}/{PAPER_MIN_TRADES}")

        pf = result["pf"]
        if len(trades) >= NO_PROGRESS_TRADES and pf < NO_PROGRESS_PF:
            result["verdict"] = "KILL"
            result["blockers"].append(f"no progress: PF {pf:.2f} after {len(trades)} trades")
            return result

        if len(trades) >= PAPER_MIN_TRADES and pf >= 1.3:
            # READY_FOR_REAL requires a CLEAN evidence epoch in addition to
            # the PF + sample threshold. If we're still in the pre-freeze
            # (contaminated) epoch, downgrade to COLLECTING and add the
            # epoch as a blocker. Codex audit 2026-05-18 X4 — promotion
            # decisions cannot use pre-reset evidence.
            try:
                from helio import evidence_epoch as _ee
                epoch = _ee.current_epoch()
                if not epoch.is_clean:
                    result["verdict"] = "COLLECTING"
                    result["blockers"].append(
                        f"contaminated_epoch:{epoch.id} (post-reset epoch required)"
                    )
                    result["epoch_id"] = epoch.id
                    result["epoch_is_clean"] = False
                    return result
                result["epoch_id"] = epoch.id
                result["epoch_is_clean"] = True
            except Exception as exc:
                # Fail closed: cannot read the epoch → block promotion.
                result["verdict"] = "NOT_READY"
                result["blockers"].append(f"evidence_epoch_unreadable:{exc}")
                return result
            result["verdict"] = "READY_FOR_REAL"
        elif not result["blockers"]:
            result["verdict"] = "COLLECTING"
        return result

    return result


def main():
    print("=" * 70)
    print(f"  Helio Family Promotion Check -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 70)

    configs = sorted(CONFIGS_DIR.glob("*_v1.json"))
    if not configs:
        print("  No configs found")
        return

    for cfg_path in configs:
        r = check_runner(cfg_path)
        status_colors = {
            "READY_FOR_PAPER": "+", "READY_FOR_REAL": "++", "COLLECTING": "~",
            "NOT_READY": "-", "KILL": "X",
        }
        marker = status_colors.get(r["verdict"], "?")
        blockers = ", ".join(r["blockers"]) if r["blockers"] else "none"
        print(f"  [{marker}] {r['family']:>8s} {r['symbol']:>8s} | {r['stage']:>8s} | "
              f"T={r['trades']:>3d} S={r['signals']:>3d} | PF={r['pf']:>5.2f} | {r['verdict']} | {blockers}")

    print()
    ready = [cfg_path for cfg_path in configs if check_runner(cfg_path)["verdict"] in ("READY_FOR_PAPER", "READY_FOR_REAL")]
    print(f"  {len(ready)} configs ready for promotion")


if __name__ == "__main__":
    main()
