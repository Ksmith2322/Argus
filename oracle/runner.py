#!/usr/bin/env python3
"""oracle/runner.py -- Polymarket opportunity scanner.

Scans all active markets for edge signals:
  1. Extreme prices (near 0 or 100 = free money if wrong, or confirmation trades)
  2. Volume spikes (sudden interest = something is happening)
  3. Mispriced markets (contradictory pricing in related markets)
  4. High liquidity + tight spread (tradeable opportunities)
  5. Time decay plays (expiring soon at non-extreme prices)

Usage:
    python -m oracle.runner                    # Scan + Discord
    python -m oracle.runner --dry-run          # Print only
    python -m oracle.runner --search "trump"   # Search specific topic
    python -m oracle.runner --category politics
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from oracle.ops.polymarket_client import (
    get_markets, get_events, search_markets, parse_market,
    get_spread, get_price_history,
)

LOGS_DIR = REPO / "oracle" / "logs"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


# ── Edge Detection Strategies ────────────────────────────────

def score_market(m: dict) -> dict:
    """Score a parsed market for trading opportunity. Returns enriched market dict."""
    score = 0
    signals = []
    edge_type = "WATCH"

    yes = m["yes_price"]
    vol_24h = m["volume_24h"]
    vol_1w = m["volume_1w"]
    liquidity = m["liquidity"]
    days_left = m["days_to_resolution"]

    # ── 1. Extreme Price (near certainty or near zero) ──────
    # Markets at 95-99% = confirmation trade (buy YES cheap)
    # Markets at 1-5% = contrarian bet (buy YES if you think market is wrong)
    if 0.95 <= yes <= 0.99 and days_left and days_left <= 7:
        score += 15
        signals.append(f"NEAR_CERTAIN: {m['implied_prob']}% with {days_left}d left")
    elif 0.01 <= yes <= 0.05 and days_left and days_left <= 14:
        score += 20
        signals.append(f"LONGSHOT: {m['implied_prob']}% — high payoff if wrong")
    elif 0.85 <= yes <= 0.94:
        score += 5
        signals.append(f"HIGH_PROB: {m['implied_prob']}%")

    # ── 2. Volume Spike (24h vs 1w average) ─────────────────
    if vol_1w > 0:
        daily_avg = vol_1w / 7
        vol_ratio = vol_24h / daily_avg if daily_avg > 0 else 0
        if vol_ratio > 3:
            score += 25
            signals.append(f"VOLUME_SPIKE: {vol_ratio:.1f}x avg (${vol_24h/1e6:.1f}M 24h)")
            edge_type = "VOLUME"
        elif vol_ratio > 2:
            score += 15
            signals.append(f"ABOVE_AVG_VOL: {vol_ratio:.1f}x (${vol_24h/1e6:.1f}M)")
    elif vol_24h > 500000:
        score += 10
        signals.append(f"HIGH_VOL: ${vol_24h/1e6:.1f}M 24h")

    # ── 3. Liquidity (tradeable size) ───────────────────────
    if liquidity > 1000000:
        score += 15
        signals.append(f"DEEP_LIQUIDITY: ${liquidity/1e6:.1f}M")
    elif liquidity > 100000:
        score += 5
    elif liquidity < 10000:
        score -= 10  # too thin to trade

    # ── 4. Time Decay (expiring soon at non-extreme prices) ─
    if days_left is not None:
        if days_left <= 3 and 0.10 < yes < 0.90:
            score += 30
            signals.append(f"EXPIRING_SOON: {days_left}d left at {m['implied_prob']}%")
            edge_type = "TIME_DECAY"
        elif days_left <= 7 and 0.20 < yes < 0.80:
            score += 15
            signals.append(f"NEAR_EXPIRY: {days_left}d at {m['implied_prob']}%")

    # ── 5. Price dislocation (50/50 markets with high volume = contested) ─
    if 0.40 <= yes <= 0.60 and vol_24h > 100000:
        score += 10
        signals.append(f"CONTESTED: {m['implied_prob']}% — market divided")
        if vol_24h > 1000000:
            score += 10
            edge_type = "CONTESTED"

    # ── 6. High conviction move (price shifted recently) ────
    # Detect by comparing current price to what we'd expect from volume patterns
    if vol_24h > 1000000 and (yes > 0.85 or yes < 0.15):
        score += 10
        signals.append(f"HIGH_CONVICTION: ${vol_24h/1e6:.1f}M at {m['implied_prob']}%")

    # Minimum score threshold
    score = max(0, min(100, score))

    return {
        **m,
        "score": score,
        "signals": signals,
        "edge_type": edge_type if score >= 30 else "WATCH",
    }


def scan_all(min_score: int = 25, limit: int = 200) -> list[dict]:
    """Scan all active markets and score for opportunities."""
    print(f"Fetching top {limit} markets by 24h volume...")
    raw_markets = get_markets(limit=limit, order="volume24hr")
    if not raw_markets:
        print("  No markets returned from API.")
        return []

    print(f"  Got {len(raw_markets)} markets. Scoring...")
    scored = []
    for m in raw_markets:
        parsed = parse_market(m)
        if not parsed["active"] or parsed["closed"]:
            continue
        enriched = score_market(parsed)
        if enriched["score"] >= min_score:
            scored.append(enriched)

    scored.sort(key=lambda x: x["score"], reverse=True)
    print(f"  {len(scored)} markets above score {min_score}")
    return scored


def scan_search(query: str, min_score: int = 15) -> list[dict]:
    """Search and score markets by keyword."""
    print(f"Searching: '{query}'...")
    raw = search_markets(query, limit=50)
    scored = []
    for m in raw:
        parsed = parse_market(m)
        if not parsed["active"] or parsed["closed"]:
            continue
        enriched = score_market(parsed)
        if enriched["score"] >= min_score:
            scored.append(enriched)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ── Output Formatting ────────────────────────────────────────

def format_table(markets: list[dict], top_n: int = 20) -> str:
    lines = []
    lines.append(f"{'Score':>5s} {'Edge':12s} {'Prob':>5s} {'Vol24h':>10s} {'Liq':>8s} {'Days':>5s} {'Question'}")
    lines.append("-" * 90)
    for m in markets[:top_n]:
        vol_str = f"${m['volume_24h']/1e6:.1f}M" if m['volume_24h'] > 1e6 else f"${m['volume_24h']/1e3:.0f}K"
        liq_str = f"${m['liquidity']/1e6:.1f}M" if m['liquidity'] > 1e6 else f"${m['liquidity']/1e3:.0f}K"
        days_str = str(m['days_to_resolution']) if m['days_to_resolution'] is not None else "?"
        question = m['question'][:50]
        lines.append(f"{m['score']:5d} {m['edge_type']:12s} {m['implied_prob']:4.0f}% {vol_str:>10s} {liq_str:>8s} {days_str:>5s} {question}")
    return "\n".join(lines)


def format_discord(markets: list[dict], top_n: int = 10) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**ORACLE Polymarket Scanner -- {now}**\n"]

    hot = [m for m in markets if m["score"] >= 50]
    warm = [m for m in markets if 30 <= m["score"] < 50]

    if hot:
        lines.append(f"**HOT OPPORTUNITIES ({len(hot)}):**")
        for m in hot[:top_n]:
            signals_str = " | ".join(m["signals"][:3])
            vol_str = f"${m['volume_24h']/1e6:.1f}M" if m['volume_24h'] > 1e6 else f"${m['volume_24h']/1e3:.0f}K"
            lines.append(
                f"  **{m['question'][:60]}**\n"
                f"    {m['implied_prob']:.0f}% YES | {m['edge_type']} | score={m['score']} | "
                f"Vol={vol_str} | {m.get('days_to_resolution', '?')}d left\n"
                f"    {signals_str}"
            )

    if warm:
        lines.append(f"\n**WATCHLIST ({len(warm)}):**")
        for m in warm[:5]:
            lines.append(
                f"  {m['question'][:55]} | {m['implied_prob']:.0f}% | "
                f"score={m['score']} | {m['edge_type']}"
            )

    if not hot and not warm:
        lines.append("No high-conviction opportunities right now.")

    lines.append(f"\n_Scanned {len(markets)} active markets_")
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


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Oracle Polymarket Scanner")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--search", help="Search by keyword")
    parser.add_argument("--category", help="Filter by category tag")
    parser.add_argument("--min-score", type=int, default=25)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"{'=' * 70}")
    print(f"ORACLE Polymarket Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 70}")

    if args.search:
        markets = scan_search(args.search, min_score=args.min_score)
    else:
        markets = scan_all(min_score=args.min_score, limit=200)

    # Print table
    print(f"\n{format_table(markets, top_n=args.top)}")

    # Discord
    report = format_discord(markets, top_n=args.top)
    print(f"\n{report}")

    if not args.dry_run:
        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")

    # Save scan
    log_path = LOGS_DIR / f"scan_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    log_path.write_text(json.dumps(markets[:50], indent=2, default=str))
    print(f"Saved: {log_path}")


if __name__ == "__main__":
    main()
