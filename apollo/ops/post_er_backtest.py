"""apollo/ops/post_er_backtest.py -- Post-earnings drift backtest (the REAL strategy).

Enter Day 2 after earnings IF:
  - Beat confirmed (surprise > 0)
  - Day 1 close is ABOVE pre-ER close (market confirmed the beat)
  - No same-day exit (PDT safe by design)

Hold 5-20 days with trailing stop.
This is what actually works — entering AFTER the result is known and confirmed.

Usage:
    python -m apollo.ops.post_er_backtest
    python -m apollo.ops.post_er_backtest --min-surprise 2
    python -m apollo.ops.post_er_backtest --symbols NVDA AMD TSM
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit(1)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
RESULTS_DIR = REPO / "apollo" / "data" / "backtest_results"

from apollo.ops.earnings_calendar import FULL_UNIVERSE


def run_post_er_backtest(
    symbols: list[str] | None = None,
    min_surprise_pct: float = 0.0,
    require_gap_up: bool = True,
    max_hold_days: int = 20,
    stop_pct: float = 5.0,
    trail_trigger_pct: float = 3.0,
    trail_pct: float = 4.0,
):
    """Backtest: enter Day 2 after confirmed beat, ride the drift."""
    syms = symbols or FULL_UNIVERSE
    all_trades = []

    print(f"{'='*70}")
    print("APOLLO POST-ER DRIFT BACKTEST (Enter Day 2 After Confirmed Beat)")
    print(f"{'='*70}")
    print(f"  Universe: {len(syms)} stocks")
    print(f"  Min surprise: {min_surprise_pct}% | Require gap up: {require_gap_up}")
    print(f"  Stop: {stop_pct}% | Trail: {trail_pct}% after {trail_trigger_pct}%")
    print(f"  Max hold: {max_hold_days}d")
    print()

    for i, sym in enumerate(syms):
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.earnings_history
            if hist is None or len(hist) < 2:
                continue

            df = ticker.history(period="2y", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 60:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df["Close"]
            high = df["High"]
            low = df["Low"]

            for q_idx, (q_date_raw, row) in enumerate(hist.iterrows()):
                surprise = row.get("surprisePercent", 0)
                if surprise is None or np.isnan(surprise):
                    continue
                surprise_pct = surprise * 100

                # Only enter on beats
                if surprise_pct < min_surprise_pct:
                    continue

                q_date = q_date_raw.date() if hasattr(q_date_raw, 'date') else q_date_raw

                # Find Day 0 (pre-ER close), Day 1 (post-ER), Day 2 (entry)
                mask_pre = close.index.date <= q_date
                mask_post = close.index.date > q_date
                if mask_pre.sum() < 5 or mask_post.sum() < max_hold_days + 3:
                    continue

                pre_er_close = float(close[close.index[mask_pre][-1]])
                post_dates = close.index[mask_post]

                day1_close = float(close[post_dates[0]])
                day1_high = float(high[post_dates[0]])
                day1_low = float(low[post_dates[0]])

                # Gap check: Day 1 close must be above pre-ER close (market confirmed beat)
                gap_pct = (day1_close - pre_er_close) / pre_er_close * 100
                if require_gap_up and day1_close <= pre_er_close:
                    continue  # market didn't confirm the beat — skip

                # Enter at Day 2 open (approximated by Day 2 close for simplicity)
                if len(post_dates) < 3:
                    continue
                entry_price = float(close[post_dates[1]])  # Day 2
                entry_date = post_dates[1]

                stop_price = entry_price * (1 - stop_pct / 100)
                target_price = entry_price * (1 + 20.0 / 100)  # 20% target

                # Simulate forward
                exit_reason = None
                exit_price = entry_price
                days_held = 0
                peak_price = entry_price
                trailing_stop = None

                for j in range(2, min(len(post_dates), max_hold_days + 3)):
                    td = post_dates[j]
                    days_held = j - 1  # days from entry (Day 2)
                    cur_close = float(close[td])
                    cur_high = float(high[td])
                    cur_low = float(low[td])
                    peak_price = max(peak_price, cur_high)

                    # Stop
                    if cur_low <= stop_price:
                        exit_reason = "stop"
                        exit_price = stop_price
                        break

                    # Target
                    if cur_high >= target_price:
                        exit_reason = "target"
                        exit_price = target_price
                        break

                    # Trailing stop
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
                    continue

                pnl_pct = (exit_price - entry_price) / entry_price * 100

                all_trades.append({
                    "symbol": sym,
                    "earnings_date": str(q_date),
                    "entry_date": str(entry_date.date()),
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "pre_er_close": round(pre_er_close, 2),
                    "day1_gap_pct": round(gap_pct, 1),
                    "surprise_pct": round(surprise_pct, 1),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": exit_reason,
                    "days_held": days_held,
                })

        except Exception:
            continue

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(syms)} scanned ({len(all_trades)} trades)...")
            time.sleep(0.3)

    if not all_trades:
        print("\n  No trades generated.")
        return

    # Results
    pnls = [t["pnl_pct"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    exits = defaultdict(int)
    for t in all_trades:
        exits[t["exit_reason"]] += 1

    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999

    peak_eq = 0
    cum_eq = 0
    max_dd = 0
    for p in pnls:
        cum_eq += p
        peak_eq = max(peak_eq, cum_eq)
        max_dd = max(max_dd, peak_eq - cum_eq)

    print(f"\n{'='*70}")
    print("RESULTS (Post-ER Drift: Enter Day 2 After Confirmed Beat)")
    print(f"{'='*70}")
    print(f"  Stocks:       {len(set(t['symbol'] for t in all_trades))}")
    print(f"  Trades:       {len(all_trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:     {len(wins)/len(pnls)*100:.1f}%")
    print(f"  Profit Factor:{pf}")
    print(f"  Total PnL:    {sum(pnls):+.1f}%")
    print(f"  Expectancy:   {np.mean(pnls):+.2f}%/trade")
    print(f"  Avg Win:      {np.mean(wins):+.1f}%" if wins else "")
    print(f"  Avg Loss:     {np.mean(losses):+.1f}%" if losses else "")
    print(f"  Max Drawdown: {max_dd:.1f}%")
    print(f"  Avg Hold:     {np.mean([t['days_held'] for t in all_trades]):.1f} days")
    print(f"  Exits:        {dict(exits)}")

    # By gap size
    print(f"\n  By Day 1 Gap Size:")
    for lo, hi, label in [(0, 3, 'Small gap 0-3%'), (3, 7, 'Medium gap 3-7%'), (7, 15, 'Big gap 7-15%'), (15, 999, 'Huge gap 15%+')]:
        bucket = [t for t in all_trades if lo <= t["day1_gap_pct"] < hi]
        if bucket:
            b_pnls = [t["pnl_pct"] for t in bucket]
            b_wins = sum(1 for p in b_pnls if p > 0)
            b_pf = sum(p for p in b_pnls if p > 0) / abs(sum(p for p in b_pnls if p <= 0)) if any(p <= 0 for p in b_pnls) else 999
            print(f"    {label}: {len(bucket)} trades | WR {b_wins/len(bucket)*100:.0f}% | PF {b_pf:.2f} | PnL {sum(b_pnls):+.1f}%")

    # By surprise size
    print(f"\n  By Surprise Size:")
    for lo, hi, label in [(0, 3, 'Small beat 0-3%'), (3, 8, 'Medium beat 3-8%'), (8, 20, 'Big beat 8-20%'), (20, 999, 'Huge beat 20%+')]:
        bucket = [t for t in all_trades if lo <= t["surprise_pct"] < hi]
        if bucket:
            b_pnls = [t["pnl_pct"] for t in bucket]
            b_wins = sum(1 for p in b_pnls if p > 0)
            print(f"    {label}: {len(bucket)} trades | WR {b_wins/len(bucket)*100:.0f}% | PnL {sum(b_pnls):+.1f}%")

    # Top/bottom symbols
    by_sym = defaultdict(list)
    for t in all_trades:
        by_sym[t["symbol"]].append(t["pnl_pct"])
    sorted_syms = sorted(by_sym.items(), key=lambda x: sum(x[1]), reverse=True)

    print(f"\n  Top 10:")
    for sym, pl in sorted_syms[:10]:
        w = sum(1 for p in pl if p > 0)
        print(f"    {sym:8s} | {len(pl)} trades | WR {w/len(pl)*100:.0f}% | PnL {sum(pl):+.1f}%")

    print(f"\n  Bottom 10:")
    for sym, pl in sorted_syms[-10:]:
        w = sum(1 for p in pl if p > 0)
        print(f"    {sym:8s} | {len(pl)} trades | WR {w/len(pl)*100:.0f}% | PnL {sum(pl):+.1f}%")

    # Save
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = RESULTS_DIR / f"post_er_{ts}.csv"
    pd.DataFrame(all_trades).to_csv(out, index=False)
    print(f"\n  Saved: {out}")


def main():
    parser = argparse.ArgumentParser(description="Apollo Post-ER Drift Backtest")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--min-surprise", type=float, default=0.0, help="Min surprise %% to enter (default: 0 = any beat)")
    parser.add_argument("--no-gap-filter", action="store_true", help="Enter even if Day 1 closed below pre-ER")
    parser.add_argument("--stop", type=float, default=5.0)
    parser.add_argument("--trail", type=float, default=4.0)
    args = parser.parse_args()

    run_post_er_backtest(
        symbols=args.symbols,
        min_surprise_pct=args.min_surprise,
        require_gap_up=not args.no_gap_filter,
        stop_pct=args.stop,
        trail_pct=args.trail,
    )


if __name__ == "__main__":
    main()
