"""
Tori Trades Runner -- 4H Trendline Swing Trading System
=========================================================
Per Tori Trades rulebook: top-down trendline analysis (monthly->4H),
three setups (bounce, break, break & retest), Action Line + Safety Line.
Instruments: PL=F, CL=F, GC=F, YM=F (commodities/futures).

Modes:
  --backtest    Download 4H data, walk-forward backtest
  --scan        One-shot scan for current setups
  --loop        Continuous scan every 4 hours
  --live        IBKR execution placeholder

Usage:
  python -m forge.tori.runner --backtest
  python -m forge.tori.runner --scan
  python -m forge.tori.runner --loop
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "tori"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from forge.logging_setup import setup_logging
log = setup_logging("tori")

RESEARCH_ONLY = False  # 2026-04-26: flipped to False so tori actually trades.
# Was True since 2026-04-17 (sizing module not aligned to fleet_sizing.json).
# Notional caps in fleet_sizing.json v6 (1.0× anchor for stock/etf, 5× for micro_future)
# will clamp any oversizing. User wants live observations by end of May for kill/keep.
# Still excluded from fleet_perf_summary USD roll-up.

# Scope_down (2026-04-20): Tori validated subset is name=='Dow' AND direction=='LONG'
# (see strategy_confidence/tori_validated.json: PF 3.65 / P(exp>0)=1.00 / WF 4/4
# on n=78). Dow PF 2.48 dominates PL 1.52 / CL 1.57 / GC 1.84; SHORT PF 1.74 vs
# LONG 3.36 — US-equity upward drift asymmetry. Flip SCOPE_DOW_LONG_ONLY to False
# for research runs reconstructing the union. Narrow TICKERS to YM=F to match.
SCOPE_DOW_LONG_ONLY = True

# Instruments
if SCOPE_DOW_LONG_ONLY:
    TICKERS = ["YM=F"]
else:
    TICKERS = ["PL=F", "CL=F", "GC=F", "YM=F"]
TICKER_NAMES = {
    "PL=F": "Platinum",
    "CL=F": "Crude Oil",
    "GC=F": "Gold",
    "YM=F": "Dow",
}

# Fallback proxies when futures data is unavailable
PROXIES = {
    "PL=F": "PPLT",     # Platinum ETF
    "CL=F": "USO",      # Crude Oil ETF
    "GC=F": "GLD",      # Gold ETF
    "YM=F": "DIA",      # Dow ETF
}

IBKR_CLIENT_IDS = {"PL=F": 104, "CL=F": 105, "GC=F": 106, "YM=F": 107}

# Sizing
from forge.tori.sizing import POINT_VALUES, TICKER_TO_MICRO, compute_tori_size

def _broker_anchor_or_raise() -> float:
    """Resolve equity from the live broker. No hardcoded fallback —
    aligned with 2026-04-23 architecture. Called lazily so that module
    import doesn't fail if the broker is cold."""
    from helio.fleet_sizing import get_sizing_anchor_usd
    return get_sizing_anchor_usd()


BASE_RISK_PCT = 0.015  # 1.5%
MIN_RR = 2.0  # 2R minimum target

# 2026-04-19: 2-year backtest across all 4 instruments showed bounce setups
# lose on all 4 (Platinum/Crude/Gold/Dow) while break setups profit on all 4.
# Current detector produces few signals that cluster around line failures
# (fast -1R stops, avg hold 1-11 bars). Disabled until the detector is
# rebuilt. Flip to True to re-enable for comparison runs.
TORI_ENABLE_BOUNCE = False

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def download_data(
    ticker: str,
    period: str = "6mo",
    interval: str = "1h",
) -> pd.DataFrame:
    """
    Download data via yfinance.
    yfinance doesn't support 4H interval directly, so we download 1H
    and resample to 4H.
    """
    import yfinance as yf

    log.info(f"Downloading {ticker} | period={period} interval={interval}")
    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        proxy = PROXIES.get(ticker)
        if proxy:
            log.warning(f"No data for {ticker}, trying {proxy} as proxy")
            df = yf.download(proxy, period=period, interval=interval, progress=False)

    if df.empty:
        raise RuntimeError(f"Could not download data for {ticker}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("US/Eastern")
    df = df.sort_index()

    log.info(f"  {ticker}: {len(df)} 1H bars from {df.index[0]} to {df.index[-1]}")
    return df


def resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H bars to 4H bars."""
    ohlc = df.resample("4h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open"])
    return ohlc


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add ATR column using Wilder's smoothing."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    df["ATR"] = atr
    return df


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------


def run_backtest(
    datasets: dict[str, pd.DataFrame],
    equity: float | None = None,
) -> list[dict]:
    """
    Walk-forward backtest on 4H bars:
    1. For each bar, compute trendlines from prior data
    2. Score trendlines, check for setups on A/A+ only
    3. Entry at 4H close, stop beyond Safety Line + 0.3 ATR
    4. Target 2R min, trail along Safety Line
    5. Exit: stop hit, Safety Line broken, or target hit
    6. Max 1 position per instrument at a time
    7. Risk 1.5% equity per trade
    """
    from forge.tori.trendlines import (
        find_swing_highs, find_swing_lows,
        fit_ascending_trendline, fit_descending_trendline,
        score_trendline_quality, _trendline_value_at,
        check_bounce, check_break, check_retest,
        _find_opposing_safety, _find_trailing_trendline_stop,
        _is_rejection_candle, _deduplicate_trendlines,
    )

    all_trades = []
    current_equity = equity if equity is not None else _broker_anchor_or_raise()
    equity_curve = [current_equity]

    # Minimum bars needed before we start looking for setups
    MIN_LOOKBACK = 60  # ~10 days of 4H bars

    for ticker, df in datasets.items():
        micro = TICKER_TO_MICRO.get(ticker, ticker)
        pv = POINT_VALUES.get(micro, 1.0)
        name = TICKER_NAMES.get(ticker, ticker)

        log.info(f"Backtesting {name} ({ticker}): {len(df)} 4H bars")

        open_trade: Optional[dict] = None
        broken_lines: list[dict] = []  # track broken trendlines for retest

        for i in range(MIN_LOOKBACK, len(df)):
            atr = df["ATR"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            # --- Manage open trade ---
            if open_trade is not None:
                bar = df.iloc[i]
                trade = open_trade

                # Update trailing Safety Line.
                # v2 (2026-04-19): angled trend-line trail per Tori's "steeper
                # trend line" method. Falls back to the legacy flat-swing
                # trail only when not enough post-entry swings exist yet to
                # fit a valid angled line. See forge/tori/TORITRADES_RULEBOOK.md
                # v2 updates for the audit that motivated this change.
                pnl_points = 0
                entry_bar = trade.get("entry_bar", i)
                if trade["direction"] == "LONG":
                    pnl_points = bar["Close"] - trade["entry_price"]
                    new_safety = _find_trailing_trendline_stop(
                        df, i, entry_bar, "LONG", atr
                    )
                    if new_safety is None:
                        new_safety = _find_opposing_safety(df.iloc[:i+1], i, "LONG", atr)
                    if new_safety and new_safety > trade["stop_price"]:
                        trade["stop_price"] = new_safety

                    # Check stop
                    if bar["Low"] <= trade["stop_price"]:
                        exit_price = trade["stop_price"]
                        pnl_points = exit_price - trade["entry_price"]
                        _close_trade(trade, exit_price, pnl_points, i, df, "stop_hit",
                                     all_trades, pv, current_equity)
                        current_equity += pnl_points * pv * trade.get("contracts", 1)
                        equity_curve.append(current_equity)
                        open_trade = None
                        continue

                else:  # SHORT
                    pnl_points = trade["entry_price"] - bar["Close"]
                    new_safety = _find_trailing_trendline_stop(
                        df, i, entry_bar, "SHORT", atr
                    )
                    if new_safety is None:
                        new_safety = _find_opposing_safety(df.iloc[:i+1], i, "SHORT", atr)
                    if new_safety and new_safety < trade["stop_price"]:
                        trade["stop_price"] = new_safety

                    if bar["High"] >= trade["stop_price"]:
                        exit_price = trade["stop_price"]
                        pnl_points = trade["entry_price"] - exit_price
                        _close_trade(trade, exit_price, pnl_points, i, df, "stop_hit",
                                     all_trades, pv, current_equity)
                        current_equity += pnl_points * pv * trade.get("contracts", 1)
                        equity_curve.append(current_equity)
                        open_trade = None
                        continue

                # Check 2R target
                risk_points = abs(trade["entry_price"] - trade["initial_stop"])
                if risk_points > 0 and pnl_points / risk_points >= MIN_RR:
                    # Trail tighter -- if we already have 2R, use tighter safety
                    pass  # let the trail run

                # Max hold: 120 bars (~20 trading days = ~4 weeks)
                if i - trade["entry_bar"] >= 120:
                    exit_price = float(bar["Close"])
                    if trade["direction"] == "LONG":
                        pnl_points = exit_price - trade["entry_price"]
                    else:
                        pnl_points = trade["entry_price"] - exit_price
                    _close_trade(trade, exit_price, pnl_points, i, df, "max_hold",
                                 all_trades, pv, current_equity)
                    current_equity += pnl_points * pv * trade.get("contracts", 1)
                    equity_curve.append(current_equity)
                    open_trade = None
                    continue

                continue  # still in trade, skip scanning

            # --- No open trade: scan for setups ---
            window = df.iloc[max(0, i - MIN_LOOKBACK) : i + 1].copy()
            if len(window) < 30:
                continue

            swing_highs = find_swing_highs(window)
            swing_lows = find_swing_lows(window)

            # Build trendlines
            trendlines = []
            for chunk_size in [6, 10, 15]:
                for start in range(0, max(len(swing_lows) - chunk_size + 1, 1), max(chunk_size // 2, 1)):
                    chunk = swing_lows[start : start + chunk_size]
                    tl = fit_ascending_trendline(chunk)
                    if tl and tl["touches"] >= 2:
                        trendlines.append(tl)

                for start in range(0, max(len(swing_highs) - chunk_size + 1, 1), max(chunk_size // 2, 1)):
                    chunk = swing_highs[start : start + chunk_size]
                    tl = fit_descending_trendline(chunk)
                    if tl and tl["touches"] >= 2:
                        trendlines.append(tl)

            trendlines = _deduplicate_trendlines(trendlines, window)

            # Score and filter to A/A+ only
            best_setup = None
            best_grade_rank = 99

            grade_rank = {"A+": 0, "A": 1, "B": 2, "C": 3}

            for tl in trendlines:
                quality = score_trendline_quality(tl, window)
                if not quality["valid"]:
                    continue
                if quality["grade"] not in ("A+", "A"):
                    continue

                g_rank = grade_rank[quality["grade"]]

                # Map trendline indices from window to df
                offset = max(0, i - MIN_LOOKBACK)
                tl_mapped = {**tl, "start_idx": tl["start_idx"] + offset,
                             "end_idx": tl["end_idx"] + offset}

                # Check bounce (2026-04-19 validation: our bounce detector
                # produces a handful of signals per 2-year window and all
                # lose across all 4 instruments, while break setups profit
                # on all 4. Likely a detector/stop-logic issue, not a
                # validation of her teaching. Disabled by default until
                # the detector is rebuilt.)
                if TORI_ENABLE_BOUNCE:
                    bounce = check_bounce(df, tl_mapped, i, atr)
                    if bounce and g_rank < best_grade_rank:
                        bounce["quality"] = quality
                        best_setup = bounce
                        best_grade_rank = g_rank

                # Check break
                brk = check_break(df, tl_mapped, i, atr)
                if brk and g_rank < best_grade_rank:
                    brk["quality"] = quality
                    brk["broken_tl"] = tl_mapped
                    best_setup = brk
                    best_grade_rank = g_rank
                    # Track for potential retest
                    broken_lines.append({
                        "tl": tl_mapped,
                        "break_bar": i,
                        "direction": brk["direction"],
                    })

            # Check retests on previously broken lines
            for bl in broken_lines[-5:]:  # only recent breaks
                retest = check_retest(df, bl["tl"], i, atr, bl["break_bar"])
                if retest:
                    q = score_trendline_quality(bl["tl"], df.iloc[max(0, bl["tl"]["start_idx"]):min(len(df), bl["tl"]["end_idx"]+1)])
                    if q["valid"] and q["grade"] in ("A+", "A"):
                        g_rank = grade_rank[q["grade"]]
                        if g_rank <= best_grade_rank:
                            retest["quality"] = q
                            best_setup = retest
                            best_grade_rank = g_rank

            if best_setup is None:
                continue

            if SCOPE_DOW_LONG_ONLY and str(best_setup.get("direction", "")).upper() != "LONG":
                continue

            # --- Open trade ---
            entry_price = best_setup["entry_price"]
            stop_price = best_setup["safety_line"]
            grade = best_setup["quality"]["grade"]
            setup_type = best_setup["setup"]

            sizing = compute_tori_size(
                equity=current_equity,
                entry_price=entry_price,
                stop_price=stop_price,
                grade=grade,
                setup_type=setup_type,
                point_value=pv,
            )

            if sizing["skip"]:
                continue

            open_trade = {
                "ticker": ticker,
                "name": name,
                "direction": best_setup["direction"],
                "setup": setup_type,
                "grade": grade,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "initial_stop": stop_price,
                "entry_bar": i,
                "entry_date": str(df.index[i]),
                "contracts": sizing["contracts"],
                "risk_usd": sizing["risk_usd"],
                "action_line": best_setup.get("action_line"),
                "safety_line": best_setup.get("safety_line"),
            }

        # Close any remaining open trade at end of data
        if open_trade is not None:
            last_bar = df.iloc[-1]
            exit_price = float(last_bar["Close"])
            if open_trade["direction"] == "LONG":
                pnl_points = exit_price - open_trade["entry_price"]
            else:
                pnl_points = open_trade["entry_price"] - exit_price
            _close_trade(open_trade, exit_price, pnl_points, len(df) - 1, df,
                         "end_of_data", all_trades, pv, current_equity)
            current_equity += pnl_points * pv * open_trade.get("contracts", 1)
            equity_curve.append(current_equity)
            open_trade = None

    return all_trades, equity_curve, current_equity


def _close_trade(
    trade: dict, exit_price: float, pnl_points: float,
    exit_bar: int, df: pd.DataFrame, exit_reason: str,
    all_trades: list, point_value: float, equity: float,
):
    """Finalize and record a closed trade."""
    contracts = trade.get("contracts", 1)
    pnl_usd = pnl_points * point_value * contracts
    risk_points = abs(trade["entry_price"] - trade["initial_stop"])
    r_multiple = pnl_points / risk_points if risk_points > 0 else 0

    record = {
        "ticker": trade["ticker"],
        "name": trade["name"],
        "direction": trade["direction"],
        "setup": trade["setup"],
        "grade": trade["grade"],
        "entry_price": round(trade["entry_price"], 4),
        "exit_price": round(exit_price, 4),
        "stop_price": round(trade["initial_stop"], 4),
        "pnl_points": round(pnl_points, 4),
        "pnl_usd": round(pnl_usd, 2),
        "r_multiple": round(r_multiple, 2),
        "contracts": contracts,
        "entry_date": trade["entry_date"],
        "exit_date": str(df.index[exit_bar]),
        "hold_bars": exit_bar - trade["entry_bar"],
        "exit_reason": exit_reason,
    }
    all_trades.append(record)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_backtest_report(
    trades: list[dict],
    equity_curve: list[float],
    final_equity: float,
    starting_equity: float,
):
    """Print comprehensive backtest results."""
    print("\n" + "=" * 80)
    print("  TORI TRADES -- 4H TRENDLINE SWING SYSTEM -- BACKTEST REPORT")
    print("=" * 80)

    if not trades:
        print("\n  No trades generated.\n")
        return

    df_trades = pd.DataFrame(trades)

    # --- Overall ---
    total = len(df_trades)
    wins = df_trades[df_trades["pnl_usd"] > 0]
    losses = df_trades[df_trades["pnl_usd"] <= 0]
    win_rate = len(wins) / total * 100 if total > 0 else 0

    gross_profit = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    net_pnl = df_trades["pnl_usd"].sum()
    avg_r = df_trades["r_multiple"].mean()
    avg_hold = df_trades["hold_bars"].mean()

    # Max drawdown from equity curve
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak * 100
    max_dd = dd.max()

    print(f"\n  OVERALL PERFORMANCE")
    print(f"  {'-' * 50}")
    print(f"  Total Trades:       {total}")
    print(f"  Win Rate:           {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor:      {pf:.2f}")
    print(f"  Net P&L:            ${net_pnl:,.2f}")
    print(f"  Avg R:R:            {avg_r:.2f}R")
    print(f"  Avg Hold (4H bars): {avg_hold:.1f}  (~{avg_hold/6:.1f} days)")
    print(f"  Starting Equity:    ${starting_equity:,.2f}")
    print(f"  Final Equity:       ${final_equity:,.2f}")
    print(f"  Return:             {(final_equity/starting_equity - 1)*100:.1f}%")
    print(f"  Max Drawdown:       {max_dd:.1f}%")

    # --- Per instrument ---
    print(f"\n  PER INSTRUMENT")
    print(f"  {'-' * 50}")
    print(f"  {'Instrument':<14} {'Trades':>6} {'Win%':>6} {'PF':>6} {'Avg R':>6} {'P&L':>10}")
    print(f"  {'-' * 50}")

    for ticker in df_trades["ticker"].unique():
        t = df_trades[df_trades["ticker"] == ticker]
        tw = t[t["pnl_usd"] > 0]
        tl = t[t["pnl_usd"] <= 0]
        t_wr = len(tw) / len(t) * 100 if len(t) > 0 else 0
        t_gp = tw["pnl_usd"].sum() if len(tw) > 0 else 0
        t_gl = abs(tl["pnl_usd"].sum()) if len(tl) > 0 else 0
        t_pf = t_gp / t_gl if t_gl > 0 else float("inf")
        t_ar = t["r_multiple"].mean()
        t_pnl = t["pnl_usd"].sum()
        name = TICKER_NAMES.get(ticker, ticker)
        print(f"  {name:<14} {len(t):>6} {t_wr:>5.1f}% {t_pf:>6.2f} {t_ar:>5.2f}R ${t_pnl:>9,.2f}")

    # --- By setup type ---
    print(f"\n  BY SETUP TYPE")
    print(f"  {'-' * 50}")
    print(f"  {'Setup':<16} {'Trades':>6} {'Win%':>6} {'PF':>6} {'Avg R':>6} {'P&L':>10}")
    print(f"  {'-' * 50}")

    for setup in ["bounce", "break", "break_retest"]:
        t = df_trades[df_trades["setup"] == setup]
        if len(t) == 0:
            print(f"  {setup:<16} {0:>6}     --      --      --          --")
            continue
        tw = t[t["pnl_usd"] > 0]
        tl = t[t["pnl_usd"] <= 0]
        t_wr = len(tw) / len(t) * 100
        t_gp = tw["pnl_usd"].sum() if len(tw) > 0 else 0
        t_gl = abs(tl["pnl_usd"].sum()) if len(tl) > 0 else 0
        t_pf = t_gp / t_gl if t_gl > 0 else float("inf")
        t_ar = t["r_multiple"].mean()
        t_pnl = t["pnl_usd"].sum()
        print(f"  {setup:<16} {len(t):>6} {t_wr:>5.1f}% {t_pf:>6.2f} {t_ar:>5.2f}R ${t_pnl:>9,.2f}")

    # --- By trendline grade ---
    print(f"\n  BY TRENDLINE GRADE")
    print(f"  {'-' * 50}")
    print(f"  {'Grade':<10} {'Trades':>6} {'Win%':>6} {'PF':>6} {'Avg R':>6} {'P&L':>10}")
    print(f"  {'-' * 50}")

    for grade in ["A+", "A", "B"]:
        t = df_trades[df_trades["grade"] == grade]
        if len(t) == 0:
            print(f"  {grade:<10} {0:>6}     --      --      --          --")
            continue
        tw = t[t["pnl_usd"] > 0]
        tl_g = t[t["pnl_usd"] <= 0]
        t_wr = len(tw) / len(t) * 100
        t_gp = tw["pnl_usd"].sum() if len(tw) > 0 else 0
        t_gl = abs(tl_g["pnl_usd"].sum()) if len(tl_g) > 0 else 0
        t_pf = t_gp / t_gl if t_gl > 0 else float("inf")
        t_ar = t["r_multiple"].mean()
        t_pnl = t["pnl_usd"].sum()
        print(f"  {grade:<10} {len(t):>6} {t_wr:>5.1f}% {t_pf:>6.2f} {t_ar:>5.2f}R ${t_pnl:>9,.2f}")

    # --- Best / worst trades ---
    print(f"\n  BEST TRADES")
    print(f"  {'-' * 50}")
    top = df_trades.nlargest(5, "pnl_usd")
    for _, row in top.iterrows():
        print(f"  {row['name']:<12} {row['direction']:<6} {row['setup']:<14} "
              f"{row['r_multiple']:>5.1f}R  ${row['pnl_usd']:>9,.2f}  {row['entry_date'][:10]}")

    print(f"\n  WORST TRADES")
    print(f"  {'-' * 50}")
    bottom = df_trades.nsmallest(5, "pnl_usd")
    for _, row in bottom.iterrows():
        print(f"  {row['name']:<12} {row['direction']:<6} {row['setup']:<14} "
              f"{row['r_multiple']:>5.1f}R  ${row['pnl_usd']:>9,.2f}  {row['entry_date'][:10]}")

    # --- All trades list ---
    print(f"\n  ALL TRADES")
    print(f"  {'-' * 80}")
    print(f"  {'#':>3} {'Instrument':<12} {'Dir':<6} {'Setup':<14} {'Grade':<5} "
          f"{'Entry':>10} {'Exit':>10} {'R':>6} {'P&L':>10} {'Bars':>5} {'Exit Reason':<12}")
    print(f"  {'-' * 80}")
    for idx, row in df_trades.iterrows():
        print(f"  {idx+1:>3} {row['name']:<12} {row['direction']:<6} {row['setup']:<14} "
              f"{row['grade']:<5} {row['entry_price']:>10.2f} {row['exit_price']:>10.2f} "
              f"{row['r_multiple']:>5.1f}R ${row['pnl_usd']:>9,.2f} {row['hold_bars']:>5} "
              f"{row['exit_reason']:<12}")

    print(f"\n{'=' * 80}\n")


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------


def run_scan(datasets: dict[str, pd.DataFrame]):
    """One-shot scan: find current A/A+ trendlines and potential setups."""
    from forge.tori.trendlines import (
        find_swing_highs, find_swing_lows,
        fit_ascending_trendline, fit_descending_trendline,
        score_trendline_quality, _trendline_value_at,
        check_bounce, check_break,
        _deduplicate_trendlines,
    )

    print("\n" + "=" * 80)
    print("  TORI TRADES -- LIVE SCAN")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)

    for ticker, df in datasets.items():
        name = TICKER_NAMES.get(ticker, ticker)
        last_bar = len(df) - 1
        atr = float(df["ATR"].iloc[last_bar])
        close = float(df["Close"].iloc[last_bar])

        print(f"\n  {name} ({ticker})  --  Close: {close:.2f}  ATR: {atr:.2f}")
        print(f"  {'-' * 60}")

        swing_highs = find_swing_highs(df)
        swing_lows = find_swing_lows(df)

        trendlines = []
        for chunk_size in [6, 10, 15]:
            for start in range(0, max(len(swing_lows) - chunk_size + 1, 1), max(chunk_size // 2, 1)):
                chunk = swing_lows[start : start + chunk_size]
                tl = fit_ascending_trendline(chunk)
                if tl and tl["touches"] >= 2:
                    trendlines.append(tl)

            for start in range(0, max(len(swing_highs) - chunk_size + 1, 1), max(chunk_size // 2, 1)):
                chunk = swing_highs[start : start + chunk_size]
                tl = fit_descending_trendline(chunk)
                if tl and tl["touches"] >= 2:
                    trendlines.append(tl)

        trendlines = _deduplicate_trendlines(trendlines, df)

        found_any = False
        for tl in trendlines:
            quality = score_trendline_quality(tl, df)
            if not quality["valid"] or quality["grade"] not in ("A+", "A"):
                continue

            tl_val = _trendline_value_at(tl, last_bar)
            dist = abs(close - tl_val)
            dist_pct = dist / close * 100

            direction_label = "ascending" if tl["direction"] == "ascending" else "descending"
            status = ""

            # Check for active setups (bounce gated by TORI_ENABLE_BOUNCE
            # after the 2026-04-19 validation showed bounce lost on every
            # instrument while break profited on every instrument)
            bounce = check_bounce(df, tl, last_bar, atr) if TORI_ENABLE_BOUNCE else None
            brk = check_break(df, tl, last_bar, atr)

            if bounce:
                status = f"BOUNCE SIGNAL -- {bounce['direction']}"
            elif brk and dist <= 3.0 * atr:
                status = f"BREAK SIGNAL -- {brk['direction']}"
            elif dist <= 1.0 * atr:
                status = "WATCH -- price approaching line"
            elif dist <= 2.0 * atr:
                status = "monitoring"
            else:
                continue  # skip distant lines in scan output

            emoji_grade = quality["grade"]
            print(f"    {emoji_grade} {direction_label} TL with {quality['touches']} touches, "
                  f"spacing {quality['avg_spacing']:.0f} bars, "
                  f"price {dist:.1f} pts ({dist_pct:.1f}%) from line -- {status}")
            found_any = True

        if not found_any:
            print(f"    No A/A+ trendlines found")

    print(f"\n{'=' * 80}\n")


# ---------------------------------------------------------------------------
# Loop mode
# ---------------------------------------------------------------------------


def run_loop(datasets: dict[str, pd.DataFrame]):
    """Continuous scan every 4 hours."""
    print("\n  Tori Trades loop mode -- scanning every 4 hours")
    print("  Press Ctrl+C to stop\n")

    heartbeat_path = LOG_DIR / "heartbeat.json"
    signals_path = LOG_DIR / "signals.csv"

    while True:
        try:
            # Re-download fresh data
            fresh = {}
            for ticker in TICKERS:
                try:
                    df = download_data(ticker, period="3mo", interval="1h")
                    df = resample_to_4h(df)
                    df = compute_atr(df)
                    fresh[ticker] = df
                except Exception as e:
                    log.warning(f"Failed to download {ticker}: {e}")

            if fresh:
                run_scan(fresh)

            # Write heartbeat
            heartbeat_path.write_text(json.dumps({
                "system": "tori",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instruments": list(fresh.keys()),
                "status": "ok",
                "mode": "research_only",  # 2026-04-17: not promotion candidate
            }, indent=2))

            log.info("Sleeping 4 hours until next scan...")
            time.sleep(4 * 3600)

        except KeyboardInterrupt:
            log.info("Loop stopped by user")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(60)


# ---------------------------------------------------------------------------
# Live mode (placeholder)
# ---------------------------------------------------------------------------


def run_live():
    """Live IBKR paper execution. Re-runs the scan logic on a 4hr cycle, captures
    BREAK signals with structured data, submits brackets via signal_executor.

    2026-04-24: replaces the placeholder. Trades MYM (micro Dow) and similar
    micros on IBKR paper, client_id=115. ATR-based bracket: 1× stop, 2× target
    (Tori conservative R:R from validated scope_down).
    """
    from forge.tori.trendlines import (
        find_swing_highs, find_swing_lows,
        fit_ascending_trendline, fit_descending_trendline,
        score_trendline_quality, _trendline_value_at,
        check_bounce, check_break,
        _deduplicate_trendlines,
    )
    from helio import ibkr_execution as ibkr
    from helio import signal_executor as sx
    from helio.fleet_sizing import compute_risk_usd, max_notional_usd

    IBKR_CLIENT_ID = 115
    LOOP_S = 4 * 3600  # 4hr cycle (matches tori's tf)

    state_path = LOG_DIR / "live_state.json"
    state = {"open_trades": {}, "trade_count": 0}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info(f"tori LIVE mode starting (client_id={IBKR_CLIENT_ID})")

    heartbeat_path = LOG_DIR / "heartbeat.json"

    while True:
        try:
            ib = None
            try:
                ib = ibkr.connect(IBKR_CLIENT_ID)
            except Exception as exc:
                log.warning("IBKR connect failed: %s", exc)

            # Heartbeat at top of each cycle so the dashboard's stale-detector
            # doesn't false-flag tori when it's just mid-4hr-sleep (failure mode #5).
            try:
                heartbeat_path.write_text(json.dumps({
                    "system": "tori",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instruments": list(TICKERS),
                    "status": "ok",
                    "mode": "live",
                    "client_id": IBKR_CLIENT_ID,
                    "open_trade_count": len(state.get("open_trades", {})),
                    "trade_count": state.get("trade_count", 0),
                    "next_cycle_in_s": LOOP_S,
                }, indent=2), encoding="utf-8")
            except Exception:
                pass

            try:
                # 1. Manage open positions
                if ib is not None and state.get("open_trades"):
                    closed = sx.check_open_positions(state, ib)
                    for c in closed:
                        log.info(f"CLOSED {c['symbol']} ({c['reason']}) @ {c['fill_price']:.2f} pnl=${c['pnl_usd']:.2f}")
                        state["trade_count"] = state.get("trade_count", 0) + 1

                # 2. Scan for new setups
                if ib is not None:
                    for ticker in TICKERS:
                        try:
                            df = download_data(ticker, period="3mo", interval="1h")
                            df = resample_to_4h(df)
                            df = compute_atr(df)
                            if df.empty or df["ATR"].isna().all():
                                continue
                            last_bar = len(df) - 1
                            atr = float(df["ATR"].iloc[last_bar])
                            close = float(df["Close"].iloc[last_bar])

                            swing_highs = find_swing_highs(df)
                            swing_lows = find_swing_lows(df)

                            trendlines = []
                            for chunk_size in [6, 10, 15]:
                                for start in range(0, max(len(swing_lows) - chunk_size + 1, 1),
                                                    max(chunk_size // 2, 1)):
                                    chunk = swing_lows[start: start + chunk_size]
                                    tl = fit_ascending_trendline(chunk)
                                    if tl and tl["touches"] >= 2:
                                        trendlines.append(tl)
                                for start in range(0, max(len(swing_highs) - chunk_size + 1, 1),
                                                    max(chunk_size // 2, 1)):
                                    chunk = swing_highs[start: start + chunk_size]
                                    tl = fit_descending_trendline(chunk)
                                    if tl and tl["touches"] >= 2:
                                        trendlines.append(tl)

                            trendlines = _deduplicate_trendlines(trendlines, df)

                            for tl in trendlines:
                                quality = score_trendline_quality(tl, df)
                                if not quality["valid"] or quality["grade"] not in ("A+", "A"):
                                    continue
                                tl_val = _trendline_value_at(tl, last_bar)
                                dist = abs(close - tl_val)
                                if dist > 3.0 * atr:
                                    continue

                                # Validated subset: BREAK signals only (per scope_down)
                                brk = check_break(df, tl, last_bar, atr)
                                if not brk:
                                    continue

                                direction = brk["direction"]
                                entry = close
                                if direction == "long":
                                    stop_px = entry - atr
                                    target_px = entry + 2.0 * atr
                                else:
                                    stop_px = entry + atr
                                    target_px = entry - 2.0 * atr

                                # Sizing: micro contract for the underlying
                                from forge.tori.sizing import POINT_VALUES, TICKER_TO_MICRO
                                micro = TICKER_TO_MICRO.get(ticker, ticker)
                                pt_usd = POINT_VALUES.get(micro, 0.50)
                                risk_budget = compute_risk_usd(strategy_label="forge_tori")
                                stop_dist = abs(entry - stop_px)
                                contracts = max(1, int(risk_budget / max(stop_dist * pt_usd, 1e-6)))
                                cap = max_notional_usd("micro_future")
                                if cap > 0 and entry * pt_usd * contracts > cap:
                                    contracts = max(1, int(cap / max(entry * pt_usd, 1e-6)))

                                sig = sx.SignalEntry(
                                    symbol=micro, direction=direction, size=contracts,
                                    stop_px=stop_px, target_px=target_px,
                                    instrument_type="micro_future", price_decimals=0,
                                    max_hold_bars=8,  # 32 hours at 4hr bars
                                )
                                if sx.submit_signal(state, ib, sig):
                                    log.info(f"LIVE {direction} {micro} entry={entry:.0f} stop={stop_px:.0f} target={target_px:.0f}")
                        except Exception as e:
                            log.error(f"tori live scan error {ticker}: {e}")
            finally:
                if ib is not None:
                    ibkr.disconnect(ib)
                try:
                    state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
                except Exception:
                    pass

            log.info("tori live cycle complete; sleeping %ds", LOOP_S)
            time.sleep(LOOP_S)
        except KeyboardInterrupt:
            log.info("tori live stopped")
            break
        except Exception as e:
            log.error(f"live loop error: {e}", exc_info=True)
            time.sleep(60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Tori Trades Runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backtest", action="store_true", help="Run backtest on historical 4H data")
    group.add_argument("--scan", action="store_true", help="One-shot scan for current setups")
    group.add_argument("--loop", action="store_true", help="Continuous scan every 4 hours")
    group.add_argument("--live", action="store_true", help="IBKR execution (placeholder)")
    parser.add_argument("--period", default="6mo", help="Data period (default: 6mo)")
    parser.add_argument("--equity", type=float, default=None, help="Starting equity (default: pull from broker)")
    args = parser.parse_args()

    if args.live:
        run_live()
        return

    # Download data
    datasets = {}
    for ticker in TICKERS:
        try:
            df = download_data(ticker, period=args.period, interval="1h")
            df = resample_to_4h(df)
            df = compute_atr(df)
            datasets[ticker] = df
            log.info(f"  {ticker}: {len(df)} 4H bars after resample")
        except Exception as e:
            log.error(f"Failed to load {ticker}: {e}")

    if not datasets:
        log.error("No data loaded. Exiting.")
        sys.exit(1)

    if args.backtest:
        trades, equity_curve, final_equity = run_backtest(datasets, equity=args.equity)
        print_backtest_report(trades, equity_curve, final_equity, args.equity)

        # Save trades CSV
        if trades:
            csv_path = LOG_DIR / "backtest_trades.csv"
            keys = trades[0].keys()
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(trades)
            log.info(f"Trades saved to {csv_path}")

    elif args.scan:
        run_scan(datasets)

    elif args.loop:
        run_loop(datasets)


if __name__ == "__main__":
    main()
