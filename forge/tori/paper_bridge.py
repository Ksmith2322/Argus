"""Paper-execution bridge for Tori.

Tori has a full backtest engine (forge.tori.runner.run_backtest) with
complex trendline-based entries and trailing-stop exits. Re-implementing
that stateful logic in a live loop would duplicate substantial code.

Pragmatic approach adopted here: rerun the backtest on a rolling window
of recent data on each evaluation, compare generated trades to a persisted
"last processed" marker, and materialize only NEW trades as paper fills.

Why this is reasonable:
  1. Backtest logic is authoritative — same code produces backtest and
     paper trades.
  2. Scope-down narrows to YM=F + Dow + LONG (SCOPE_DOW_LONG_ONLY=True),
     so the search surface is small.
  3. Trade cadence is low (4H bars → few setups per week) so rerunning
     the backtest on 60d of data is cheap.
  4. Idempotent — marker prevents double-emitting trades across runs.

Output:
  - forge/logs/tori/paper_trades.csv: new trades per run
  - forge/logs/tori/paper_state.json: last_processed_entry_date marker
  - canonical_fills.jsonl: dual-write for fleet-wide aggregation

Usage:
  python -m forge.tori.paper_bridge --evaluate   # one cycle
  python -m forge.tori.paper_bridge --loop       # continuous (4H cadence)
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

from forge.tori.runner import (  # noqa: E402
    TICKERS, SCOPE_DOW_LONG_ONLY, download_data, resample_to_4h,
    compute_atr, run_backtest, STARTING_EQUITY,
)
from forge.logging_setup import setup_logging  # noqa: E402

log = setup_logging("tori_paper")

LOG_DIR = REPO / "forge" / "logs" / "tori"
STATE_PATH = LOG_DIR / "paper_state.json"
TRADES_PATH = LOG_DIR / "paper_trades.csv"

TRADE_CSV_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_points", "pnl_usd",
    "r_multiple", "contracts", "entry_date", "exit_date", "hold_bars",
    "exit_reason", "setup", "grade", "ticker", "name", "stop_price",
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
    """Mirror to canonical_fills.jsonl for fleet-wide aggregation."""
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_tori",
            symbol=trade.get("ticker", "YM=F"),
            direction=(trade.get("direction") or "LONG").lower(),
            side="EXIT",
            entry_ts=str(trade.get("entry_date", "")),
            exit_ts=str(trade.get("exit_date", "")),
            entry_px=float(trade.get("entry_price", 0)),
            exit_px=float(trade.get("exit_price", 0)),
            size=float(trade.get("contracts", 0)),
            risk_usd=0.0,
            pnl_usd=round(float(trade.get("pnl_usd", 0)), 2),
            exit_reason=trade.get("exit_reason", ""),
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")


def evaluate_once() -> dict:
    """Download latest data, run backtest on rolling 60d window, emit
    any trades that landed since the last run."""
    if not SCOPE_DOW_LONG_ONLY:
        log.warning("SCOPE_DOW_LONG_ONLY=False — paper bridge assumes the Dow subset. "
                    "Trades outside that subset will still fire but may diverge from "
                    "the validated claim.")

    log.info("Tori paper-bridge eval starting; tickers=%s", TICKERS)
    datasets = {}
    for ticker in TICKERS:
        df = download_data(ticker, period="60d", interval="1h")
        if df.empty:
            log.warning(f"no data for {ticker}; skipping")
            continue
        df_4h = resample_to_4h(df)
        df_4h = compute_atr(df_4h)
        datasets[ticker] = df_4h

    if not datasets:
        log.error("No datasets loaded; aborting cycle")
        return {"status": "no_data", "new_trades": 0}

    all_trades = run_backtest(datasets, equity=STARTING_EQUITY)
    if not all_trades:
        log.info("backtest produced 0 trades in rolling window")
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
            "contracts": t.get("contracts"),
            "entry_date": t.get("entry_date"),
            "exit_date": t.get("exit_date"),
            "hold_bars": t.get("hold_bars"),
            "exit_reason": t.get("exit_reason"),
            "setup": t.get("setup"),
            "grade": t.get("grade"),
            "ticker": t.get("ticker"),
            "name": t.get("name"),
            "stop_price": t.get("stop_price"),
        })
        _dual_write_canonical(t)
        log.info(
            f"PAPER TRADE {t.get('direction')} {t.get('ticker')} "
            f"entry {t.get('entry_price')} exit {t.get('exit_price')} "
            f"pnl_usd {t.get('pnl_usd')} R {t.get('r_multiple')} "
            f"reason={t.get('exit_reason')}"
        )

    if new_trades:
        state["last_processed_entry_date"] = str(new_trades[-1]["entry_date"])
        state["paper_trade_count"] = state.get("paper_trade_count", 0) + len(new_trades)
        state["last_eval_ts"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)

    log.info(f"Tori paper-bridge cycle done: {len(new_trades)} new paper trades")
    return {"status": "ok", "new_trades": len(new_trades), "total_paper_trades": state.get("paper_trade_count", 0)}


def loop_mode() -> None:
    """Re-evaluate every 4H (at H:05 UTC to give yfinance time to publish)."""
    log.info("Tori paper-bridge loop mode starting")
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Fire at :05 past every 4-hour boundary (0,4,8,12,16,20 UTC)
            next_hour = ((now.hour // 4) + 1) * 4
            if next_hour >= 24:
                next_fire = now.replace(hour=0, minute=5, second=0, microsecond=0) + timedelta(days=1)
            else:
                next_fire = now.replace(hour=next_hour, minute=5, second=0, microsecond=0)
            sleep_s = max(30, (next_fire - now).total_seconds())
            log.info(f"next eval at {next_fire.isoformat()} (sleep {sleep_s:.0f}s)")
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as e:
                log.error(f"evaluate_once failed: {e}", exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("loop stopped by user")
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true", help="one eval cycle")
    g.add_argument("--loop", action="store_true", help="continuous 4H loop")
    args = ap.parse_args()
    if args.evaluate:
        r = evaluate_once()
        print(json.dumps(r, indent=2))
    elif args.loop:
        loop_mode()
    return 0


if __name__ == "__main__":
    sys.exit(main())
