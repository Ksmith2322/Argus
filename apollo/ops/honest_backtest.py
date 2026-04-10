"""apollo/ops/honest_backtest.py -- Honest blind-entry earnings backtest.

This is the REAL test. Previous backtest was post-ER drift (buy AFTER knowing
the result). This one simulates what we actually do:

  1. Enter at market close the day BEFORE earnings (blind — don't know result)
  2. Direction based on beat rate + BB squeeze + setup quality (same scoring)
  3. Minimum 2-day hold (PDT constraint)
  4. Exit rules: trailing stop, target, timeout — same as live

This will give us the TRUE expected PF for Apollo's pre-earnings entry strategy.

Usage:
    python -m apollo.ops.honest_backtest
    python -m apollo.ops.honest_backtest --symbols NVDA AMD TSM
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: pip install yfinance")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO / "apollo" / "data" / "backtest_results"

# Same universe as Apollo
from apollo.ops.earnings_calendar import FULL_UNIVERSE


def run_honest_backtest(
    symbols: list[str] | None = None,
    min_beat_rate: float = 0.60,
    max_hold_days: int = 20,
    pdt_min_hold: int = 2,
    stop_pct: float = 8.0,
    target_pct: float = 20.0,
    trail_trigger_pct: float = 5.0,
    trail_pct: float = 5.0,
):
    """Run the honest blind-entry backtest.

    For each stock with earnings history:
      1. Find all past earnings dates
      2. For each ER: check beat_rate of PRIOR quarters (no future peek)
      3. If beat_rate >= threshold: enter long at close day before ER
      4. Simulate PDT-constrained exit (min 2-day hold)
      5. Apply stop/target/trailing/timeout
    """
    syms = symbols or FULL_UNIVERSE
    all_trades = []

    print(f"{'='*70}")
    print("APOLLO HONEST BACKTEST (Blind Entry + PDT Constraint)")
    print(f"{'='*70}")
    print(f"  Universe: {len(syms)} stocks")
    print(f"  Min beat rate: {min_beat_rate:.0%}")
    print(f"  PDT min hold: {pdt_min_hold} days")
    print(f"  Stop: {stop_pct}% | Target: {target_pct}% | Trail: {trail_pct}% after {trail_trigger_pct}%")
    print()

    for i, sym in enumerate(syms):
        try:
            ticker = yf.Ticker(sym)

            # Get earnings history
            hist = ticker.earnings_history
            if hist is None or len(hist) < 4:
                continue

            # Get price data (2 years daily)
            df = ticker.history(period="2y", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 100:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df["Close"]
            high = df["High"]
            low = df["Low"]

            # Get BB width for squeeze detection
            sma_20 = close.rolling(20).mean()
            std_20 = close.rolling(20).std()
            bb_width = (2 * std_20) / sma_20

            # Process each earnings date
            earnings_rows = list(hist.iterrows())

            for q_idx in range(len(earnings_rows)):
                q_date_raw, row = earnings_rows[q_idx]
                surprise = row.get("surprisePercent", 0)
                if surprise is None or np.isnan(surprise):
                    continue

                # Get quarter date
                q_date = q_date_raw.date() if hasattr(q_date_raw, 'date') else q_date_raw

                # PRIOR beat rate (only quarters BEFORE this one — no future peek)
                prior_quarters = earnings_rows[q_idx + 1:]  # history is reverse chronological
                if len(prior_quarters) < 3:
                    continue  # need at least 3 prior quarters

                prior_surprises = []
                for _, pr in prior_quarters[:8]:  # up to 8 prior quarters
                    ps = pr.get("surprisePercent", 0)
                    if ps is not None and not np.isnan(ps):
                        prior_surprises.append(ps)

                if len(prior_surprises) < 3:
                    continue

                beat_rate = sum(1 for s in prior_surprises if s > 0.01) / len(prior_surprises)

                # Decision: do we enter?
                if beat_rate < min_beat_rate:
                    continue  # skip — not enough historical beats

                # Direction: only longs (shorts don't work per our backtest)
                direction = "long"

                # Check BB squeeze at entry time
                entry_date_target = q_date - timedelta(days=1)

                # Find the trading day closest to day before earnings
                mask = close.index.date <= entry_date_target
                if mask.sum() < 30:
                    continue
                pre_er_idx = close.index[mask][-1]
                entry_price = float(close[pre_er_idx])

                # BB percentile at entry
                bb_at_entry = bb_width.get(pre_er_idx, np.nan)
                bb_vals = bb_width[:pre_er_idx].dropna().values
                bb_pctile = np.searchsorted(np.sort(bb_vals), bb_at_entry) / len(bb_vals) * 100 if len(bb_vals) > 20 and not np.isnan(bb_at_entry) else 50

                # Simple score: beat_rate + squeeze bonus
                score = int(beat_rate * 60)
                if bb_pctile < 20:
                    score += 20
                elif bb_pctile < 40:
                    score += 10

                if score < 50:
                    continue  # not high enough conviction

                # Set stop/target
                stop_price = entry_price * (1 - stop_pct / 100)
                target_price = entry_price * (1 + target_pct / 100)

                # Simulate forward from entry
                post_mask = close.index.date > entry_date_target
                if post_mask.sum() < pdt_min_hold + 1:
                    continue

                post_dates = close.index[post_mask]
                exit_reason = None
                exit_price = entry_price
                days_held = 0
                peak_price = entry_price
                trailing_stop = None

                for j, trade_date in enumerate(post_dates):
                    days_held = j + 1
                    cur_close = float(close[trade_date])
                    cur_high = float(high[trade_date])
                    cur_low = float(low[trade_date])

                    # Track peak for trailing stop
                    peak_price = max(peak_price, cur_high)

                    # PDT: cannot exit before day 2
                    if days_held < pdt_min_hold:
                        continue

                    # Stop loss
                    if cur_low <= stop_price:
                        exit_reason = "stop"
                        exit_price = stop_price
                        break

                    # Target
                    if cur_high >= target_price:
                        exit_reason = "target"
                        exit_price = target_price
                        break

                    # Trailing stop (activate after trail_trigger_pct profit)
                    unrealized = (cur_close - entry_price) / entry_price * 100
                    if unrealized >= trail_trigger_pct:
                        new_trail = peak_price * (1 - trail_pct / 100)
                        if trailing_stop is None or new_trail > trailing_stop:
                            trailing_stop = new_trail
                        if cur_low <= trailing_stop:
                            exit_reason = "trailing_stop"
                            exit_price = trailing_stop
                            break

                    # Timeout
                    if days_held >= max_hold_days:
                        exit_reason = "timeout"
                        exit_price = cur_close
                        break

                if exit_reason is None:
                    # Ran out of data
                    continue

                pnl_pct = (exit_price - entry_price) / entry_price * 100

                all_trades.append({
                    "symbol": sym,
                    "direction": direction,
                    "earnings_date": str(q_date),
                    "entry_date": str(pre_er_idx.date()),
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": exit_reason,
                    "days_held": days_held,
                    "beat_rate": round(beat_rate, 2),
                    "actual_surprise": round(surprise * 100, 1),
                    "bb_pctile": round(bb_pctile, 0),
                    "score": score,
                })

        except Exception:
            continue

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(syms)} scanned ({len(all_trades)} trades so far)...")
            time.sleep(0.3)

    # Results
    if not all_trades:
        print("\n  No trades generated.")
        return {}

    pnls = [t["pnl_pct"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    exits = defaultdict(int)
    for t in all_trades:
        exits[t["exit_reason"]] += 1

    # Drawdown
    peak_eq = 0
    cum_eq = 0
    max_dd = 0
    for p in pnls:
        cum_eq += p
        peak_eq = max(peak_eq, cum_eq)
        max_dd = max(max_dd, peak_eq - cum_eq)

    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999

    print(f"\n{'='*70}")
    print("RESULTS (Honest Blind Entry + PDT)")
    print(f"{'='*70}")
    print(f"  Stocks tested:  {len(set(t['symbol'] for t in all_trades))}")
    print(f"  Total trades:   {len(all_trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:       {len(wins)/len(pnls)*100:.1f}%")
    print(f"  Profit Factor:  {pf}")
    print(f"  Total PnL:      {sum(pnls):+.1f}%")
    print(f"  Expectancy:     {np.mean(pnls):+.2f}%/trade")
    print(f"  Avg Win:        {np.mean(wins):+.1f}%" if wins else "")
    print(f"  Avg Loss:       {np.mean(losses):+.1f}%" if losses else "")
    print(f"  Max Drawdown:   {max_dd:.1f}%")
    print(f"  Avg Hold:       {np.mean([t['days_held'] for t in all_trades]):.1f} days")
    print(f"  Exits:          {dict(exits)}")

    # How often did we bet on a beat and actually get one?
    actual_beats = sum(1 for t in all_trades if t["actual_surprise"] > 0)
    print(f"\n  Bet on beat: {len(all_trades)} times | Actually beat: {actual_beats} ({actual_beats/len(all_trades)*100:.0f}%)")

    # PnL split: when beat happened vs didn't
    beat_trades = [t for t in all_trades if t["actual_surprise"] > 0]
    miss_trades = [t for t in all_trades if t["actual_surprise"] <= 0]
    if beat_trades:
        bt_pnls = [t["pnl_pct"] for t in beat_trades]
        print(f"  When beat: {len(beat_trades)} trades, WR {sum(1 for p in bt_pnls if p>0)/len(bt_pnls)*100:.0f}%, PnL {sum(bt_pnls):+.1f}%")
    if miss_trades:
        mt_pnls = [t["pnl_pct"] for t in miss_trades]
        print(f"  When miss: {len(miss_trades)} trades, WR {sum(1 for p in mt_pnls if p>0)/len(mt_pnls)*100:.0f}%, PnL {sum(mt_pnls):+.1f}%")

    # By score bucket
    print(f"\n  By Score:")
    for lo, hi in [(50, 60), (60, 70), (70, 80), (80, 100)]:
        bucket = [t for t in all_trades if lo <= t["score"] < hi]
        if bucket:
            b_pnls = [t["pnl_pct"] for t in bucket]
            b_wins = sum(1 for p in b_pnls if p > 0)
            b_pf = sum(p for p in b_pnls if p > 0) / abs(sum(p for p in b_pnls if p <= 0)) if any(p <= 0 for p in b_pnls) else 999
            print(f"    Score {lo}-{hi}: {len(bucket)} trades | WR {b_wins/len(bucket)*100:.0f}% | "
                  f"PF {b_pf:.2f} | PnL {sum(b_pnls):+.1f}% | Exp {np.mean(b_pnls):+.2f}%")

    # Top/bottom performers
    by_sym = defaultdict(list)
    for t in all_trades:
        by_sym[t["symbol"]].append(t["pnl_pct"])
    sorted_syms = sorted(by_sym.items(), key=lambda x: sum(x[1]), reverse=True)

    print(f"\n  Top 10 Performers:")
    for sym, pnl_list in sorted_syms[:10]:
        w = sum(1 for p in pnl_list if p > 0)
        print(f"    {sym:8s} | {len(pnl_list)} trades | WR {w/len(pnl_list)*100:.0f}% | PnL {sum(pnl_list):+.1f}%")

    print(f"\n  Bottom 10:")
    for sym, pnl_list in sorted_syms[-10:]:
        w = sum(1 for p in pnl_list if p > 0)
        print(f"    {sym:8s} | {len(pnl_list)} trades | WR {w/len(pnl_list)*100:.0f}% | PnL {sum(pnl_list):+.1f}%")

    # Save
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = RESULTS_DIR / f"honest_bt_{ts}.csv"
    pd.DataFrame(all_trades).to_csv(out_path, index=False)
    print(f"\n  Saved: {out_path}")

    return {
        "trades": len(all_trades),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "pf": pf,
        "total_pnl": round(sum(pnls), 1),
        "expectancy": round(np.mean(pnls), 2),
        "max_dd": round(max_dd, 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Apollo Honest Backtest")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols")
    parser.add_argument("--min-beat", type=float, default=0.60, help="Min beat rate to enter (default 0.60)")
    parser.add_argument("--stop", type=float, default=8.0, help="Stop loss %% (default 8)")
    parser.add_argument("--target", type=float, default=20.0, help="Target %% (default 20)")
    args = parser.parse_args()

    run_honest_backtest(
        symbols=args.symbols,
        min_beat_rate=args.min_beat,
        stop_pct=args.stop,
        target_pct=args.target,
    )


if __name__ == "__main__":
    main()
