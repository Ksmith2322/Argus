"""Paper-execution bridge for Cue Banks — same pattern as forge.tori.paper_bridge.

Rerun the backtest on a rolling window, emit only trades newer than the
persisted marker as paper fills. Avoids re-implementing the complex
intraday multi-TF confluence entry logic in a live loop.

Scope: with SCOPE_SD_SUPPLY_ZONE_ONLY=True (forge/cuebanks/runner.py) the
validated subset (factors contains 'S/D supply zone') is the only one that
fires. Strategy trades YM=F (Micro Dow) in NY session only.

Usage:
  python -m forge.cuebanks.paper_bridge --evaluate
  python -m forge.cuebanks.paper_bridge --loop
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from forge.cuebanks.runner import run_backtest, SCOPE_SD_SUPPLY_ZONE_ONLY  # noqa: E402
from forge.logging_setup import setup_logging  # noqa: E402

log = setup_logging("cuebanks_paper")

LOG_DIR = REPO / "forge" / "logs" / "cuebanks"
STATE_PATH = LOG_DIR / "paper_state.json"
TRADES_PATH = LOG_DIR / "paper_trades.csv"

TRADE_CSV_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_points", "pnl_usd",
    "r_multiple", "entry_date", "exit_date", "hold_bars", "exit_reason",
    "factors", "confluence_score",
]


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_processed_entry_date": "", "paper_trade_count": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_processed_entry_date": "", "paper_trade_count": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _append_trade(row: dict) -> None:
    first_write = not TRADES_PATH.exists()
    TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_CSV_FIELDS, extrasaction="ignore")
        if first_write:
            w.writeheader()
        w.writerow(row)


def _dual_write_canonical(trade: dict) -> None:
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_cuebanks",
            symbol="YM=F",
            direction=(trade.get("direction") or "LONG").lower(),
            side="EXIT",
            entry_ts=str(trade.get("entry_date", "")),
            exit_ts=str(trade.get("exit_date", "")),
            entry_px=float(trade.get("entry_price", 0)),
            exit_px=float(trade.get("exit_price", 0)),
            size=1.0,
            risk_usd=0.0,
            pnl_usd=round(float(trade.get("pnl_usd", 0)), 2),
            exit_reason=trade.get("exit_reason", ""),
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")


def evaluate_once() -> dict:
    if not SCOPE_SD_SUPPLY_ZONE_ONLY:
        log.warning("SCOPE_SD_SUPPLY_ZONE_ONLY=False — paper bridge assumes the "
                    "validated supply-zone subset. Union signals will still fire.")

    log.info("Cue Banks paper-bridge eval starting")

    # run_backtest() is self-contained — downloads YM=F data, computes
    # multi-TF confluence, returns list of trades.
    try:
        all_trades = run_backtest()
    except Exception as e:
        log.error(f"run_backtest failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "new_trades": 0}

    if not all_trades:
        log.info("backtest produced 0 trades")
        return {"status": "no_trades", "new_trades": 0}

    state = _load_state()
    cutoff = state.get("last_processed_entry_date", "")

    new_trades = [t for t in all_trades if str(t.get("entry_date", "")) > cutoff]
    new_trades.sort(key=lambda t: str(t.get("entry_date", "")))

    for t in new_trades:
        _append_trade({
            "ts": t.get("exit_date") or t.get("entry_date"),
            "direction": t.get("direction"),
            "entry_px": t.get("entry_price"),
            "exit_px": t.get("exit_price"),
            "pnl_points": t.get("pnl_points"),
            "pnl_usd": t.get("pnl_usd"),
            "r_multiple": t.get("r_multiple"),
            "entry_date": t.get("entry_date"),
            "exit_date": t.get("exit_date"),
            "hold_bars": t.get("hold_bars"),
            "exit_reason": t.get("exit_reason"),
            "factors": "|".join(t.get("factors", [])) if isinstance(t.get("factors"), list) else (t.get("factors") or ""),
            "confluence_score": t.get("confluence_score"),
        })
        _dual_write_canonical(t)
        log.info(
            f"PAPER TRADE {t.get('direction')} entry {t.get('entry_price')} "
            f"exit {t.get('exit_price')} pnl {t.get('pnl_usd')} reason={t.get('exit_reason')}"
        )

    if new_trades:
        state["last_processed_entry_date"] = str(new_trades[-1]["entry_date"])
        state["paper_trade_count"] = state.get("paper_trade_count", 0) + len(new_trades)
        state["last_eval_ts"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)

    log.info(f"Cue Banks paper-bridge cycle done: {len(new_trades)} new trades")
    return {"status": "ok", "new_trades": len(new_trades), "total": state.get("paper_trade_count", 0)}


def loop_mode() -> None:
    """Re-evaluate every 15m during NY session (13:30-20:00 UTC)."""
    log.info("Cue Banks paper-bridge loop starting — NY session only")
    while True:
        try:
            now = datetime.now(timezone.utc)
            in_session = 13 <= now.hour <= 20
            if in_session:
                try:
                    evaluate_once()
                except Exception as e:
                    log.error(f"evaluate_once failed: {e}", exc_info=True)
                time.sleep(15 * 60)
            else:
                # Sleep until next NY open
                next_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
                if next_open <= now:
                    next_open += timedelta(days=1)
                sleep_s = (next_open - now).total_seconds()
                log.info(f"outside NY session; sleeping until {next_open.isoformat()} ({sleep_s/3600:.1f}h)")
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            log.info("loop stopped by user")
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--loop", action="store_true")
    args = ap.parse_args()
    if args.evaluate:
        print(json.dumps(evaluate_once(), indent=2))
    elif args.loop:
        loop_mode()
    return 0


if __name__ == "__main__":
    sys.exit(main())
