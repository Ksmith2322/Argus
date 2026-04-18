"""
Cue Banks Runner — US30 Multi-TF Confluence Trading
=====================================================
Per Cue Banks rulebook: Daily/H4 bias → mark S/R, Fibs, exhaustion, trendlines
for confluence clusters → wait for break/pullback confirmation on M5 inside the
zone → tight stop outside level + high RR Fib targets (1:7+).

Modes:
  --backtest    Download YM=F multi-TF data, run full backtest
  --scan        One-shot scan for current setups
  --loop        Continuous signal-only, re-scan every 5 min during NY
  --live        IBKR placeholder (MYM, client ID 108)

Usage:
  python -m forge.cuebanks.runner --backtest
  python -m forge.cuebanks.runner --scan
  python -m forge.cuebanks.runner --loop
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

from forge.cuebanks.confluence import (
    compute_fib_levels,
    detect_consolidation_break,
    detect_exhaustion,
    detect_gap_at_session_open,
    detect_structure,
    find_horizontal_sr,
    find_swing_points,
    score_confluence,
)
from forge.cuebanks.sizing import compute_cuebanks_size

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "cuebanks"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from forge.logging_setup import setup_logging
log = setup_logging("cuebanks")

RESEARCH_ONLY = True  # 2026-04-17: rulebook exists, production readiness not
# validated. Local sizing module not aligned to fleet_sizing.json. Keep out
# of fleet USD roll-up until promotion candidate.

# Instrument
TICKER = "YM=F"
TICKER_PROXY = "^DJI"  # Fallback if futures data unavailable
POINT_VALUE_MYM = 0.50

# Session: NY 9:30-16:00 EST
NY_OPEN_HOUR, NY_OPEN_MIN = 9, 30
NY_CLOSE_HOUR, NY_CLOSE_MIN = 16, 0
MAX_TRADES_PER_DAY = 2
MIN_RR = 5.0  # Conservative (rulebook says 1:7-1:8)

IBKR_CLIENT_ID = 108

# Starting equity for backtest
BACKTEST_EQUITY = 10_000.0


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def download_data(
    ticker: str, period: str = "60d", interval: str = "5m",
) -> pd.DataFrame:
    """Download data via yfinance."""
    import yfinance as yf

    log.info(f"Downloading {ticker} | period={period} interval={interval}")
    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        proxy = TICKER_PROXY
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


def resample_to_tf(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 5-min bars to a higher timeframe (H1, 4h, D)."""
    resampled = df_5m.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open"])
    return resampled


def is_ny_session(ts) -> bool:
    """Check if timestamp is in NY session (9:30-16:00 EST)."""
    h, m = ts.hour, ts.minute
    t = h * 60 + m
    start = NY_OPEN_HOUR * 60 + NY_OPEN_MIN
    end = NY_CLOSE_HOUR * 60 + NY_CLOSE_MIN
    return start <= t <= end


def get_trading_date(ts) -> str:
    return ts.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest():
    """
    Full backtest: multi-TF confluence trading on US30.
    Daily for bias, H4 for S/R/Fib/confluence, M5 for entry.
    """
    log.info("=" * 70)
    log.info("CUE BANKS BACKTEST — US30 Multi-TF Confluence")
    log.info("=" * 70)

    # Download data
    df_daily = download_data(TICKER, period="1y", interval="1d")
    df_5m = download_data(TICKER, period="60d", interval="5m")

    # Resample to H4 and H1
    df_h4 = resample_to_tf(df_5m, "4h")
    df_h1 = resample_to_tf(df_5m, "1h")

    log.info(f"Daily: {len(df_daily)} bars | H4: {len(df_h4)} bars | "
             f"H1: {len(df_h1)} bars | M5: {len(df_5m)} bars")

    # Get unique trading days from M5 data
    trading_days = sorted(set(df_5m.index.strftime("%Y-%m-%d")))
    log.info(f"Trading days in M5 data: {len(trading_days)}")

    trades = []
    equity = BACKTEST_EQUITY
    equity_curve = [(trading_days[0] if trading_days else "start", equity)]

    for day_str in trading_days:
        day_trades = 0

        # M5 bars for this day
        day_m5 = df_5m[df_5m.index.strftime("%Y-%m-%d") == day_str]
        if len(day_m5) < 10:
            continue

        # 1. Daily bias from bars before this day
        prior_daily = df_daily[df_daily.index.strftime("%Y-%m-%d") < day_str]
        if len(prior_daily) < 20:
            continue
        daily_structure = detect_structure(prior_daily, lookback=20)
        daily_bias = daily_structure["bias"]

        # 2. H4 analysis: S/R, Fibs, structure
        prior_h4 = df_h4[df_h4.index.strftime("%Y-%m-%d") <= day_str]
        if len(prior_h4) < 20:
            continue

        h4_sr_levels = find_horizontal_sr(prior_h4, tolerance_pct=0.001, min_touches=2)
        h4_swing_highs, h4_swing_lows = find_swing_points(prior_h4, left=6, right=3)

        # Compute Fibs from most recent H4 swings
        fib_levels = {}
        if h4_swing_highs and h4_swing_lows:
            recent_high = max(h4_swing_highs[-3:], key=lambda x: x["price"])["price"]
            recent_low = min(h4_swing_lows[-3:], key=lambda x: x["price"])["price"]
            if recent_high > recent_low:
                fib_levels = compute_fib_levels(recent_high, recent_low)

        h4_structure = detect_structure(prior_h4, lookback=20)

        # 3. Gap detection for this day
        gap = detect_gap_at_session_open(df_5m, day_str, min_gap_pct=0.3)

        # 4. Scan M5 bars during NY session
        ny_bars = day_m5[day_m5.index.map(is_ny_session)]
        if ny_bars.empty:
            continue

        for i in range(5, len(ny_bars)):
            if day_trades >= MAX_TRADES_PER_DAY:
                break

            bar = ny_bars.iloc[i]
            bar_ts = ny_bars.index[i]
            price = float(bar["Close"])

            # Get bar index in full df for exhaustion/consolidation
            full_idx = df_5m.index.get_loc(bar_ts)
            if isinstance(full_idx, slice):
                full_idx = full_idx.start

            # Detect confluence factors
            exhaustion = detect_exhaustion(df_5m, full_idx)
            consol_break = detect_consolidation_break(df_5m, full_idx, lookback=30)

            # Simple trendline break proxy: price breaking above/below
            # recent M5 trend (using 20-bar moving direction)
            trendline_break = False
            if full_idx >= 20:
                recent_20 = df_5m.iloc[full_idx - 20:full_idx]
                slope = (float(recent_20["Close"].iloc[-1]) - float(recent_20["Close"].iloc[0])) / 20
                if slope > 0 and price < float(recent_20["Close"].iloc[-1]) - abs(slope) * 5:
                    trendline_break = True  # broke downtrend
                elif slope < 0 and price > float(recent_20["Close"].iloc[-1]) + abs(slope) * 5:
                    trendline_break = True  # broke uptrend

            # Score confluence
            result = score_confluence(
                price=price,
                sr_levels=h4_sr_levels,
                fib_levels=fib_levels,
                structure=h4_structure,
                exhaustion=exhaustion,
                trendline_break=trendline_break,
                consolidation_break=consol_break,
                gap=gap if i < 12 else None,  # Gap only relevant early in session
                tolerance_pct=0.002,
            )

            if not result["tradeable"]:
                continue

            direction = result["direction"]
            if direction is None:
                continue

            # Bias alignment check: direction must match daily bias
            if daily_bias == "bullish" and direction != "LONG":
                continue
            if daily_bias == "bearish" and direction != "SHORT":
                continue

            # Confirmation: look for pullback + reversal in last 3 bars
            confirmed = False
            if i >= 3:
                prev_bars = ny_bars.iloc[i - 3:i + 1]
                closes = prev_bars["Close"].values.astype(float)
                if direction == "LONG":
                    # Pullback (dip) then recovery
                    if closes[-2] < closes[-3] and closes[-1] > closes[-2]:
                        confirmed = True
                elif direction == "SHORT":
                    if closes[-2] > closes[-3] and closes[-1] < closes[-2]:
                        confirmed = True

            if not confirmed:
                continue

            entry_price = result["entry_price"]
            stop_price = result["stop_price"]
            stop_dist = abs(entry_price - stop_price)

            if stop_dist < 1:
                continue

            # Compute R:R for TP levels
            tp1 = result["tp1"]  # ~3.82R
            tp2 = result["tp2"]  # ~6.18R
            tp3 = result["tp3"]  # ~7.27R

            # Size the trade
            sizing = compute_cuebanks_size(
                equity=equity,
                entry_price=entry_price,
                stop_price=stop_price,
                confluence_score=int(result["score"]),
            )

            if sizing["skip"]:
                continue

            # Simulate trade outcome using remaining M5 bars
            outcome = simulate_trade(
                df_5m, full_idx, direction, entry_price, stop_price,
                tp1, tp2, tp3, max_bars=60,
            )

            pnl_points = outcome["pnl_points"]
            pnl_usd = pnl_points * POINT_VALUE_MYM * sizing["contracts"]
            rr_achieved = pnl_points / stop_dist if stop_dist > 0 else 0

            equity += pnl_usd
            day_trades += 1

            trade = {
                "date": day_str,
                "time": bar_ts.strftime("%H:%M"),
                "direction": direction,
                "entry": entry_price,
                "stop": stop_price,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "confluence_score": result["score"],
                "factors": " | ".join(result["factors"]),
                "exit_price": outcome["exit_price"],
                "exit_reason": outcome["exit_reason"],
                "pnl_points": round(pnl_points, 1),
                "pnl_usd": round(pnl_usd, 2),
                "rr_achieved": round(rr_achieved, 2),
                "contracts": sizing["contracts"],
                "risk_usd": sizing["risk_usd"],
                "equity_after": round(equity, 2),
            }
            trades.append(trade)

        equity_curve.append((day_str, round(equity, 2)))

    # Save trades to CSV
    csv_path = LOG_DIR / "cuebanks_backtest_trades.csv"
    if trades:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
        log.info(f"Trades saved to {csv_path}")

    # Print report
    print_backtest_report(trades, equity_curve)


def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    direction: str,
    entry_price: float,
    stop_price: float,
    tp1: float,
    tp2: float,
    tp3: float,
    max_bars: int = 60,
) -> dict:
    """
    Simulate a trade using M5 bars after entry.
    Scale-out: 50% at TP1, remainder trails to TP2/TP3 or gets stopped.
    """
    stop_dist = abs(entry_price - stop_price)
    remaining_pct = 1.0
    realized_points = 0.0
    tp1_hit = False
    trailing_stop = stop_price

    for i in range(1, max_bars + 1):
        idx = entry_idx + i
        if idx >= len(df):
            # End of data — exit at last known price
            last_price = float(df["Close"].iloc[-1])
            if direction == "LONG":
                realized_points += (last_price - entry_price) * remaining_pct
            else:
                realized_points += (entry_price - last_price) * remaining_pct
            return {"exit_price": last_price, "exit_reason": "end_of_data",
                    "pnl_points": realized_points, "bars_held": i}

        bar = df.iloc[idx]
        high = float(bar["High"])
        low = float(bar["Low"])
        close = float(bar["Close"])

        if direction == "LONG":
            # Check stop
            if low <= trailing_stop:
                realized_points += (trailing_stop - entry_price) * remaining_pct
                return {"exit_price": trailing_stop, "exit_reason": "stopped",
                        "pnl_points": realized_points, "bars_held": i}

            # Check TP1 (take 50%)
            if not tp1_hit and high >= tp1:
                realized_points += (tp1 - entry_price) * 0.5
                remaining_pct = 0.5
                tp1_hit = True
                trailing_stop = entry_price  # Move to breakeven

            # Check TP2/TP3
            if tp1_hit:
                if high >= tp3:
                    realized_points += (tp3 - entry_price) * remaining_pct
                    return {"exit_price": tp3, "exit_reason": "tp3",
                            "pnl_points": realized_points, "bars_held": i}
                if high >= tp2:
                    realized_points += (tp2 - entry_price) * remaining_pct
                    return {"exit_price": tp2, "exit_reason": "tp2",
                            "pnl_points": realized_points, "bars_held": i}
                # Trail stop
                new_trail = close - stop_dist
                if new_trail > trailing_stop:
                    trailing_stop = new_trail

        else:  # SHORT
            # Check stop
            if high >= trailing_stop:
                realized_points += (entry_price - trailing_stop) * remaining_pct
                return {"exit_price": trailing_stop, "exit_reason": "stopped",
                        "pnl_points": realized_points, "bars_held": i}

            # Check TP1
            if not tp1_hit and low <= tp1:
                realized_points += (entry_price - tp1) * 0.5
                remaining_pct = 0.5
                tp1_hit = True
                trailing_stop = entry_price

            # Check TP2/TP3
            if tp1_hit:
                if low <= tp3:
                    realized_points += (entry_price - tp3) * remaining_pct
                    return {"exit_price": tp3, "exit_reason": "tp3",
                            "pnl_points": realized_points, "bars_held": i}
                if low <= tp2:
                    realized_points += (entry_price - tp2) * remaining_pct
                    return {"exit_price": tp2, "exit_reason": "tp2",
                            "pnl_points": realized_points, "bars_held": i}
                new_trail = close + stop_dist
                if new_trail < trailing_stop:
                    trailing_stop = new_trail

    # Max bars reached — exit at close
    last_close = float(df["Close"].iloc[min(entry_idx + max_bars, len(df) - 1)])
    if direction == "LONG":
        realized_points += (last_close - entry_price) * remaining_pct
    else:
        realized_points += (entry_price - last_close) * remaining_pct

    return {"exit_price": last_close, "exit_reason": "max_bars",
            "pnl_points": realized_points, "bars_held": max_bars}


def print_backtest_report(trades: list, equity_curve: list):
    """Print comprehensive backtest report."""
    print("\n" + "=" * 70)
    print("CUE BANKS BACKTEST REPORT — US30 Multi-TF Confluence")
    print("=" * 70)

    if not trades:
        print("\nNo trades generated. This can happen if:")
        print("  - Market data lacks clear structure/confluence")
        print("  - Daily bias was neutral throughout")
        print("  - No pullback confirmations during NY session")
        print("\nThis is EXPECTED for a high-conviction system (many days = no trade).")
        print(f"\nEquity: ${BACKTEST_EQUITY:,.2f} (unchanged)")
        return

    n = len(trades)
    winners = [t for t in trades if t["pnl_usd"] > 0]
    losers = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(winners) / n * 100 if n > 0 else 0

    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losers)) if losers else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win = np.mean([t["pnl_usd"] for t in winners]) if winners else 0
    avg_loss = np.mean([abs(t["pnl_usd"]) for t in losers]) if losers else 0
    avg_rr = np.mean([t["rr_achieved"] for t in trades])

    # Max drawdown
    peak = BACKTEST_EQUITY
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # By confluence score
    by_score = defaultdict(list)
    for t in trades:
        by_score[t["confluence_score"]].append(t)

    # By exit reason
    by_exit = defaultdict(int)
    for t in trades:
        by_exit[t["exit_reason"]] += 1

    final_equity = equity_curve[-1][1] if equity_curve else BACKTEST_EQUITY

    print(f"\n--- Summary ---")
    print(f"  Total trades:     {n}")
    print(f"  Winners:          {len(winners)} ({win_rate:.1f}%)")
    print(f"  Losers:           {len(losers)} ({100 - win_rate:.1f}%)")
    print(f"  Total PnL:        ${total_pnl:,.2f}")
    print(f"  Profit Factor:    {pf:.2f}")
    print(f"  Avg Win:          ${avg_win:,.2f}")
    print(f"  Avg Loss:         ${avg_loss:,.2f}")
    print(f"  Avg R:R:          {avg_rr:.2f}")
    print(f"  Max Drawdown:     {max_dd:.2f}%")
    print(f"  Starting Equity:  ${BACKTEST_EQUITY:,.2f}")
    print(f"  Final Equity:     ${final_equity:,.2f}")
    print(f"  Return:           {(final_equity - BACKTEST_EQUITY) / BACKTEST_EQUITY * 100:.2f}%")

    print(f"\n--- By Confluence Score ---")
    for score in sorted(by_score.keys()):
        group = by_score[score]
        g_wins = sum(1 for t in group if t["pnl_usd"] > 0)
        g_pnl = sum(t["pnl_usd"] for t in group)
        g_wr = g_wins / len(group) * 100 if group else 0
        print(f"  Score {score}: {len(group)} trades, {g_wr:.0f}% WR, ${g_pnl:,.2f} PnL")

    print(f"\n--- By Exit Reason ---")
    for reason, count in sorted(by_exit.items()):
        print(f"  {reason}: {count}")

    print(f"\n--- Direction ---")
    longs = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]
    if longs:
        l_wr = sum(1 for t in longs if t["pnl_usd"] > 0) / len(longs) * 100
        print(f"  LONG:  {len(longs)} trades, {l_wr:.0f}% WR, ${sum(t['pnl_usd'] for t in longs):,.2f}")
    if shorts:
        s_wr = sum(1 for t in shorts if t["pnl_usd"] > 0) / len(shorts) * 100
        print(f"  SHORT: {len(shorts)} trades, {s_wr:.0f}% WR, ${sum(t['pnl_usd'] for t in shorts):,.2f}")

    # Best and worst trades
    if trades:
        best = max(trades, key=lambda t: t["pnl_usd"])
        worst = min(trades, key=lambda t: t["pnl_usd"])
        print(f"\n--- Notable Trades ---")
        print(f"  Best:  {best['date']} {best['direction']} "
              f"${best['pnl_usd']:,.2f} ({best['rr_achieved']:.1f}R) "
              f"[{best['factors'][:60]}]")
        print(f"  Worst: {worst['date']} {worst['direction']} "
              f"${worst['pnl_usd']:,.2f} ({worst['rr_achieved']:.1f}R) "
              f"[{worst['factors'][:60]}]")

    # Equity curve (sampled)
    print(f"\n--- Equity Curve (sampled) ---")
    step = max(1, len(equity_curve) // 10)
    for i in range(0, len(equity_curve), step):
        date, eq = equity_curve[i]
        bar = "#" * int((eq - BACKTEST_EQUITY * 0.9) / (BACKTEST_EQUITY * 0.01))
        print(f"  {date}: ${eq:>10,.2f} {bar[:40]}")
    if equity_curve:
        date, eq = equity_curve[-1]
        print(f"  {date}: ${eq:>10,.2f} (final)")

    print()


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------

def run_scan():
    """One-shot scan: show current confluence zones and approaching setups."""
    log.info("=" * 70)
    log.info("CUE BANKS SCAN — US30 Confluence Zones")
    log.info("=" * 70)

    # Download recent data
    df_daily = download_data(TICKER, period="6mo", interval="1d")
    df_5m = download_data(TICKER, period="5d", interval="5m")

    df_h4 = resample_to_tf(df_5m, "4h")
    df_h1 = resample_to_tf(df_5m, "1h")

    current_price = float(df_5m["Close"].iloc[-1])
    last_ts = df_5m.index[-1]

    print(f"\n{'=' * 70}")
    print(f"CUE BANKS SCAN — {TICKER}")
    print(f"{'=' * 70}")
    print(f"  Time:  {last_ts}")
    print(f"  Price: {current_price:,.2f}")

    # Daily bias
    daily_structure = detect_structure(df_daily, lookback=20)
    print(f"\n--- Daily Bias ---")
    print(f"  Structure: {daily_structure['bias'].upper()}")

    # H4 bias
    h4_structure = detect_structure(df_h4, lookback=20)
    print(f"  H4 Bias:   {h4_structure['bias'].upper()}")

    # S/R levels
    h4_sr = find_horizontal_sr(df_h4, tolerance_pct=0.001, min_touches=2)
    print(f"\n--- H4 S/R Levels ({len(h4_sr)}) ---")
    for sr in h4_sr:
        dist_pct = (sr["level"] - current_price) / current_price * 100
        tags = []
        if sr["is_round_number"]:
            tags.append("ROUND")
        if sr["is_role_flip"]:
            tags.append("FLIP")
        approaching = abs(dist_pct) < 0.3
        marker = " <<< APPROACHING" if approaching else ""
        print(f"  {sr['type']:>10} @ {sr['level']:>10,.2f}  "
              f"({dist_pct:+.2f}%) touches={sr['touches']} "
              f"{' '.join(tags)}{marker}")

    # Fibonacci
    swing_highs, swing_lows = find_swing_points(df_h4, left=6, right=3)
    fib_levels = {}
    if swing_highs and swing_lows:
        recent_high = max(swing_highs[-3:], key=lambda x: x["price"])["price"]
        recent_low = min(swing_lows[-3:], key=lambda x: x["price"])["price"]
        if recent_high > recent_low:
            fib_levels = compute_fib_levels(recent_high, recent_low)
            print(f"\n--- Fibonacci (H4 swing {recent_low:,.0f} -> {recent_high:,.0f}) ---")
            print(f"  Retracements:")
            for name, level in fib_levels["retracements"].items():
                dist_pct = (level - current_price) / current_price * 100
                approaching = abs(dist_pct) < 0.3
                marker = " <<< APPROACHING" if approaching else ""
                print(f"    {name}: {level:>10,.2f} ({dist_pct:+.2f}%){marker}")
            print(f"  Extensions:")
            for name, level in fib_levels["extensions"].items():
                dist_pct = (level - current_price) / current_price * 100
                print(f"    {name}: {level:>10,.2f} ({dist_pct:+.2f}%)")

    # Current exhaustion check
    exhaustion = detect_exhaustion(df_5m, len(df_5m) - 1)
    print(f"\n--- Current Bar Analysis ---")
    if exhaustion:
        print(f"  Exhaustion: {exhaustion['direction']} (wick ratio {exhaustion['wick_ratio']}x)")
    else:
        print(f"  Exhaustion: None")

    consol = detect_consolidation_break(df_5m, len(df_5m) - 1, lookback=30)
    if consol:
        print(f"  Consolidation Break: {consol['direction']} "
              f"(range {consol['range_low']:,.0f}-{consol['range_high']:,.0f})")
    else:
        print(f"  Consolidation Break: None")

    # Gap check
    today = last_ts.strftime("%Y-%m-%d")
    gap = detect_gap_at_session_open(df_5m, today, min_gap_pct=0.3)
    if gap:
        print(f"  Gap: {gap['direction']} ({gap['gap_size_pct']:.2f}%) "
              f"{'FILLED' if gap['filled'] else 'OPEN'}")
    else:
        print(f"  Gap: None")

    # Score confluence at current price
    result = score_confluence(
        price=current_price,
        sr_levels=h4_sr,
        fib_levels=fib_levels,
        structure=h4_structure,
        exhaustion=exhaustion,
        trendline_break=False,
        consolidation_break=consol,
        gap=gap,
    )

    print(f"\n--- Confluence Score ---")
    print(f"  Score:     {result['score']}")
    print(f"  Tradeable: {'YES' if result['tradeable'] else 'NO'} (need 3+)")
    print(f"  Sniper:    {'YES' if result['sniper'] else 'NO'} (need 4+)")
    print(f"  Direction: {result['direction'] or 'N/A'}")
    print(f"  Factors:")
    for f in result["factors"]:
        print(f"    - {f}")

    if result["tradeable"]:
        print(f"\n  >>> TRADE SETUP <<<")
        print(f"  Entry: {result['entry_price']:,.2f}")
        print(f"  Stop:  {result['stop_price']:,.2f}")
        print(f"  TP1:   {result['tp1']:,.2f} (3.8R)")
        print(f"  TP2:   {result['tp2']:,.2f} (6.2R)")
        print(f"  TP3:   {result['tp3']:,.2f} (7.3R)")

        sizing = compute_cuebanks_size(
            equity=10_000,
            entry_price=result["entry_price"],
            stop_price=result["stop_price"],
            confluence_score=int(result["score"]),
        )
        print(f"\n  Sizing ($10K account):")
        print(f"    Contracts: {sizing['contracts']} MYM")
        print(f"    Risk:      ${sizing['risk_usd']:,.2f} ({sizing['risk_pct']:.2f}%)")
    else:
        # Show nearest zones approaching
        print(f"\n  No active setup. Watching zones:")
        nearby = [sr for sr in h4_sr
                  if abs(sr["level"] - current_price) / current_price < 0.01]
        for sr in nearby[:5]:
            dist = (sr["level"] - current_price) / current_price * 100
            print(f"    {sr['type']} @ {sr['level']:,.2f} ({dist:+.2f}%)")

    print()


# ---------------------------------------------------------------------------
# Loop mode
# ---------------------------------------------------------------------------

def run_loop():
    """Continuous scan every 5 min during NY session."""
    log.info("CUE BANKS LOOP MODE — scanning every 5 min")
    heartbeat_path = LOG_DIR / "heartbeat.json"
    signal_csv = LOG_DIR / "cuebanks_signals.csv"

    while True:
        now = datetime.now()
        # Check if NY session
        h, m = now.hour, now.minute
        t = h * 60 + m
        ny_start = NY_OPEN_HOUR * 60 + NY_OPEN_MIN
        ny_end = NY_CLOSE_HOUR * 60 + NY_CLOSE_MIN

        # Write heartbeat
        heartbeat = {
            "system": "cuebanks",
            "timestamp": now.isoformat(),
            "status": "scanning" if ny_start <= t <= ny_end else "waiting",
            "mode": "research_only",  # 2026-04-17: gated; see RESEARCH_ONLY flag at top
            "run_phase": "loop",
        }
        heartbeat_path.write_text(json.dumps(heartbeat, indent=2))

        if ny_start <= t <= ny_end:
            try:
                log.info(f"Scanning at {now.strftime('%H:%M:%S')}...")
                # Run a quick scan
                df_5m = download_data(TICKER, period="5d", interval="5m")
                df_h4 = resample_to_tf(df_5m, "4h")

                current_price = float(df_5m["Close"].iloc[-1])
                h4_sr = find_horizontal_sr(df_h4)
                h4_structure = detect_structure(df_h4)

                swing_highs, swing_lows = find_swing_points(df_h4, left=6, right=3)
                fib_levels = {}
                if swing_highs and swing_lows:
                    rh = max(swing_highs[-3:], key=lambda x: x["price"])["price"]
                    rl = min(swing_lows[-3:], key=lambda x: x["price"])["price"]
                    if rh > rl:
                        fib_levels = compute_fib_levels(rh, rl)

                exhaustion = detect_exhaustion(df_5m, len(df_5m) - 1)
                consol = detect_consolidation_break(df_5m, len(df_5m) - 1)

                result = score_confluence(
                    price=current_price,
                    sr_levels=h4_sr,
                    fib_levels=fib_levels,
                    structure=h4_structure,
                    exhaustion=exhaustion,
                    trendline_break=False,
                    consolidation_break=consol,
                    gap=None,
                )

                log.info(f"  Price={current_price:,.2f} Score={result['score']} "
                         f"Dir={result['direction']} Tradeable={result['tradeable']}")

                if result["tradeable"]:
                    log.info(f"  >>> SIGNAL: {result['direction']} "
                             f"score={result['score']} factors={result['factors']}")

                    # Append to signal CSV
                    row = {
                        "timestamp": now.isoformat(),
                        "price": current_price,
                        "score": result["score"],
                        "direction": result["direction"],
                        "factors": " | ".join(result["factors"]),
                        "entry": result["entry_price"],
                        "stop": result["stop_price"],
                        "tp1": result["tp1"],
                    }
                    file_exists = signal_csv.exists()
                    with open(signal_csv, "a", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=row.keys())
                        if not file_exists:
                            writer.writeheader()
                        writer.writerow(row)

                    # Conviction scorer integration
                    try:
                        from forge.conviction import score_conviction
                        conv = score_conviction(
                            system="cuebanks", ticker="YM=F",
                            direction=result["direction"],
                            signal_strength=result["score"] / 7.0,
                        )
                        log.info(f"  Conviction: {conv.get('size_multiplier', 1.0):.2f}x")
                    except Exception:
                        pass

            except Exception as e:
                log.error(f"Scan error: {e}")
        else:
            log.info(f"Outside NY session ({now.strftime('%H:%M')}). Waiting...")

        time.sleep(300)  # 5 minutes


# ---------------------------------------------------------------------------
# Live mode (placeholder)
# ---------------------------------------------------------------------------

def run_live():
    """IBKR live trading placeholder — MYM (Micro Dow), client ID 108."""
    log.info("CUE BANKS LIVE MODE — IBKR placeholder")
    print("\n" + "=" * 70)
    print("CUE BANKS LIVE MODE — NOT YET IMPLEMENTED")
    print("=" * 70)
    print(f"  Contract:  MYM (Micro Dow Futures)")
    print(f"  Exchange:  CBOT")
    print(f"  Client ID: {IBKR_CLIENT_ID}")
    print(f"  Strategy:  Multi-TF Confluence (Daily/H4 bias, M5 entry)")
    print(f"  Min Score: 3+ confluence factors")
    print(f"  R:R Target: 1:5 to 1:8")
    print(f"  Max Trades: {MAX_TRADES_PER_DAY}/day")
    print(f"\n  To implement: connect via ib_insync, subscribe to MYM bars,")
    print(f"  run confluence scoring in real-time, execute on signal.")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cue Banks Runner — US30 Multi-TF Confluence Trading"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backtest", action="store_true", help="Run backtest")
    group.add_argument("--scan", action="store_true", help="One-shot scan")
    group.add_argument("--loop", action="store_true", help="Continuous loop")
    group.add_argument("--live", action="store_true", help="IBKR live (placeholder)")

    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.scan:
        run_scan()
    elif args.loop:
        run_loop()
    elif args.live:
        run_live()


if __name__ == "__main__":
    main()
