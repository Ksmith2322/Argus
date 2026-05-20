"""forge.xs_momentum — runner + backtest for cross-sectional 12-1 momentum.

The runner is monthly-cadence: it evaluates positions on the first
trading day of each month, exits old picks not in this month's top
quintile, enters new picks.

Modes:
  --backtest    Simulate over historical data, report stats.
  --check       Print this-month's ranking + recommended picks.
  --evaluate    One cycle: rebalance to current top quintile.
  --loop        Daemon mode (sleeps until next month-start).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.fleet_sizing import max_notional_usd
from helio import ibkr_execution as ibkr
from helio.xs_momentum import (
    DEFAULT_UNIVERSE,
    LOOKBACK_LONG_DAYS, LOOKBACK_SHORT_DAYS,
    TOP_QUINTILE_FRACTION,
    rank_universe_by_momentum, select_top_quintile,
)

IBKR_CLIENT_ID = 121
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "xs_momentum"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"

log = setup_logging("xs_momentum")

PARAMS = {
    "version": "v1_20260519",
    "universe": list(DEFAULT_UNIVERSE),
    "long_lookback": LOOKBACK_LONG_DAYS,
    "short_lookback": LOOKBACK_SHORT_DAYS,
    "top_quintile_fraction": TOP_QUINTILE_FRACTION,
    # Position sizing — each pick gets equal weight from the strategy's
    # allocated capital. 3 picks × 33% each = 100% deployed.
    "per_pick_fraction": 1.0 / 3.0,
    "eval_hour_utc": 14,
    "eval_minute_utc": 30,
}


# ── Data ──────────────────────────────────────────────────────────────────

def _fetch_history(tickers: list[str], period: str = "10y") -> pd.DataFrame:
    """Download daily closes for the universe. Returns a DataFrame indexed
    by date, columns = tickers."""
    out = yf.download(tickers, period=period, interval="1d",
                      progress=False, auto_adjust=False, group_by="ticker")
    cols = {}
    for t in tickers:
        if (t, "Close") in out.columns:
            cols[t] = out[(t, "Close")]
    df = pd.DataFrame(cols).dropna(how="all")
    df.index = pd.to_datetime(df.index, utc=True)
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
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


TRADE_FIELDS = [
    "ts", "side", "ticker", "px", "qty", "momentum_score",
    "pnl_usd", "pnl_pct", "session_id", "config_version",
]


def _ensure_trade_csv():
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in TRADE_FIELDS}
        )


def _write_heartbeat(state: dict, last_rebalance: dict | None) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "xs_momentum",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "current_picks": state.get("current_picks", {}),
        "last_rebalance": last_rebalance,
        "version": PARAMS["version"],
    }, indent=2, default=str))


# ── Backtest ──────────────────────────────────────────────────────────────

def backtest(period: str = "10y") -> dict:
    """Simulate monthly rebalancing over the historical window.

    At each month-end (resampled from daily closes), rank universe by 12-1
    momentum, compare to last month's picks, simulate sells of removed
    picks and buys of new picks at the next month's first available close.
    """
    universe = list(PARAMS["universe"])
    long_lb = PARAMS["long_lookback"]
    short_lb = PARAMS["short_lookback"]

    closes = _fetch_history(universe, period=period)
    if closes.empty:
        return {"error": "no data fetched"}

    # Get month-end indices (last bar of each month)
    closes.index = closes.index.tz_convert("UTC")
    month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
    if len(month_ends) < 2:
        return {"error": "insufficient monthly history"}

    holdings: dict[str, dict] = {}  # ticker -> {entry_px, entry_idx}
    trades: list[dict] = []

    for month_end in month_ends:
        i = closes.index.get_loc(month_end)
        if i <= long_lb + short_lb:
            continue

        # Rank using data ENDING AT month_end (avoid look-ahead)
        per_asset_closes = {
            t: closes[t].iloc[: i + 1].dropna().tolist()
            for t in universe
            if t in closes.columns
        }
        ranked = rank_universe_by_momentum(per_asset_closes,
                                            long_lookback=long_lb,
                                            short_lookback=short_lb)
        if not ranked:
            continue
        picks = select_top_quintile(ranked, fraction=PARAMS["top_quintile_fraction"])
        pick_tickers = {p.ticker for p in picks}

        # Execution happens at the NEXT trading day's close (no look-ahead)
        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        # Sell removed
        for ticker in list(holdings.keys()):
            if ticker not in pick_tickers:
                entry = holdings.pop(ticker)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                trades.append({
                    "ticker": ticker,
                    "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"],
                    "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                    "hold_days": (exec_date - pd.to_datetime(entry["entry_date"])).days,
                })

        # Buy new
        for p in picks:
            if p.ticker in holdings:
                continue
            entry_px = float(closes[p.ticker].iloc[next_idx])
            holdings[p.ticker] = {
                "entry_px": entry_px,
                "entry_date": exec_date.isoformat(),
            }

    # Close any remaining holdings at the final bar
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for ticker, entry in holdings.items():
        exit_px = float(closes[ticker].iloc[final_idx])
        pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
        trades.append({
            "ticker": ticker,
            "entry_date": entry["entry_date"],
            "exit_date": final_date.isoformat(),
            "entry_px": entry["entry_px"],
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
            "hold_days": (final_date - pd.to_datetime(entry["entry_date"])).days,
        })

    if not trades:
        return {"universe_size": len(universe), "trades": 0,
                "note": "no rebalances produced trades"}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)

    # Portfolio equity curve: equal-weight across simultaneous picks.
    # Approximation: compound average per-trade returns (since picks are
    # held in parallel, the fleet return per month is the average of the
    # 3 picks' month return).
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    # Group trades by exit_date to approximate monthly portfolio return
    by_month: dict[str, list[float]] = {}
    for t in trades:
        m = str(t["exit_date"])[:7]
        by_month.setdefault(m, []).append(t["pnl_pct"])
    for m in sorted(by_month):
        ret = sum(by_month[m]) / len(by_month[m]) / 100.0
        eq *= (1.0 + ret)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    try:
        first_dt = min(pd.to_datetime(t["entry_date"]) for t in trades)
        last_dt = max(pd.to_datetime(t["exit_date"]) for t in trades)
        days = max(1, (last_dt - first_dt).days)
        cagr = (eq ** (365.25 / days) - 1.0) * 100.0
    except Exception:
        cagr = 0.0

    return {
        "universe_size": len(universe),
        "trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "portfolio_growth_pct": round((eq - 1.0) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "first_entry": str(min(t["entry_date"] for t in trades)),
        "last_exit": str(max(t["exit_date"] for t in trades)),
        "trades_per_ticker": _count_per_ticker(trades),
    }


def _count_per_ticker(trades: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in trades:
        out[t["ticker"]] = out.get(t["ticker"], 0) + 1
    return out


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--period", default="10y")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)

    if args.backtest:
        stats = backtest(period=args.period)
        print(json.dumps(stats, indent=2, default=str))
        return 0

    if args.check:
        universe = PARAMS["universe"]
        closes = _fetch_history(universe, period="2y")
        per_asset = {
            t: closes[t].dropna().tolist()
            for t in universe if t in closes.columns
        }
        ranked = rank_universe_by_momentum(
            per_asset,
            long_lookback=PARAMS["long_lookback"],
            short_lookback=PARAMS["short_lookback"],
        )
        picks = select_top_quintile(ranked, fraction=PARAMS["top_quintile_fraction"])
        print(json.dumps({
            "as_of": str(closes.index[-1]) if len(closes) else None,
            "ranking": [{"ticker": s.ticker, "score": round(s.score * 100, 2)} for s in ranked],
            "picks": [{"ticker": p.ticker, "score": round(p.score * 100, 2)} for p in picks],
        }, indent=2, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
