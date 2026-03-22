"""MNQ (Nasdaq Micro) Paper Trading Runner via IBKR TWS Gateway.

Runs the vol_burst strategy live on paper:
- Trigger: vol_burst_z > 1.0 AND range_accel > 0 AND US session (13-20 UTC)
- Direction: long near session low, short near session high
- Stop: 30bps, Target: 60bps, Timeout: 60 minutes

Usage:
    python -m argus_flow.runner_mnq
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

try:
    from ib_insync import IB, Future, MarketOrder, util
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("mnq_runner")

# ── Config ──────────────────────────────────────────────────
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_MNQ_CLIENT_ID", "11"))

SYMBOL = "MNQ"
EXCHANGE = "CME"
EXPIRY = "20260618"

# Strategy params (vol_burst, best config)
VOL_BURST_Z_MIN = 1.0
RANGE_ACCEL_MIN = 0.0
SESSION_START_UTC = 13
SESSION_END_UTC = 20
STOP_BPS = 30
TARGET_BPS = 60
TIMEOUT_MINUTES = 60
DIST_LONG_THRESHOLD = 0.4
DIST_SHORT_THRESHOLD = 0.6
MIN_SIGNAL_GAP_MINUTES = 15
NUM_CONTRACTS = 1

# Artifact paths
LOG_DIR = Path("argus_flow/logs/mnq")
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRADE_LOG = LOG_DIR / "trades.csv"
SIGNAL_LOG = LOG_DIR / "signals.csv"
STATE_FILE = LOG_DIR / "state.json"


class BarBuffer:
    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.bars: list[dict] = []

    def add(self, bar: dict) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            self.bars = self.bars[-self.maxlen:]

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.bars) if self.bars else pd.DataFrame()

    def __len__(self) -> int:
        return len(self.bars)


class StrategyState:
    def __init__(self):
        self.position = "FLAT"
        self.entry_price = 0.0
        self.entry_time = None
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time = None
        self.last_signal_time = None
        self.trade_count = 0
        self.pnl_points = 0.0

    def save(self):
        STATE_FILE.write_text(json.dumps({
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "trade_count": self.trade_count,
            "pnl_points": self.pnl_points,
        }, indent=2))

    def load(self):
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text())
            self.position = d.get("position", "FLAT")
            self.entry_price = d.get("entry_price", 0.0)
            self.trade_count = d.get("trade_count", 0)
            self.pnl_points = d.get("pnl_points", 0.0)
            log.info(f"Restored state: {self.position}, trades={self.trade_count}, pnl={self.pnl_points:.1f}pts")


def compute_features(buf: BarBuffer) -> dict | None:
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

    # Volume burst z-score
    vol_burst_z = 0.0
    if "volume" in df.columns and df["volume"].sum() > 0:
        vol_5m = df["volume"].iloc[-5:].sum()
        vol_5m_hist = df["volume"].rolling(5).sum().iloc[-ctx_win:-5]
        if len(vol_5m_hist) > 10 and vol_5m_hist.mean() > 0:
            vol_burst_z = (vol_5m - vol_5m_hist.mean()) / vol_5m_hist.mean()

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
        "vol_burst_z": vol_burst_z,
        "dist_from_low": dist_from_low,
        "hour": hour,
        "price": current_px,
    }


def check_trigger(features: dict) -> str | None:
    if features["vol_burst_z"] <= VOL_BURST_Z_MIN:
        return None
    if features["range_accel"] <= RANGE_ACCEL_MIN:
        return None
    if not (SESSION_START_UTC <= features["hour"] <= SESSION_END_UTC):
        return None

    if features["dist_from_low"] < DIST_LONG_THRESHOLD:
        return "long"
    elif features["dist_from_low"] > DIST_SHORT_THRESHOLD:
        return "short"
    else:
        return "long"


def log_signal(features: dict, direction: str | None, action: str):
    if not SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, "w", newline="") as f:
            csv.writer(f).writerow(["ts", "price", "range_pct", "vol_z", "range_accel",
                                     "vol_burst_z", "dist_from_low", "hour", "direction", "action"])
    with open(SIGNAL_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            features["price"], f"{features['range_pct']:.6f}",
            f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}",
            f"{features['vol_burst_z']:.4f}", f"{features['dist_from_low']:.4f}",
            features["hour"], direction or "", action,
        ])


def log_trade(state: StrategyState, exit_price: float, exit_reason: str):
    if not TRADE_LOG.exists():
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow(["ts", "direction", "entry_px", "exit_px", "pnl_pts",
                                     "pnl_usd", "exit_reason", "duration_min", "trade_num"])

    if state.position == "LONG":
        pnl_pts = exit_price - state.entry_price
    else:
        pnl_pts = state.entry_price - exit_price

    pnl_usd = pnl_pts * 2.0 * NUM_CONTRACTS  # MNQ = $2/point

    duration = 0
    if state.entry_time:
        duration = (datetime.now(timezone.utc) - state.entry_time).total_seconds() / 60

    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            state.position.lower(), f"{state.entry_price:.2f}",
            f"{exit_price:.2f}", f"{pnl_pts:.2f}",
            f"{pnl_usd:.2f}", exit_reason, f"{duration:.1f}", state.trade_count,
        ])

    return pnl_pts


def main():
    log.info("=" * 60)
    log.info(f"MNQ Paper Runner - Vol Burst Strategy")
    log.info(f"SL={STOP_BPS}bps TP={TARGET_BPS}bps Timeout={TIMEOUT_MINUTES}min")
    log.info(f"Contracts: {NUM_CONTRACTS} ({SYMBOL})")
    log.info("=" * 60)

    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        sys.exit(1)

    log.info(f"Connected. Account: {ib.managedAccounts()}")

    contract = Future(symbol=SYMBOL, exchange=EXCHANGE, lastTradeDateOrContractMonth=EXPIRY)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        log.error(f"Could not qualify {SYMBOL} contract")
        sys.exit(1)
    contract = qualified[0]
    log.info(f"Contract: {contract}")

    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)

    state = StrategyState()
    state.load()
    buf = BarBuffer(maxlen=300)

    log.info("Loading recent bars...")
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="5 D",
        barSizeSetting="1 min", whatToShow="TRADES", useRTH=False,
    )
    for b in bars:
        buf.add({"ts": str(b.date), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume})
    log.info(f"Seeded {len(buf)} bars")

    current_bar_minute = None
    current_bar = {"open": 0, "high": 0, "low": -1, "close": 0, "volume": 0}

    log.info("Starting main loop (Ctrl+C to stop)...")

    try:
        while True:
            ib.sleep(1)

            mid = None
            if ticker.last and ticker.last > 0:
                mid = ticker.last
            elif ticker.bid and ticker.bid > 0 and ticker.ask and ticker.ask > 0:
                mid = (ticker.bid + ticker.ask) / 2

            if mid is None or mid <= 0:
                continue

            vol = ticker.volume if ticker.volume and ticker.volume > 0 else 0

            now = datetime.now(timezone.utc)
            bar_minute = now.replace(second=0, microsecond=0)

            if current_bar_minute is None:
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": vol}
            elif bar_minute > current_bar_minute:
                current_bar["ts"] = str(current_bar_minute)
                buf.add(current_bar)
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": vol}
            else:
                current_bar["high"] = max(current_bar["high"], mid)
                current_bar["low"] = min(current_bar["low"], mid) if current_bar["low"] > 0 else mid
                current_bar["close"] = mid
                current_bar["volume"] = vol

            # Position management
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

                if state.timeout_time and now >= state.timeout_time:
                    exit_reason = "timeout"

                if exit_reason:
                    pnl = log_trade(state, exit_price, exit_reason)
                    state.pnl_points += pnl
                    pnl_usd = pnl * 2.0 * NUM_CONTRACTS
                    log.info(
                        f"EXIT {state.position} @ {exit_price:.2f} "
                        f"reason={exit_reason} pnl={pnl:+.2f}pts (${pnl_usd:+.2f}) "
                        f"total={state.pnl_points:+.2f}pts trades={state.trade_count}"
                    )
                    state.position = "FLAT"
                    state.save()
                continue

            # Signal evaluation
            if len(buf) >= 60:
                features = compute_features(buf)
                if features is None:
                    continue

                direction = check_trigger(features)

                if direction and state.last_signal_time:
                    gap = (now - state.last_signal_time).total_seconds() / 60
                    if gap < MIN_SIGNAL_GAP_MINUTES:
                        direction = None

                if direction:
                    entry_px = mid
                    stop_dist = entry_px * (STOP_BPS / 10000)
                    target_dist = entry_px * (TARGET_BPS / 10000)

                    if direction == "long":
                        stop_px = entry_px - stop_dist
                        target_px = entry_px + target_dist
                    else:
                        stop_px = entry_px + stop_dist
                        target_px = entry_px - target_dist

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
                        f"ENTRY {direction.upper()} @ {entry_px:.2f} "
                        f"stop={stop_px:.2f} target={target_px:.2f} "
                        f"vol_burst={features['vol_burst_z']:.2f} "
                        f"range_accel={features['range_accel']:.3f} "
                        f"dist={features['dist_from_low']:.3f}"
                    )
                else:
                    if now.minute % 5 == 0 and now.second < 2:
                        log_signal(features, None, "NO_TRIGGER")

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        state.save()
        ib.disconnect()
        log.info(f"Final: trades={state.trade_count} pnl={state.pnl_points:+.2f}pts")


if __name__ == "__main__":
    main()