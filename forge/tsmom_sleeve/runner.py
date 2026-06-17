"""forge.tsmom_sleeve.runner -- Time-series momentum SHADOW runner.

Per-asset TSMOM sleeve across 11 SURVIVES tickers from the 2026-05-26
TSMOM sweep (ops/reports/system_audit/tsmom_sweep.md). Long if asset's
3-month trailing return > 0 at month-end; flat otherwise. Rebalance
monthly. NO IBKR connection -- writes virtual trades only.

Universe (per Moskowitz-Ooi-Pedersen 2012, filtered to disciplined-gate
survivors with PF >= 1.30, CI lower >= 1.20, pre-tax CAGR >= 13%):
  NVDA 35%, NFLX 31%, AVGO 22%, AMD 22%, BKNG 22%, AAPL 19%,
  AMZN 18%, MA 18%, SMH 17%, QQQ 14%, CSX 13%

Sleeve metrics from backtest:
  - Cadence: ~1.2 fills/month (each ticker fires ~1-1.6 transitions/yr)
  - Equal-weight CAGR: 21.0% pretax / 12.8% after-tax
  - vs SPY 8% after-tax baseline: +4.8pp

WHY SHADOW (allocation 0.0):
  - Backtest passes per-strategy CAGR bar (21% vs 13% required) but
    FAILS cadence bar (1.2/mo vs 30/mo required).
  - Operator chose to defer live deployment until post-vetting (Aug 31)
    review when current 4-runner fleet has proved its plumbing.
  - Shadow runner accumulates real signal data with zero capital risk,
    so by Aug 31 we have 3-4 months of live signal history to inform
    promotion decision.

Modes:
  --check       Print current per-asset signals (no write)
  --evaluate    One cycle: compute signals + log state changes as virtual fills
  --loop        Daemon: wake daily, evaluate at month-end (or weekly check)

Outputs:
  forge/logs/tsmom_sleeve/
    virtual_trades.csv     # one row per signal flip (virtual ENTRY/EXIT)
    monthly_picks.jsonl    # one record per evaluation with all per-asset signals
    state.json             # current positions per ticker (long/flat)
    heartbeat.json         # fleet_monitor heartbeat
    runner.log             # standard log
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging


STRATEGY_LABEL = "forge_tsmom_sleeve"
# No IBKR_CLIENT_ID -- this runner never connects to the broker

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "tsmom_sleeve"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
VIRTUAL_TRADES_PATH = LOG_DIR / "virtual_trades.csv"
PICKS_JSONL_PATH = LOG_DIR / "monthly_picks.jsonl"

log = setup_logging("tsmom_sleeve")
NY_TZ = ZoneInfo("America/New_York")

# 11 SURVIVES from the 2026-05-26 TSMOM sweep. Pinned here as the sleeve
# universe. Any future revision requires re-running the sweep + updating
# this list + bumping PARAMS["version"].
TSMOM_UNIVERSE = [
    "NVDA", "NFLX", "AVGO", "AMD", "BKNG", "AAPL",
    "AMZN", "MA", "SMH", "QQQ", "CSX",
]

PARAMS = {
    "version": "v1_20260526",
    "lookback_months": 3,
    "min_long_threshold": 0.0,   # signal: trailing N-month return > 0
    # Evaluate weekly during loop mode (signal can only change at month-end
    # but checking weekly catches the change quickly without burning yfinance
    # quota). Actual flip detection requires month-end resampling.
    "eval_hour_utc": 20,
    "eval_minute_utc": 0,
    "eval_day_of_week": 0,        # 0=Monday weekly wake
}


VIRTUAL_TRADE_FIELDS = [
    "ts", "ticker", "side", "signal_value", "entry_ts", "exit_ts",
    "entry_px", "exit_px", "pnl_pct", "held_months",
    "session_id", "config_version",
]


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        # ticker -> {"position": "long"|"flat", "entry_ts": iso, "entry_px": float}
        "positions": {},
        "last_eval_ts": "",
        "transition_count": 0,
        "session_id": str(uuid.uuid4())[:8],
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(state: dict, action: str) -> None:
    try:
        from helio.strategy_common import git_sha as _git_sha
        git_sha = _git_sha(REPO)
    except Exception:
        git_sha = "unknown"
    longs = [t for t, p in state.get("positions", {}).items()
             if p.get("position") == "long"]
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "tsmom_sleeve",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow",                     # never paper / never real
        "transition_count": state.get("transition_count", 0),
        "currently_long": longs,
        "n_long": len(longs),
        "universe_size": len(TSMOM_UNIVERSE),
        "last_action": action,
        "version": PARAMS["version"],
        "git_sha": git_sha,
    }, indent=2, default=str))


def _ensure_csv():
    if not VIRTUAL_TRADES_PATH.exists():
        with open(VIRTUAL_TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(VIRTUAL_TRADE_FIELDS)


def _append_virtual_trade(row: dict) -> None:
    _ensure_csv()
    with open(VIRTUAL_TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=VIRTUAL_TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in VIRTUAL_TRADE_FIELDS}
        )


def _fetch_history(ticker: str, period: str = "18mo") -> pd.DataFrame:
    """Fetch enough daily history to compute a 3-month-lagged signal at
    month-end. 18 months gives plenty of buffer."""
    try:
        df = yf.download(ticker, period=period, interval="1d",
                          progress=False, auto_adjust=False)
    except Exception as exc:
        log.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index)
    return df


def compute_signals() -> dict:
    """For each ticker, compute current month-end TSMOM signal (long if
    3m trailing return > 0). Returns {ticker: {"signal": "long"|"flat",
    "trailing_return_pct": float, "ref_close": float, "ref_date": str}}."""
    out = {}
    for ticker in TSMOM_UNIVERSE:
        df = _fetch_history(ticker)
        if df.empty or len(df) < 80:
            out[ticker] = {"signal": "flat", "trailing_return_pct": None,
                           "ref_close": None, "ref_date": None,
                           "error": "insufficient_data"}
            continue
        # Last close
        last_close = float(df["Close"].iloc[-1])
        last_date = df.index[-1].date().isoformat()
        # Close 3 trading-months ago (~63 trading days)
        lookback_bars = PARAMS["lookback_months"] * 21
        if len(df) <= lookback_bars:
            out[ticker] = {"signal": "flat", "trailing_return_pct": None,
                           "ref_close": last_close, "ref_date": last_date,
                           "error": "insufficient_lookback"}
            continue
        past_close = float(df["Close"].iloc[-lookback_bars])
        trailing = (last_close - past_close) / past_close
        signal = "long" if trailing > PARAMS["min_long_threshold"] else "flat"
        out[ticker] = {
            "signal": signal,
            "trailing_return_pct": round(trailing * 100, 2),
            "ref_close": last_close,
            "ref_date": last_date,
        }
    return out


def evaluate_once(force: bool = False) -> dict:
    """Compute signals, detect state changes, record virtual fills.
    Returns summary dict."""
    state = _load_state()
    sig = compute_signals()
    now_utc = datetime.now(timezone.utc).isoformat()
    positions = state.setdefault("positions", {})
    transitions = []
    for ticker, info in sig.items():
        new_signal = info["signal"]
        ref_close = info.get("ref_close")
        ref_date = info.get("ref_date")
        cur = positions.get(ticker, {"position": "flat"})
        cur_pos = cur.get("position", "flat")
        if new_signal == "long" and cur_pos == "flat":
            # Virtual ENTRY
            positions[ticker] = {
                "position": "long",
                "entry_ts": now_utc,
                "entry_px": ref_close,
                "entry_date": ref_date,
            }
            _append_virtual_trade({
                "ts": now_utc, "ticker": ticker, "side": "ENTRY",
                "signal_value": info["trailing_return_pct"],
                "entry_ts": now_utc, "entry_px": ref_close,
                "session_id": state["session_id"],
                "config_version": PARAMS["version"],
            })
            transitions.append({"ticker": ticker, "action": "ENTRY",
                                 "entry_px": ref_close})
            state["transition_count"] += 1
            log.info("VIRTUAL ENTRY %s @ %.2f (trailing %.2f%%)",
                     ticker, ref_close, info["trailing_return_pct"])
        elif new_signal == "flat" and cur_pos == "long":
            # Virtual EXIT
            entry_px = cur.get("entry_px")
            entry_ts = cur.get("entry_ts")
            entry_date = cur.get("entry_date")
            if entry_px and ref_close:
                pnl_pct = (ref_close - entry_px) / entry_px * 100.0
            else:
                pnl_pct = None
            try:
                e_dt = pd.to_datetime(entry_date)
                x_dt = pd.to_datetime(ref_date)
                held_months = round((x_dt - e_dt).days / 30.4, 1)
            except Exception:
                held_months = None
            _append_virtual_trade({
                "ts": now_utc, "ticker": ticker, "side": "EXIT",
                "signal_value": info["trailing_return_pct"],
                "entry_ts": entry_ts, "exit_ts": now_utc,
                "entry_px": entry_px, "exit_px": ref_close,
                "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else "",
                "held_months": held_months,
                "session_id": state["session_id"],
                "config_version": PARAMS["version"],
            })
            transitions.append({
                "ticker": ticker, "action": "EXIT",
                "entry_px": entry_px, "exit_px": ref_close,
                "pnl_pct": pnl_pct,
            })
            state["transition_count"] += 1
            positions[ticker] = {"position": "flat"}
            log.info("VIRTUAL EXIT %s @ %.2f (entry %.2f, pnl %+.2f%%)",
                     ticker, ref_close, entry_px or 0,
                     pnl_pct or 0)
        # else: signal unchanged -- nothing to do

    state["last_eval_ts"] = now_utc

    # Append a full snapshot record to the JSONL audit log
    snapshot = {
        "ts": now_utc,
        "signals": sig,
        "transitions": transitions,
        "n_long_after": sum(1 for p in positions.values()
                            if p.get("position") == "long"),
    }
    with open(PICKS_JSONL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, default=str) + "\n")

    _save_state(state)
    _write_heartbeat(state, action=f"evaluated_{len(transitions)}_transitions")
    return {
        "ts": now_utc,
        "transitions": transitions,
        "n_transitions": len(transitions),
        "n_long_after": snapshot["n_long_after"],
        "signals": sig,
    }


def loop_mode():
    """Wake weekly (Monday 20:00 UTC), evaluate. Signal flips can only
    happen at month-end but checking weekly catches them within ~7 days."""
    log.info("tsmom_sleeve --loop started: weekly wake (Mon %02d:%02d UTC)",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    try:
        _write_heartbeat(_load_state(), action="boot")
    except Exception as exc:
        log.warning("boot heartbeat write failed: %s", exc)
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Find next Monday at eval_hour
            days_until_monday = (PARAMS["eval_day_of_week"] - now.weekday()) % 7
            if days_until_monday == 0 and now.hour >= PARAMS["eval_hour_utc"]:
                days_until_monday = 7
            fire = (now.replace(hour=PARAMS["eval_hour_utc"],
                                 minute=PARAMS["eval_minute_utc"],
                                 second=15, microsecond=0)
                    + timedelta(days=days_until_monday))
            sleep_s = max(5.0, (fire - now).total_seconds())
            log.info("next wake at %s UTC (sleep %.0fs)",
                     fire.isoformat(), sleep_s)
            # Sleep in 5-minute chunks so heartbeat stays fresh
            remaining = sleep_s
            while remaining > 0:
                chunk = min(300.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
                _write_heartbeat(_load_state(), action="sleeping")
            try:
                summary = evaluate_once()
                log.info("eval done: %d transitions, %d currently long",
                         summary["n_transitions"], summary["n_long_after"])
            except Exception as exc:
                log.error("evaluate_once failed: %s", exc, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("tsmom_sleeve loop stopped by user")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Print current per-asset signals, no write")
    parser.add_argument("--evaluate", action="store_true",
                        help="One eval cycle: compute + write virtual fills")
    parser.add_argument("--loop", action="store_true",
                        help="Daemon: weekly wake")
    args = parser.parse_args(argv)

    if args.check:
        sig = compute_signals()
        n_long = sum(1 for s in sig.values() if s["signal"] == "long")
        print(json.dumps({
            "universe": TSMOM_UNIVERSE,
            "signals": sig,
            "n_long": n_long,
            "version": PARAMS["version"],
        }, indent=2, default=str))
        return 0

    if args.evaluate or args.loop:
        from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
        try:
            with acquire_runner_lock(STRATEGY_LABEL):
                if args.evaluate:
                    summary = evaluate_once()
                    print(json.dumps(summary, indent=2, default=str))
                    return 0
                loop_mode()
                return 0
        except RunnerAlreadyRunning as exc:
            log.error("REFUSING_DUPLICATE_RUNNER: %s", exc)
            return 75

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
