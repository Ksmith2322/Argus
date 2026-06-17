"""forge.coint_pairs — runner + backtest for cointegrated pairs.

Modes:
  --backtest   Simulate over historical data, report stats per-pair + portfolio.
  --check      Print current z-score + half-life for each pair; recommend entries.
  --evaluate   One cycle: scan all pairs, take entries/exits, save state.
  --loop       Daemon mode (daily check during US session).
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.cointegration import (
    DEFAULT_PAIRS,
    compute_pair_stats,
    evaluate_pair_signal,
)

IBKR_CLIENT_ID = 122
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "coint_pairs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("coint_pairs")

PARAMS = {
    "version": "v1_20260520",
    "pairs": [list(p) for p in DEFAULT_PAIRS],
    "lookback": 60,          # trading days for z-score window
    "entry_z": 2.0,
    "exit_z": 0.0,           # exit when z crosses 0 (full mean revert)
    "stop_z": 3.0,           # hard stop on |z| >= 3
    "min_half_life_days": 3.0,
    "max_half_life_days": 60.0,
    # Per-pair notional fraction of strategy cap. 8 pairs × 0.15 = 1.2x cap
    # if every pair is on simultaneously (rare — most pairs sit at z ~ 0).
    "per_pair_fraction": 0.15,
}


# ── Data ───────────────────────────────────────────────────────────────────

def _fetch_history(tickers: list[str], period: str = "5y") -> pd.DataFrame:
    out = yf.download(tickers, period=period, interval="1d",
                      progress=False, auto_adjust=False, group_by="ticker")
    cols = {}
    for t in tickers:
        if (t, "Close") in out.columns:
            cols[t] = out[(t, "Close")]
    df = pd.DataFrame(cols).dropna(how="all")
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# ── Backtest ───────────────────────────────────────────────────────────────

def backtest(period: str = "5y") -> dict:
    """Walk forward through history; at each bar, check each pair's
    z-score + half-life and act on entries/exits."""
    all_tickers = sorted({t for pair in PARAMS["pairs"] for t in pair})
    closes = _fetch_history(all_tickers, period=period)
    if closes.empty:
        return {"error": "no data fetched"}

    lookback = PARAMS["lookback"]
    if len(closes) <= lookback + 5:
        return {"error": f"insufficient history (need >{lookback}+5 bars)"}

    # Per-pair state during backtest
    pair_states: dict[str, dict] = {}
    trades: list[dict] = []

    for i in range(lookback, len(closes)):
        for pair in PARAMS["pairs"]:
            ticker_a, ticker_b = pair
            if ticker_a not in closes.columns or ticker_b not in closes.columns:
                continue
            a_series = closes[ticker_a].iloc[:i + 1].dropna().tolist()
            b_series = closes[ticker_b].iloc[:i + 1].dropna().tolist()
            if len(a_series) < lookback or len(b_series) < lookback:
                continue
            stats = compute_pair_stats(ticker_a, ticker_b,
                                       a_series, b_series, lookback=lookback)
            pair_key = f"{ticker_a}_{ticker_b}"
            open_pos = pair_states.get(pair_key)
            has_open = open_pos["side"] if open_pos else ""
            decision = evaluate_pair_signal(
                stats,
                has_open_position=has_open,
                entry_z=PARAMS["entry_z"],
                exit_z=PARAMS["exit_z"],
                stop_z=PARAMS["stop_z"],
                min_half_life_days=PARAMS["min_half_life_days"],
                max_half_life_days=PARAMS["max_half_life_days"],
            )

            if decision.action == "ENTER_LONG_SPREAD":
                pair_states[pair_key] = {
                    "side": "long", "entry_idx": i,
                    "entry_spread": stats.spread_now,
                    "entry_z": stats.zscore, "beta": stats.beta,
                    "entry_a": float(closes[ticker_a].iloc[i]),
                    "entry_b": float(closes[ticker_b].iloc[i]),
                }
            elif decision.action == "ENTER_SHORT_SPREAD":
                pair_states[pair_key] = {
                    "side": "short", "entry_idx": i,
                    "entry_spread": stats.spread_now,
                    "entry_z": stats.zscore, "beta": stats.beta,
                    "entry_a": float(closes[ticker_a].iloc[i]),
                    "entry_b": float(closes[ticker_b].iloc[i]),
                }
            elif decision.action == "EXIT" and open_pos:
                exit_a = float(closes[ticker_a].iloc[i])
                exit_b = float(closes[ticker_b].iloc[i])
                beta = open_pos["beta"]
                # P&L per pair-unit: long the spread = +1 share of A, -beta of B
                # short the spread = -1 share of A, +beta of B
                if open_pos["side"] == "long":
                    pnl_per_unit = (exit_a - open_pos["entry_a"]) - beta * (exit_b - open_pos["entry_b"])
                else:
                    pnl_per_unit = -(exit_a - open_pos["entry_a"]) + beta * (exit_b - open_pos["entry_b"])
                # Normalize by entry notional (avg of legs) so PnL is a %
                entry_notional = (open_pos["entry_a"] + abs(beta) * open_pos["entry_b"]) / 2.0
                pnl_pct = (pnl_per_unit / entry_notional) * 100.0 if entry_notional > 0 else 0.0
                trades.append({
                    "pair": pair_key,
                    "side": open_pos["side"],
                    "entry_ts": closes.index[open_pos["entry_idx"]].isoformat(),
                    "exit_ts": closes.index[i].isoformat(),
                    "entry_z": open_pos["entry_z"],
                    "exit_z": stats.zscore,
                    "beta": open_pos["beta"],
                    "pnl_pct": pnl_pct,
                    "hold_bars": i - open_pos["entry_idx"],
                    "exit_reason": decision.reason,
                })
                pair_states.pop(pair_key, None)

    if not trades:
        return {
            "pairs_tested": len(PARAMS["pairs"]),
            "trades": 0,
            "note": "no qualifying entries fired",
        }

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)

    # Equity curve assumes per-pair sizing fraction; pairs compounded sequentially
    # (approximation — some are simultaneous in practice)
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0 * PARAMS["per_pair_fraction"])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    try:
        first_dt = min(pd.to_datetime(t["entry_ts"]) for t in trades)
        last_dt = max(pd.to_datetime(t["exit_ts"]) for t in trades)
        days = max(1, (last_dt - first_dt).days)
        cagr = (eq ** (365.25 / days) - 1.0) * 100.0
    except Exception:
        cagr = 0.0

    # Per-pair breakdown
    per_pair: dict[str, int] = {}
    per_pair_pnl: dict[str, float] = {}
    for t in trades:
        p = t["pair"]
        per_pair[p] = per_pair.get(p, 0) + 1
        per_pair_pnl[p] = per_pair_pnl.get(p, 0.0) + t["pnl_pct"]

    return {
        "pairs_tested": len(PARAMS["pairs"]),
        "trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "total_pct_unscaled": round(sum(pnls), 2),
        "portfolio_growth_pct_scaled": round((eq - 1.0) * 100, 2),
        "cagr_pct_scaled": round(cagr, 2),
        "max_drawdown_pct_scaled": round(max_dd * 100, 2),
        "trades_per_pair": per_pair,
        "pnl_pct_per_pair": {k: round(v, 2) for k, v in per_pair_pnl.items()},
    }


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--period", default="5y")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.backtest:
        stats = backtest(period=args.period)
        print(json.dumps(stats, indent=2, default=str))
        return 0

    if args.check:
        all_tickers = sorted({t for pair in PARAMS["pairs"] for t in pair})
        closes = _fetch_history(all_tickers, period="2y")
        out_rows = []
        for pair in PARAMS["pairs"]:
            ticker_a, ticker_b = pair
            if ticker_a not in closes.columns or ticker_b not in closes.columns:
                continue
            a_series = closes[ticker_a].dropna().tolist()
            b_series = closes[ticker_b].dropna().tolist()
            stats = compute_pair_stats(ticker_a, ticker_b,
                                       a_series, b_series,
                                       lookback=PARAMS["lookback"])
            decision = evaluate_pair_signal(
                stats, has_open_position="",
                entry_z=PARAMS["entry_z"], exit_z=PARAMS["exit_z"],
                stop_z=PARAMS["stop_z"],
                min_half_life_days=PARAMS["min_half_life_days"],
                max_half_life_days=PARAMS["max_half_life_days"],
            )
            out_rows.append({
                "pair": f"{ticker_a}/{ticker_b}",
                "zscore": round(stats.zscore, 3),
                "half_life_days": round(stats.half_life_days, 1),
                "beta": round(stats.beta, 4),
                "action": decision.action,
                "reason": decision.reason,
            })
        print(json.dumps(out_rows, indent=2, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
