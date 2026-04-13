"""
Mamba Runner — NAS100 5-Min Trendline Breakout Strategy
=========================================================
Modes:
  --backtest    Download NQ=F 5-min data, run full backtest, report results
  --scan        One-shot scan for current trendline setups
  --signal-only Stream 5-min bars, log signals (no execution)
  --live        Full execution via IBKR (MNQ, client ID 102)
  --loop        Continuous signal-only, re-scan every 5 minutes

Usage:
  python -m forge.mamba.runner --backtest
  python -m forge.mamba.runner --scan
  python -m forge.mamba.runner --signal-only
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mamba")

# Session times in ET (Eastern Time)
SESSION_LONDON_START = 3   # 03:00 ET
SESSION_NY_START = 9       # 09:30 ET (we use 9 for simplicity)
SESSION_END = 11           # 11:30 ET
SESSION_OVERLAP_START = 9  # London + NY overlap

TICKER = "NQ=F"
IBKR_CLIENT_ID = 102
MNQ_POINT_VALUE = 2.0  # $2 per point for MNQ

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def download_data(ticker: str = TICKER, period: str = "60d", interval: str = "5m") -> pd.DataFrame:
    """Download intraday data via yfinance."""
    import yfinance as yf

    log.info(f"Downloading {ticker} | period={period} interval={interval}")
    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        log.warning(f"No data for {ticker}, trying ^NDX as proxy")
        df = yf.download("^NDX", period=period, interval=interval, progress=False)

    if df.empty:
        raise RuntimeError(f"Could not download data for {ticker}")

    # Flatten multi-level columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    # Ensure timezone-aware index in US/Eastern
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("US/Eastern")

    df = df.sort_index()
    log.info(f"Downloaded {len(df)} bars from {df.index[0]} to {df.index[-1]}")
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

    # Wilder's smoothing
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    df["ATR"] = atr
    return df


def in_session(ts) -> Optional[str]:
    """Check if timestamp is in allowed trading session (03:00-11:30 ET).
    Returns session name or None."""
    hour = ts.hour
    minute = ts.minute

    if hour < SESSION_LONDON_START:
        return None
    if hour > SESSION_END or (hour == SESSION_END and minute > 30):
        return None

    if hour >= SESSION_OVERLAP_START:
        return "NY" if hour >= 10 else "overlap"
    return "London"


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, min_touches: int = 3, rr_target: float = 4.0) -> list[dict]:
    """
    Walk through each bar, scan for trendline breakouts, simulate trades.
    """
    from forge.mamba.trendlines import (
        find_pivot_highs, find_pivot_lows, fit_trendline,
        count_touches, detect_breakout, trendline_value_at,
        MIN_TRENDLINE_BARS,
    )

    trades = []
    open_trade = None
    lookback = 100  # minimum bars before we start scanning

    log.info(f"Running backtest on {len(df)} bars, min_touches={min_touches}, R:R target={rr_target}")

    for i in range(lookback, len(df)):
        bar_time = df.index[i]

        # --- Manage open trade ---
        if open_trade is not None:
            high_i = df["High"].iloc[i]
            low_i = df["Low"].iloc[i]
            close_i = df["Close"].iloc[i]

            hit_stop = False
            hit_target = False

            if open_trade["direction"] == "LONG":
                if low_i <= open_trade["stop"]:
                    hit_stop = True
                if high_i >= open_trade["target"]:
                    hit_target = True
            else:  # SHORT
                if high_i >= open_trade["stop"]:
                    hit_stop = True
                if low_i <= open_trade["target"]:
                    hit_target = True

            if hit_stop or hit_target:
                if hit_target and hit_stop:
                    # Assume stop hit first if open is beyond stop (conservative)
                    exit_price = open_trade["stop"]
                    outcome = "stop"
                elif hit_target:
                    exit_price = open_trade["target"]
                    outcome = "target"
                else:
                    exit_price = open_trade["stop"]
                    outcome = "stop"

                if open_trade["direction"] == "LONG":
                    pnl_points = exit_price - open_trade["entry"]
                else:
                    pnl_points = open_trade["entry"] - exit_price

                stop_dist = abs(open_trade["entry"] - open_trade["stop"])
                rr_achieved = pnl_points / stop_dist if stop_dist > 0 else 0

                trade_record = {
                    **open_trade,
                    "exit_time": str(bar_time),
                    "exit_price": round(exit_price, 2),
                    "pnl_points": round(pnl_points, 2),
                    "pnl_usd": round(pnl_points * MNQ_POINT_VALUE, 2),
                    "rr_achieved": round(rr_achieved, 2),
                    "outcome": outcome,
                    "bars_held": i - open_trade["bar_index"],
                }
                trades.append(trade_record)
                open_trade = None
            continue  # Don't open a new trade while one is open

        # --- Session filter ---
        session = in_session(bar_time)
        if session is None:
            continue

        # --- Scan for breakouts ---
        window = df.iloc[max(0, i - 200) : i + 1].copy()
        if len(window) < lookback // 2:
            continue

        # Recompute on window
        window = compute_atr(window)
        if window["ATR"].iloc[-1] != window["ATR"].iloc[-1]:  # NaN check
            continue

        current_atr = window["ATR"].iloc[-1]
        if current_atr <= 0:
            continue

        # Find pivots and trendlines in the window
        pivot_highs = find_pivot_highs(window)
        pivot_lows = find_pivot_lows(window)

        best_setup = None
        best_score = 0

        # Check descending trendlines (for bullish breakouts)
        for end_idx in range(len(pivot_highs) - 1, 0, -1):
            for start_idx in range(end_idx - 1, max(end_idx - 4, -1), -1):
                pair = [pivot_highs[start_idx], pivot_highs[end_idx]]
                tl = fit_trendline(pair)
                if tl is None:
                    continue
                if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                    continue

                last_idx = len(window) - 1
                extended = {**tl, "end_bar": last_idx}
                touches = count_touches(window, extended)
                if touches < min_touches:
                    continue

                bo = detect_breakout(window, extended, last_idx, current_atr)
                if bo and (touches > best_score):
                    best_setup = {"trendline": extended, "breakout": bo, "touches": touches}
                    best_score = touches

        # Check ascending trendlines (for bearish breakouts)
        for end_idx in range(len(pivot_lows) - 1, 0, -1):
            for start_idx in range(end_idx - 1, max(end_idx - 4, -1), -1):
                pair = [pivot_lows[start_idx], pivot_lows[end_idx]]
                tl = fit_trendline(pair)
                if tl is None:
                    continue
                if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                    continue

                last_idx = len(window) - 1
                extended = {**tl, "end_bar": last_idx}
                touches = count_touches(window, extended)
                if touches < min_touches:
                    continue

                bo = detect_breakout(window, extended, last_idx, current_atr)
                if bo and (touches > best_score):
                    best_setup = {"trendline": extended, "breakout": bo, "touches": touches}
                    best_score = touches

        if best_setup is None:
            continue

        bo = best_setup["breakout"]
        tl = best_setup["trendline"]
        entry_price = bo["break_price"]

        # Stop placement: use the most recent pivot on the other side of the trendline
        # For LONG: stop below last swing low
        # For SHORT: stop above last swing high
        if bo["direction"] == "LONG":
            if pivot_lows:
                stop_price = min(p["price"] for p in pivot_lows[-3:])
            else:
                stop_price = entry_price - current_atr * 2
        else:
            if pivot_highs:
                stop_price = max(p["price"] for p in pivot_highs[-3:])
            else:
                stop_price = entry_price + current_atr * 2

        stop_dist = abs(entry_price - stop_price)

        # Sanity: skip if stop is too tight or too wide
        if stop_dist < current_atr * 0.3 or stop_dist > current_atr * 5:
            continue

        # Target at R:R ratio
        if bo["direction"] == "LONG":
            target_price = entry_price + stop_dist * rr_target
        else:
            target_price = entry_price - stop_dist * rr_target

        open_trade = {
            "entry_time": str(bar_time),
            "direction": bo["direction"],
            "entry": round(entry_price, 2),
            "stop": round(stop_price, 2),
            "target": round(target_price, 2),
            "stop_dist": round(stop_dist, 2),
            "touches": best_setup["touches"],
            "volume_ratio": bo["volume_ratio"],
            "session": session,
            "bar_index": i,
        }

    # Close any still-open trade at last close
    if open_trade is not None:
        last_close = df["Close"].iloc[-1]
        if open_trade["direction"] == "LONG":
            pnl_points = last_close - open_trade["entry"]
        else:
            pnl_points = open_trade["entry"] - last_close
        stop_dist = abs(open_trade["entry"] - open_trade["stop"])
        rr_achieved = pnl_points / stop_dist if stop_dist > 0 else 0
        trade_record = {
            **open_trade,
            "exit_time": str(df.index[-1]),
            "exit_price": round(float(last_close), 2),
            "pnl_points": round(float(pnl_points), 2),
            "pnl_usd": round(float(pnl_points) * MNQ_POINT_VALUE, 2),
            "rr_achieved": round(float(rr_achieved), 2),
            "outcome": "open_at_end",
            "bars_held": len(df) - 1 - open_trade["bar_index"],
        }
        trades.append(trade_record)

    return trades


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_backtest_report(trades: list[dict]):
    """Print comprehensive backtest results."""
    if not trades:
        print("\n=== MAMBA BACKTEST REPORT ===")
        print("No trades generated.")
        return

    print("\n" + "=" * 70)
    print("  MAMBA BACKTEST REPORT — NAS100 5-Min Trendline Breakout")
    print("=" * 70)

    n = len(trades)
    winners = [t for t in trades if t["pnl_points"] > 0]
    losers = [t for t in trades if t["pnl_points"] <= 0]
    win_rate = len(winners) / n * 100

    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losers)) if losers else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win_rr = np.mean([t["rr_achieved"] for t in winners]) if winners else 0
    avg_loss_rr = np.mean([t["rr_achieved"] for t in losers]) if losers else 0

    print(f"\n  Total trades:      {n}")
    print(f"  Winners:           {len(winners)}  ({win_rate:.1f}%)")
    print(f"  Losers:            {len(losers)}  ({100 - win_rate:.1f}%)")
    print(f"\n  Total P&L:         ${total_pnl:,.2f}")
    print(f"  Gross profit:      ${gross_profit:,.2f}")
    print(f"  Gross loss:        ${gross_loss:,.2f}")
    print(f"  Profit factor:     {pf:.2f}")
    print(f"\n  Avg R:R winners:   {avg_win_rr:.2f}")
    print(f"  Avg R:R losers:    {avg_loss_rr:.2f}")

    if trades:
        best = max(trades, key=lambda t: t["pnl_usd"])
        worst = min(trades, key=lambda t: t["pnl_usd"])
        print(f"\n  Best trade:        ${best['pnl_usd']:,.2f}  ({best['direction']} @ {best['entry_time'][:16]})")
        print(f"  Worst trade:       ${worst['pnl_usd']:,.2f}  ({worst['direction']} @ {worst['entry_time'][:16]})")

    # P&L curve
    print(f"\n  --- Equity Curve ---")
    cumulative = 0
    curve_points = []
    for t in trades:
        cumulative += t["pnl_usd"]
        curve_points.append(cumulative)

    if curve_points:
        peak = max(curve_points)
        drawdowns = [peak - c for c in curve_points]
        max_dd = max(drawdowns) if drawdowns else 0
        print(f"  Peak equity:       ${peak:,.2f}")
        print(f"  Max drawdown:      ${max_dd:,.2f}")
        print(f"  Final equity:      ${cumulative:,.2f}")

        # Simple ASCII curve
        if len(curve_points) > 1:
            mn, mx = min(curve_points), max(curve_points)
            rng = mx - mn if mx != mn else 1
            width = 50
            print(f"\n  P&L: ", end="")
            for cp in curve_points:
                bar_pos = int((cp - mn) / rng * width)
                print("+" if cp >= 0 else "-", end="")
            print()

    # By session
    print(f"\n  --- By Session ---")
    sessions = {}
    for t in trades:
        s = t.get("session", "unknown")
        if s not in sessions:
            sessions[s] = {"count": 0, "pnl": 0, "wins": 0}
        sessions[s]["count"] += 1
        sessions[s]["pnl"] += t["pnl_usd"]
        if t["pnl_points"] > 0:
            sessions[s]["wins"] += 1

    for s, data in sorted(sessions.items()):
        wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
        print(f"  {s:12s}  trades={data['count']:3d}  P&L=${data['pnl']:8,.2f}  WR={wr:.0f}%")

    # By touch count
    print(f"\n  --- By Touch Count ---")
    touch_groups = {}
    for t in trades:
        tc = t.get("touches", 0)
        if tc not in touch_groups:
            touch_groups[tc] = {"count": 0, "pnl": 0, "wins": 0}
        touch_groups[tc]["count"] += 1
        touch_groups[tc]["pnl"] += t["pnl_usd"]
        if t["pnl_points"] > 0:
            touch_groups[tc]["wins"] += 1

    for tc, data in sorted(touch_groups.items()):
        wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
        print(f"  {tc} touches:    trades={data['count']:3d}  P&L=${data['pnl']:8,.2f}  WR={wr:.0f}%")

    # Trade log
    print(f"\n  --- Trade Log (last 20) ---")
    print(f"  {'Time':<18s} {'Dir':5s} {'Entry':>10s} {'Exit':>10s} {'P&L':>10s} {'R:R':>6s} {'Tch':>4s} {'Session':>8s}")
    print(f"  {'-'*18} {'-'*5} {'-'*10} {'-'*10} {'-'*10} {'-'*6} {'-'*4} {'-'*8}")
    for t in trades[-20:]:
        entry_short = t["entry_time"][5:16] if len(t["entry_time"]) > 16 else t["entry_time"]
        print(
            f"  {entry_short:<18s} {t['direction']:5s} {t['entry']:10.2f} "
            f"{t['exit_price']:10.2f} {t['pnl_usd']:10.2f} {t['rr_achieved']:6.2f} "
            f"{t['touches']:4d} {t.get('session', '?'):>8s}"
        )

    print("\n" + "=" * 70)


def save_trades_csv(trades: list[dict], path: Path):
    """Save trades to CSV."""
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(trades[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)
    log.info(f"Saved {len(trades)} trades to {path}")


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------

def run_scan():
    """One-shot scan: download recent data, show current trendline setups."""
    from forge.mamba.trendlines import scan_for_setups, trendline_value_at

    df = download_data(period="60d")
    df = compute_atr(df)

    setups = scan_for_setups(df, min_touches=3)

    print("\n" + "=" * 70)
    print("  MAMBA SCAN — Current NAS100 Trendline Setups")
    print("=" * 70)
    print(f"  Data: {len(df)} bars | Last: {df.index[-1]}")
    print(f"  Current ATR(14): {df['ATR'].iloc[-1]:.2f}")
    print(f"  Current Close:   {df['Close'].iloc[-1]:.2f}")

    if not setups:
        print("\n  No active trendline setups with 3+ touches.")
    else:
        print(f"\n  Found {len(setups)} setup(s):\n")
        for idx, s in enumerate(setups[:10], 1):
            tl = s["trendline"]
            tl_now = s["tl_value_now"]
            close = s["current_close"]
            dist = close - tl_now if tl["direction"] == "descending" else tl_now - close
            dist_pct = dist / s["current_atr"] * 100 if s["current_atr"] > 0 else 0

            status = "** BREAKOUT **" if s["breakout"] else f"{dist:.1f} pts ({dist_pct:.0f}% ATR) away"

            print(f"  [{idx}] {tl['direction'].upper()} trendline")
            print(f"      Touches: {s['touches']}  |  Conviction: {s['conviction']:.0f}/100")
            print(f"      Slope: {tl['slope']:.4f}  |  TL value now: {tl_now:.2f}")
            print(f"      Duration: {tl['end_bar'] - tl['start_bar']} bars")
            print(f"      Status: {status}")
            if s["breakout"]:
                bo = s["breakout"]
                print(f"      Breakout: {bo['direction']} @ {bo['break_price']:.2f}  (vol ratio {bo['volume_ratio']:.1f}x)")
            print()

    print("=" * 70)


# ---------------------------------------------------------------------------
# Signal-only / live mode helpers
# ---------------------------------------------------------------------------

def write_heartbeat():
    """Write heartbeat file."""
    hb = {
        "system": "mamba",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "ticker": TICKER,
    }
    hb_path = LOG_DIR / "heartbeat.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hb_path, "w") as f:
        json.dump(hb, f, indent=2)


def append_signal(signal: dict):
    """Append signal to signals CSV."""
    sig_path = LOG_DIR / "signals.csv"
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = sig_path.exists()
    with open(sig_path, "a", newline="") as f:
        fieldnames = ["timestamp", "direction", "entry", "stop", "target",
                       "touches", "conviction", "volume_ratio", "session"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(signal)


def run_signal_loop(loop: bool = False):
    """Signal-only mode: download data, scan, log signals. Optionally loop."""
    from forge.mamba.trendlines import scan_for_setups

    try:
        from forge.conviction import score_conviction
    except ImportError:
        score_conviction = None
        log.warning("Could not import score_conviction — running without fleet conviction")

    while True:
        try:
            write_heartbeat()
            df = download_data(period="5d")
            df = compute_atr(df)

            setups = scan_for_setups(df, min_touches=3)

            for s in setups:
                if s["breakout"]:
                    bo = s["breakout"]
                    direction = bo["direction"]

                    # Conviction scoring
                    conv_mult = 1.0
                    if score_conviction:
                        try:
                            result = score_conviction("mamba", TICKER, direction)
                            conv_mult = result.get("size_multiplier", 1.0)
                        except Exception as e:
                            log.warning(f"Conviction scorer error: {e}")

                    session = in_session(df.index[-1])
                    signal = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "direction": direction,
                        "entry": bo["break_price"],
                        "stop": 0,  # Would need pivot calc
                        "target": 0,
                        "touches": s["touches"],
                        "conviction": conv_mult,
                        "volume_ratio": bo["volume_ratio"],
                        "session": session or "off-hours",
                    }
                    append_signal(signal)
                    log.info(f"SIGNAL: {direction} NQ=F | touches={s['touches']} | vol={bo['volume_ratio']:.1f}x | conviction={conv_mult:.2f}")

            if not loop:
                break

            log.info("Sleeping 5 minutes before next scan...")
            time.sleep(300)

        except KeyboardInterrupt:
            log.info("Interrupted.")
            break
        except Exception as e:
            log.error(f"Error in signal loop: {e}")
            if not loop:
                break
            time.sleep(60)


def run_live():
    """Placeholder for live IBKR execution."""
    log.error("Live mode not yet implemented. Use --signal-only to generate signals.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mamba — NAS100 Trendline Breakout")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest", action="store_true", help="Run full backtest")
    mode.add_argument("--scan", action="store_true", help="One-shot scan for setups")
    mode.add_argument("--signal-only", action="store_true", help="Signal-only mode")
    mode.add_argument("--live", action="store_true", help="Live execution via IBKR")
    mode.add_argument("--loop", action="store_true", help="Continuous signal-only")

    parser.add_argument("--min-touches", type=int, default=3, help="Minimum trendline touches")
    parser.add_argument("--rr-target", type=float, default=4.0, help="R:R target for backtest")

    args = parser.parse_args()

    if args.backtest:
        df = download_data(period="60d")
        df = compute_atr(df)
        log.info(f"Data shape: {df.shape}, ATR range: {df['ATR'].min():.2f} - {df['ATR'].max():.2f}")

        trades = run_backtest(df, min_touches=args.min_touches, rr_target=args.rr_target)
        print_backtest_report(trades)
        save_trades_csv(trades, LOG_DIR / "backtest_trades.csv")

    elif args.scan:
        run_scan()

    elif args.signal_only:
        run_signal_loop(loop=False)

    elif args.loop:
        run_signal_loop(loop=True)

    elif args.live:
        run_live()


if __name__ == "__main__":
    main()
