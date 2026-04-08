#!/usr/bin/env python3
"""helio/breakout_scanner.py -- Nightly stock breakout readiness scanner.

Scores every stock in the universe on how close it is to a breakout setup.
Posts top candidates to Discord. Designed to run nightly after market close.

Uses the breakout profile from breakout_research.py to score current conditions.

Usage:
    python -m helio.breakout_scanner                # Scan + post to Discord
    python -m helio.breakout_scanner --dry-run      # Print only
    python -m helio.breakout_scanner --refresh       # Re-download prices first
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

DATA_DIR = REPO / "helio" / "data" / "breakout"
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Import universe from research module
from helio.breakout_research import (
    UNIVERSE, download_universe,
    _compute_atr, _compute_bb_width, _compute_rsi, _percentile_rank,
)


def score_stock(sym: str) -> dict | None:
    """Score a single stock on breakout readiness (0-100)."""
    csv_path = DATA_DIR / f"{sym}_daily.csv"
    if not csv_path.exists():
        return None

    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    except Exception:
        return None

    if len(df) < 60 or "Close" not in df.columns:
        return None

    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    volume = df["Volume"].values if "Volume" in df.columns else np.zeros(len(df))
    i = len(close) - 1  # latest bar

    # Compute indicators
    bb_width = _compute_bb_width(close, 20)
    rsi = _compute_rsi(close, 14)
    atr = _compute_atr(high, low, close, 14)
    ema_8 = pd.Series(close).ewm(span=8).mean().values
    ema_20 = pd.Series(close).ewm(span=20).mean().values
    ema_50 = pd.Series(close).ewm(span=50).mean().values
    ema_200 = pd.Series(close).ewm(span=200).mean().values
    vol_20 = pd.Series(volume).rolling(20).mean().values

    # Current values
    price = close[i]
    cur_bb = bb_width[i] if not np.isnan(bb_width[i]) else 0
    cur_rsi = rsi[i] if not np.isnan(rsi[i]) else 50
    cur_atr = atr[i] if not np.isnan(atr[i]) else 0
    cur_vol_ratio = volume[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1

    # BB percentile (how tight is the current squeeze vs history)
    bb_pctile = _percentile_rank(bb_width[max(0, i - 252):i], cur_bb)

    # Volume trend (is volume drying up = accumulation, or spiking = breakout starting)
    vol_5 = np.mean(volume[max(0, i - 5):i + 1])
    vol_20_avg = np.mean(volume[max(0, i - 20):i + 1])
    vol_trend = (vol_5 / vol_20_avg - 1) if vol_20_avg > 0 else 0

    # Price compression (how tight is the 20-day range)
    range_20d = (np.max(close[max(0, i - 20):i + 1]) - np.min(close[max(0, i - 20):i + 1])) / price * 100

    # Narrow range days
    narrow_days = 0
    daily_ranges = [(high[j] - low[j]) / close[j] * 100 for j in range(max(0, i - 10), i + 1)]
    avg_range = np.mean(daily_ranges) if daily_ranges else 1
    for j in range(i, max(0, i - 10), -1):
        if (high[j] - low[j]) / close[j] * 100 < avg_range * 0.7:
            narrow_days += 1
        else:
            break

    # EMA structure
    above_ema20 = close[i] > ema_20[i]
    above_ema50 = close[i] > ema_50[i]
    above_ema200 = close[i] > ema_200[i]
    ema_aligned = ema_8[i] > ema_20[i] > ema_50[i]  # bullish alignment

    # 5-day and 20-day returns
    ret_5d = (close[i] / close[max(0, i - 5)] - 1) * 100
    ret_20d = (close[i] / close[max(0, i - 20)] - 1) * 100

    # Distance from 52-week high/low
    yr_data = close[max(0, i - 252):i + 1]
    dist_from_52w_high = (price - np.max(yr_data)) / np.max(yr_data) * 100 if len(yr_data) > 0 else 0
    dist_from_52w_low = (price - np.min(yr_data)) / np.min(yr_data) * 100 if len(yr_data) > 0 and np.min(yr_data) > 0 else 0

    # ── SCORING (0-100) ──────────────────────────────────────
    score = 50  # neutral baseline

    # 1. BB squeeze (tighter = more coiled = higher score) — UP TO +20
    if bb_pctile < 10:
        score += 20  # extreme squeeze
    elif bb_pctile < 25:
        score += 15
    elif bb_pctile < 40:
        score += 8
    elif bb_pctile > 80:
        score -= 10  # already expanded, breakout may be over

    # 2. Volume pattern — UP TO +15
    if vol_trend < -0.2 and cur_vol_ratio < 0.8:
        score += 15  # declining volume = accumulation before breakout
    elif vol_trend > 0.5 and cur_vol_ratio > 1.5:
        score += 10  # volume spike starting = breakout may be happening NOW
    elif cur_vol_ratio > 2.0:
        score += 5   # high volume day

    # 3. EMA structure — UP TO +15
    if ema_aligned and above_ema200:
        score += 15  # perfect bullish structure
    elif above_ema50 and above_ema200:
        score += 10  # solid uptrend
    elif above_ema20:
        score += 5
    elif not above_ema20 and not above_ema50:
        score -= 5   # below key EMAs

    # 4. RSI sweet spot — UP TO +10
    if 40 <= cur_rsi <= 60:
        score += 10  # neutral = coiled, not overextended
    elif 30 <= cur_rsi < 40:
        score += 5   # slightly oversold = potential reversal breakout
    elif cur_rsi > 75:
        score -= 10  # overbought, breakout risk is to the downside

    # 5. Price compression — UP TO +10
    if range_20d < 10:
        score += 10  # very tight range
    elif range_20d < 15:
        score += 5
    elif range_20d > 30:
        score -= 5   # already volatile

    # 6. Narrow range days — UP TO +10
    score += min(narrow_days * 3, 10)

    # 7. Near 52-week high (bullish breakout territory) — UP TO +10
    if dist_from_52w_high > -5:
        score += 10  # within 5% of 52w high
    elif dist_from_52w_high > -15:
        score += 5

    # 8. Recent momentum — small bonus/penalty
    if -3 < ret_5d < 3:
        score += 3   # quiet week = coiling
    elif ret_5d > 10:
        score -= 5   # already moved, may be late

    # Clamp to 0-100
    score = max(0, min(100, score))

    # Determine setup type
    if bb_pctile < 25 and vol_trend < -0.1:
        setup = "SQUEEZE"
    elif cur_vol_ratio > 1.5 and ret_5d > 3:
        setup = "MOMENTUM"
    elif above_ema200 and dist_from_52w_high > -5:
        setup = "BREAKOUT_ZONE"
    elif cur_rsi < 35 and above_ema200:
        setup = "OVERSOLD_BOUNCE"
    else:
        setup = "WATCH"

    return {
        "symbol": sym,
        "sector": UNIVERSE.get(sym, "unknown"),
        "score": score,
        "setup": setup,
        "price": round(price, 2),
        "bb_pctile": round(bb_pctile, 1),
        "rsi": round(cur_rsi, 1),
        "vol_ratio": round(cur_vol_ratio, 2),
        "vol_trend": round(vol_trend, 3),
        "range_20d": round(range_20d, 1),
        "narrow_days": narrow_days,
        "ema_aligned": ema_aligned,
        "above_200": above_ema200,
        "ret_5d": round(ret_5d, 1),
        "ret_20d": round(ret_20d, 1),
        "dist_52w_high": round(dist_from_52w_high, 1),
        "atr_pct": round(cur_atr / price * 100, 2) if price > 0 else 0,
    }


def scan_universe(refresh: bool = False) -> list[dict]:
    """Score all stocks and return sorted by breakout readiness."""
    if refresh:
        download_universe(period="1y")

    results = []
    for sym in UNIVERSE:
        score = score_stock(sym)
        if score:
            results.append(score)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def format_discord_report(results: list[dict], top_n: int = 10) -> str:
    """Format scanner results for Discord."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Breakout Scanner -- {now}**\n"]

    # Top candidates
    hot = [r for r in results if r["score"] >= 70]
    warm = [r for r in results if 60 <= r["score"] < 70]

    if hot:
        lines.append(f"**HOT ({len(hot)} stocks, score >= 70):**")
        for r in hot[:top_n]:
            lines.append(
                f"  **{r['symbol']}** [{r['setup']}] score={r['score']} | "
                f"${r['price']} | BB={r['bb_pctile']:.0f}%ile | RSI={r['rsi']:.0f} | "
                f"Vol={r['vol_ratio']:.1f}x | 5d={r['ret_5d']:+.1f}% | "
                f"52wH={r['dist_52w_high']:+.1f}%"
            )

    if warm:
        lines.append(f"\n**WARMING ({len(warm)} stocks, score 60-69):**")
        for r in warm[:top_n]:
            lines.append(
                f"  {r['symbol']} [{r['setup']}] score={r['score']} | "
                f"${r['price']} | BB={r['bb_pctile']:.0f}%ile | RSI={r['rsi']:.0f}"
            )

    if not hot and not warm:
        lines.append("No high-conviction setups today. Market may be in wait-and-see mode.")

    # Market breadth
    avg_score = np.mean([r["score"] for r in results]) if results else 0
    above_70 = sum(1 for r in results if r["score"] >= 70)
    lines.append(f"\n_Fleet avg score: {avg_score:.0f} | {above_70} stocks hot | {len(results)} scanned_")

    return "\n".join(lines)


def send_discord(content: str) -> bool:
    if not WEBHOOK_URL:
        return False
    import requests
    try:
        chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)]
        ok = True
        for chunk in chunks:
            r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10)
            ok = ok and r.status_code in (200, 204)
            time.sleep(0.5)
        return ok
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Nightly Breakout Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Discord")
    parser.add_argument("--refresh", action="store_true", help="Re-download prices first")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    args = parser.parse_args()

    print(f"{'=' * 60}")
    print(f"Breakout Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 60}")

    results = scan_universe(refresh=args.refresh)

    # Print full table
    print(f"\n{'Symbol':8s} {'Sector':12s} {'Score':>5s} {'Setup':15s} {'Price':>8s} "
          f"{'BB%':>5s} {'RSI':>5s} {'Vol':>5s} {'5d':>6s} {'52wH':>6s}")
    print("-" * 85)
    for r in results[:args.top]:
        print(f"{r['symbol']:8s} {r['sector']:12s} {r['score']:5d} {r['setup']:15s} "
              f"${r['price']:>7.2f} {r['bb_pctile']:5.0f} {r['rsi']:5.1f} "
              f"{r['vol_ratio']:5.2f} {r['ret_5d']:+5.1f}% {r['dist_52w_high']:+5.1f}%")

    # Discord report
    report = format_discord_report(results, top_n=args.top)
    print(f"\n{report}")

    if not args.dry_run:
        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")
    else:
        print("\n[DRY RUN] Skipping Discord post.")

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"scan_{datetime.now().strftime('%Y%m%d')}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
