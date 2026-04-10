#!/usr/bin/env python3
"""ares/runner.py -- Sector rotation runner.

Monthly evaluation: rank sectors, rebalance to top 2.
Posts rotation signals to Discord.

Usage:
    python -m ares.runner                    # Evaluate + show signal
    python -m ares.runner --backtest         # Run full backtest
    python -m ares.runner --backtest --extended  # Extended universe (12 ETFs)
    python -m ares.runner --dry-run          # Just show what would change
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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

from ares.strategies.rotation import (
    DEFAULT_UNIVERSE, EXTENDED_UNIVERSE,
    generate_signal, backtest, rank_sectors,
)
try:
    from helio.ibkr_executor import IBKRExecutor, CLIENT_IDS
    _IBKR_AVAILABLE = True
except ImportError:
    _IBKR_AVAILABLE = False

DATA_DIR = REPO / "ares" / "data"
LOGS_DIR = REPO / "ares" / "logs"
POSITIONS_FILE = LOGS_DIR / "positions.json"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


def download_data(universe: list[str], period: str = "5y") -> dict[str, pd.DataFrame]:
    """Download daily data for all symbols."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}

    for sym in universe:
        cache = DATA_DIR / f"{sym}_daily.csv"

        # Use cache if < 12h old
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
            if not df.empty and len(df) >= 60:
                df.index.name = "date"
                df.to_csv(cache)
                data[sym] = df
        except Exception:
            continue

    return data


def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_positions(positions: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


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


def execute_rotation(signal, data, executor, equity_per_slot: float):
    """Execute Ares rotation via IBKR. Sells positions to exit, buys top N."""
    if executor is None:
        print("  [DRY] No IBKR executor — skipping execution")
        return False

    # Sells first
    for sym in signal.sell:
        executor.close_position(sym)
        print(f"  EXECUTED CLOSE: {sym}")

    # Buys
    for sym in signal.buy:
        sym_df = data.get(sym)
        if sym_df is None:
            continue
        price = float(sym_df["Close"].iloc[-1])
        shares = int(equity_per_slot / price)
        if shares <= 0:
            print(f"  SKIP {sym}: insufficient equity per slot for ${price:.2f}")
            continue
        order_id = executor.submit_market(symbol=sym, direction="long", quantity=shares)
        print(f"  EXECUTED BUY: {sym} {shares} shares @ ~${price:.2f} order={order_id}")

    return True


def run_evaluation(universe: list[str], dry_run: bool = True, force: bool = False):
    """Run monthly evaluation and post signal.

    Only rebalances on the first trading day of the month unless --force.
    """
    positions = load_positions()
    current_holdings = list(positions.get("holdings", {}).keys())

    # Monthly rebalance guard
    last_rebalance = positions.get("last_rebalance", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not force and last_rebalance:
        try:
            last_dt = datetime.fromisoformat(last_rebalance.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since < 25:
                print(f"  Last rebalance: {last_rebalance[:10]} ({days_since}d ago) — skipping (monthly cadence)")
                # Still post current holdings to Discord for awareness
                if current_holdings:
                    msg = f"**ARES Status -- {today}**\nCurrent holdings: {', '.join(current_holdings)}\nNext rebalance: in {25 - days_since}d"
                    send_discord(msg)
                return
        except Exception:
            pass

    print(f"Downloading data for {len(universe)} ETFs...")
    data = download_data(universe)

    signal = generate_signal(data, current_holdings=current_holdings, top_n=2, universe=universe)

    # Print rankings
    print(f"\n{'Rank':>4s} {'Symbol':8s} {'Score':>8s} {'3M':>8s} {'1M':>8s} {'1W':>8s}")
    print("-" * 50)
    for r in signal.rankings:
        marker = " <-- HOLD" if r["symbol"] in current_holdings else (" <-- BUY" if r["symbol"] in signal.buy else "")
        print(f"  {r['rank']:2d}  {r['symbol']:8s} {r['score']:+7.2f}  "
              f"{r['return_3m']:+7.1f}% {r['return_1m']:+7.1f}% {r['return_1w']:+7.1f}%{marker}")

    # Risk-off check
    if signal.risk_off:
        print(f"\n  RISK OFF: {signal.risk_off_reason}")
    else:
        print(f"\n  SPY vs 200 EMA: {signal.spy_vs_200:+.1f}% (risk ON)")

    # Actions
    if signal.buy:
        print(f"\n  BUY:  {', '.join(signal.buy)}")
    if signal.sell:
        print(f"  SELL: {', '.join(signal.sell)}")
    if signal.hold:
        print(f"  HOLD: {', '.join(signal.hold)}")
    if not signal.buy and not signal.sell:
        print(f"\n  No changes needed.")

    # Discord report
    lines = [f"**ARES Sector Rotation -- {signal.date}**\n"]
    lines.append("**Rankings:**")
    for r in signal.rankings:
        tag = " **BUY**" if r["symbol"] in signal.buy else (" HOLD" if r["symbol"] in signal.hold else "")
        lines.append(f"  #{r['rank']} {r['symbol']} score={r['score']:+.2f} "
                     f"(3M:{r['return_3m']:+.1f}%, 1M:{r['return_1m']:+.1f}%){tag}")

    if signal.risk_off:
        lines.append(f"\n**RISK OFF** — {signal.risk_off_reason}")
    elif signal.buy or signal.sell:
        if signal.buy:
            lines.append(f"\n**BUY:** {', '.join(signal.buy)}")
        if signal.sell:
            lines.append(f"**SELL:** {', '.join(signal.sell)}")
    else:
        lines.append(f"\nNo changes. Holding: {', '.join(signal.hold) or 'cash'}")

    report = "\n".join(lines)

    if not dry_run:
        # IBKR execution
        executor = None
        if _IBKR_AVAILABLE and (signal.buy or signal.sell):
            executor = IBKRExecutor(
                client_id=CLIENT_IDS["ares"],
                system="ares",
                model_equity_usd=10000,
            )
            if executor.connect():
                # Equal weight across top N
                equity_per_slot = 10000 / 2  # top 2 holdings
                execute_rotation(signal, data, executor, equity_per_slot)
                executor.disconnect()
            else:
                print("  IBKR connection failed — positions tracked but not executed")
                executor = None

        # Update positions tracking
        holdings = positions.get("holdings", {})
        for sym in signal.sell:
            holdings.pop(sym, None)
        for sym in signal.buy:
            sym_df = data.get(sym)
            if sym_df is not None:
                holdings[sym] = {
                    "entry_price": round(float(sym_df["Close"].iloc[-1]), 2),
                    "entry_date": datetime.now(timezone.utc).isoformat(),
                }
        positions["holdings"] = holdings
        positions["last_rebalance"] = datetime.now(timezone.utc).isoformat()
        positions["last_signal"] = {
            "buy": signal.buy, "sell": signal.sell,
            "hold": signal.hold, "risk_off": signal.risk_off,
        }
        save_positions(positions)

        ok = send_discord(report)
        print(f"\nDiscord: {'sent' if ok else 'FAILED'}")
    else:
        print(f"\n{report}")
        print("\n[DRY RUN] No positions updated.")

    # Save signal log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"signal_{signal.date}.json"
    log_path.write_text(json.dumps({
        "date": signal.date,
        "rankings": signal.rankings,
        "buy": signal.buy, "sell": signal.sell, "hold": signal.hold,
        "risk_off": signal.risk_off, "spy_vs_200": signal.spy_vs_200,
    }, indent=2))
    print(f"Saved: {log_path}")


def run_backtest(universe: list[str]):
    """Run full backtest and print results."""
    print(f"Downloading data for {len(universe)} ETFs...")
    data = download_data(universe)

    print(f"\n{'=' * 60}")
    print("ARES SECTOR ROTATION BACKTEST")
    print(f"Universe: {', '.join(universe)}")
    print(f"{'=' * 60}")

    result = backtest(data, universe=universe, top_n=2)

    if result.get("error"):
        print(f"  Error: {result['error']}")
        return

    if result["trades"] == 0:
        print("  No trades generated.")
        return

    print(f"  Period:        {result['years']} years")
    print(f"  Trades:        {result['trades']} ({result['wins']}W / {result['losses']}L)")
    print(f"  Win Rate:      {result['win_rate']}%")
    print(f"  Profit Factor: {result['profit_factor']}")
    print(f"  Total Return:  {result['total_return_pct']:+.1f}%")
    print(f"  Ann. Return:   {result['annualized_return_pct']:+.1f}%")
    print(f"  Max Drawdown:  {result['max_drawdown_pct']:.1f}%")
    print(f"  Avg Hold:      {result['avg_hold_days']:.0f} days")
    print(f"  Rebalances:    {result['rebalances']}")
    print(f"  ${result['start_equity']:,.0f} -> ${result['final_equity']:,.0f}")

    # Save results
    RESULTS_DIR = REPO / "ares" / "data" / "backtest_results"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    result_path = RESULTS_DIR / f"backtest_{ts}.json"
    # Don't save full equity curve in summary (too large)
    save_result = {k: v for k, v in result.items() if k != "equity_curve"}
    result_path.write_text(json.dumps(save_result, indent=2, default=str))
    print(f"\n  Saved: {result_path}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Ares Sector Rotation")
    parser.add_argument("--backtest", action="store_true", help="Run backtest")
    parser.add_argument("--extended", action="store_true", help="Use extended 12-ETF universe")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", help="Actually update positions + Discord + IBKR")
    parser.add_argument("--force", action="store_true", help="Force rebalance even if not month-end")
    args = parser.parse_args()

    universe = EXTENDED_UNIVERSE if args.extended else DEFAULT_UNIVERSE
    dry_run = not args.execute

    if args.backtest:
        run_backtest(universe)
    else:
        print(f"{'=' * 60}")
        print(f"ARES Sector Rotation -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 60}")
        run_evaluation(universe, dry_run=dry_run, force=args.force)


if __name__ == "__main__":
    main()
