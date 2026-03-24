"""Generic FX Paper Trading Runner via IBKR TWS Gateway.

Runs the range+accel or T4 strategy on any FX pair.

Usage:
    python -m argus_flow.runner_fx_generic --pair USDJPY --config argus_flow/configs/usdjpy_paper_v1.json
    python -m argus_flow.runner_fx_generic --pair AUDUSD --config argus_flow/configs/audusd_paper_v1.json
    python -m argus_flow.runner_fx_generic --pair GBPJPY --config argus_flow/configs/gbpjpy_paper_v1.json
"""
from __future__ import annotations

import argparse
import asyncio
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
    from ib_insync import IB, Forex
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("fx_runner")

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))


class BarBuffer:
    def __init__(self, maxlen=300):
        self.maxlen = maxlen
        self.bars = []

    def add(self, bar):
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            self.bars = self.bars[-self.maxlen:]

    def to_df(self):
        return pd.DataFrame(self.bars) if self.bars else pd.DataFrame()

    def __len__(self):
        return len(self.bars)


class State:
    def __init__(self, state_file):
        self.file = Path(state_file)
        self.file.parent.mkdir(parents=True, exist_ok=True)
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
        self.file.write_text(json.dumps({
            "position": self.position, "entry_price": self.entry_price,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "trade_count": self.trade_count, "pnl_pips": self.pnl_pips,
        }, indent=2))

    def load(self):
        if self.file.exists():
            d = json.loads(self.file.read_text())
            self.position = d.get("position", "FLAT")
            self.entry_price = d.get("entry_price", 0.0)
            self.stop_price = d.get("stop_price", 0.0)
            self.target_price = d.get("target_price", 0.0)
            self.trade_count = d.get("trade_count", 0)
            self.pnl_pips = d.get("pnl_pips", 0.0)
            if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
                log.warning(f"Restored {self.position} but stop/target missing — forcing FLAT")
                self.position = "FLAT"
            log.info(f"Restored: {self.position}, trades={self.trade_count}, pnl={self.pnl_pips:.1f}pip")


def compute_features(buf, pip_mult=10000):
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

    return {
        "range_pct": range_pct, "vol_z": vol_z, "range_accel": range_accel,
        "dist_from_low": dist_from_low, "hour": datetime.now(timezone.utc).hour,
        "price": current_px,
    }


def check_trigger(features, cfg):
    trigger = cfg.get("trigger", {})
    if features["range_pct"] < trigger.get("range_pct_min", 0.0012):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    if features.get("vol_z", 0) <= trigger.get("vol_z_min", -999):
        return None
    h = features["hour"]
    if not (trigger.get("session_start_utc", 0) <= h <= trigger.get("session_end_utc", 23)):
        return None

    dist = features["dist_from_low"]
    if dist < cfg.get("direction", {}).get("dist_long_threshold", 0.4):
        return "long"
    elif dist > cfg.get("direction", {}).get("dist_short_threshold", 0.6):
        return "short"
    return "long"


def main(pair, config_path, client_id):
    cfg = json.loads(Path(config_path).read_text())
    risk = cfg.get("risk", {})
    stop_pips = risk.get("stop_pips", 20)
    target_pips = risk.get("target_pips", 40)
    timeout_min = risk.get("timeout_minutes", 60)
    min_gap = risk.get("min_signal_gap_minutes", 15)
    lot_size = risk.get("lot_size", 20000)

    log_dir = Path(f"argus_flow/logs/{pair.lower()}")
    log_dir.mkdir(parents=True, exist_ok=True)
    trade_log = log_dir / "trades.csv"
    signal_log = log_dir / "signals.csv"

    log.info("=" * 60)
    log.info(f"{pair} Paper Runner — {cfg.get('strategy', '?')}")
    log.info(f"SL={stop_pips}pip TP={target_pips}pip Timeout={timeout_min}min")
    log.info("=" * 60)

    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=10)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return True

    log.info(f"Connected. Account: {ib.managedAccounts()}")

    contract = Forex(pair)
    ib.qualifyContracts(contract)
    log.info(f"Contract: {contract}")
    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)

    state = State(log_dir / "state.json")
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

    log.info("Starting main loop...")

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
                pip_size = 0.01 if "JPY" in pair else 0.0001

                if state.position == "LONG":
                    if mid <= state.stop_price: exit_reason = "stop"
                    elif mid >= state.target_price: exit_reason = "target"
                elif state.position == "SHORT":
                    if mid >= state.stop_price: exit_reason = "stop"
                    elif mid <= state.target_price: exit_reason = "target"

                if state.timeout_time and now >= state.timeout_time:
                    exit_reason = "timeout"

                if exit_reason:
                    if state.position == "LONG":
                        pnl_pips = (mid - state.entry_price) / pip_size
                    else:
                        pnl_pips = (state.entry_price - mid) / pip_size
                    state.pnl_pips += pnl_pips
                    dur = (now - state.entry_time).total_seconds() / 60 if state.entry_time else 0

                    if not trade_log.exists():
                        with open(trade_log, "w", newline="") as f:
                            csv.writer(f).writerow(["ts", "direction", "entry_px", "exit_px", "pnl_pips", "exit_reason", "duration_min", "trade_num"])
                    with open(trade_log, "a", newline="") as f:
                        csv.writer(f).writerow([now.isoformat(), state.position.lower(), f"{state.entry_price:.5f}", f"{mid:.5f}", f"{pnl_pips:.2f}", exit_reason, f"{dur:.1f}", state.trade_count])

                    log.info(f"EXIT {state.position} @ {mid:.5f} reason={exit_reason} pnl={pnl_pips:+.1f}pip total={state.pnl_pips:+.1f}pip trades={state.trade_count}")
                    state.position = "FLAT"
                    state.save()
                continue

            # Signal evaluation
            if len(buf) >= 60:
                features = compute_features(buf)
                if features is None:
                    continue

                direction = check_trigger(features, cfg)

                if direction and state.last_signal_time:
                    gap = (now - state.last_signal_time).total_seconds() / 60
                    if gap < min_gap:
                        direction = None

                if direction:
                    pip_size = 0.01 if "JPY" in pair else 0.0001
                    entry_px = mid
                    if direction == "long":
                        stop_px = entry_px - stop_pips * pip_size
                        target_px = entry_px + target_pips * pip_size
                    else:
                        stop_px = entry_px + stop_pips * pip_size
                        target_px = entry_px - target_pips * pip_size

                    state.position = direction.upper()
                    state.entry_price = entry_px
                    state.entry_time = now
                    state.stop_price = stop_px
                    state.target_price = target_px
                    state.timeout_time = now.replace(second=0) + pd.Timedelta(minutes=timeout_min)
                    state.last_signal_time = now
                    state.trade_count += 1
                    state.save()

                    if not signal_log.exists():
                        with open(signal_log, "w", newline="") as f:
                            csv.writer(f).writerow(["ts", "price", "range_pct", "vol_z", "range_accel", "dist_from_low", "hour", "direction", "action"])
                    with open(signal_log, "a", newline="") as f:
                        csv.writer(f).writerow([now.isoformat(), features["price"], f"{features['range_pct']:.6f}", f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}", f"{features['dist_from_low']:.4f}", features["hour"], direction, "ENTRY"])

                    log.info(f"ENTRY {direction.upper()} @ {entry_px:.5f} stop={stop_px:.5f} target={target_px:.5f}")
                else:
                    if now.minute % 5 == 0 and now.second < 2:
                        if not signal_log.exists():
                            with open(signal_log, "w", newline="") as f:
                                csv.writer(f).writerow(["ts", "price", "range_pct", "vol_z", "range_accel", "dist_from_low", "hour", "direction", "action"])
                        with open(signal_log, "a", newline="") as f:
                            csv.writer(f).writerow([now.isoformat(), features["price"], f"{features['range_pct']:.6f}", f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}", f"{features['dist_from_low']:.4f}", features["hour"], "", "NO_TRIGGER"])

    except KeyboardInterrupt:
        log.info("Shutting down...")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return False
    except (ConnectionError, OSError, asyncio.CancelledError) as e:
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


def run():
    parser = argparse.ArgumentParser(description="Generic FX Runner")
    parser.add_argument("--pair", required=True, help="FX pair (e.g., USDJPY)")
    parser.add_argument("--config", required=True, help="Config JSON path")
    parser.add_argument("--client-id", type=int, required=True, help="IBKR client ID")
    args = parser.parse_args()

    max_retries = 100
    retry_delay = 10
    for attempt in range(max_retries):
        if attempt > 0:
            log.info(f"Reconnect {attempt}/{max_retries} in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 120)
        should_reconnect = main(args.pair, args.config, args.client_id)
        if should_reconnect is False:
            break
    log.info("Runner stopped.")


if __name__ == "__main__":
    run()