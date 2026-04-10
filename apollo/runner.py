#!/usr/bin/env python3
"""apollo/runner.py -- Earnings & catalyst event scanner.

Scans a universe of stocks for upcoming earnings and scores the pre-earnings
setup quality. Alerts you 1-5 days before the event.

Edge sources:
  1. Pre-earnings BB squeeze (compression = big move coming)
  2. Volume buildup (smart money positioning)
  3. Historical earnings surprise pattern (serial beaters)
  4. Price vs analyst estimates (set up for surprise)
  5. Post-earnings drift on recent reporters

Usage:
    python -m apollo.runner                    # Scan upcoming earnings
    python -m apollo.runner --dry-run          # Print only
    python -m apollo.runner --days 14          # Look ahead 14 days
    python -m apollo.runner --backtest         # Backtest earnings drift
"""
import argparse
import csv
import json
import os
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from apollo.strategies.catalyst_signals import get_all_signals
from apollo.strategies.market_data import get_full_profile
from apollo.strategies.position_rules import create_entry_plan, format_trade_plan

LOGS_DIR = REPO / "apollo" / "logs"
DATA_DIR = REPO / "apollo" / "data"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Scan universe — stocks with liquid options and frequent big earnings moves
UNIVERSE = [
    # Mega tech (always big movers on earnings)
    "NVDA", "TSLA", "META", "AMZN", "GOOGL", "AAPL", "MSFT", "NFLX",
    # Semis
    "AMD", "AVGO", "MU", "MRVL", "ARM", "SMCI", "TSM",
    # Fintech / Growth
    "SOFI", "PLTR", "COIN", "HOOD", "UPST", "AFRM", "NU",
    # Biotech
    "MRNA", "BNTX", "CRSP",
    # EV / Energy
    "RIVN", "LCID", "PLUG", "FSLR", "ENPH",
    # Retail / Consumer
    "SHOP", "SNAP", "PINS", "RBLX",
    # China
    "BABA", "PDD", "JD", "NIO",
    # Other movers
    "MARA", "GME", "AMC",
]


def get_earnings_data(symbol: str) -> dict | None:
    """Fetch earnings calendar and history for a symbol."""
    try:
        ticker = yf.Ticker(symbol)

        # Calendar (next earnings date + estimates)
        cal = ticker.calendar or {}
        earnings_dates = cal.get("Earnings Date", [])
        next_earnings = None
        if earnings_dates:
            if isinstance(earnings_dates, list) and len(earnings_dates) > 0:
                next_earnings = earnings_dates[0]
            elif hasattr(earnings_dates, 'date'):
                next_earnings = earnings_dates

        # Earnings history (surprise patterns)
        history = None
        try:
            history = ticker.earnings_history
        except Exception:
            pass

        # Current price data
        df = ticker.history(period="3mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 20:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        return {
            "symbol": symbol,
            "next_earnings": next_earnings,
            "eps_estimate": cal.get("Earnings Average"),
            "eps_high": cal.get("Earnings High"),
            "eps_low": cal.get("Earnings Low"),
            "revenue_estimate": cal.get("Revenue Average"),
            "history": history,
            "price_data": df,
        }
    except Exception as e:
        return None


def score_pre_earnings_setup(data: dict) -> dict | None:
    """Score a stock's pre-earnings setup quality (0-100)."""
    df = data["price_data"]
    symbol = data["symbol"]
    next_earnings = data["next_earnings"]

    if next_earnings is None:
        return None

    # Days until earnings
    if hasattr(next_earnings, 'date'):
        earnings_date = next_earnings
    else:
        try:
            earnings_date = pd.Timestamp(next_earnings).date()
        except Exception:
            return None

    today = datetime.now().date()
    days_until = (earnings_date - today).days

    if days_until < -5 or days_until > 30:
        return None  # Too far out or already passed

    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    volume = df["Volume"].values if "Volume" in df.columns else np.zeros(len(df))
    n = len(close)

    # Indicators
    ema_20 = pd.Series(close).ewm(span=20).mean().values
    ema_50 = pd.Series(close).ewm(span=50).mean().values
    sma_20 = pd.Series(close).rolling(20).mean().values
    std_20 = pd.Series(close).rolling(20).std().values
    bb_width = (2 * std_20 / sma_20) if sma_20[-1] > 0 else np.zeros(n)
    vol_20 = pd.Series(volume).rolling(20).mean().values

    # RSI
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gains).rolling(14).mean().values
    avg_loss = pd.Series(losses).rolling(14).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain), where=avg_loss > 0)
    rsi = 100 - (100 / (1 + rs))

    i = n - 1
    score = 40  # baseline
    signals = []
    direction = None

    # ── 1. BB Squeeze (pre-earnings compression) ────────────
    bb_pctile = 50
    bb_valid = bb_width[~np.isnan(bb_width)]
    if len(bb_valid) > 20:
        bb_pctile = np.searchsorted(np.sort(bb_valid), bb_width[i]) / len(bb_valid) * 100

    if bb_pctile < 15:
        score += 25
        signals.append(f"SQUEEZE: BB at {bb_pctile:.0f}%ile — coiled for big move")
    elif bb_pctile < 30:
        score += 12
        signals.append(f"TIGHT: BB at {bb_pctile:.0f}%ile")

    # ── 2. Volume Pattern (accumulation before earnings) ────
    vol_ratio = volume[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1
    # Check if volume is trending up in last 5 days vs prior 10
    vol_recent = np.mean(volume[max(0, i-5):i+1])
    vol_prior = np.mean(volume[max(0, i-15):max(0, i-5)])
    vol_trend = (vol_recent / vol_prior - 1) if vol_prior > 0 else 0

    if vol_trend > 0.3 and vol_ratio > 1.3:
        score += 20
        signals.append(f"ACCUMULATION: Vol trending up {vol_trend:+.0%}, {vol_ratio:.1f}x avg")
    elif vol_ratio > 1.5:
        score += 10
        signals.append(f"HIGH_VOL: {vol_ratio:.1f}x average")

    # ── 3. Historical Surprise Pattern ──────────────────────
    hist = data.get("history")
    beat_rate = 0.5
    avg_surprise = 0
    if hist is not None and len(hist) >= 4:
        surprises = hist["surprisePercent"].dropna().values
        if len(surprises) >= 4:
            beats = sum(1 for s in surprises if s > 0)
            beat_rate = beats / len(surprises)
            avg_surprise = np.mean(surprises) * 100

            if beat_rate >= 0.75:
                score += 15
                signals.append(f"SERIAL_BEATER: {beat_rate:.0%} beat rate, avg surprise {avg_surprise:+.1f}%")
                direction = "long"
            elif beat_rate <= 0.25:
                score += 10
                signals.append(f"SERIAL_MISSER: {beat_rate:.0%} beat rate — short candidate")
                direction = "short"

    # ── 4. Price Position (trending into earnings) ──────────
    ret_20d = (close[i] / close[max(0, i-20)] - 1) * 100 if i >= 20 else 0
    ret_5d = (close[i] / close[max(0, i-5)] - 1) * 100 if i >= 5 else 0

    if ret_20d > 10 and beat_rate > 0.6:
        score += 10
        signals.append(f"MOMENTUM_INTO_ER: +{ret_20d:.1f}% last 20d")
        direction = direction or "long"
    elif ret_20d < -10 and beat_rate < 0.5:
        score += 10
        signals.append(f"WEAK_INTO_ER: {ret_20d:+.1f}% last 20d — gap down risk")
        direction = direction or "short"

    # ── 5. Days Until Earnings Weighting ────────────────────
    if 1 <= days_until <= 5:
        score += 10
        signals.append(f"IMMINENT: {days_until}d to earnings")
    elif 0 <= days_until <= 1:
        score += 5
        signals.append(f"TODAY/TOMORROW: {days_until}d")
    elif days_until < 0 and days_until >= -3:
        # Post-earnings — check for drift opportunity
        signals.append(f"JUST_REPORTED: {abs(days_until)}d ago")
        if ret_5d > 5:
            score += 15
            signals.append(f"POST_ER_DRIFT_UP: +{ret_5d:.1f}% since report — momentum")
            direction = "long"
        elif ret_5d < -5:
            score += 10
            signals.append(f"POST_ER_DROP: {ret_5d:+.1f}% — bounce or continuation?")

    # ── 6. EMA Structure ────────────────────────────────────
    above_50 = close[i] > ema_50[i]
    if direction == "long" and above_50:
        score += 5
    elif direction == "short" and not above_50:
        score += 5

    score = max(0, min(100, score))

    return {
        "symbol": symbol,
        "score": score,
        "direction": direction or "neutral",
        "signals": signals,
        "earnings_date": str(earnings_date),
        "days_until": days_until,
        "price": round(close[i], 2),
        "bb_pctile": round(bb_pctile, 1),
        "rsi": round(rsi[i], 1) if not np.isnan(rsi[i]) else 50,
        "vol_ratio": round(vol_ratio, 2),
        "vol_trend": round(vol_trend, 3),
        "ret_5d": round(ret_5d, 1),
        "ret_20d": round(ret_20d, 1),
        "beat_rate": round(beat_rate, 2),
        "avg_surprise": round(avg_surprise, 1),
        "eps_estimate": data.get("eps_estimate"),
    }


def backtest_earnings_drift(symbols: list[str] | None = None):
    """Backtest post-earnings drift: buy after positive surprise, sell after 20d."""
    syms = symbols or UNIVERSE[:20]
    all_trades = []

    print(f"\n{'='*70}")
    print("APOLLO EARNINGS DRIFT BACKTEST")
    print(f"{'='*70}")

    for sym in syms:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.earnings_history
            df = ticker.history(period="2y", interval="1d", auto_adjust=True)

            if hist is None or len(hist) < 4 or df.empty:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df["Close"]

            for _, row in hist.iterrows():
                surprise = row.get("surprisePercent", 0)
                if surprise is None or np.isnan(surprise):
                    continue

                # Find the earnings date in price data
                q_date = row.name
                if hasattr(q_date, 'date'):
                    q_date = q_date.date()

                # Find nearest trading day
                mask = close.index.date >= q_date
                if mask.sum() < 22:
                    continue

                post_idx = close.index[mask]
                if len(post_idx) < 22:
                    continue

                entry_price = close[post_idx[1]]  # day after earnings
                exit_price = close[post_idx[min(21, len(post_idx)-1)]]  # 20 days later

                if surprise > 0.02:  # >2% beat → buy
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                    direction = "long"
                elif surprise < -0.02:  # >2% miss → short
                    pnl_pct = (entry_price - exit_price) / entry_price * 100
                    direction = "short"
                else:
                    continue

                all_trades.append({
                    "symbol": sym,
                    "direction": direction,
                    "earnings_date": str(q_date),
                    "surprise_pct": round(surprise * 100, 2),
                    "entry_price": round(float(entry_price), 2),
                    "exit_price": round(float(exit_price), 2),
                    "pnl_pct": round(float(pnl_pct), 2),
                    "hold_days": 20,
                })
        except Exception:
            continue

    if not all_trades:
        print("  No trades generated.")
        return

    pnls = [t["pnl_pct"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    print(f"  Symbols: {len(set(t['symbol'] for t in all_trades))}")
    print(f"  Trades:  {len(all_trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate: {len(wins)/len(pnls)*100:.1f}%")
    print(f"  PF: {sum(wins)/abs(sum(losses)):.2f}" if losses and sum(losses) != 0 else "  PF: inf")
    print(f"  Total PnL: {sum(pnls):+.1f}%")
    print(f"  Expectancy: {np.mean(pnls):+.2f}%/trade")
    print(f"  Avg Win: {np.mean(wins):+.1f}%" if wins else "")
    print(f"  Avg Loss: {np.mean(losses):+.1f}%" if losses else "")

    # By direction
    for d in ["long", "short"]:
        d_trades = [t for t in all_trades if t["direction"] == d]
        if d_trades:
            d_pnls = [t["pnl_pct"] for t in d_trades]
            d_wins = sum(1 for p in d_pnls if p > 0)
            print(f"  {d:8s}: {len(d_trades)} trades | WR {d_wins/len(d_trades)*100:.0f}% | PnL {sum(d_pnls):+.1f}%")

    # Top performers
    by_sym = defaultdict(list)
    for t in all_trades:
        by_sym[t["symbol"]].append(t["pnl_pct"])
    print(f"\n  Per-Symbol:")
    for sym, pnls_list in sorted(by_sym.items(), key=lambda x: sum(x[1]), reverse=True):
        w = sum(1 for p in pnls_list if p > 0)
        print(f"    {sym:8s} | {len(pnls_list)} trades | WR {w/len(pnls_list)*100:.0f}% | PnL {sum(pnls_list):+.1f}%")

    # Save
    RESULTS_DIR = REPO / "apollo" / "data" / "backtest_results"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    pd.DataFrame(all_trades).to_csv(RESULTS_DIR / f"drift_{ts}.csv", index=False)
    print(f"\n  Saved: {RESULTS_DIR / f'drift_{ts}.csv'}")


def format_discord(results: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**APOLLO Earnings Scanner -- {now}**\n"]

    imminent = [r for r in results if r["score"] >= 60 and 0 <= r.get("days_until", 99) <= 5]
    watching = [r for r in results if r["score"] >= 45 and r not in imminent]
    post_er = [r for r in results if r.get("days_until", 0) < 0 and r["score"] >= 50]

    if imminent:
        lines.append(f"**EARNINGS THIS WEEK ({len(imminent)}):**")
        for r in imminent:
            dir_emoji = "LONG" if r["direction"] == "long" else ("SHORT" if r["direction"] == "short" else "NEUTRAL")
            conviction = r.get("conviction", "?").upper()
            pc = r.get("options_pc_ratio", "?")
            short_pct = r.get("short_pct_float", 0)
            upside = r.get("upside_pct", 0)
            lines.append(
                f"  **{r['symbol']}** [{dir_emoji}] score={r['score']} | {conviction} conviction\n"
                f"    ER: {r['earnings_date']} ({r['days_until']}d) | Beat: {r['beat_rate']:.0%} | "
                f"P/C: {pc} | Short: {short_pct:.1f}%\n"
                f"    ${r['price']} | BB={r['bb_pctile']:.0f}%ile | Upside: {upside:+.0f}% to target\n"
                f"    {' | '.join(r['signals'][:4])}"
            )
            if r.get("trade_plan"):
                lines.append(f"\n{r['trade_plan']}")

    if post_er:
        lines.append(f"\n**POST-EARNINGS DRIFT ({len(post_er)}):**")
        for r in post_er[:5]:
            lines.append(
                f"  {r['symbol']} | {r['ret_5d']:+.1f}% since report | score={r['score']}"
            )

    if watching:
        lines.append(f"\n**WATCHLIST ({len(watching)}):**")
        for r in watching[:5]:
            lines.append(
                f"  {r['symbol']} | ER: {r['earnings_date']} ({r['days_until']}d) | "
                f"score={r['score']} | Beat rate: {r['beat_rate']:.0%}"
            )

    if not imminent and not watching and not post_er:
        lines.append("No high-conviction earnings setups this week.")

    return "\n".join(lines)


def send_discord(content: str) -> bool:
    if not WEBHOOK_URL:
        return False
    import requests
    try:
        chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
        ok = True
        for chunk in chunks:
            r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10)
            ok = ok and r.status_code in (200, 204)
            time.sleep(0.5)
        return ok
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Apollo Earnings Scanner")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=10, help="Look ahead days (default: 10)")
    parser.add_argument("--backtest", action="store_true", help="Run earnings drift backtest")
    parser.add_argument("--min-score", type=int, default=40)
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if args.backtest:
        backtest_earnings_drift()
        return

    print(f"{'='*60}")
    print(f"APOLLO Earnings Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Scanning {len(UNIVERSE)} stocks for earnings within {args.days} days")
    print(f"{'='*60}")

    results = []
    for i, sym in enumerate(UNIVERSE):
        data = get_earnings_data(sym)
        if data is None:
            continue
        scored = score_pre_earnings_setup(data)
        if scored and scored["score"] >= args.min_score:
            # Enrich with catalyst signals (options flow, insider, analyst)
            if scored["days_until"] is not None and -5 <= scored["days_until"] <= 14:
                try:
                    catalyst = get_all_signals(sym)
                    scored["catalyst_score"] = catalyst["total_signal"]
                    scored["catalyst_consensus"] = catalyst["consensus"]
                    scored["catalyst_details"] = catalyst["details"]
                    scored["options_pc_ratio"] = catalyst["options"].get("pc_vol_ratio")
                    scored["insider_buys_30d"] = catalyst["insider"].get("buys_30d", 0)
                    scored["analyst_upgrades"] = catalyst["analyst"].get("upgrades_30d", 0)
                    scored["analyst_buy_count"] = catalyst["analyst"].get("buy_count", 0)
                    # Boost score with catalyst signals
                    scored["score"] = min(100, scored["score"] + max(0, catalyst["total_signal"] // 2))
                    scored["signals"].extend(catalyst["details"])
                except Exception:
                    scored["catalyst_score"] = 0

                # Market data (short interest, targets, valuation)
                try:
                    profile = get_full_profile(sym)
                    scored["short_pct_float"] = profile["short_interest"].get("short_pct_float", 0)
                    scored["short_ratio"] = profile["short_interest"].get("short_ratio", 0)
                    scored["analyst_target"] = profile["analyst_targets"].get("target_mean", 0)
                    scored["upside_pct"] = profile["analyst_targets"].get("upside_pct", 0)
                    scored["revenue_growth"] = profile["valuation"].get("revenue_growth", 0)
                    scored["score"] = min(100, scored["score"] + max(0, profile["total_signal"] // 3))
                    scored["signals"].extend(profile["details"])
                except Exception:
                    pass

                # Generate trade plan if score is high enough
                plan = create_entry_plan(scored)
                if plan:
                    scored["trade_plan"] = format_trade_plan(plan)
                    scored["conviction"] = plan.conviction
                    scored["risk_pct"] = plan.risk_pct
            results.append(scored)
        if (i + 1) % 10 == 0:
            print(f"  Scanned {i+1}/{len(UNIVERSE)}...")
            time.sleep(0.5)

    results.sort(key=lambda x: x["score"], reverse=True)

    # Print table
    print(f"\n{'Symbol':8s} {'Score':>5s} {'Dir':8s} {'ER Date':12s} {'Days':>5s} {'Price':>8s} {'BB%':>5s} {'Vol':>5s} {'Beat':>5s} {'Signals'}")
    print("-" * 100)
    for r in results[:20]:
        sigs = "; ".join(r["signals"][:2])[:40]
        print(f"{r['symbol']:8s} {r['score']:5d} {r['direction']:8s} {r['earnings_date']:12s} "
              f"{r['days_until']:5d} ${r['price']:>7.2f} {r['bb_pctile']:5.0f} "
              f"{r['vol_ratio']:5.2f} {r['beat_rate']:5.0%} {sigs}")

    # Discord
    report = format_discord(results)
    print(f"\n{report}")

    if not args.dry_run:
        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")

    # Save
    log_path = LOGS_DIR / f"scan_{datetime.now().strftime('%Y%m%d')}.json"
    log_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Saved: {log_path}")


if __name__ == "__main__":
    main()
