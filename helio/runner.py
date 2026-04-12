"""Helio Swing Trading Runner — daily/4hr trend-following on ETFs via IBKR.

Evaluates once per bar close (daily or 4hr). Holds positions 1-15 days.
Uses EMA trend + range expansion + volume surge for entries.
ATR-scaled stops with trailing logic for exits.

Usage:
    python -m helio.runner
    python -m helio.runner --configs helio/configs/gld_swing_v1.json
    python -m helio.runner --paper  (default)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

try:
    from helio.portfolio_guard import check_new_entry as _pg_check_new_entry
except ImportError:
    _pg_check_new_entry = None

load_dotenv()

REPO = Path(__file__).resolve().parents[1]
HELIO_ROOT = Path(__file__).resolve().parent
LOGS_ROOT = HELIO_ROOT / "logs"
CONFIGS_DIR = HELIO_ROOT / "configs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] helio | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("helio")


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════

class SwingState:
    """Per-instrument position state with atomic persistence."""

    def __init__(self, path: Path):
        self.path = path
        self.position = "FLAT"  # FLAT | LONG | SHORT
        self.entry_price = 0.0
        self.entry_date = ""
        self.stop_price = 0.0
        self.initial_stop = 0.0
        self.trail_stop = 0.0
        self.target_price = 0.0
        self.bars_held = 0
        self.highest = 0.0
        self.lowest = 999999.0
        self.trade_count = 0
        self.pnl_total = 0.0
        self.entry_type = ""

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2, default=str))
        tmp.replace(self.path)

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            for k, v in data.items():
                if k != "path" and hasattr(self, k):
                    setattr(self, k, v)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# Indicators
# ═══════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Compute EMA, ATR, range expansion, volume surge."""
    tf = cfg.get("timeframe", {})
    entry = cfg.get("entry", {})
    risk = cfg.get("risk", {})

    ema_period = tf.get("ema_period", 50)
    atr_period = risk.get("atr_period", 14)

    df = df.copy()
    df["ema"] = df["Close"].ewm(span=ema_period, adjust=False).mean()
    df["ema_slope"] = df["ema"].diff(tf.get("ema_slope_lookback", 3)) / df["ema"].shift(tf.get("ema_slope_lookback", 3))

    # ATR
    df["tr"] = np.maximum(
        df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))),
    )
    df["atr"] = df["tr"].rolling(atr_period).mean()

    # Range expansion
    df["range_pct"] = (df["High"] - df["Low"]) / df["Close"]
    df["avg_range"] = df["range_pct"].rolling(20).mean()
    df["range_expanded"] = df["range_pct"] > df["avg_range"] * entry.get("range_expansion_mult", 1.2)

    # Volume surge
    df["avg_vol"] = df["Volume"].rolling(20).mean()
    df["vol_surge"] = df["Volume"] > df["avg_vol"] * entry.get("volume_surge_mult", 1.2)

    # Pullback to EMA
    df["near_ema"] = abs(df["Close"] - df["ema"]) < df["atr"] * entry.get("ema_pullback_atr_mult", 0.5)

    return df


# ═══════════════════════════════════════════════════════════════
# Strategy Logic
# ═══════════════════════════════════════════════════════════════

def check_entry(row, cfg: dict) -> Optional[str]:
    """Check if current bar triggers a swing entry. Returns 'LONG', 'SHORT', or None."""
    tf = cfg.get("timeframe", {})
    entry_cfg = cfg.get("entry", {})

    slope_thresh = tf.get("ema_slope_threshold", 0.001)
    pullback_slope = entry_cfg.get("ema_slope_pullback_threshold", 0.002)

    trend_up = row["Close"] > row["ema"] and row["ema_slope"] > slope_thresh
    trend_down = row["Close"] < row["ema"] and row["ema_slope"] < -slope_thresh

    # Entry type A: range expansion + volume in trend direction
    entry_a = row["range_expanded"] and row["vol_surge"]
    # Entry type B: pullback to EMA in established trend
    entry_b = row["near_ema"] and abs(row["ema_slope"]) > pullback_slope

    if trend_up and (entry_a or entry_b):
        return "LONG"
    if trend_down and (entry_a or entry_b):
        return "SHORT"
    return None


def manage_position(state: SwingState, row, cfg: dict) -> Optional[str]:
    """Manage open position. Returns exit_reason or None."""
    risk = cfg.get("risk", {})
    atr_trail_mult = risk.get("atr_trail_mult", 1.0)
    max_hold = risk.get("max_hold_days", 7)

    state.bars_held += 1
    atr = row["atr"]

    if state.position == "LONG":
        state.highest = max(state.highest, row["High"])
        risk_dist = state.entry_price - state.initial_stop

        # Breakeven after 1R
        if risk_dist > 0 and state.highest - state.entry_price >= risk_dist:
            state.trail_stop = max(state.trail_stop, state.entry_price)
        # Trail after 2R
        if risk_dist > 0 and state.highest - state.entry_price >= risk_dist * 2:
            new_trail = state.highest - atr * atr_trail_mult
            state.trail_stop = max(state.trail_stop, new_trail)

        state.stop_price = max(state.stop_price, state.trail_stop)

        if row["Low"] <= state.stop_price:
            return "stop" if state.stop_price == state.initial_stop else "trail"
        if state.bars_held >= max_hold:
            return "timeout"

    elif state.position == "SHORT":
        state.lowest = min(state.lowest, row["Low"])
        risk_dist = state.initial_stop - state.entry_price

        if risk_dist > 0 and state.entry_price - state.lowest >= risk_dist:
            state.trail_stop = min(state.trail_stop, state.entry_price)
        if risk_dist > 0 and state.entry_price - state.lowest >= risk_dist * 2:
            new_trail = state.lowest + atr * atr_trail_mult
            state.trail_stop = min(state.trail_stop, new_trail)

        state.stop_price = min(state.stop_price, state.trail_stop)

        if row["High"] >= state.stop_price:
            return "stop" if state.stop_price == state.initial_stop else "trail"
        if state.bars_held >= max_hold:
            return "timeout"

    return None


# ═══════════════════════════════════════════════════════════════
# Watcher Signal Logging (observe-only — no trades, no positions)
# ═══════════════════════════════════════════════════════════════

def _log_watcher_signal(log_dir: Path, symbol: str, direction: str, latest, date_str: str):
    """Log a watcher-mode signal to signals.csv without entering a position."""
    import csv
    sig_file = log_dir / "signals.csv"
    header = ["date", "symbol", "direction", "close", "atr", "ema", "stage"]
    row = {
        "date": date_str,
        "symbol": symbol,
        "direction": direction,
        "close": f"{float(latest['Close']):.2f}",
        "atr": f"{float(latest['atr']):.4f}",
        "ema": f"{float(latest['ema']):.2f}",
        "stage": "watcher",
    }
    write_header = not sig_file.exists()
    with open(sig_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ═══════════════════════════════════════════════════════════════
# Trade Logging
# ═══════════════════════════════════════════════════════════════

def log_trade(log_dir: Path, state: SwingState, exit_price: float, exit_reason: str, exit_date: str):
    """Append closed trade to trades.csv."""
    trade_file = log_dir / "trades.csv"
    header = ["entry_date", "exit_date", "direction", "entry_px", "exit_px",
              "pnl_pct", "pnl_usd", "bars_held", "exit_reason", "entry_type", "trade_num"]

    if state.position == "LONG":
        pnl_pct = (exit_price - state.entry_price) / state.entry_price * 100
    else:
        pnl_pct = (state.entry_price - exit_price) / state.entry_price * 100

    row = {
        "entry_date": state.entry_date,
        "exit_date": exit_date,
        "direction": state.position,
        "entry_px": f"{state.entry_price:.2f}",
        "exit_px": f"{exit_price:.2f}",
        "pnl_pct": f"{pnl_pct:.3f}",
        "pnl_usd": "",  # filled when real sizing is applied
        "bars_held": state.bars_held,
        "exit_reason": exit_reason,
        "entry_type": state.entry_type,
        "trade_num": state.trade_count,
    }

    write_header = not trade_file.exists()
    with open(trade_file, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if write_header:
            w.writeheader()
        w.writerow(row)

    return pnl_pct


# ═══════════════════════════════════════════════════════════════
# Live Runner (IBKR)
# ═══════════════════════════════════════════════════════════════

def run_live(configs: list[Path]):
    """Run Helio with live IBKR data. Evaluates once per day at market close."""
    import sys
    sys.path.insert(0, str(REPO))
    from ops.process_lock import ProcessLock, ProcessLockError, build_runner_lock_name
    from ib_insync import IB

    # --- Process lock: prevent duplicate launches ---
    lock_name = build_runner_lock_name(
        client_id=200,
        config_paths=[str(c) for c in configs],
        exclude=None,
    )
    lock = ProcessLock(lock_name)
    try:
        lock.acquire(metadata={"family": "helio", "strategy": "swing_trend", "configs": [str(c) for c in configs]})
    except ProcessLockError as e:
        log.error(f"Cannot start: {e}")
        return
    log.info(f"Process lock acquired: {lock_name}")

    # --- Stage enforcement: read from first config ---
    first_cfg = json.loads(configs[0].read_text())
    stage = first_cfg.get("deployment", {}).get("stage", "watcher")
    can_execute = stage in ("paper", "real")
    log.info(f"Stage: {stage} | execution_allowed={can_execute}")

    ib = IB()
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "7496"))

    log.info(f"Connecting to IBKR {host}:{port}...")
    ib.connect(host, port, clientId=200, timeout=15)
    log.info(f"Connected. Account: {ib.managedAccounts()}")

    # Load configs and create instruments
    instruments = []
    for cfg_path in configs:
        cfg = json.loads(cfg_path.read_text())
        symbol = cfg["symbol"]
        log_dir = LOGS_ROOT / symbol.lower()
        log_dir.mkdir(parents=True, exist_ok=True)

        state = SwingState(log_dir / "state.json")
        state.load()

        contract = _build_contract(cfg)
        ib.qualifyContracts(contract)

        instruments.append({
            "config": cfg,
            "config_path": str(cfg_path),
            "symbol": symbol,
            "contract": contract,
            "state": state,
            "log_dir": log_dir,
        })
        log.info(f"  {symbol}: {state.position} | trades={state.trade_count}")

    log.info(f"Loaded {len(instruments)} instruments. Starting evaluation loop...")

    # Main loop — evaluate every 5 minutes, act on daily bar close
    last_eval_date = ""

    try:
        while True:
            ib.sleep(60)  # check every minute
            now = datetime.now(timezone.utc)

            # Only evaluate once per day after 21:00 UTC (market close)
            today = now.strftime("%Y-%m-%d")
            if today == last_eval_date:
                continue
            if now.hour < 21:
                continue

            last_eval_date = today
            log.info(f"=== Daily evaluation {today} ===")

            for inst in instruments:
                try:
                    _evaluate_instrument(ib, inst, now)
                except Exception as e:
                    log.error(f"{inst['symbol']}: evaluation error: {e}")

    except KeyboardInterrupt:
        log.info("Shutting down...")
        for inst in instruments:
            inst["state"].save()
        ib.disconnect()
        lock.release()
        log.info("Process lock released.")


def _evaluate_instrument(ib, inst: dict, now: datetime):
    """Evaluate one instrument for entry/exit on daily close."""
    cfg = inst["config"]
    state = inst["state"]
    symbol = inst["symbol"]
    log_dir = inst["log_dir"]

    # Fetch recent daily bars
    bars = ib.reqHistoricalData(
        inst["contract"],
        endDateTime="",
        durationStr="120 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=cfg.get("ibkr_sec_type") != "FUT",
    )
    if not bars or len(bars) < 60:
        log.warning(f"{symbol}: insufficient bars ({len(bars) if bars else 0})")
        return

    df = pd.DataFrame([{
        "Date": b.date, "Open": b.open, "High": b.high,
        "Low": b.low, "Close": b.close, "Volume": b.volume,
    } for b in bars])

    df = compute_indicators(df, cfg)

    # --- Regime classification ---
    from helio.regime_router import classify as classify_regime
    regime_info = classify_regime(df)
    log.info(f"{symbol}: REGIME {regime_info['regime']} (conf={regime_info['confidence']:.3f}) | priority={regime_info['family_priority']} | sizing_mod={regime_info['sizing_modifier']}")

    latest = df.iloc[-1]
    today_str = str(latest["Date"])[:10]

    # Write heartbeat (schema: system + family + stage for cross-family visibility)
    hb = {
        "ts": now.isoformat(),
        "system": "helio",
        "family": "helio",
        "stage": inst["config"].get("deployment", {}).get("stage", "watcher"),
        "symbol": symbol,
        "position": state.position,
        "close": float(latest["Close"]),
        "ema": float(latest["ema"]),
        "atr": float(latest["atr"]),
        "ema_slope": float(latest["ema_slope"]),
        "range_expanded": bool(latest["range_expanded"]),
        "vol_surge": bool(latest["vol_surge"]),
        "bars_held": state.bars_held,
        "trade_count": state.trade_count,
        "pnl_total": state.pnl_total,
        "regime": regime_info["regime"],
        "regime_confidence": regime_info["confidence"],
    }
    (log_dir / "heartbeat.json").write_text(json.dumps(hb, indent=2, default=str))

    # Position management
    if state.position != "FLAT":
        exit_reason = manage_position(state, latest, cfg)
        if exit_reason:
            exit_price = float(latest["Close"])
            if exit_reason in ("stop", "trail"):
                exit_price = state.stop_price

            pnl = log_trade(log_dir, state, exit_price, exit_reason, today_str)
            state.pnl_total += pnl
            log.info(f"{symbol}: EXIT {state.position} @ {exit_price:.2f} | {exit_reason} | PnL={pnl:+.2f}% | bars={state.bars_held}")

            state.position = "FLAT"
            state.entry_price = 0.0
            state.entry_date = ""
            state.stop_price = 0.0
            state.initial_stop = 0.0
            state.trail_stop = 0.0
            state.target_price = 0.0
            state.highest = 0.0
            state.lowest = 0.0
            state.bars_held = 0
            state.save()
        else:
            log.info(f"{symbol}: HOLD {state.position} @ {state.entry_price:.2f} | bars={state.bars_held} | stop={state.stop_price:.2f}")
            state.save()
        return

    # --- Watcher stage: observe-only, no synthetic positions ---
    _stage = cfg.get("deployment", {}).get("stage", "watcher")
    if _stage == "watcher":
        # Watchers log signals but NEVER enter positions or increment trades.
        direction = check_entry(latest, cfg)
        if direction:
            log.info(f"{symbol}: WATCHER signal {direction} (observe-only, not entering)")
            # Log the signal to signals.csv for later analysis
            _log_watcher_signal(log_dir, symbol, direction, latest, today_str)
        return

    # Regime depriority gate — only for paper/real stages
    if "helio" not in regime_info["family_priority"][:2]:
        log.info(f"{symbol}: REGIME_DEPRIORITY helio not in top-2 {regime_info['family_priority'][:2]} — skipping entry eval")
        return

    # Entry check (paper/real stages only)
    direction = check_entry(latest, cfg)
    if direction:
        risk_cfg = cfg.get("risk", {})
        atr = float(latest["atr"])
        entry_price = float(latest["Close"])

        if direction == "LONG":
            stop = entry_price - atr * risk_cfg.get("atr_stop_mult", 2.0)
        else:
            stop = entry_price + atr * risk_cfg.get("atr_stop_mult", 2.0)

        entry_type = "A" if latest["range_expanded"] and latest["vol_surge"] else "B"

        # Portfolio guard — check cross-family limits before new entry
        if _pg_check_new_entry is not None:
            pg_check = _pg_check_new_entry("helio_swing", cfg["symbol"], direction)
            if not pg_check.allowed:
                log.info(f"{symbol}: BLOCKED by portfolio guard — {pg_check.reason}")
                return
            if pg_check.warnings:
                log.info(f"{symbol}: portfolio guard warnings: {pg_check.warnings}")

        state.position = direction
        state.entry_price = entry_price
        state.entry_date = today_str
        state.stop_price = stop
        state.initial_stop = stop
        state.trail_stop = stop
        state.highest = entry_price
        state.lowest = entry_price
        state.bars_held = 0
        state.trade_count += 1
        state.entry_type = entry_type
        state.save()

        log.info(f"{symbol}: ENTRY {direction} @ {entry_price:.2f} | stop={stop:.2f} | ATR={atr:.2f} | type={entry_type}")

        # Log signal
        sig_file = log_dir / "signals.csv"
        sig_header = ["ts", "direction", "entry_px", "stop_px", "atr", "ema", "ema_slope",
                       "range_expanded", "vol_surge", "near_ema", "entry_type"]
        write_header = not sig_file.exists()
        with open(sig_file, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sig_header)
            if write_header:
                w.writeheader()
            w.writerow({
                "ts": today_str, "direction": direction,
                "entry_px": f"{entry_price:.2f}", "stop_px": f"{stop:.2f}",
                "atr": f"{atr:.2f}", "ema": f"{float(latest['ema']):.2f}",
                "ema_slope": f"{float(latest['ema_slope']):.6f}",
                "range_expanded": latest["range_expanded"],
                "vol_surge": latest["vol_surge"],
                "near_ema": latest["near_ema"],
                "entry_type": entry_type,
            })
    else:
        log.info(f"{symbol}: NO SIGNAL | close={float(latest['Close']):.2f} ema={float(latest['ema']):.2f} slope={float(latest['ema_slope']):.4f}")


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════

def _build_contract(cfg: dict):
    from ib_insync import ContFuture, Stock

    if cfg.get("ibkr_sec_type") == "FUT":
        return ContFuture(
            cfg["ibkr_symbol"],
            exchange=cfg.get("ibkr_exchange", "CME"),
        )
    return Stock(
        cfg["ibkr_symbol"],
        cfg.get("ibkr_exchange", "SMART"),
        cfg.get("ibkr_currency", "USD"),
    )


def main():
    parser = argparse.ArgumentParser(description="Helio Swing Trading Runner")
    parser.add_argument("--configs", nargs="*", default=None,
                        help="Config files to load. Default: all in helio/configs/")
    args = parser.parse_args()

    if args.configs:
        configs = [Path(c) for c in args.configs]
    else:
        configs = sorted(CONFIGS_DIR.glob("*_swing_v1.json"))

    if not configs:
        log.error("No configs found")
        return

    log.info(f"Helio Swing Runner starting with {len(configs)} instruments")
    run_live(configs)


if __name__ == "__main__":
    main()
