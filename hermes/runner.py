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
try:
    from helio.ibkr_executor import IBKRExecutor, CLIENT_IDS
    _IBKR_AVAILABLE = True
except ImportError:
    _IBKR_AVAILABLE = False

try:
    from helio.fleet_risk import check_exposure
    _FLEET_RISK_AVAILABLE = True
except ImportError:
    _FLEET_RISK_AVAILABLE = False

try:
    from helio.portfolio_guard import check_new_entry as _pg_check_new_entry
except ImportError:
    _pg_check_new_entry = None

from ops.process_lock import ProcessLock, ProcessLockError

DATA_DIR = REPO / "hermes" / "data"
LOGS_DIR = REPO / "hermes" / "logs"
POSITIONS_FILE = LOGS_DIR / "positions.json"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
MODEL_EQUITY = 10000
MAX_RISK_PCT = 0.01  # 1% per gap fill trade (smaller than Titan)
MIN_SCORE_TO_ENTER = 80  # Only score 80+ from backtest validation
MAX_HOLD_DAYS = 3

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


def load_positions() -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_positions(positions: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


def execute_entries(gaps: list[dict], executor, positions: dict) -> int:
    """Enter new gap fill trades. Only score 80+, gap-down direction."""
    if executor is None:
        return 0

    # Filter to only gap-DOWN long fills (proven edge)
    actionable = [g for g in gaps if g["score"] >= MIN_SCORE_TO_ENTER and g["direction"] == "long"]
    if not actionable:
        return 0

    entered = 0
    for g in actionable[:3]:  # max 3 new positions per scan
        sym = g["symbol"]
        if sym in positions:
            continue

        # Cross-system exposure check
        if _FLEET_RISK_AVAILABLE:
            exposure = check_exposure(sym)
            if exposure["blocked"]:
                print(f"  {sym}: FLEET BLOCK -- {exposure['reason']}")
                continue
        if _pg_check_new_entry is not None:
            pg_check = _pg_check_new_entry("hermes_gap", sym, "LONG")
            if not pg_check.allowed:
                print(f"  {sym}: PORTFOLIO BLOCK -- {pg_check.reason}")
                continue
            if pg_check.warnings:
                print(f"  {sym}: portfolio warnings -- {pg_check.warnings}")
        risk_amount = MODEL_EQUITY * MAX_RISK_PCT
        risk_per_share = abs(g["entry_price"] - g["stop_price"])
        if risk_per_share <= 0:
            continue
        shares = int(risk_amount / risk_per_share)
        if shares <= 0:
            continue

        order_id = executor.submit_bracket(
            symbol=sym, direction="long", quantity=shares,
            entry_price=g["entry_price"], stop_price=g["stop_price"],
            target_price=g["target_price"], order_type="MKT",
        )
        if order_id:
            positions[sym] = {
                "direction": "long",
                "entry_price": g["entry_price"],
                "stop_price": g["stop_price"],
                "target_price": g["target_price"],
                "shares": shares,
                "entry_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "score": g["score"],
                "gap_pct": g["gap_pct"],
                "order_id": order_id,
            }
            entered += 1
            send_discord(
                f"**HERMES ENTRY: {sym} LONG (gap fill)**\n"
                f"Score {g['score']} | Gap {g['gap_pct']}% | "
                f"${g['entry_price']:.2f} → ${g['target_price']:.2f} | Stop ${g['stop_price']:.2f}"
            )
    return entered


def check_exits(positions: dict, executor) -> int:
    """Check open positions for stop/target/timeout exits."""
    if not positions:
        return 0

    closed = []
    for sym, pos in list(positions.items()):
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period="5d", interval="1d", auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            current = float(df["Close"].iloc[-1])
            today_high = float(df["High"].iloc[-1])
            today_low = float(df["Low"].iloc[-1])
        except Exception:
            continue

        entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_held = (datetime.now(timezone.utc) - entry_date).days

        exit_reason = None
        exit_price = current

        # PDT rule eliminated by SEC (2026-04-14). No minimum hold required.

        if pos["direction"] == "long":
            if today_low <= pos["stop_price"]:
                exit_reason = "stop"
                exit_price = pos["stop_price"]
            elif today_high >= pos["target_price"]:
                exit_reason = "target"
                exit_price = pos["target_price"]

        if days_held >= MAX_HOLD_DAYS:
            exit_reason = "timeout"

        if exit_reason:
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
            if executor:
                executor.close_position(sym)
            send_discord(
                f"**HERMES EXIT: {sym} LONG -- {exit_reason.upper()}**\n"
                f"PnL: {pnl_pct:+.2f}% | {days_held}d held"
            )
            closed.append(sym)

    for sym in closed:
        del positions[sym]
    return len(closed)


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


def run_cycle(args, executor):
    """One scan + execution + exit cycle."""
    print(f"\n=== HERMES cycle @ {datetime.now(timezone.utc).strftime('%H:%M UTC')} ===")

    # Atlas regime context (LOG_ONLY during burn-in)
    try:
        from forge.atlas.fleet_gate import check_atlas
        _atlas = check_atlas()
        if _atlas.available:
            _atlas.log_recommendation("hermes")
    except Exception:
        pass  # Atlas is optional — never break the runner

    positions = load_positions()

    # Check exits first
    closed = check_exits(positions, executor)
    if closed:
        print(f"  Closed {closed} positions")

    # Scan for new entries
    gaps = scan_today(min_score=args.min_score)
    if gaps:
        print(f"\n{'Symbol':8s} {'Type':8s} {'Gap%':>6s} {'Dir':6s} {'Score':>5s} {'Entry':>8s} {'Target':>8s}")
        print("-" * 65)
        for g in gaps[:10]:
            print(f"{g['symbol']:8s} {g['gap_type']:8s} {g['gap_pct']:+5.1f}% {g['direction']:6s} "
                  f"{g['score']:5d} ${g['entry_price']:>7.2f} ${g['target_price']:>7.2f}")
    else:
        print("  No actionable gaps")

    # Execute new entries (only if live + executor)
    if executor and gaps:
        entered = execute_entries(gaps, executor, positions)
        if entered:
            print(f"  Entered {entered} new positions")

    save_positions(positions)

    # Discord summary
    if gaps:
        report = format_discord(gaps)
        send_discord(report)

    # Heartbeat
    try:
        hb_path = LOGS_DIR / "heartbeat.json"
        hb_path.write_text(json.dumps({
            "system": "hermes",
            "family": "hermes_gap",
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": "LIVE" if executor else "PAPER",
            "open_positions": len(positions),
            "gaps_today": len(gaps),
            "ibkr_connected": executor.is_connected() if executor else False,
        }, indent=2))
    except Exception:
        pass

    # Save scan log
    log_path = LOGS_DIR / f"scan_{datetime.now().strftime('%Y%m%d')}.json"
    log_path.write_text(json.dumps(gaps, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="Hermes Gap Fill Scanner")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true", help="Live IBKR execution")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval-min", type=int, default=120, help="Loop interval in minutes")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--min-score", type=int, default=55)
    args = parser.parse_args()
    lock = None
    if not args.backtest:
        lock = ProcessLock("hermes_gap_runner")
        try:
            lock.acquire(metadata={
                "family": "hermes_gap",
                "mode": "LIVE" if args.live else "PAPER",
                "loop": args.loop,
            })
        except ProcessLockError as exc:
            print(f"Cannot start Hermes runner: {exc}")
            return

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    executor = None
    try:
        if args.backtest:
            run_backtest(min_score=args.min_score)
            return

        print(f"{'=' * 60}")
        print(f"HERMES Gap Scanner -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 60}")

        # Connect IBKR if live
        if args.live and _IBKR_AVAILABLE:
            executor = IBKRExecutor(
                client_id=CLIENT_IDS["hermes"],
                system="hermes",
                model_equity_usd=MODEL_EQUITY,
            )
            if not executor.connect():
                print("  IBKR connection failed — running in scan-only mode")
                executor = None

        if args.loop:
            try:
                while True:
                    run_cycle(args, executor)
                    print(f"\nSleeping {args.interval_min}min until next cycle...")
                    time.sleep(args.interval_min * 60)
            except KeyboardInterrupt:
                print("\nHermes stopped")
        else:
            run_cycle(args, executor)
    finally:
        if executor:
            executor.disconnect()
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
