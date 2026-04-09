#!/usr/bin/env python3
"""hermes/runner.py -- Gap fill scanner and runner.

Scans a universe of volatile stocks for gap events, scores fill probability,
and posts actionable signals to Discord.

Usage:
    python -m hermes.runner                     # Scan today's gaps
    python -m hermes.runner --backtest          # Full backtest
    python -m hermes.runner --dry-run           # Print only
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
    print("ERROR: pip install yfinance")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from hermes.strategies.gap_fill import detect_gaps, backtest_gaps, GapSignal

DATA_DIR = REPO / "hermes" / "data"
LOGS_DIR = REPO / "hermes" / "logs"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Scan universe — volatile stocks with frequent gaps
SCAN_UNIVERSE = [
    # High-beta tech/growth
    "NVDA", "TSLA", "PLTR", "SOFI", "AMD", "COIN", "MARA",
    "SMCI", "ARM", "UPST", "HOOD", "AFRM",
    # Biotech (big gap potential)
    "MRNA", "BNTX", "CRSP", "EDIT",
    # Meme / high retail
    "GME", "AMC", "RIVN", "LCID", "NIO",
    # Sector ETFs
    "SMH", "XBI", "ARKK", "XLE", "GDX",
    # Large cap movers
    "META", "AMZN", "NFLX", "GOOGL",
]


def download_data(symbols: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Download daily data for gap scanning."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    for sym in symbols:
        cache = DATA_DIR / f"{sym}_daily.csv"
        if cache.exists() and (time.time() - cache.stat().st_mtime) < 43200:
            try:
                data[sym] = pd.read_csv(cache, index_col=0, parse_dates=True)
                continue
            except Exception:
                pass
        try:
            df = yf.download(sym, period=period, interval="1d", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty and len(df) >= 30:
                df.index.name = "date"
                df.to_csv(cache)
                data[sym] = df
        except Exception:
            continue
    return data


def scan_today(symbols: list[str] | None = None, min_score: int = 55) -> list[dict]:
    """Scan for gaps in today's data."""
    syms = symbols or SCAN_UNIVERSE
    data = download_data(syms, period="3mo")
    all_gaps = []

    for sym in syms:
        df = data.get(sym)
        if df is None or len(df) < 30:
            continue

        gaps = detect_gaps(df, min_gap_pct=2.0)
        if not gaps:
            continue

        # Only latest gap (today or most recent)
        latest = gaps[-1]
        # Check if it's from the last trading day
        latest_date = latest["date"]
        df_latest = str(df.index[-1])[:10]
        if latest_date != df_latest:
            continue  # gap isn't from the most recent bar

        if latest["score"] >= min_score:
            latest["symbol"] = sym
            all_gaps.append(latest)

    all_gaps.sort(key=lambda g: g["score"], reverse=True)
    return all_gaps


def run_backtest(symbols: list[str] | None = None, min_score: int = 50):
    """Backtest gap fill across all symbols."""
    syms = symbols or SCAN_UNIVERSE
    data = download_data(syms, period="2y")

    print(f"\n{'=' * 70}")
    print(f"HERMES GAP FILL BACKTEST")
    print(f"Symbols: {len(syms)} | Min score: {min_score} | Max hold: 3 days")
    print(f"{'=' * 70}")

    all_trades = []
    for sym in syms:
        df = data.get(sym)
        if df is None or len(df) < 60:
            continue
        trades = backtest_gaps(df, sym, min_gap_pct=2.0, min_score=min_score, max_hold_days=3)
        if trades:
            all_trades.extend(trades)
            pnls = [t["pnl_pct"] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            total = sum(pnls)
            pf = sum(p for p in pnls if p > 0) / abs(sum(p for p in pnls if p <= 0)) if any(p <= 0 for p in pnls) else 999
            fills = sum(1 for t in trades if t["exit_reason"] == "fill")
            fill_rate = fills / len(trades) * 100
            print(f"  {sym:8s} | {len(trades):3d} gaps | {wins}W/{len(trades)-wins}L | "
                  f"PF {pf:.2f} | PnL {total:+.1f}% | Fill rate {fill_rate:.0f}%")

    if not all_trades:
        print("  No gaps found.")
        return

    # Fleet summary
    pnls = [t["pnl_pct"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    exits = defaultdict(int)
    for t in all_trades:
        exits[t["exit_reason"]] += 1

    print(f"\n{'=' * 70}")
    print("FLEET SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total gaps:    {len(all_trades)}")
    print(f"  Win rate:      {len(wins)/len(pnls)*100:.1f}%")
    print(f"  Profit factor: {sum(wins)/abs(sum(losses)):.2f}" if losses and sum(losses) != 0 else "  PF: inf")
    print(f"  Total PnL:     {sum(pnls):+.1f}%")
    print(f"  Expectancy:    {np.mean(pnls):+.3f}%/trade")
    print(f"  Avg hold:      {np.mean([t['days_held'] for t in all_trades]):.1f} days")
    print(f"  Exits:         {dict(exits)}")
    print(f"  Fill rate:     {exits.get('fill',0)/len(all_trades)*100:.1f}%")

    # By gap type
    for gt in ["GAP_UP", "GAP_DOWN"]:
        gt_trades = [t for t in all_trades if t["gap_type"] == gt]
        if gt_trades:
            gt_pnls = [t["pnl_pct"] for t in gt_trades]
            gt_wins = sum(1 for p in gt_pnls if p > 0)
            gt_fills = sum(1 for t in gt_trades if t["exit_reason"] == "fill")
            print(f"  {gt:10s}: {len(gt_trades)} trades | WR {gt_wins/len(gt_trades)*100:.0f}% | "
                  f"PnL {sum(gt_pnls):+.1f}% | Fill {gt_fills/len(gt_trades)*100:.0f}%")

    # By score bucket
    print(f"\nBy Score:")
    for lo, hi in [(50, 60), (60, 70), (70, 80), (80, 100)]:
        bucket = [t for t in all_trades if lo <= t["score"] < hi]
        if bucket:
            b_pnls = [t["pnl_pct"] for t in bucket]
            b_wins = sum(1 for p in b_pnls if p > 0)
            print(f"  Score {lo}-{hi}: {len(bucket)} trades | WR {b_wins/len(bucket)*100:.0f}% | "
                  f"PnL {sum(b_pnls):+.1f}% | Exp {np.mean(b_pnls):+.2f}%")

    # Save
    RESULTS_DIR = REPO / "hermes" / "data" / "backtest_results"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    pd.DataFrame(all_trades).to_csv(RESULTS_DIR / f"trades_{ts}.csv", index=False)
    print(f"\n  Saved: {RESULTS_DIR / f'trades_{ts}.csv'}")


def format_discord(gaps: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**HERMES Gap Fill Scanner -- {now}**\n"]

    if not gaps:
        lines.append("No actionable gaps today.")
        return "\n".join(lines)

    for g in gaps[:10]:
        risk_pct = abs(g["entry_price"] - g["stop_price"]) / g["entry_price"] * 100
        lines.append(
            f"  **{g['symbol']}** {g['gap_type']} {g['gap_pct']:+.1f}% | "
            f"{g['direction'].upper()} score={g['score']}\n"
            f"    Entry: ${g['entry_price']:.2f} | Target: ${g['target_price']:.2f} (fill) | "
            f"Stop: ${g['stop_price']:.2f} ({risk_pct:.1f}% risk) | "
            f"Vol={g['volume_ratio']:.1f}x | RSI={g['rsi']:.0f}"
        )

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
    parser = argparse.ArgumentParser(description="Hermes Gap Fill Scanner")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--min-score", type=int, default=55)
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if args.backtest:
        run_backtest(min_score=args.min_score)
        return

    print(f"{'=' * 60}")
    print(f"HERMES Gap Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 60}")

    gaps = scan_today(min_score=args.min_score)

    if gaps:
        print(f"\n{'Symbol':8s} {'Type':8s} {'Gap%':>6s} {'Dir':6s} {'Score':>5s} {'Entry':>8s} {'Target':>8s} {'R:R':>5s}")
        print("-" * 65)
        for g in gaps:
            print(f"{g['symbol']:8s} {g['gap_type']:8s} {g['gap_pct']:+5.1f}% {g['direction']:6s} "
                  f"{g['score']:5d} ${g['entry_price']:>7.2f} ${g['target_price']:>7.2f} {g['risk_reward']:5.2f}")
    else:
        print("\n  No actionable gaps today.")

    report = format_discord(gaps)
    print(f"\n{report}")

    if not args.dry_run or args.execute:
        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")

    # Save
    log_path = LOGS_DIR / f"scan_{datetime.now().strftime('%Y%m%d')}.json"
    log_path.write_text(json.dumps(gaps, indent=2, default=str))
    print(f"Saved: {log_path}")


if __name__ == "__main__":
    main()
