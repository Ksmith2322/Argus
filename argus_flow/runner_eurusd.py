"""EUR/USD Paper Trading Runner via IBKR TWS Gateway.

Runs the T4 Full Stack strategy live on paper:
- Trigger: range_pct >= 0.0012 AND range_accel > 0 AND vol_z > 0 AND session 08-19 UTC
- Direction: long near session low, short near session high
- Stop: 20 pips, Target: 40 pips, Timeout: 60 minutes

Usage:
    python -m argus_flow.runner_eurusd
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

try:
    from ib_insync import IB, Forex, MarketOrder, LimitOrder, util
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("eurusd_runner")

# ── Config ──────────────────────────────────────────────────
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "10"))

# Strategy params (T4 full stack, best config)
RANGE_PCT_MIN = 0.0012
RANGE_ACCEL_MIN = 0.0
VOL_Z_MIN = 0.0
SESSION_START_UTC = 8
SESSION_END_UTC = 19
STOP_PIPS = 20
TARGET_PIPS = 40
TIMEOUT_MINUTES = 60
DIST_LONG_THRESHOLD = 0.4
DIST_SHORT_THRESHOLD = 0.6
MIN_SIGNAL_GAP_MINUTES = 15
LOT_SIZE = 20000  # micro lot = 1000, mini = 10000, this = 2 mini lots

# Artifact paths
LOG_DIR = Path("argus_flow/logs/eurusd")
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRADE_LOG = LOG_DIR / "trades.csv"
SIGNAL_LOG = LOG_DIR / "signals.csv"
STATE_FILE = LOG_DIR / "state.json"


class BarBuffer:
    """Rolling buffer of 1-minute bars for feature computation."""

    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.bars: list[dict] = []

    def add(self, bar: dict) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            self.bars = self.bars[-self.maxlen:]

    def to_df(self) -> pd.DataFrame:
        if not self.bars:
            return pd.DataFrame()
        return pd.DataFrame(self.bars)

    def __len__(self) -> int:
        return len(self.bars)


class StrategyState:
    """Tracks current position and trade lifecycle."""

    def __init__(self):
        self.position = "FLAT"  # FLAT, LONG, SHORT
        self.entry_price = 0.0
        self.entry_time = None
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time = None
        self.last_signal_time = None
        self.trade_count = 0
        self.pnl_pips = 0.0

    def save(self):
        STATE_FILE.write_text(json.dumps({
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "trade_count": self.trade_count,
            "pnl_pips": self.pnl_pips,
        }, indent=2))

    def load(self):
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text())
            self.position = d.get("position", "FLAT")
            self.entry_price = d.get("entry_price", 0.0)
            self.trade_count = d.get("trade_count", 0)
            self.pnl_pips = d.get("pnl_pips", 0.0)
            log.info(f"Restored state: {self.position}, trades={self.trade_count}, pnl={self.pnl_pips:.1f}pip")


def compute_features(buf: BarBuffer) -> dict | None:
    """Compute strategy features from bar buffer."""
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))

    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    # Range pct
    range_pct = (pre["high"].max() - pre["low"].min()) / pre["close"].iloc[-1]

    # Vol z
    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    # Range accel
    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

    # Session position
    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    current_px = pre["close"].iloc[-1]
    dist_from_low = (current_px - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    hour = datetime.now(timezone.utc).hour

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "dist_from_low": dist_from_low,
        "hour": hour,
        "price": current_px,
    }


def check_trigger(features: dict) -> str | None:
    """Check if T4 trigger fires. Returns 'long', 'short', or None."""
    if features["range_pct"] < RANGE_PCT_MIN:
        return None
    if features["range_accel"] <= RANGE_ACCEL_MIN:
        return None
    if features["vol_z"] <= VOL_Z_MIN:
        return None
    if not (SESSION_START_UTC <= features["hour"] <= SESSION_END_UTC):
        return None

    if features["dist_from_low"] < DIST_LONG_THRESHOLD:
        return "long"
    elif features["dist_from_low"] > DIST_SHORT_THRESHOLD:
        return "short"
    else:
        return "long"  # default


def log_signal(features: dict, direction: str | None, action: str):
    """Append signal to CSV log."""
    if not SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "price", "range_pct", "vol_z", "range_accel",
                        "dist_from_low", "hour", "direction", "action"])
    with open(SIGNAL_LOG, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            datetime.now(timezone.utc).isoformat(),
            features["price"], f"{features['range_pct']:.6f}",
            f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}",
            f"{features['dist_from_low']:.4f}", features["hour"],
            direction or "", action,
        ])


def log_trade(state: StrategyState, exit_price: float, exit_reason: str):
    """Append closed trade to CSV log."""
    if not TRADE_LOG.exists():
        with open(TRADE_LOG, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "direction", "entry_px", "exit_px", "pnl_pips",
                        "exit_reason", "duration_min", "trade_num"])

    if state.position == "LONG":
        pnl_pips = (exit_price - state.entry_price) * 10000
    else:
        pnl_pips = (state.entry_price - exit_price) * 10000

    duration = 0
    if state.entry_time:
        duration = (datetime.now(timezone.utc) - state.entry_time).total_seconds() / 60

    with open(TRADE_LOG, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            datetime.now(timezone.utc).isoformat(),
            state.position.lower(), f"{state.entry_price:.5f}",
            f"{exit_price:.5f}", f"{pnl_pips:.2f}",
            exit_reason, f"{duration:.1f}", state.trade_count,
        ])

    return pnl_pips


def main():
    log.info("="*60)
    log.info("EUR/USD Paper Runner — T4 Full Stack Strategy")
    log.info(f"SL={STOP_PIPS}pip TP={TARGET_PIPS}pip Timeout={TIMEOUT_MINUTES}min")
    log.info(f"Lot size: {LOT_SIZE} units")
    log.info(f"Gateway: {IBKR_HOST}:{IBKR_PORT}")
    log.info("="*60)

    # Connect
    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        sys.exit(1)

    accounts = ib.managedAccounts()
    log.info(f"Connected. Account: {accounts}")

    contract = Forex("EURUSD")
    ib.qualifyContracts(contract)
    log.info(f"Contract: {contract}")

    # Subscribe to market data
    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)

    # State
    state = StrategyState()
    state.load()
    buf = BarBuffer(maxlen=300)

    # Seed with recent history
    log.info("Loading recent bars...")
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="5 D",
        barSizeSetting="1 min", whatToShow="MIDPOINT", useRTH=False,
    )
    for b in bars:
        buf.add({"ts": str(b.date), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume})
    log.info(f"Seeded {len(buf)} bars")

    # Track current bar
    current_bar_minute = None
    current_bar = {"open": 0, "high": 0, "low": 999, "close": 0, "volume": 0}

    log.info("Starting main loop (Ctrl+C to stop)...")

    try:
        while True:
            ib.sleep(1)  # process events

            # Get current price
            mid = None
            if ticker.bid > 0 and ticker.ask > 0:
                mid = (ticker.bid + ticker.ask) / 2
            elif ticker.last > 0:
                mid = ticker.last

            if mid is None:
                continue

            now = datetime.now(timezone.utc)
            bar_minute = now.replace(second=0, microsecond=0)

            # Build 1-minute bars
            if current_bar_minute is None:
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": 0}
            elif bar_minute > current_bar_minute:
                # Close current bar
                current_bar["ts"] = str(current_bar_minute)
                buf.add(current_bar)

                # Start new bar
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": 0}
            else:
                # Update current bar
                current_bar["high"] = max(current_bar["high"], mid)
                current_bar["low"] = min(current_bar["low"], mid)
                current_bar["close"] = mid

            # ── Position management (check every tick) ──
            if state.position != "FLAT":
                exit_reason = None
                exit_price = mid

                if state.position == "LONG":
                    if mid <= state.stop_price:
                        exit_reason = "stop"
                    elif mid >= state.target_price:
                        exit_reason = "target"
                elif state.position == "SHORT":
                    if mid >= state.stop_price:
                        exit_reason = "stop"
                    elif mid <= state.target_price:
                        exit_reason = "target"

                # Timeout check
                if state.timeout_time and now >= state.timeout_time:
                    exit_reason = "timeout"

                if exit_reason:
                    pnl = log_trade(state, exit_price, exit_reason)
                    state.pnl_pips += pnl
                    log.info(
                        f"EXIT {state.position} @ {exit_price:.5f} "
                        f"reason={exit_reason} pnl={pnl:+.1f}pip "
                        f"total={state.pnl_pips:+.1f}pip trades={state.trade_count}"
                    )
                    state.position = "FLAT"
                    state.save()
                continue  # don't evaluate new signals while in position

            # ── Signal evaluation (only on new bars, when FLAT) ──
            if bar_minute <= current_bar_minute and len(buf) >= 60:
                # Only evaluate once per minute
                features = compute_features(buf)
                if features is None:
                    continue

                direction = check_trigger(features)

                # Min gap between signals
                if direction and state.last_signal_time:
                    gap = (now - state.last_signal_time).total_seconds() / 60
                    if gap < MIN_SIGNAL_GAP_MINUTES:
                        direction = None

                if direction:
                    pip_size = 0.0001
                    entry_px = mid

                    if direction == "long":
                        stop_px = entry_px - STOP_PIPS * pip_size
                        target_px = entry_px + TARGET_PIPS * pip_size
                    else:
                        stop_px = entry_px + STOP_PIPS * pip_size
                        target_px = entry_px - TARGET_PIPS * pip_size

                    state.position = direction.upper()
                    state.entry_price = entry_px
                    state.entry_time = now
                    state.stop_price = stop_px
                    state.target_price = target_px
                    state.timeout_time = now.replace(second=0) + pd.Timedelta(minutes=TIMEOUT_MINUTES)
                    state.last_signal_time = now
                    state.trade_count += 1
                    state.save()

                    log_signal(features, direction, "ENTRY")

                    log.info(
                        f"ENTRY {direction.upper()} @ {entry_px:.5f} "
                        f"stop={stop_px:.5f} target={target_px:.5f} "
                        f"timeout={TIMEOUT_MINUTES}min "
                        f"features: rng={features['range_pct']:.4f} "
                        f"accel={features['range_accel']:.3f} "
                        f"vol_z={features['vol_z']:.3f} "
                        f"dist={features['dist_from_low']:.3f}"
                    )
                else:
                    # Log missed signal periodically (every 5 min)
                    if now.minute % 5 == 0 and now.second < 2:
                        log_signal(features, None, "NO_TRIGGER")

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        state.save()
        ib.disconnect()
        log.info(f"Final state: trades={state.trade_count} pnl={state.pnl_pips:+.1f}pip")


if __name__ == "__main__":
    main()