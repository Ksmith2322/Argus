"""
Mamba Runner v2 — Dual-Instrument NY Open Breakout Strategy
=============================================================
Per MambaFX rulebook: NQ=F + YM=F, NY session only (9:25-10:30 EST),
dual timeframe (5-min bias, 1-min structure), confluence scoring,
max 2 trades/day, 1:3-1:5 R:R with scale-out.

Modes:
  --backtest    Download NQ=F + YM=F 5-min data, run full backtest
  --scan        One-shot scan for current setups (pre-market prep)
  --loop        Continuous signal-only, re-scan every 5 min during NY
  --live        IBKR execution on MNQ (client 102) and MYM (client 103)

Usage:
  python -m forge.mamba.runner --backtest
  python -m forge.mamba.runner --scan
  python -m forge.mamba.runner --loop
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "mamba"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from forge.logging_setup import setup_logging
log = setup_logging("mamba")

# Instruments
TICKERS = ["NQ=F", "YM=F"]
TICKER_TO_MICRO = {"NQ=F": "MNQ", "YM=F": "MYM"}
POINT_VALUES = {"NQ=F": 2.0, "YM=F": 0.50}  # micro contract values

# Session: NY only, 9:25-10:30 EST
NY_MARKUP_HOUR = 9
NY_MARKUP_MIN = 25
NY_OPEN_HOUR = 9
NY_OPEN_MIN = 30
NY_CUTOFF_HOUR = 10
NY_CUTOFF_MIN = 30

MAX_TRADES_PER_DAY = 2
MAX_HOLD_BARS_5MIN = 12  # 60 min max on 5-min bars (safety)
RR_CONSERVATIVE = 3.0    # 50% off at 1:3
RR_FULL = 5.0            # trail remainder to 1:5

IBKR_CLIENT_IDS = {"NQ=F": 102, "YM=F": 103}

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def download_data(
    ticker: str, period: str = "60d", interval: str = "5m",
) -> pd.DataFrame:
    """Download intraday data via yfinance."""
    import yfinance as yf

    log.info(f"Downloading {ticker} | period={period} interval={interval}")
    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        # Fallback proxies
        proxies = {"NQ=F": "^NDX", "YM=F": "^DJI"}
        proxy = proxies.get(ticker)
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
    log.info(f"  {ticker}: {len(df)} bars from {df.index[0]} to {df.index[-1]}")
    return df


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


def synthesize_1min_from_5min(df_5min: pd.DataFrame) -> pd.DataFrame:
    """
    Synthesize approximate 1-min bars from 5-min bars for backtesting.
    Splits each 5-min bar into 5 synthetic 1-min bars using OHLC interpolation.
    """
    rows = []
    for idx in range(len(df_5min)):
        bar = df_5min.iloc[idx]
        ts = df_5min.index[idx]
        o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]
        vol = bar["Volume"] / 5 if bar["Volume"] > 0 else 0

        # Determine if bullish or bearish bar
        bullish = c >= o

        if bullish:
            # O -> L -> H -> (mid) -> C
            prices = [o, l, h, (h + c) / 2, c]
        else:
            # O -> H -> L -> (mid) -> C
            prices = [o, h, l, (l + c) / 2, c]

        for m in range(5):
            ts_1m = ts + pd.Timedelta(minutes=m)
            p = prices[m]
            p_next = prices[m + 1] if m < 4 else c

            bar_o = p
            bar_c = p_next
            bar_h = max(p, p_next)
            bar_l = min(p, p_next)

            rows.append({
                "Open": bar_o, "High": bar_h, "Low": bar_l,
                "Close": bar_c, "Volume": vol,
            })

    if not rows:
        return pd.DataFrame()

    # Build index from first bar
    first_ts = df_5min.index[0]
    idx = pd.date_range(start=first_ts, periods=len(rows), freq="1min", tz=first_ts.tz)
    result = pd.DataFrame(rows, index=idx[:len(rows)])
    return result


def is_ny_session(ts, phase: str = "trade") -> bool:
    """Check if timestamp is in the NY session window."""
    h, m = ts.hour, ts.minute
    t = h * 60 + m

    if phase == "markup":
        # 9:25 EST
        return t >= NY_MARKUP_HOUR * 60 + NY_MARKUP_MIN
    else:
        # 9:30 - 10:30 EST
        start = NY_OPEN_HOUR * 60 + NY_OPEN_MIN
        end = NY_CUTOFF_HOUR * 60 + NY_CUTOFF_MIN
        return start <= t <= end


def get_trading_date(ts) -> str:
    """Get the trading date string from a timestamp."""
    return ts.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Bias detection (5-min)
# ---------------------------------------------------------------------------

def determine_bias(df_window: pd.DataFrame) -> str:
    """
    Determine directional bias from 5-min data (last 4-6 hours).
    Returns: 'bullish', 'bearish', or 'neutral'
    """
    from forge.mamba.trendlines import find_pivot_highs, find_pivot_lows

    if len(df_window) < 10:
        return "neutral"

    highs = find_pivot_highs(df_window, left_bars=3, right_bars=2)
    lows = find_pivot_lows(df_window, left_bars=3, right_bars=2)

    if len(highs) < 2 or len(lows) < 2:
        return "neutral"

    # Check for HH/HL (bullish) or LH/LL (bearish) pattern in recent pivots
    recent_highs = highs[-3:]
    recent_lows = lows[-3:]

    hh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] > recent_highs[i - 1]["price"])
    hl_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] > recent_lows[i - 1]["price"])

    lh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] < recent_highs[i - 1]["price"])
    ll_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] < recent_lows[i - 1]["price"])

    bull_score = hh_count + hl_count
    bear_score = lh_count + ll_count

    if bull_score >= 2 and bull_score > bear_score:
        return "bullish"
    elif bear_score >= 2 and bear_score > bull_score:
        return "bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(
    datasets: dict[str, pd.DataFrame],
    min_touches: int = 2,
    rr_target: float = 4.0,
) -> list[dict]:
    """
    Walk through each day / each instrument:
    1. At 9:25 EST: mark S/R levels + trendlines on last 4-6 hours of 5-min
    2. Determine bias
    3. 9:30-10:30: scan for breakout + confluence scoring
    4. Max 2 trades per day (across both instruments)
    5. Scale-out: 50% at 1:3, rest trails to 1:5
    6. Hard exit at 10:30 EST
    """
    from forge.mamba.trendlines import (
        find_pivot_highs, find_pivot_lows, fit_trendline,
        count_touches, detect_breakout, trendline_value_at,
        find_support_resistance, check_sr_break,
        detect_1min_structure, score_confluences,
        check_candle_quality, check_volume_spike,
        MIN_TRENDLINE_BARS,
    )

    all_trades = []
    lookback = 60  # 5 hours of 5-min bars

    for ticker, df in datasets.items():
        point_value = POINT_VALUES.get(ticker, 2.0)
        micro = TICKER_TO_MICRO.get(ticker, ticker)

        log.info(f"Backtesting {ticker} ({micro}): {len(df)} bars")

        # Group bars by trading date
        df["_date"] = df.index.date
        trading_days = sorted(df["_date"].unique())

        daily_trade_count: dict[str, int] = defaultdict(int)

        for day in trading_days:
            day_str = str(day)

            # Get all bars for this day
            day_mask = df["_date"] == day
            day_bars = df[day_mask]

            if len(day_bars) < 5:
                continue

            # --- Phase 1: Markup at 9:25 ---
            # Get the bars leading up to market open (last 4-6 hours of available data)
            # Find the index of the first bar at/after 9:25
            markup_bars = day_bars[
                (day_bars.index.hour * 60 + day_bars.index.minute) >= NY_MARKUP_HOUR * 60 + NY_MARKUP_MIN
            ]
            if markup_bars.empty:
                continue

            markup_time = markup_bars.index[0]

            # Get lookback window: all bars before markup time (previous session + pre-market)
            all_before = df[df.index < markup_time]
            if len(all_before) < lookback:
                continue

            window = all_before.iloc[-lookback:].copy()
            window = compute_atr(window)
            if window["ATR"].isna().all():
                continue

            current_atr = window["ATR"].dropna().iloc[-1]
            if current_atr <= 0:
                continue

            # Find S/R levels and trendlines
            sr_levels = find_support_resistance(window)
            bias = determine_bias(window)

            if bias == "neutral":
                continue

            # Find active trendlines
            pivot_highs = find_pivot_highs(window)
            pivot_lows = find_pivot_lows(window)

            active_trendlines = []
            # Descending trendlines
            for end_idx in range(len(pivot_highs) - 1, 0, -1):
                for start_idx in range(end_idx - 1, max(end_idx - 4, -1), -1):
                    pair = [pivot_highs[start_idx], pivot_highs[end_idx]]
                    tl = fit_trendline(pair)
                    if tl and (tl["end_bar"] - tl["start_bar"]) >= MIN_TRENDLINE_BARS:
                        ext = {**tl, "end_bar": len(window) - 1}
                        t = count_touches(window, ext)
                        if t >= min_touches:
                            active_trendlines.append(ext)
            # Ascending trendlines
            for end_idx in range(len(pivot_lows) - 1, 0, -1):
                for start_idx in range(end_idx - 1, max(end_idx - 4, -1), -1):
                    pair = [pivot_lows[start_idx], pivot_lows[end_idx]]
                    tl = fit_trendline(pair)
                    if tl and (tl["end_bar"] - tl["start_bar"]) >= MIN_TRENDLINE_BARS:
                        ext = {**tl, "end_bar": len(window) - 1}
                        t = count_touches(window, ext)
                        if t >= min_touches:
                            active_trendlines.append(ext)

            # --- Phase 2: Trade window 9:30-10:30 ---
            trade_bars = day_bars[
                ((day_bars.index.hour * 60 + day_bars.index.minute) >= NY_OPEN_HOUR * 60 + NY_OPEN_MIN) &
                ((day_bars.index.hour * 60 + day_bars.index.minute) <= NY_CUTOFF_HOUR * 60 + NY_CUTOFF_MIN)
            ]

            if trade_bars.empty:
                continue

            open_trade = None

            for bar_pos in range(len(trade_bars)):
                bar_time = trade_bars.index[bar_pos]
                bar = trade_bars.iloc[bar_pos]

                # --- Manage open trade ---
                if open_trade is not None:
                    high_i = bar["High"]
                    low_i = bar["Low"]

                    hit_stop = False
                    hit_target1 = False
                    hit_target2 = False

                    if open_trade["direction"] == "LONG":
                        if low_i <= open_trade["stop"]:
                            hit_stop = True
                        if high_i >= open_trade["target1"]:
                            hit_target1 = True
                        if high_i >= open_trade["target2"]:
                            hit_target2 = True
                    else:
                        if high_i >= open_trade["stop"]:
                            hit_stop = True
                        if low_i <= open_trade["target1"]:
                            hit_target1 = True
                        if low_i <= open_trade["target2"]:
                            hit_target2 = True

                    # Time-based exit: hard cutoff at 10:30
                    time_exit = (bar_time.hour * 60 + bar_time.minute) >= NY_CUTOFF_HOUR * 60 + NY_CUTOFF_MIN
                    bars_held = bar_pos - open_trade["_entry_bar_pos"]

                    if hit_stop or hit_target2 or time_exit or bars_held >= MAX_HOLD_BARS_5MIN:
                        if hit_target2:
                            # Full target hit: 50% at 1:3 + 50% at 1:5
                            pnl_t1 = open_trade["stop_dist"] * RR_CONSERVATIVE * 0.5
                            pnl_t2 = open_trade["stop_dist"] * RR_FULL * 0.5
                            pnl_points = pnl_t1 + pnl_t2
                            outcome = "target_full"
                            exit_price = open_trade["target2"]
                        elif hit_target1 and not hit_stop:
                            # Partial target: 50% at 1:3, rest at current price
                            pnl_t1 = open_trade["stop_dist"] * RR_CONSERVATIVE * 0.5
                            if open_trade["direction"] == "LONG":
                                pnl_rest = (bar["Close"] - open_trade["entry"]) * 0.5
                            else:
                                pnl_rest = (open_trade["entry"] - bar["Close"]) * 0.5
                            pnl_points = pnl_t1 + max(pnl_rest, 0)
                            outcome = "target_partial"
                            exit_price = float(bar["Close"])
                        elif hit_stop:
                            pnl_points = -open_trade["stop_dist"]
                            outcome = "stop"
                            exit_price = open_trade["stop"]
                        else:
                            # Time exit or max bars
                            if open_trade["direction"] == "LONG":
                                pnl_points = float(bar["Close"]) - open_trade["entry"]
                            else:
                                pnl_points = open_trade["entry"] - float(bar["Close"])
                            outcome = "time_exit"
                            exit_price = float(bar["Close"])

                        rr_achieved = pnl_points / open_trade["stop_dist"] if open_trade["stop_dist"] > 0 else 0

                        trade_record = {
                            "ticker": ticker,
                            "instrument": micro,
                            "entry_time": str(open_trade["entry_time"]),
                            "exit_time": str(bar_time),
                            "direction": open_trade["direction"],
                            "bias": open_trade["bias"],
                            "entry": open_trade["entry"],
                            "stop": open_trade["stop"],
                            "target1": open_trade["target1"],
                            "target2": open_trade["target2"],
                            "exit_price": round(exit_price, 2),
                            "stop_dist": round(open_trade["stop_dist"], 2),
                            "pnl_points": round(pnl_points, 2),
                            "pnl_usd": round(pnl_points * point_value, 2),
                            "rr_achieved": round(rr_achieved, 2),
                            "outcome": outcome,
                            "confluences": open_trade["confluences"],
                            "bars_held": bars_held,
                            "day": day_str,
                            "dow": day.strftime("%A") if hasattr(day, "strftime") else pd.Timestamp(day).strftime("%A"),
                        }
                        all_trades.append(trade_record)
                        open_trade = None

                    continue  # Don't open new trade while managing one

                # --- Check daily trade cap ---
                if daily_trade_count[day_str] >= MAX_TRADES_PER_DAY:
                    continue

                # --- Check for breakout signals ---
                # Determine direction from bias
                if bias == "bullish":
                    direction = "LONG"
                elif bias == "bearish":
                    direction = "SHORT"
                else:
                    continue

                close_price = float(bar["Close"])

                # Check each confluence factor
                # 1. S/R break
                sr_broken = check_sr_break(close_price, sr_levels, direction, current_atr)
                has_sr_break = sr_broken is not None

                # 2. Trendline break
                has_tl_break = False
                for tl in active_trendlines:
                    # Project trendline value to current bar position
                    # We need to map the bar position to the trendline's coordinate space
                    # Use offset from the markup window end
                    projected_bar = len(window) - 1 + bar_pos + 1
                    tl_val = trendline_value_at(tl, projected_bar)

                    if direction == "LONG" and tl["direction"] == "descending":
                        if close_price > tl_val + current_atr * 0.1:
                            has_tl_break = True
                            break
                    elif direction == "SHORT" and tl["direction"] == "ascending":
                        if close_price < tl_val - current_atr * 0.1:
                            has_tl_break = True
                            break

                # 3. 1-min structure (synthesize from recent 5-min bars)
                struct_start = max(0, bar_pos - 6)
                recent_5min = trade_bars.iloc[struct_start:bar_pos + 1].copy()
                if len(recent_5min) >= 3:
                    synth_1min = synthesize_1min_from_5min(recent_5min)
                    if len(synth_1min) >= 8:
                        structure = detect_1min_structure(synth_1min, direction)
                        has_structure = structure is not None and structure["confirmed"]
                    else:
                        has_structure = False
                else:
                    has_structure = False

                # 4. Volume spike
                # Find this bar's position in the full df for volume check
                full_idx = df.index.get_loc(bar_time)
                if isinstance(full_idx, slice):
                    full_idx = full_idx.start
                has_volume = check_volume_spike(df, full_idx, threshold=2.0)

                # 5. Candle quality
                has_candle = check_candle_quality(bar)

                # Score confluences
                confluences = score_confluences(
                    sr_break=has_sr_break,
                    trendline_break=has_tl_break,
                    structure_confirmed=has_structure,
                    volume_spike=has_volume,
                    candle_quality=has_candle,
                )

                # Per-confluence-bucket backtest (2026-04-16) showed:
                #   3-conf: 13% WR, -$576 (noise)
                #   4-conf: 26% WR, +$13 (breakeven)
                #   5-conf: 36% WR, +$176 (matches rulebook 35-45% target)
                # Edge lives ONLY in 5-confluence sniper setups. Structure is mandatory.
                if confluences < 5 or not has_structure:
                    continue

                # --- ENTRY ---
                entry_price = close_price

                # Stop: ATR-based, 10-20 points typical. (Tried swing-based per
                # rulebook 2026-04-16 but synth_1min swing detection produced
                # tighter stops that got whipsawed — WR dropped 36%->21%. Keeping
                # ATR until a better swing detector is in place.)
                stop_dist = max(current_atr * 0.8, 10)
                stop_dist = min(stop_dist, 25)  # cap at 25 points

                if direction == "LONG":
                    stop_price = entry_price - stop_dist
                    target1 = entry_price + stop_dist * RR_CONSERVATIVE
                    target2 = entry_price + stop_dist * RR_FULL
                else:
                    stop_price = entry_price + stop_dist
                    target1 = entry_price - stop_dist * RR_CONSERVATIVE
                    target2 = entry_price - stop_dist * RR_FULL

                open_trade = {
                    "entry_time": bar_time,
                    "direction": direction,
                    "bias": bias,
                    "entry": round(entry_price, 2),
                    "stop": round(stop_price, 2),
                    "target1": round(target1, 2),
                    "target2": round(target2, 2),
                    "stop_dist": round(stop_dist, 2),
                    "confluences": confluences,
                    "_entry_bar_pos": bar_pos,
                }
                daily_trade_count[day_str] += 1

            # Close any still-open trade at session end
            if open_trade is not None:
                last_bar = trade_bars.iloc[-1]
                last_time = trade_bars.index[-1]
                if open_trade["direction"] == "LONG":
                    pnl_points = float(last_bar["Close"]) - open_trade["entry"]
                else:
                    pnl_points = open_trade["entry"] - float(last_bar["Close"])
                rr_achieved = pnl_points / open_trade["stop_dist"] if open_trade["stop_dist"] > 0 else 0
                all_trades.append({
                    "ticker": ticker,
                    "instrument": micro,
                    "entry_time": str(open_trade["entry_time"]),
                    "exit_time": str(last_time),
                    "direction": open_trade["direction"],
                    "bias": open_trade["bias"],
                    "entry": open_trade["entry"],
                    "stop": open_trade["stop"],
                    "target1": open_trade["target1"],
                    "target2": open_trade["target2"],
                    "exit_price": round(float(last_bar["Close"]), 2),
                    "stop_dist": open_trade["stop_dist"],
                    "pnl_points": round(pnl_points, 2),
                    "pnl_usd": round(pnl_points * point_value, 2),
                    "rr_achieved": round(rr_achieved, 2),
                    "outcome": "session_end",
                    "confluences": open_trade["confluences"],
                    "bars_held": len(trade_bars) - 1 - open_trade["_entry_bar_pos"],
                    "day": day_str,
                    "dow": pd.Timestamp(day).strftime("%A"),
                })
                open_trade = None

        # Cleanup temp column
        df.drop(columns=["_date"], inplace=True, errors="ignore")

    # Sort all trades by entry time
    all_trades.sort(key=lambda t: t["entry_time"])
    return all_trades


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_backtest_report(trades: list[dict]):
    """Print comprehensive backtest results."""
    if not trades:
        print("\n=== MAMBA v2 BACKTEST REPORT ===")
        print("No trades generated.")
        return

    print("\n" + "=" * 75)
    print("  MAMBA v2 BACKTEST — NY Open Breakout (NQ + YM)")
    print("=" * 75)

    n = len(trades)
    winners = [t for t in trades if t["pnl_points"] > 0]
    losers = [t for t in trades if t["pnl_points"] <= 0]
    win_rate = len(winners) / n * 100 if n > 0 else 0

    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losers)) if losers else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win_rr = np.mean([t["rr_achieved"] for t in winners]) if winners else 0
    avg_loss_rr = np.mean([t["rr_achieved"] for t in losers]) if losers else 0
    avg_bars = np.mean([t["bars_held"] for t in trades])
    avg_conf = np.mean([t["confluences"] for t in trades])

    unique_days = len(set(t["day"] for t in trades))

    print(f"\n  OVERALL")
    print(f"  {'-' * 40}")
    print(f"  Total trades:      {n} across {unique_days} days")
    print(f"  Winners:           {len(winners)}  ({win_rate:.1f}%)")
    print(f"  Losers:            {len(losers)}  ({100 - win_rate:.1f}%)")
    print(f"\n  Total P&L:         ${total_pnl:,.2f}")
    print(f"  Gross profit:      ${gross_profit:,.2f}")
    print(f"  Gross loss:        ${gross_loss:,.2f}")
    print(f"  Profit factor:     {pf:.2f}")
    print(f"\n  Avg R:R winners:   {avg_win_rr:.2f}")
    print(f"  Avg R:R losers:    {avg_loss_rr:.2f}")
    print(f"  Avg bars held:     {avg_bars:.1f}")
    print(f"  Avg confluences:   {avg_conf:.1f}")

    # By outcome
    print(f"\n  BY OUTCOME")
    print(f"  {'-' * 40}")
    outcomes = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in trades:
        outcomes[t["outcome"]]["count"] += 1
        outcomes[t["outcome"]]["pnl"] += t["pnl_usd"]
    for outcome, data in sorted(outcomes.items()):
        print(f"  {outcome:18s}  n={data['count']:3d}  P&L=${data['pnl']:8,.2f}")

    # By instrument
    print(f"\n  BY INSTRUMENT")
    print(f"  {'-' * 40}")
    for ticker in TICKERS:
        micro = TICKER_TO_MICRO.get(ticker, ticker)
        inst_trades = [t for t in trades if t["ticker"] == ticker]
        if not inst_trades:
            print(f"  {micro:6s}  (no trades)")
            continue
        inst_w = [t for t in inst_trades if t["pnl_points"] > 0]
        inst_wr = len(inst_w) / len(inst_trades) * 100
        inst_pnl = sum(t["pnl_usd"] for t in inst_trades)
        print(f"  {micro:6s}  trades={len(inst_trades):3d}  WR={inst_wr:.0f}%  P&L=${inst_pnl:8,.2f}")

    # By confluence count
    print(f"\n  BY CONFLUENCE COUNT")
    print(f"  {'-' * 40}")
    conf_groups = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        c = t["confluences"]
        conf_groups[c]["count"] += 1
        conf_groups[c]["pnl"] += t["pnl_usd"]
        if t["pnl_points"] > 0:
            conf_groups[c]["wins"] += 1
    for c, data in sorted(conf_groups.items()):
        wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
        print(f"  {c} confluences:  trades={data['count']:3d}  WR={wr:.0f}%  P&L=${data['pnl']:8,.2f}")

    # By day of week
    print(f"\n  BY DAY OF WEEK")
    print(f"  {'-' * 40}")
    dow_groups = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        d = t.get("dow", "Unknown")
        dow_groups[d]["count"] += 1
        dow_groups[d]["pnl"] += t["pnl_usd"]
        if t["pnl_points"] > 0:
            dow_groups[d]["wins"] += 1
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    for d in dow_order:
        if d in dow_groups:
            data = dow_groups[d]
            wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
            print(f"  {d:12s}  trades={data['count']:3d}  WR={wr:.0f}%  P&L=${data['pnl']:8,.2f}")

    # Equity curve
    print(f"\n  EQUITY CURVE")
    print(f"  {'-' * 40}")
    cumulative = 0.0
    curve = []
    for t in trades:
        cumulative += t["pnl_usd"]
        curve.append(cumulative)

    if curve:
        peak = max(curve)
        trough_from_peak = 0.0
        running_peak = curve[0]
        for c in curve:
            running_peak = max(running_peak, c)
            trough_from_peak = max(trough_from_peak, running_peak - c)

        print(f"  Peak equity:       ${peak:,.2f}")
        print(f"  Max drawdown:      ${trough_from_peak:,.2f}")
        print(f"  Final equity:      ${cumulative:,.2f}")

        # ASCII equity curve
        if len(curve) > 1:
            mn, mx = min(curve), max(curve)
            rng = mx - mn if mx != mn else 1
            width = 60
            rows = 10
            grid = [[" "] * width for _ in range(rows)]
            for idx_c, val in enumerate(curve):
                x = int(idx_c / max(len(curve) - 1, 1) * (width - 1))
                y = int((val - mn) / rng * (rows - 1))
                y = max(0, min(rows - 1, y))
                grid[rows - 1 - y][x] = "*"
            print()
            for row in grid:
                print(f"  |{''.join(row)}|")
            print(f"  +{'-' * width}+")
            print(f"   ${mn:,.0f}{' ' * (width - 12)}${mx:,.0f}")

    # Best and worst trades
    if trades:
        best = max(trades, key=lambda t: t["pnl_usd"])
        worst = min(trades, key=lambda t: t["pnl_usd"])
        print(f"\n  EXAMPLE TRADES")
        print(f"  {'-' * 40}")
        print(f"  Best:  ${best['pnl_usd']:,.2f}  {best['direction']} {best['ticker']} @ {best['entry_time'][:16]}")
        print(f"         entry={best['entry']} exit={best['exit_price']} R:R={best['rr_achieved']:.1f} conf={best['confluences']}")
        print(f"  Worst: ${worst['pnl_usd']:,.2f}  {worst['direction']} {worst['ticker']} @ {worst['entry_time'][:16]}")
        print(f"         entry={worst['entry']} exit={worst['exit_price']} R:R={worst['rr_achieved']:.1f} conf={worst['confluences']}")

    # Trade log
    print(f"\n  TRADE LOG (last 25)")
    print(f"  {'-' * 72}")
    print(f"  {'Time':<18s} {'Tkr':4s} {'Dir':5s} {'Entry':>9s} {'Exit':>9s} {'P&L$':>8s} {'R:R':>5s} {'Cf':>3s} {'Out':>12s}")
    for t in trades[-25:]:
        entry_short = t["entry_time"][5:16] if len(t["entry_time"]) > 16 else t["entry_time"]
        print(
            f"  {entry_short:<18s} {t['ticker'][:4]:4s} {t['direction']:5s} "
            f"{t['entry']:9.2f} {t['exit_price']:9.2f} {t['pnl_usd']:8.2f} "
            f"{t['rr_achieved']:5.2f} {t['confluences']:3d} {t['outcome']:>12s}"
        )

    print("\n" + "=" * 75)


def save_trades_csv(trades: list[dict], path: Path):
    """Save trades to CSV."""
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [k for k in trades[0].keys() if not k.startswith("_")]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
    log.info(f"Saved {len(trades)} trades to {path}")


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------

def run_scan():
    """One-shot scan: download recent data for both instruments, show current setups."""
    from forge.mamba.trendlines import (
        scan_for_setups, trendline_value_at, find_support_resistance,
    )

    print("\n" + "=" * 75)
    print("  MAMBA v2 SCAN — Pre-Market Setup (NQ + YM)")
    print("=" * 75)

    for ticker in TICKERS:
        try:
            df = download_data(ticker, period="5d")
            df = compute_atr(df)
        except Exception as e:
            print(f"\n  [{ticker}] Error: {e}")
            continue

        sr_levels = find_support_resistance(df)
        setups = scan_for_setups(df, min_touches=2)
        bias = determine_bias(df.iloc[-60:] if len(df) >= 60 else df)

        current_close = float(df["Close"].iloc[-1])
        current_atr = float(df["ATR"].iloc[-1])
        last_time = df.index[-1]

        print(f"\n  {'=' * 50}")
        print(f"  {ticker} ({TICKER_TO_MICRO[ticker]})")
        print(f"  {'=' * 50}")
        print(f"  Last bar:  {last_time}")
        print(f"  Close:     {current_close:.2f}")
        print(f"  ATR(14):   {current_atr:.2f}")
        print(f"  Bias:      {bias.upper()}")

        # S/R levels
        if sr_levels:
            print(f"\n  S/R LEVELS ({len(sr_levels)}):")
            for sr in sr_levels[:8]:
                dist = current_close - sr["level"]
                arrow = "^" if dist > 0 else "v"
                print(f"    {sr['type']:>10s}  {sr['level']:>10.2f}  touches={sr['touches']}  "
                      f"({arrow} {abs(dist):.1f} pts away)")

        # Trendline setups
        if setups:
            print(f"\n  TRENDLINE SETUPS ({len(setups)}):")
            for idx, s in enumerate(setups[:6], 1):
                tl = s["trendline"]
                tl_now = s["tl_value_now"]
                dist = current_close - tl_now if tl["direction"] == "descending" else tl_now - current_close
                status = "** BREAKOUT **" if s["breakout"] else f"{dist:.1f} pts away"

                print(f"    [{idx}] {tl['direction'].upper():12s}  touches={s['touches']}  "
                      f"conviction={s['conviction']:.0f}  {status}")
                if s["breakout"]:
                    bo = s["breakout"]
                    print(f"        {bo['direction']} @ {bo['break_price']:.2f} (vol {bo['volume_ratio']:.1f}x)")
        else:
            print(f"\n  No active trendline setups.")

        # Trading assessment
        print(f"\n  ASSESSMENT:")
        if bias == "neutral":
            print(f"    --> SKIP: No clear directional bias")
        elif not setups:
            print(f"    --> WATCH: {bias.upper()} bias but no breakout levels nearby")
        else:
            nearest = setups[0]
            dist = abs(current_close - nearest["tl_value_now"])
            if nearest["breakout"]:
                print(f"    --> ACTIVE BREAKOUT: {nearest['breakout']['direction']} setup firing")
            elif dist < current_atr * 0.5:
                print(f"    --> CLOSE: Price within 0.5 ATR of key level, watch for breakout")
            else:
                print(f"    --> WAIT: Levels marked, waiting for price to approach")

    print("\n" + "=" * 75)


# ---------------------------------------------------------------------------
# Signal loop
# ---------------------------------------------------------------------------

def write_heartbeat(status: str = "running", extra: dict | None = None):
    """Write heartbeat file."""
    hb = {
        "system": "mamba",
        "version": "v2",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "tickers": TICKERS,
    }
    if extra:
        hb.update(extra)
    hb_path = LOG_DIR / "heartbeat.json"
    with open(hb_path, "w") as f:
        json.dump(hb, f, indent=2)


def append_signal(signal: dict):
    """Append signal to signals CSV."""
    sig_path = LOG_DIR / "signals.csv"
    file_exists = sig_path.exists()
    fieldnames = ["timestamp", "ticker", "direction", "entry", "stop", "target1",
                   "target2", "confluences", "bias", "volume_ratio"]
    with open(sig_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(signal)


def run_signal_loop():
    """Run every 5 min during NY session, log signals."""
    from forge.mamba.trendlines import scan_for_setups

    try:
        from forge.conviction import score_conviction
    except ImportError:
        score_conviction = None
        log.warning("Could not import score_conviction — running without fleet conviction")

    while True:
        try:
            now = datetime.now(timezone(timedelta(hours=-4)))  # EST approx
            in_window = is_ny_session(now, phase="trade")

            if not in_window:
                write_heartbeat(status="waiting_for_ny", extra={"note": f"Current: {now.strftime('%H:%M')} EST"})
                log.info(f"Outside NY session ({now.strftime('%H:%M')} EST). Waiting...")
                time.sleep(300)
                continue

            write_heartbeat(status="scanning")

            for ticker in TICKERS:
                try:
                    df = download_data(ticker, period="5d")
                    df = compute_atr(df)
                    setups = scan_for_setups(df, min_touches=2)

                    for s in setups:
                        if s["breakout"]:
                            bo = s["breakout"]
                            signal = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "ticker": ticker,
                                "direction": bo["direction"],
                                "entry": bo["break_price"],
                                "stop": 0,
                                "target1": 0,
                                "target2": 0,
                                "confluences": s.get("touches", 0),
                                "bias": determine_bias(df.iloc[-60:]),
                                "volume_ratio": bo["volume_ratio"],
                            }
                            append_signal(signal)
                            log.info(f"SIGNAL: {bo['direction']} {ticker} | vol={bo['volume_ratio']:.1f}x")
                except Exception as e:
                    log.error(f"Error scanning {ticker}: {e}")

            log.info("Sleeping 5 minutes before next scan...")
            time.sleep(300)

        except KeyboardInterrupt:
            log.info("Interrupted.")
            break
        except Exception as e:
            log.error(f"Error in signal loop: {e}")
            time.sleep(60)


# ---------------------------------------------------------------------------
# Live mode (placeholder)
# ---------------------------------------------------------------------------

def run_live():
    """Placeholder for live IBKR execution on MNQ + MYM."""
    log.error("Live mode not yet implemented. Use --loop for signal-only.")
    log.error(f"  Would use client IDs: {IBKR_CLIENT_IDS}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mamba v2 — NY Open Breakout (NQ + YM)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest", action="store_true", help="Run full backtest")
    mode.add_argument("--scan", action="store_true", help="One-shot scan for setups")
    mode.add_argument("--loop", action="store_true", help="Continuous signal-only")
    mode.add_argument("--live", action="store_true", help="Live execution via IBKR")

    parser.add_argument("--min-touches", type=int, default=2, help="Min trendline/SR touches")
    parser.add_argument("--rr-target", type=float, default=4.0, help="Base R:R target")

    args = parser.parse_args()

    if args.backtest:
        datasets = {}
        for ticker in TICKERS:
            try:
                df = download_data(ticker, period="60d")
                df = compute_atr(df)
                log.info(f"  {ticker}: shape={df.shape}, ATR range={df['ATR'].min():.2f}-{df['ATR'].max():.2f}")
                datasets[ticker] = df
            except Exception as e:
                log.error(f"Failed to download {ticker}: {e}")

        if not datasets:
            log.error("No data available for any instrument.")
            sys.exit(1)

        trades = run_backtest(datasets, min_touches=args.min_touches, rr_target=args.rr_target)
        print_backtest_report(trades)
        save_trades_csv(trades, LOG_DIR / "backtest_trades.csv")

    elif args.scan:
        run_scan()

    elif args.loop:
        run_signal_loop()

    elif args.live:
        run_live()


if __name__ == "__main__":
    main()
