"""Generic Futures Paper Trading Runner via IBKR TWS Gateway.

Runs the vol_burst strategy on any futures contract.

Usage:
    python -m argus_flow.runner_futures_generic --symbol MES --exchange CME --expiry 20260618 --config argus_flow/configs/mes_vol_burst_paper_v1.json --client-id 16
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
    from ib_insync import IB, Future
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
log = logging.getLogger("futures_runner")

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
        self.pnl_points = 0.0

    def save(self):
        self.file.write_text(json.dumps({
            "position": self.position, "entry_price": self.entry_price,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "trade_count": self.trade_count, "pnl_points": self.pnl_points,
        }, indent=2))

    def load(self):
        if self.file.exists():
            d = json.loads(self.file.read_text())
            self.position = d.get("position", "FLAT")
            self.entry_price = d.get("entry_price", 0.0)
            self.stop_price = d.get("stop_price", 0.0)
            self.target_price = d.get("target_price", 0.0)
            self.trade_count = d.get("trade_count", 0)
            self.pnl_points = d.get("pnl_points", 0.0)
            if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
                log.warning(f"Restored {self.position} but stop/target missing — forcing FLAT")
                self.position = "FLAT"
            log.info(f"Restored: {self.position}, trades={self.trade_count}, pnl={self.pnl_points:.1f}pts")


def compute_features(buf):
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

    vol_burst_z = 0.0
    if "volume" in df.columns and df["volume"].sum() > 0:
        vol_5m = df["volume"].iloc[-5:].sum()
        vol_hist = df["volume"].rolling(5).sum().iloc[-ctx_win:-5]
        if len(vol_hist) > 10 and vol_hist.mean() > 0:
            vol_burst_z = (vol_5m - vol_hist.mean()) / vol_hist.mean()

    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    dist_from_low = (pre["close"].iloc[-1] - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    return {
        "range_pct": range_pct, "vol_z": vol_z, "range_accel": range_accel,
        "vol_burst_z": vol_burst_z, "dist_from_low": dist_from_low,
        "hour": datetime.now(timezone.utc).hour, "price": float(pre["close"].iloc[-1]),
    }


def check_trigger(features, cfg):
    trigger = cfg.get("trigger", {})
    if features.get("vol_burst_z", 0) <= trigger.get("vol_burst_z_min", 1.0):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    h = features["hour"]
    if not (trigger.get("session_start_utc", 13) <= h <= trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    if dist < cfg.get("direction", {}).get("dist_long_threshold", 0.4):
        return "long"
    elif dist > cfg.get("direction", {}).get("dist_short_threshold", 0.6):
        return "short"
    return "long"


def main(symbol, exchange, expiry, config_path, client_id):
    cfg = json.loads(Path(config_path).read_text())
    risk = cfg.get("risk", {})
    stop_bps = risk.get("stop_bps", 30)
    target_bps = risk.get("target_bps", 60)
    timeout_min = risk.get("timeout_minutes", 60)
    min_gap = risk.get("min_signal_gap_minutes", 15)

    log_dir = Path(f"argus_flow/logs/{symbol.lower()}")
    log_dir.mkdir(parents=True, exist_ok=True)
    trade_log = log_dir / "trades.csv"
    signal_log = log_dir / "signals.csv"

    log.info("=" * 60)
    log.info(f"{symbol} Paper Runner — {cfg.get('strategy', '?')}")
    log.info(f"SL={stop_bps}bps TP={target_bps}bps Timeout={timeout_min}min")
    log.info("=" * 60)

    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=10)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return True

    ib.reqMarketDataType(3)  # delayed if live not available

    contract = Future(symbol=symbol, exchange=exchange, lastTradeDateOrContractMonth=expiry)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        log.error(f"Could not qualify {symbol}")
        ib.disconnect()
        return True
    contract = qualified[0]
    log.info(f"Contract: {contract}")

    ticker = ib.reqMktData(contract)

    state = State(log_dir / "state.json")
    state.load()
    buf = BarBuffer(maxlen=300)

    log.info("Loading recent bars...")
    bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="5 D",
                                 barSizeSetting="1 min", whatToShow="TRADES", useRTH=False)
    for b in bars:
        buf.add({"ts": str(b.date), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume})
    log.info(f"Seeded {len(buf)} bars")

    current_bar_minute = None
    current_bar = {"open": 0, "high": 0, "low": -1, "close": 0, "volume": 0}

    log.info("Starting main loop...")

    try:
        while True:
            ib.sleep(1)
            mid = None
            last = getattr(ticker, 'last', None) or getattr(ticker, 'delayedLast', None)
            bid = getattr(ticker, 'bid', None) or getattr(ticker, 'delayedBid', None)
            ask = getattr(ticker, 'ask', None) or getattr(ticker, 'delayedAsk', None)
            if last and last > 0: mid = last
            elif bid and bid > 0 and ask and ask > 0: mid = (bid + ask) / 2
            if mid is None or mid <= 0:
                continue

            vol = getattr(ticker, 'volume', None) or getattr(ticker, 'delayedVolume', None) or 0
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
                        pnl_pts = mid - state.entry_price
                    else:
                        pnl_pts = state.entry_price - mid
                    state.pnl_points += pnl_pts
                    dur = (now - state.entry_time).total_seconds() / 60 if state.entry_time else 0

                    mult = float(contract.multiplier) if contract.multiplier else 1
                    pnl_usd = pnl_pts * mult

                    if not trade_log.exists():
                        with open(trade_log, "w", newline="") as f:
                            csv.writer(f).writerow(["ts","direction","entry_px","exit_px","pnl_pts","pnl_usd","exit_reason","duration_min","trade_num"])
                    with open(trade_log, "a", newline="") as f:
                        csv.writer(f).writerow([now.isoformat(), state.position.lower(), f"{state.entry_price:.2f}", f"{mid:.2f}", f"{pnl_pts:.2f}", f"{pnl_usd:.2f}", exit_reason, f"{dur:.1f}", state.trade_count])

                    log.info(f"EXIT {state.position} @ {mid:.2f} reason={exit_reason} pnl={pnl_pts:+.2f}pts (${pnl_usd:+.2f}) total={state.pnl_points:+.2f}pts trades={state.trade_count}")
                    state.position = "FLAT"
                    state.save()
                continue

            # Signal evaluation
            if len(buf) >= 60:
                features = compute_features(buf)
                if features is None: continue

                direction = check_trigger(features, cfg)
                if direction and state.last_signal_time:
                    gap = (now - state.last_signal_time).total_seconds() / 60
                    if gap < min_gap: direction = None

                if direction:
                    entry_px = mid
                    stop_dist = entry_px * (stop_bps / 10000)
                    target_dist = entry_px * (target_bps / 10000)
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
                    state.timeout_time = now.replace(second=0) + pd.Timedelta(minutes=timeout_min)
                    state.last_signal_time = now
                    state.trade_count += 1
                    state.save()

                    if not signal_log.exists():
                        with open(signal_log, "w", newline="") as f:
                            csv.writer(f).writerow(["ts","price","range_pct","vol_z","range_accel","vol_burst_z","dist_from_low","hour","direction","action"])
                    with open(signal_log, "a", newline="") as f:
                        csv.writer(f).writerow([now.isoformat(), features["price"], f"{features['range_pct']:.6f}", f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}", f"{features['vol_burst_z']:.4f}", f"{features['dist_from_low']:.4f}", features["hour"], direction, "ENTRY"])

                    log.info(f"ENTRY {direction.upper()} @ {entry_px:.2f} stop={stop_px:.2f} target={target_px:.2f} vol_burst={features['vol_burst_z']:.2f}")
                else:
                    if now.minute % 5 == 0 and now.second < 2:
                        if not signal_log.exists():
                            with open(signal_log, "w", newline="") as f:
                                csv.writer(f).writerow(["ts","price","range_pct","vol_z","range_accel","vol_burst_z","dist_from_low","hour","direction","action"])
                        with open(signal_log, "a", newline="") as f:
                            csv.writer(f).writerow([now.isoformat(), features["price"], f"{features['range_pct']:.6f}", f"{features['vol_z']:.4f}", f"{features['range_accel']:.4f}", f"{features['vol_burst_z']:.4f}", f"{features['dist_from_low']:.4f}", features["hour"], "", "NO_TRIGGER"])

    except KeyboardInterrupt:
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return False
    except (ConnectionError, OSError, asyncio.CancelledError) as e:
        log.warning(f"Connection lost: {e}")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return True
    except Exception as e:
        log.error(f"Error: {e}")
        state.save()
        try: ib.disconnect()
        except Exception: pass
        return True


def run():
    parser = argparse.ArgumentParser(description="Generic Futures Runner")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--expiry", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--client-id", type=int, required=True)
    args = parser.parse_args()

    max_retries = 100
    retry_delay = 10
    for attempt in range(max_retries):
        if attempt > 0:
            log.info(f"Reconnect {attempt}/{max_retries} in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 120)
        should_reconnect = main(args.symbol, args.exchange, args.expiry, args.config, args.client_id)
        if should_reconnect is False:
            break


if __name__ == "__main__":
    run()