"""GBP/USD Paper Trading Runner via IBKR TWS Gateway.

Runs the range+accel strategy live on paper:
- Trigger: range_pct >= 0.0012 AND range_accel > 0 AND London/NY session (8-20 UTC)
- Direction: long near session low, short near session high
- Stop: 30bps, Target: 60bps, Timeout: 60 minutes
- Excludes Asia session (negative expectancy)

Usage:
    python -m argus_flow.runner_gbpusd
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
    from ib_insync import IB, Forex, MarketOrder, util
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("gbpusd_runner")

# ── Config ──────────────────────────────────────────────────
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_GBPUSD_CLIENT_ID", "12"))

# Strategy params (range+accel, best config, exclude Asia)
RANGE_PCT_MIN = 0.0012
RANGE_ACCEL_MIN = 0.0
SESSION_START_UTC = 8   # Skip Asia (0-7)
SESSION_END_UTC = 20
STOP_PIPS = 30
TARGET_PIPS = 60
TIMEOUT_MINUTES = 60
DIST_LONG_THRESHOLD = 0.4
DIST_SHORT_THRESHOLD = 0.6
MIN_SIGNAL_GAP_MINUTES = 15
LOT_SIZE = 20000  # 2 micro lots

# Artifact paths
LOG_DIR = Path("argus_flow/logs/gbpusd")
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
            self.stop_price = d.get("stop_price", 0.0)
            self.target_price = d.get("target_price", 0.0)
            self.trade_count = d.get("trade_count", 0)
            self.pnl_pips = d.get("pnl_pips", 0.0)
            if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
                log.warning(f"Restored {self.position} but stop/target missing — forcing FLAT")
                self.position = "FLAT"
            log.info(f"Restored state: {self.position}, trades={self.trade_count}, pnl={self.pnl_pips:.1f}pip")


def compute_features(buf: BarBuffer) -> dict | None:
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))
    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    range_pct = (pre["high"].max() - pre["low"].min()) / pre["close"].iloc[-1]

    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

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
    if features["range_pct"] < RANGE_PCT_MIN:
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
                                     "dist_from_low", "hour", "direction", "action"])
    with open(SIGNAL_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            features["price"], f"{features['range_pct']:.6f}",
            f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}",
            f"{features['dist_from_low']:.4f}", features["hour"],
            direction or "", action,
        ])


def log_trade(state: StrategyState, exit_price: float, exit_reason: str):
    if not TRADE_LOG.exists():
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow(["ts", "direction", "entry_px", "exit_px", "pnl_pips",
                                     "exit_reason", "duration_min", "trade_num"])

    if state.position == "LONG":
        pnl_pips = (exit_price - state.entry_price) * 10000
    else:
        pnl_pips = (state.entry_price - exit_price) * 10000

    duration = 0
    if state.entry_time:
        duration = (datetime.now(timezone.utc) - state.entry_time).total_seconds() / 60

    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            state.position.lower(), f"{state.entry_price:.5f}",
            f"{exit_price:.5f}", f"{pnl_pips:.2f}",
            exit_reason, f"{duration:.1f}", state.trade_count,
        ])

    return pnl_pips


def main():
    log.info("=" * 60)
    log.info("GBP/USD Paper Runner - Range + Accel Strategy")
    log.info(f"SL={STOP_PIPS}pip TP={TARGET_PIPS}pip Timeout={TIMEOUT_MINUTES}min")
    log.info(f"Session: {SESSION_START_UTC}-{SESSION_END_UTC} UTC (skip Asia)")
    log.info("=" * 60)

    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        sys.exit(1)

    log.info(f"Connected. Account: {ib.managedAccounts()}")

    contract = Forex("GBPUSD")
    ib.qualifyContracts(contract)
    log.info(f"Contract: {contract}")

    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)

    state = StrategyState()
    state.load()
    buf = BarBuffer(maxlen=300)

    log.info("Loading recent bars...")
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="5 D",
        barSizeSetting="1 min", whatToShow="BID", useRTH=False,
    )
    for b in bars:
        buf.add({"ts": str(b.date), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume})
    log.info(f"Seeded {len(buf)} bars")

    current_bar_minute = None
    current_bar = {"open": 0, "high": 0, "low": 999, "close": 0, "volume": 0}

    log.info("Starting main loop (Ctrl+C to stop)...")

    try:
        while True:
            ib.sleep(1)

            mid = None
            if ticker.bid > 0 and ticker.ask > 0:
                mid = (ticker.bid + ticker.ask) / 2
            elif ticker.last > 0:
                mid = ticker.last

            if mid is None or mid <= 0:
                continue

            now = datetime.now(timezone.utc)
            bar_minute = now.replace(second=0, microsecond=0)

            if current_bar_minute is None:
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": 0}
            elif bar_minute > current_bar_minute:
                current_bar["ts"] = str(current_bar_minute)
                buf.add(current_bar)
                current_bar_minute = bar_minute
                current_bar = {"open": mid, "high": mid, "low": mid, "close": mid, "volume": 0}
            else:
                current_bar["high"] = max(current_bar["high"], mid)
                current_bar["low"] = min(current_bar["low"], mid)
                current_bar["close"] = mid

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
                    state.pnl_pips += pnl
                    log.info(
                        f"EXIT {state.position} @ {exit_price:.5f} "
                        f"reason={exit_reason} pnl={pnl:+.1f}pip "
                        f"total={state.pnl_pips:+.1f}pip trades={state.trade_count}"
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
                        f"range={features['range_pct']:.4f} "
                        f"accel={features['range_accel']:.3f} "
                        f"dist={features['dist_from_low']:.3f}"
                    )
                else:
                    if now.minute % 5 == 0 and now.second < 2:
                        log_signal(features, None, "NO_TRIGGER")

    except KeyboardInterrupt:
        log.info("Shutting down...")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return False
    except (ConnectionError, OSError) as e:
        log.warning(f"Connection lost: {e}. Will reconnect...")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return True
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return True


def run_with_reconnect():
    max_retries = 100
    retry_delay = 10
    for attempt in range(max_retries):
        if attempt > 0:
            log.info(f"Reconnect attempt {attempt}/{max_retries} in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 120)
        should_reconnect = main()
        if should_reconnect is False:
            break
        log.info("Runner exited. Preparing to reconnect...")
    log.info("Runner stopped.")


if __name__ == "__main__":
    run_with_reconnect()