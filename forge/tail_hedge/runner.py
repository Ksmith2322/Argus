"""forge_tail_hedge — defensive sleeve that engages when SPY breaks
its 200dma.

Per the 2026-05-25 backtest validation:
  PF 2.91 (n=28), Sharpe 1.91 when engaged, max DD 10.4%
  Engaged 23% of months over 20y history
  Adds to combined portfolio Sharpe 1.08 -> 1.12 with DD 23% -> 21%

LOGIC
=====
  monthly: check SPY 200dma
    if SPY > 200dma -> exit any held positions, sit flat
    if SPY < 200dma -> hold 50% GLD + 50% TLT

ROLE: HEDGE (0.80 PF floor per strategy_roles registry)
CLIENT_ID: 127 (next free slot after xs_momentum_legacy15_regime=126)

This is a small allocation sleeve. The strategy generates ZERO trade
flow ~77% of months (bullish regimes). When bearish, holds 2
positions through the bear regime. Carry cost ~+1.9% per year
worst case (over the full window if always held); positive Sharpe
contribution to the combined fleet.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from helio.blocker_ledger import record_blocker
from helio.fleet_sizing import (
    get_allocation_factor,
    get_sizing_anchor_usd,
    max_notional_usd,
)
from helio.canonical_fills import write_fill_typed
from helio.domain import Fill, make_lineage_id
from helio import ibkr_execution as ibkr
from helio.strategy_common import git_sha as _git_sha


STRATEGY_LABEL = "forge_tail_hedge"
IBKR_CLIENT_ID = 127
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "tail_hedge"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tail_hedge")


PARAMS = {
    "version": "v1_20260525",
    "universe": ["GLD", "TLT"],
    "weights": [0.5, 0.5],
    "regime_gate": "spy_above_200dma",   # ENGAGE when this is FALSE
    "regime_period": "2y",                # how much SPY history to pull
    "eval_hour_utc": 14,
    "eval_minute_utc": 30,
}


# ── Data ──────────────────────────────────────────────────────────────────

def _fetch_history(tickers: list[str], *, period: str = "2y") -> pd.DataFrame:
    """Pull daily closes via yfinance. Returns DataFrame indexed by date
    with one column per ticker. Empty DataFrame on failure (caller
    handles fail-CLOSED in evaluate_once)."""
    try:
        out = yf.download(tickers, period=period, interval="1d",
                          progress=False, auto_adjust=False,
                          group_by="ticker", threads=False)
    except Exception as exc:
        log.error("yfinance.download failed: %s", exc)
        return pd.DataFrame()
    if out is None or out.empty:
        return pd.DataFrame()
    cols = {}
    if isinstance(out.columns, pd.MultiIndex):
        for t in tickers:
            try:
                cols[t] = out[t]["Close"].dropna()
            except Exception:
                try:
                    cols[t] = out["Close"][t].dropna()
                except Exception:
                    pass
    elif "Close" in out.columns and len(tickers) == 1:
        cols[tickers[0]] = out["Close"].dropna()
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).dropna(how="all")
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


# ── State ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "current_picks": {},  # ticker -> {entry_ts, entry_px, qty}
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "last_rebalance_month": "",
        "last_decision": "",
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


TRADE_FIELDS = [
    "ts", "side", "ticker", "px", "qty", "regime_status",
    "pnl_usd", "pnl_pct", "session_id", "config_version",
]


def _append_trade(row: dict) -> None:
    is_new = not TRADES_PATH.exists()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _write_heartbeat(state: dict) -> None:
    try:
        git_sha = _git_sha(REPO)
    except Exception:
        git_sha = "unknown"
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "tail_hedge",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "current_picks": state.get("current_picks", {}),
        "last_decision": state.get("last_decision", ""),
        "version": PARAMS["version"],
        "git_sha": git_sha,
    }, indent=2, default=str))


# ── Regime check ──────────────────────────────────────────────────────────

def _check_regime() -> dict:
    """Returns {is_bullish, age_days, latest_close, sma_200} or error.

    Fail-CLOSED: if SPY data missing or stale, returns is_bullish=None
    so caller refuses to trade.
    """
    from helio.regime import fetch_regime_data, REGIME_GATES
    try:
        data = fetch_regime_data(period=PARAMS["regime_period"])
    except Exception as exc:
        return {"error": f"fetch_regime_data: {exc}", "is_bullish": None}
    spy = data.get("SPY_closes")
    if spy is None or spy.empty:
        return {"error": "no SPY data", "is_bullish": None}
    # Staleness check (same threshold as xs_momentum regime gate)
    try:
        latest_dt = pd.to_datetime(spy.index[-1])
        if latest_dt.tzinfo is not None:
            latest_dt = latest_dt.tz_localize(None)
        age_days = (
            pd.Timestamp.now().tz_localize(None) - latest_dt
        ).total_seconds() / 86400
    except Exception:
        return {"error": "stale check failed", "is_bullish": None}
    if age_days > 4.0:
        return {"error": f"SPY data {age_days:.1f}d stale",
                "is_bullish": None, "age_days": age_days}
    if len(spy) < 200:
        return {"error": f"insufficient history n={len(spy)}",
                "is_bullish": None}
    latest_close = float(spy.iloc[-1])
    sma_200 = float(spy.tail(200).mean())
    is_bullish = latest_close > sma_200
    return {
        "is_bullish": is_bullish,
        "age_days": round(age_days, 2),
        "latest_close": round(latest_close, 2),
        "sma_200": round(sma_200, 2),
    }


# ── Eval logic ────────────────────────────────────────────────────────────

def _is_rebalance_due(state: dict, now_utc: datetime) -> bool:
    """Same cadence as xs_momentum: first weekday of new calendar month."""
    if now_utc.weekday() >= 5:
        return False
    if now_utc.day > 7:
        return False
    current_month_key = now_utc.strftime("%Y-%m")
    last = state.get("last_rebalance_month", "")
    return current_month_key != last


def evaluate_once(force: bool = False) -> dict:
    summary = {"action": "noop", "reason": "",
               "now": datetime.now(timezone.utc).isoformat()}
    state = _load_state()
    state.setdefault("runtime_start", time.time())
    now_utc = datetime.now(timezone.utc)

    if not force and not _is_rebalance_due(state, now_utc):
        summary["reason"] = f"not_due (last={state.get('last_rebalance_month', '')})"
        log.info("tail_hedge skip: %s", summary["reason"])
        _write_heartbeat(state)
        return summary

    # Regime check FIRST (fail-CLOSED on missing/stale data)
    regime = _check_regime()
    if regime.get("is_bullish") is None:
        summary["action"] = "blocked"
        summary["reason"] = f"regime_check_failed:{regime.get('error', '?')}"
        record_blocker(STRATEGY_LABEL, "REGIME_CHECK_FAILED",
                       stage="regime", reason=summary["reason"],
                       context=regime)
        log.error("regime check failed: %s — REFUSING to trade", regime.get("error"))
        _write_heartbeat(state)
        return summary

    state["last_decision"] = (
        "BULLISH_FLAT" if regime["is_bullish"] else "BEARISH_HEDGE"
    )

    # Allocation check
    try:
        alloc_factor = float(get_allocation_factor(STRATEGY_LABEL))
    except Exception as exc:
        log.error("alloc read failed: %s", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_read_failed:{exc}"
        record_blocker(STRATEGY_LABEL, "ALLOC_READ_FAILED",
                       stage="allocation", reason=str(exc))
        return summary
    if alloc_factor <= 0.0:
        log.info("[ALLOC-GATE] allocation=%.3f — no rebalance", alloc_factor)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_factor={alloc_factor}"
        _write_heartbeat(state)
        return summary

    # ── DECIDE: bullish = flat, bearish = engage hedge ─────────────────
    current_picks = state.get("current_picks") or {}
    if regime["is_bullish"]:
        # Exit any held positions
        if current_picks:
            log.info("BULLISH regime — exiting %d hedge positions",
                     len(current_picks))
            # Signal-only paper exit (real IBKR ramp added in follow-up)
            for ticker in list(current_picks.keys()):
                current_picks.pop(ticker)
            state["current_picks"] = {}
            _save_state(state)
        state["last_rebalance_month"] = now_utc.strftime("%Y-%m")
        _save_state(state)
        summary["action"] = "bullish_flat"
        summary["regime"] = regime
        log.info("BULLISH regime: tail_hedge flat (SPY %.2f vs 200dma %.2f)",
                 regime["latest_close"], regime["sma_200"])
        _write_heartbeat(state)
        return summary

    # BEARISH: engage hedge sleeve
    log.info("BEARISH regime: engaging hedge (SPY %.2f vs 200dma %.2f)",
             regime["latest_close"], regime["sma_200"])

    # Pull entry prices for GLD + TLT (paper mode uses yfinance prices)
    prices_df = _fetch_history(PARAMS["universe"], period="2y")
    if prices_df.empty:
        summary["action"] = "blocked"
        summary["reason"] = "no price data for universe"
        record_blocker(STRATEGY_LABEL, "DATA_UNAVAILABLE",
                       stage="history", reason=summary["reason"])
        return summary

    anchor = get_sizing_anchor_usd()
    sleeve_notional = anchor * alloc_factor * 0.4   # 0.4x cluster cap default
    per_pick = sleeve_notional / len(PARAMS["universe"])

    new_picks = {}
    for ticker, weight in zip(PARAMS["universe"], PARAMS["weights"]):
        if ticker in current_picks:
            new_picks[ticker] = current_picks[ticker]
            continue
        try:
            entry_px = float(prices_df[ticker].iloc[-1])
        except Exception:
            log.warning("missing price for %s — skipping", ticker)
            continue
        qty = int((per_pick * weight * 2) // entry_px)  # 2 since each pick is half of sleeve
        if qty < 1:
            log.warning("ENTRY skipped for %s: per-pick=%.2f / entry_px=%.2f -> qty<1",
                        ticker, per_pick, entry_px)
            continue
        new_picks[ticker] = {
            "entry_ts": now_utc.isoformat(),
            "entry_px": entry_px,
            "qty": qty,
            "weight": weight,
        }
        # Record paper-mode entry trade row
        _append_trade({
            "ts": now_utc.isoformat(),
            "side": "ENTRY",
            "ticker": ticker,
            "px": round(entry_px, 4),
            "qty": qty,
            "regime_status": "BEARISH",
            "pnl_usd": "",
            "pnl_pct": "",
            "session_id": state.get("session_id", ""),
            "config_version": PARAMS["version"],
        })
        # Write canonical fill row (real-money path can switch to IBKR later)
        try:
            lineage = make_lineage_id(
                STRATEGY_LABEL, state.get("session_id"),
                now_utc.strftime("%Y%m%d_%H%M%S"),
            )
            write_fill_typed(Fill(
                ts=now_utc.isoformat(),
                strategy=STRATEGY_LABEL,
                symbol=ticker,
                direction="long",
                side="ENTRY",
                entry_ts=now_utc.isoformat(),
                entry_px=round(entry_px, 4),
                size=float(qty),
                broker_anchor_at_fill_usd=anchor,
                lineage_id=lineage,
            ))
        except Exception as exc:
            log.warning("canonical_fills write failed for %s: %s", ticker, exc)

    state["current_picks"] = new_picks
    state["trade_count"] = int(state.get("trade_count", 0)) + len(new_picks)
    state["last_rebalance_month"] = now_utc.strftime("%Y-%m")
    _save_state(state)

    summary["action"] = "bearish_hedge_engaged"
    summary["regime"] = regime
    summary["picks"] = list(new_picks.keys())
    log.info("HEDGE ENGAGED: %s", list(new_picks.keys()))
    _write_heartbeat(state)
    return summary


# ── Backtest passthrough ──────────────────────────────────────────────────

def backtest(period: str = "20y") -> dict:
    """Re-uses ops.audit.run_tail_hedge_backtest._run_backtest()."""
    try:
        from ops.audit.run_tail_hedge_backtest import _run_backtest
    except Exception as exc:
        return {"error": f"import: {exc}"}
    return _run_backtest(period=period)


# ── Loop mode ─────────────────────────────────────────────────────────────

def loop_mode() -> None:
    log.info("tail_hedge --loop started: daily wake at %02d:%02d UTC",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=PARAMS["eval_hour_utc"],
                               minute=PARAMS["eval_minute_utc"],
                               second=15, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            sleep_s = max(5.0, (fire - now).total_seconds())
            log.info("tail_hedge next wake at %s UTC (sleep %.0fs)",
                     fire.isoformat(), sleep_s)
            state = _load_state()
            _write_heartbeat(state)
            remaining = sleep_s
            while remaining > 0:
                chunk = min(300.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
                state = _load_state()
                _write_heartbeat(state)
            try:
                evaluate_once()
            except Exception as exc:
                log.error("evaluate_once failed: %s", exc, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("tail_hedge loop stopped by user")
            return


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--period", default="20y")
    parser.add_argument("--force", action="store_true",
                        help="With --evaluate: force rebalance even if not month-start")
    parser.add_argument("--signal-only", action="store_true")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)

    if args.backtest:
        r = backtest(args.period)
        print(json.dumps(r, indent=2, default=str)[:2000])
        return 0

    if args.check:
        regime = _check_regime()
        state = _load_state()
        print(json.dumps({
            "regime": regime,
            "current_picks": state.get("current_picks", {}),
            "last_decision": state.get("last_decision", ""),
            "trade_count": state.get("trade_count", 0),
        }, indent=2, default=str))
        return 0

    if args.evaluate or args.loop:
        from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
        try:
            with acquire_runner_lock(STRATEGY_LABEL):
                if args.evaluate:
                    print(json.dumps(evaluate_once(force=args.force),
                                     indent=2, default=str))
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
