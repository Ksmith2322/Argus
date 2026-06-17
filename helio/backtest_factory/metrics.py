"""Trade-list → metrics. The metric set is the screen filter for v1.

A trade is a dict with at minimum:
    pnl_pct      float, percent return on entry price (+ for win, - for loss)
    bars_held    int, number of bars between entry and exit
    direction    "LONG" or "SHORT"
    exit_reason  "stop" | "target" | "timeout" | "signal" | "eod"
    entry_dt     pd.Timestamp
    exit_dt      pd.Timestamp

compute_metrics returns a dict with the screen fields plus a `details`
sub-dict carrying exit-reason breakdown and bookkeeping.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


def compute_metrics(
    trades: List[Dict],
    *,
    bar_seconds: Optional[int] = None,
    bars_per_year: float = 252.0,
) -> Dict:
    """Reduce a trade list to a screen-friendly metric set.

    bar_seconds is unused in v1 (kept for forward-compat with intraday
    Sharpe annualization). bars_per_year defaults to 252 (daily bars).

    Returns dict with the keys used by the factory screen:
        n              int
        pf             float, profit factor (gross_win / gross_loss)
        wr             float, win rate (0..1)
        cagr_pct       float, naive compounded annual return
        max_dd_pct     float, max drawdown from running-peak equity
        sharpe         float, per-trade Sharpe annualized to bars_per_year
        avg_pct        float, mean trade return
        median_pct     float, median trade return
        expectancy_pct float, avg_win*wr + avg_loss*(1-wr)
        avg_bars       float
        exposure_pct   float, fraction of available bars in a position
        exits          dict {reason: count}
    """
    if not trades:
        return _empty_metrics()

    pnls = np.array([float(t["pnl_pct"]) for t in trades], dtype=float)
    bars_held = np.array([int(t.get("bars_held", 0)) for t in trades], dtype=int)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gw = float(wins.sum())
    gl = float(abs(losses.sum()))
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    wr = float(len(wins)) / float(len(pnls))

    # Equity curve (additive percent returns; not compounded — v1 simplicity)
    equity = np.concatenate(([0.0], np.cumsum(pnls)))
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(drawdown.max())

    # CAGR: span trade times if available, else assume bars_per_year cadence
    cagr = _approx_cagr(trades, total_pct=float(pnls.sum()), bars_per_year=bars_per_year)

    # Sharpe: per-trade std normalized; annualize by sqrt(trades_per_year)
    if len(pnls) > 1 and pnls.std(ddof=0) > 0:
        sharpe = float(pnls.mean() / pnls.std(ddof=0))
        # Estimate trades-per-year using actual span if we have timestamps
        tpy = _trades_per_year(trades, fallback=float(len(pnls)))
        sharpe *= math.sqrt(tpy)
    else:
        sharpe = 0.0

    exits: Dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        exits[r] = exits.get(r, 0) + 1

    expectancy = (
        (float(wins.mean()) * wr if len(wins) else 0.0)
        + (float(losses.mean()) * (1.0 - wr) if len(losses) else 0.0)
    )

    return {
        "n": int(len(pnls)),
        "pf": round(pf, 4) if math.isfinite(pf) else None,
        "wr": round(wr, 4),
        "cagr_pct": round(cagr, 4) if cagr is not None else None,
        "max_dd_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "avg_pct": round(float(pnls.mean()), 4),
        "median_pct": round(float(np.median(pnls)), 4),
        "expectancy_pct": round(expectancy, 4),
        "avg_bars": round(float(bars_held.mean()), 2),
        "exposure_pct": None,  # populated by engine when total bars known
        "exits": exits,
    }


def _empty_metrics() -> Dict:
    return {
        "n": 0,
        "pf": None,
        "wr": 0.0,
        "cagr_pct": 0.0,
        "max_dd_pct": 0.0,
        "sharpe": 0.0,
        "avg_pct": 0.0,
        "median_pct": 0.0,
        "expectancy_pct": 0.0,
        "avg_bars": 0.0,
        "exposure_pct": 0.0,
        "exits": {},
    }


def _approx_cagr(trades: List[Dict], *, total_pct: float, bars_per_year: float) -> Optional[float]:
    """Naive CAGR: assume `total_pct` accumulated over the trade span. If
    we have entry/exit timestamps, use the actual elapsed years."""
    if not trades:
        return 0.0
    try:
        first = pd.Timestamp(trades[0]["entry_dt"])
        last = pd.Timestamp(trades[-1]["exit_dt"])
        years = max((last - first).total_seconds() / (365.25 * 86400), 1.0 / 365.25)
    except (KeyError, ValueError, TypeError):
        years = max(len(trades) / bars_per_year, 1.0 / 365.25)
    # Simple-return CAGR over the span
    ratio = 1.0 + total_pct / 100.0
    if ratio <= 0:
        return -100.0
    return (ratio ** (1.0 / years) - 1.0) * 100.0


def _trades_per_year(trades: List[Dict], *, fallback: float) -> float:
    if not trades:
        return fallback
    try:
        first = pd.Timestamp(trades[0]["entry_dt"])
        last = pd.Timestamp(trades[-1]["exit_dt"])
        years = max((last - first).total_seconds() / (365.25 * 86400), 1.0 / 365.25)
        return len(trades) / years
    except (KeyError, ValueError, TypeError):
        return fallback
