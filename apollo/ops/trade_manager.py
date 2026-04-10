"""apollo/ops/trade_manager.py -- Apollo position tracking and exit management.

Tracks open positions, applies exit rules daily, logs results.
Designed for weekly review on Fridays.

Lifecycle:
  1. Apollo scanner flags a setup (score 60+)
  2. You enter before market close on entry day
  3. trade_manager tracks the position from next morning
  4. Applies PDT-safe exit rules (min 2-day hold)
  5. Logs the trade when closed
  6. Weekly summary every Friday
"""
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

LOGS_DIR = REPO / "apollo" / "logs"
POSITIONS_FILE = LOGS_DIR / "positions.json"
TRADES_FILE = LOGS_DIR / "trades.csv"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

_log = logging.getLogger("apollo.manager")

TRADE_FIELDS = [
    "symbol", "direction", "entry_date", "exit_date", "entry_price",
    "exit_price", "pnl_pct", "pnl_usd", "exit_reason", "days_held",
    "score", "conviction", "earnings_date", "beat_rate",
]


def load_positions() -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return {"positions": {}, "day_trades_this_week": 0, "week_start": ""}
    return {"positions": {}, "day_trades_this_week": 0, "week_start": ""}


def save_positions(data: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(data, indent=2, default=str))


def add_position(symbol: str, direction: str, entry_price: float,
                 score: int = 0, conviction: str = "medium",
                 earnings_date: str = "", beat_rate: float = 0,
                 stop_pct: float = 5.0, target_pct: float = 15.0):
    """Record a new position entry."""
    data = load_positions()
    if symbol in data["positions"]:
        print(f"  Already have position in {symbol}")
        return

    if direction == "long":
        stop_price = entry_price * (1 - stop_pct / 100)
        target_price = entry_price * (1 + target_pct / 100)
    else:
        stop_price = entry_price * (1 + stop_pct / 100)
        target_price = entry_price * (1 - target_pct / 100)

    data["positions"][symbol] = {
        "direction": direction,
        "entry_price": round(entry_price, 2),
        "entry_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "stop_price": round(stop_price, 2),
        "target_price": round(target_price, 2),
        "trailing_stop": None,
        "score": score,
        "conviction": conviction,
        "earnings_date": earnings_date,
        "beat_rate": beat_rate,
        "days_held": 0,
        "peak_price": entry_price,
        "trough_price": entry_price,
        "status": "OPEN",
    }
    save_positions(data)
    print(f"  ENTERED: {symbol} {direction.upper()} @ ${entry_price:.2f} | "
          f"Stop ${stop_price:.2f} | Target ${target_price:.2f}")


def check_exits():
    """Check all open positions for exit signals. Run daily."""
    data = load_positions()
    positions = data.get("positions", {})
    closed = []

    if not positions:
        print("  No open positions.")
        return

    for sym, pos in list(positions.items()):
        if pos.get("status") != "OPEN":
            continue

        # Get current price
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
        except Exception as e:
            print(f"  {sym}: price fetch failed: {e}")
            continue

        entry = pos["entry_price"]
        direction = pos["direction"]
        entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
        days_held = (datetime.now() - entry_date).days
        pos["days_held"] = days_held

        # Update peak/trough
        if direction == "long":
            pos["peak_price"] = max(pos.get("peak_price", entry), today_high)
            pos["trough_price"] = min(pos.get("trough_price", entry), today_low)
            unrealized_pct = (current - entry) / entry * 100
        else:
            pos["peak_price"] = min(pos.get("peak_price", entry), today_low)  # best short price
            pos["trough_price"] = max(pos.get("trough_price", entry), today_high)
            unrealized_pct = (entry - current) / entry * 100

        exit_reason = None
        exit_price = current

        # PDT check: minimum 2 days held before any exit
        if days_held < 2:
            print(f"  {sym}: DAY {days_held} (PDT hold) | {unrealized_pct:+.1f}% unrealized")
            continue

        # Check exit conditions
        if direction == "long":
            # Stop hit
            if today_low <= pos["stop_price"]:
                exit_reason = "stop"
                exit_price = pos["stop_price"]

            # Target hit
            elif today_high >= pos["target_price"]:
                exit_reason = "target"
                exit_price = pos["target_price"]

            # Trailing stop (activate after 5%+ profit)
            elif unrealized_pct > 5:
                trail = pos["peak_price"] * 0.95  # 5% trail from peak
                if pos.get("trailing_stop") is None or trail > pos["trailing_stop"]:
                    pos["trailing_stop"] = round(trail, 2)
                if today_low <= pos.get("trailing_stop", 0):
                    exit_reason = "trailing_stop"
                    exit_price = pos["trailing_stop"]

        else:  # short
            if today_high >= pos["stop_price"]:
                exit_reason = "stop"
                exit_price = pos["stop_price"]
            elif today_low <= pos["target_price"]:
                exit_reason = "target"
                exit_price = pos["target_price"]
            elif unrealized_pct > 5:
                trail = pos["peak_price"] * 1.05
                if pos.get("trailing_stop") is None or trail < pos["trailing_stop"]:
                    pos["trailing_stop"] = round(trail, 2)
                if today_high >= pos.get("trailing_stop", 99999):
                    exit_reason = "trailing_stop"
                    exit_price = pos["trailing_stop"]

        # Timeout: 20 days for longs, 10 for shorts
        max_days = 10 if direction == "short" else 20
        if days_held >= max_days:
            exit_reason = "timeout"
            exit_price = current

        if exit_reason:
            if direction == "long":
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_price) / entry * 100

            print(f"  {sym}: EXIT ({exit_reason}) | {pnl_pct:+.1f}% | {days_held}d held")

            # Log trade
            _log_trade(sym, pos, exit_price, exit_reason, pnl_pct, days_held)
            _send_discord(
                f"**APOLLO EXIT: {sym} {direction.upper()} -- {exit_reason.upper()}**\n"
                f"PnL: {pnl_pct:+.1f}% | Entry ${entry:.2f} -> Exit ${exit_price:.2f} | {days_held}d held"
            )
            closed.append(sym)
        else:
            trail_str = f" | Trail ${pos.get('trailing_stop', 'none')}" if pos.get("trailing_stop") else ""
            print(f"  {sym}: HOLD Day {days_held} | {unrealized_pct:+.1f}%{trail_str}")

    # Remove closed positions
    for sym in closed:
        del data["positions"][sym]
    save_positions(data)


def weekly_review():
    """Generate weekly performance summary. Run on Fridays."""
    trades = _load_trades()
    if not trades:
        print("  No closed trades yet.")
        return

    # This week's trades
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    this_week = [t for t in trades if t.get("exit_date", "") >= week_start]
    all_time = trades

    for label, trade_list in [("THIS WEEK", this_week), ("ALL TIME", all_time)]:
        if not trade_list:
            continue
        pnls = [float(t.get("pnl_pct", 0)) for t in trade_list]
        wins = sum(1 for p in pnls if p > 0)
        losses = len(pnls) - wins
        total = sum(pnls)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p <= 0))
        pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999

        print(f"\n  {label}:")
        print(f"    Trades: {len(trade_list)} ({wins}W / {losses}L)")
        print(f"    Win Rate: {wins/len(pnls)*100:.0f}%")
        print(f"    PF: {pf}")
        print(f"    Total PnL: {total:+.1f}%")
        print(f"    Avg Win: {np.mean([p for p in pnls if p > 0]):+.1f}%" if wins else "")
        print(f"    Avg Loss: {np.mean([p for p in pnls if p <= 0]):+.1f}%" if losses else "")

    # Open positions summary
    data = load_positions()
    open_pos = data.get("positions", {})
    if open_pos:
        print(f"\n  OPEN POSITIONS ({len(open_pos)}):")
        for sym, pos in open_pos.items():
            print(f"    {sym} {pos['direction'].upper()} @ ${pos['entry_price']:.2f} | "
                  f"Day {pos.get('days_held', 0)} | Score {pos.get('score', 0)}")

    # Discord weekly summary
    report = f"**APOLLO Weekly Review -- {datetime.now().strftime('%Y-%m-%d')}**\n"
    if this_week:
        pnls = [float(t["pnl_pct"]) for t in this_week]
        wins = sum(1 for p in pnls if p > 0)
        report += f"This week: {len(this_week)} trades, {wins}W/{len(this_week)-wins}L, {sum(pnls):+.1f}%\n"
    if all_time:
        pnls = [float(t["pnl_pct"]) for t in all_time]
        wins = sum(1 for p in pnls if p > 0)
        report += f"All time: {len(all_time)} trades, {wins}W/{len(all_time)-wins}L, {sum(pnls):+.1f}%"
    if open_pos:
        report += f"\nOpen: {', '.join(f'{s} {p[\"direction\"][0].upper()}' for s, p in open_pos.items())}"

    _send_discord(report)
    return report


def status():
    """Print current positions and pending setups."""
    data = load_positions()
    positions = data.get("positions", {})

    if not positions:
        print("  No open positions.")
    else:
        print(f"\n  {'Symbol':8s} {'Dir':6s} {'Entry':>8s} {'Stop':>8s} {'Target':>8s} "
              f"{'Trail':>8s} {'Days':>5s} {'Score':>5s}")
        print("  " + "-" * 65)
        for sym, pos in positions.items():
            trail = f"${pos['trailing_stop']:.2f}" if pos.get("trailing_stop") else "---"
            print(f"  {sym:8s} {pos['direction']:6s} ${pos['entry_price']:>7.2f} "
                  f"${pos['stop_price']:>7.2f} ${pos['target_price']:>7.2f} "
                  f"{trail:>8s} {pos.get('days_held', 0):5d} {pos.get('score', 0):5d}")


def _log_trade(sym, pos, exit_price, exit_reason, pnl_pct, days_held):
    write_header = not TRADES_FILE.exists()
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "symbol": sym,
            "direction": pos["direction"],
            "entry_date": pos["entry_date"],
            "exit_date": datetime.now().strftime("%Y-%m-%d"),
            "entry_price": pos["entry_price"],
            "exit_price": round(exit_price, 2),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": 0,  # TODO: calculate from position size
            "exit_reason": exit_reason,
            "days_held": days_held,
            "score": pos.get("score", 0),
            "conviction": pos.get("conviction", ""),
            "earnings_date": pos.get("earnings_date", ""),
            "beat_rate": pos.get("beat_rate", 0),
        })


def _load_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    with open(TRADES_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _send_discord(message: str):
    if not WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(WEBHOOK_URL, json={"content": message[:2000]}, timeout=10)
    except Exception:
        pass
