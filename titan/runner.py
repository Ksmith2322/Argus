#!/usr/bin/env python3
"""titan/runner.py -- Titan swing trade execution runner.

Manages the trade lifecycle for swing positions via IBKR:
  1. Reads signals from nightly scanner output
  2. Submits bracket orders (entry + stop + target)
  3. Monitors positions daily
  4. Logs trades and feeds AI overlay learning

Designed for daily evaluation — NOT a 24/7 loop like Argus.
Run after scanner, during market hours.

Usage:
    python -m titan.runner                          # Evaluate + execute
    python -m titan.runner --dry-run                # Paper simulation (no IBKR)
    python -m titan.runner --check                  # Check open positions only
    python -m titan.runner --close NVDA             # Close specific position
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from titan.ops.data_pipeline import UNIVERSE, get_data
from titan.strategies.ai_overlay import SwingOverlay

LOGS_DIR = REPO / "titan" / "logs"
POSITIONS_FILE = LOGS_DIR / "positions.json"
TRADES_FILE = LOGS_DIR / "trades.csv"
SCAN_DIR = REPO / "titan" / "logs"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("titan.runner")

TRADE_CSV_FIELDS = [
    "symbol", "direction", "strategy", "entry_date", "exit_date",
    "entry_price", "exit_price", "stop_price", "target_price",
    "pnl_pct", "pnl_usd", "exit_reason", "days_held",
    "signal_strength", "ai_action", "ai_consensus",
    "position_size", "risk_usd",
]


class TitanRunner:
    """Daily swing trade position manager."""

    def __init__(self, config: dict | None = None, dry_run: bool = True):
        cfg = config or {}
        self.dry_run = dry_run
        self.max_positions = cfg.get("max_positions", 5)
        self.max_risk_pct = cfg.get("max_risk_pct", 0.02)  # 2% per trade
        self.model_equity = cfg.get("model_equity", 10000)
        self.max_hold_days = cfg.get("max_hold_days", 20)
        self.long_only = cfg.get("long_only", True)

        self.overlay = SwingOverlay(symbol="FLEET", state_dir=LOGS_DIR)
        self.positions = self._load_positions()
        self._ib = None

        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_positions(self) -> dict:
        if POSITIONS_FILE.exists():
            try:
                return json.loads(POSITIONS_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _save_positions(self):
        POSITIONS_FILE.write_text(json.dumps(self.positions, indent=2, default=str))

    def _load_latest_scan(self) -> list[dict]:
        """Load most recent scanner output."""
        scan_files = sorted(SCAN_DIR.glob("scan_*.json"), reverse=True)
        if not scan_files:
            log.warning("No scan files found. Run scanner first.")
            return []
        latest = scan_files[0]
        try:
            signals = json.loads(latest.read_text())
            log.info(f"Loaded {len(signals)} signals from {latest.name}")
            return signals
        except Exception as e:
            log.error(f"Failed to load scan: {e}")
            return []

    def evaluate_entries(self):
        """Check scanner signals for new entries."""
        signals = self._load_latest_scan()
        if not signals:
            return

        open_count = len(self.positions)
        open_symbols = set(self.positions.keys())

        for sig in signals:
            if open_count >= self.max_positions:
                log.info(f"Max positions ({self.max_positions}) reached. Skipping remaining signals.")
                break

            sym = sig["symbol"]
            if sym in open_symbols:
                log.info(f"  {sym}: already have position, skip")
                continue

            if self.long_only and sig["direction"] == "SHORT":
                continue

            # Minimum signal strength
            if sig["strength"] < 65:
                continue

            # AI overlay filter
            ai_action = sig.get("ai_action", "TAKE")
            if ai_action == "SKIP":
                log.info(f"  {sym}: AI SKIP (consensus={sig.get('ai_consensus', 0):+.2f})")
                continue

            # Position sizing
            risk_amount = self.model_equity * self.max_risk_pct
            entry = sig["entry"]
            stop = sig["stop"]
            risk_per_share = abs(entry - stop)
            if risk_per_share <= 0:
                continue
            shares = int(risk_amount / risk_per_share)
            if shares <= 0:
                continue
            risk_usd = shares * risk_per_share

            log.info(
                f"  ENTRY: {sym} {sig['direction']} @ ${entry:.2f} | "
                f"Stop ${stop:.2f} | Target ${sig['target']:.2f} | "
                f"{shares} shares | Risk ${risk_usd:.2f} | "
                f"AI={ai_action}({sig.get('ai_consensus', 0):+.2f}) | "
                f"Strategy={sig['strategy']}"
            )

            if not self.dry_run:
                success = self._submit_order(sym, sig["direction"], entry, stop, sig["target"], shares)
                if not success:
                    log.error(f"  Order submission failed for {sym}")
                    continue

            # Record position
            self.positions[sym] = {
                "direction": sig["direction"],
                "entry_price": entry,
                "stop_price": stop,
                "target_price": sig["target"],
                "shares": shares,
                "risk_usd": round(risk_usd, 2),
                "entry_date": datetime.now(timezone.utc).isoformat(),
                "strategy": sig["strategy"],
                "signal_strength": sig["strength"],
                "ai_action": ai_action,
                "ai_consensus": sig.get("ai_consensus", 0),
            }
            open_count += 1
            open_symbols.add(sym)

            self._send_discord(
                f"**TITAN ENTRY: {sym} {sig['direction']}**\n"
                f"${entry:.2f} | Stop ${stop:.2f} | Target ${sig['target']:.2f} | "
                f"{shares} shares | {sig['strategy']}"
            )

        self._save_positions()

    def check_exits(self):
        """Check open positions for stop/target/timeout exits."""
        if not self.positions:
            log.info("No open positions.")
            return

        closed = []
        for sym, pos in list(self.positions.items()):
            daily = get_data(sym, "daily")
            if daily is None or len(daily) < 2:
                continue

            current_price = daily["Close"].iloc[-1]
            current_high = daily["High"].iloc[-1]
            current_low = daily["Low"].iloc[-1]
            entry_date = datetime.fromisoformat(pos["entry_date"])
            days_held = (datetime.now(timezone.utc) - entry_date).days

            exit_reason = None
            exit_price = current_price

            if pos["direction"] == "LONG":
                if current_low <= pos["stop_price"]:
                    exit_reason = "stop"
                    exit_price = pos["stop_price"]
                elif current_high >= pos["target_price"]:
                    exit_reason = "target"
                    exit_price = pos["target_price"]
            else:
                if current_high >= pos["stop_price"]:
                    exit_reason = "stop"
                    exit_price = pos["stop_price"]
                elif current_low <= pos["target_price"]:
                    exit_reason = "target"
                    exit_price = pos["target_price"]

            if days_held >= self.max_hold_days:
                exit_reason = "timeout"
                exit_price = current_price

            if exit_reason:
                if pos["direction"] == "LONG":
                    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                else:
                    pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100
                pnl_usd = pnl_pct / 100 * pos["entry_price"] * pos["shares"]

                log.info(
                    f"  EXIT: {sym} {pos['direction']} | {exit_reason} | "
                    f"PnL {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | {days_held}d held"
                )

                # AI overlay learning
                self.overlay.learn(pnl_pct)

                # Log trade
                self._log_trade(sym, pos, exit_price, exit_reason, pnl_pct, pnl_usd, days_held)

                self._send_discord(
                    f"**TITAN EXIT: {sym} {pos['direction']} — {exit_reason.upper()}**\n"
                    f"PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | {days_held}d held"
                )

                if not self.dry_run:
                    self._close_position(sym)

                closed.append(sym)
            else:
                # Status update
                if pos["direction"] == "LONG":
                    unrealized = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                else:
                    unrealized = (pos["entry_price"] - current_price) / pos["entry_price"] * 100
                log.info(f"  HOLD: {sym} {pos['direction']} | {unrealized:+.2f}% | {days_held}d")

        for sym in closed:
            del self.positions[sym]
        self._save_positions()

    def status(self):
        """Print current portfolio status."""
        if not self.positions:
            print("No open positions.")
            return

        print(f"\n{'Symbol':8s} {'Dir':6s} {'Entry':>8s} {'Stop':>8s} {'Target':>8s} {'Days':>5s} {'Strategy':15s}")
        print("-" * 65)
        for sym, pos in self.positions.items():
            entry_date = datetime.fromisoformat(pos["entry_date"])
            days = (datetime.now(timezone.utc) - entry_date).days
            print(f"{sym:8s} {pos['direction']:6s} ${pos['entry_price']:>7.2f} "
                  f"${pos['stop_price']:>7.2f} ${pos['target_price']:>7.2f} "
                  f"{days:5d} {pos['strategy']:15s}")

    def _log_trade(self, sym, pos, exit_price, exit_reason, pnl_pct, pnl_usd, days_held):
        write_header = not TRADES_FILE.exists()
        with open(TRADES_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_CSV_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow({
                "symbol": sym,
                "direction": pos["direction"],
                "strategy": pos["strategy"],
                "entry_date": pos["entry_date"][:10],
                "exit_date": datetime.now(timezone.utc).isoformat()[:10],
                "entry_price": pos["entry_price"],
                "exit_price": round(exit_price, 2),
                "stop_price": pos["stop_price"],
                "target_price": pos["target_price"],
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "exit_reason": exit_reason,
                "days_held": days_held,
                "signal_strength": pos.get("signal_strength", 0),
                "ai_action": pos.get("ai_action", ""),
                "ai_consensus": pos.get("ai_consensus", 0),
                "position_size": pos.get("shares", 0),
                "risk_usd": pos.get("risk_usd", 0),
            })

    def _submit_order(self, sym, direction, entry, stop, target, shares):
        """Submit bracket order to IBKR. Returns True on success."""
        # TODO: Wire IBKR ib_insync order submission
        log.info(f"  [IBKR] Would submit {direction} {shares} {sym} @ ${entry:.2f}")
        return True

    def _close_position(self, sym):
        """Close IBKR position."""
        log.info(f"  [IBKR] Would close {sym}")

    def _send_discord(self, message):
        if not WEBHOOK_URL:
            return
        try:
            import requests
            requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Titan Swing Runner")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Paper mode (default)")
    parser.add_argument("--live", action="store_true", help="Live IBKR execution")
    parser.add_argument("--check", action="store_true", help="Check positions only")
    parser.add_argument("--close", help="Close a specific symbol")
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--equity", type=float, default=10000)
    args = parser.parse_args()

    dry_run = not args.live

    config = {
        "max_positions": args.max_positions,
        "model_equity": args.equity,
        "long_only": True,
    }

    runner = TitanRunner(config=config, dry_run=dry_run)

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"{'=' * 60}")
    print(f"TITAN Runner [{mode}] — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 60}")

    if args.check:
        runner.status()
        return

    if args.close:
        if args.close in runner.positions:
            # Force close
            pos = runner.positions[args.close]
            daily = get_data(args.close, "daily")
            if daily is not None:
                exit_px = daily["Close"].iloc[-1]
                entry_date = datetime.fromisoformat(pos["entry_date"])
                days = (datetime.now(timezone.utc) - entry_date).days
                if pos["direction"] == "LONG":
                    pnl_pct = (exit_px - pos["entry_price"]) / pos["entry_price"] * 100
                else:
                    pnl_pct = (pos["entry_price"] - exit_px) / pos["entry_price"] * 100
                pnl_usd = pnl_pct / 100 * pos["entry_price"] * pos["shares"]
                runner._log_trade(args.close, pos, exit_px, "manual_close", pnl_pct, pnl_usd, days)
                runner.overlay.learn(pnl_pct)
                del runner.positions[args.close]
                runner._save_positions()
                print(f"Closed {args.close}: PnL {pnl_pct:+.2f}%")
        else:
            print(f"{args.close} not in positions")
        return

    # Normal flow: check exits first, then evaluate new entries
    print("\n[1] Checking exits on open positions...")
    runner.check_exits()

    print("\n[2] Evaluating new entries from scanner...")
    runner.evaluate_entries()

    print("\n[3] Current portfolio:")
    runner.status()

    # Save AI overlay state
    print(f"\nAI Overlay: {runner.overlay.get_weight_summary()}")


if __name__ == "__main__":
    main()
