"""Hermes Momentum/Breakout Runner — daily evaluation, trades consolidation breaks.

Enters when price breaks N-bar consolidation range with volume surge.
Exits on target (3-4x ATR), stop (2x ATR), or timeout.

Usage:
    python -m helio.runner_hermes
    python -m helio.runner_hermes --configs helio/configs/hermes_gold_v1.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

try:
    from helio.portfolio_guard import check_new_entry as _pg_check_new_entry
except ImportError:
    _pg_check_new_entry = None

load_dotenv()

HELIO_ROOT = Path(__file__).resolve().parent
LOGS_ROOT = HELIO_ROOT / "logs"
CONFIGS_DIR = HELIO_ROOT / "configs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] hermes | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("hermes")


class HermesState:
    def __init__(self, path: Path):
        self.path = path
        self.position = "FLAT"
        self.entry_price = 0.0
        self.entry_date = ""
        self.stop_price = 0.0
        self.target_price = 0.0
        self.bars_held = 0
        self.trade_count = 0
        self.pnl_total = 0.0

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in self.__dict__.items() if k != "path"}, indent=2, default=str))
        tmp.replace(self.path)

    def load(self):
        if not self.path.exists():
            return
        try:
            for k, v in json.loads(self.path.read_text()).items():
                if hasattr(self, k):
                    setattr(self, k, v)
        except Exception:
            pass


def compute_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = df.copy()
    df["atr"] = np.maximum(df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1)))).rolling(14).mean()
    df["avg_vol"] = df["Volume"].rolling(20).mean()
    return df


def evaluate(state: HermesState, df: pd.DataFrame, cfg: dict, today_str: str, log_dir: Path):
    entry_cfg = cfg.get("entry", {})
    risk_cfg = cfg.get("risk", {})
    consol_bars = cfg.get("timeframe", {}).get("consolidation_bars", 7)
    brk_mult = entry_cfg.get("breakout_atr_mult", 0.5)
    vol_mult = entry_cfg.get("volume_surge_mult", 2.0)
    atr_stop = risk_cfg.get("atr_stop_mult", 2.0)
    atr_target = risk_cfg.get("atr_target_mult", 4.0)
    max_hold = risk_cfg.get("max_hold_days", 24)

    row = df.iloc[-1]
    atr = float(row["atr"])
    close = float(row["Close"])

    # Position management
    if state.position != "FLAT":
        state.bars_held += 1
        exit_reason = None
        exit_price = close

        if state.position == "LONG":
            if row["Low"] <= state.stop_price:
                exit_reason = "stop"; exit_price = state.stop_price
            elif row["High"] >= state.target_price:
                exit_reason = "target"; exit_price = state.target_price
            elif state.bars_held >= max_hold:
                exit_reason = "timeout"
        else:
            if row["High"] >= state.stop_price:
                exit_reason = "stop"; exit_price = state.stop_price
            elif row["Low"] <= state.target_price:
                exit_reason = "target"; exit_price = state.target_price
            elif state.bars_held >= max_hold:
                exit_reason = "timeout"

        if exit_reason:
            pnl = ((exit_price - state.entry_price) / state.entry_price * 100) if state.position == "LONG" \
                else ((state.entry_price - exit_price) / state.entry_price * 100)
            state.pnl_total += pnl
            _log_trade(log_dir, state, exit_price, exit_reason, today_str, pnl)
            log.info(f"{cfg['symbol']}: EXIT {state.position} @ {exit_price:.2f} | {exit_reason} | PnL={pnl:+.2f}%")
            state.position = "FLAT"
            state.bars_held = 0
            state.save()
            return

        log.info(f"{cfg['symbol']}: HOLD {state.position} | bars={state.bars_held} | stop={state.stop_price:.2f} target={state.target_price:.2f}")
        state.save()
        return

    # Entry check — consolidation breakout
    if len(df) < consol_bars + 5:
        return

    window = df.iloc[-(consol_bars + 1):-1]
    window_range = window["High"].max() - window["Low"].min()
    is_consolidated = window_range < atr * consol_bars * brk_mult

    if not is_consolidated:
        return

    range_high = float(window["High"].max())
    range_low = float(window["Low"].min())
    vol_ok = row["Volume"] > row["avg_vol"] * vol_mult

    # Determine breakout direction (if any)
    _breakout_dir = None
    if close > range_high and vol_ok:
        _breakout_dir = "LONG"
    elif close < range_low and vol_ok:
        _breakout_dir = "SHORT"

    if _breakout_dir is None:
        return

    # Portfolio guard — check cross-family limits before new entry
    if _pg_check_new_entry is not None:
        pg_check = _pg_check_new_entry("hermes", cfg["symbol"], _breakout_dir)
        if not pg_check.allowed:
            log.info(f"{cfg['symbol']}: BLOCKED by portfolio guard — {pg_check.reason}")
            return
        if pg_check.warnings:
            log.info(f"{cfg['symbol']}: portfolio guard warnings: {pg_check.warnings}")

    if _breakout_dir == "LONG":
        stop = close - atr * atr_stop
        target = close + atr * atr_target
        state.position = "LONG"
        state.entry_price = close
        state.stop_price = stop
        state.target_price = target
        state.entry_date = today_str
        state.bars_held = 0
        state.trade_count += 1
        state.save()
        _log_signal(log_dir, today_str, "LONG", close, stop, target, atr, window_range)
        log.info(f"{cfg['symbol']}: BREAKOUT LONG @ {close:.2f} | range={range_high:.2f}-{range_low:.2f} | stop={stop:.2f} target={target:.2f}")

    else:  # SHORT
        stop = close + atr * atr_stop
        target = close - atr * atr_target
        state.position = "SHORT"
        state.entry_price = close
        state.stop_price = stop
        state.target_price = target
        state.entry_date = today_str
        state.bars_held = 0
        state.trade_count += 1
        state.save()
        _log_signal(log_dir, today_str, "SHORT", close, stop, target, atr, window_range)
        log.info(f"{cfg['symbol']}: BREAKOUT SHORT @ {close:.2f} | range={range_high:.2f}-{range_low:.2f} | stop={stop:.2f} target={target:.2f}")


def _log_trade(log_dir, state, exit_price, exit_reason, exit_date, pnl):
    f = log_dir / "trades.csv"
    header = ["entry_date", "exit_date", "direction", "entry_px", "exit_px", "pnl_pct", "bars_held", "exit_reason", "trade_num"]
    write_h = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        if write_h: w.writeheader()
        w.writerow({"entry_date": state.entry_date, "exit_date": exit_date, "direction": state.position,
                     "entry_px": f"{state.entry_price:.2f}", "exit_px": f"{exit_price:.2f}",
                     "pnl_pct": f"{pnl:.3f}", "bars_held": state.bars_held, "exit_reason": exit_reason,
                     "trade_num": state.trade_count})


def _log_signal(log_dir, ts, direction, entry, stop, target, atr, consol_range):
    f = log_dir / "signals.csv"
    header = ["ts", "direction", "entry_px", "stop_px", "target_px", "atr", "consolidation_range"]
    write_h = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        if write_h: w.writeheader()
        w.writerow({"ts": ts, "direction": direction, "entry_px": f"{entry:.2f}", "stop_px": f"{stop:.2f}",
                     "target_px": f"{target:.2f}", "atr": f"{atr:.2f}", "consolidation_range": f"{consol_range:.2f}"})


def run_live(configs: list[Path]):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ops.process_lock import ProcessLock, ProcessLockError, build_runner_lock_name
    from ib_insync import IB

    # --- Process lock: prevent duplicate launches ---
    lock_name = build_runner_lock_name(
        client_id=210,
        config_paths=[str(c) for c in configs],
        exclude=None,
    )
    lock = ProcessLock(lock_name)
    try:
        lock.acquire(metadata={"family": "hermes", "strategy": "momentum_breakout", "configs": [str(c) for c in configs]})
    except ProcessLockError as e:
        log.error(f"Cannot start: {e}")
        return
    log.info(f"Process lock acquired: {lock_name}")

    # --- Stage enforcement ---
    first_cfg = json.loads(configs[0].read_text())
    stage = first_cfg.get("deployment", {}).get("stage", "watcher")
    log.info(f"Stage: {stage} | execution_allowed={stage in ('paper', 'real')}")

    ib = IB()
    ib.connect(os.getenv("IBKR_HOST", "127.0.0.1"), int(os.getenv("IBKR_PORT", "7496")), clientId=210, timeout=15)
    log.info(f"Connected. Account: {ib.managedAccounts()}")

    instruments = []
    for cfg_path in configs:
        cfg = json.loads(cfg_path.read_text())
        sym = cfg["symbol"]
        log_dir = LOGS_ROOT / f"hermes_{sym.lower()}"
        log_dir.mkdir(parents=True, exist_ok=True)
        state = HermesState(log_dir / "state.json")
        state.load()

        contract = _build_contract(cfg)
        ib.qualifyContracts(contract)

        instruments.append({"config": cfg, "state": state, "contract": contract, "log_dir": log_dir})
        log.info(f"  {sym}: {state.position} | trades={state.trade_count}")

    log.info(f"Hermes running with {len(instruments)} instruments")
    last_eval = ""

    try:
        while True:
            ib.sleep(60)
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            if today == last_eval or now.hour < 21:
                continue
            last_eval = today
            log.info(f"=== Hermes daily evaluation {today} ===")

            for inst in instruments:
                try:
                    cfg = inst["config"]
                    what = "TRADES" if cfg.get("ibkr_sec_type") == "FUT" else "MIDPOINT"
                    use_rth = cfg.get("ibkr_sec_type") != "FUT"
                    bars = ib.reqHistoricalData(inst["contract"], endDateTime="", durationStr="120 D",
                                                barSizeSetting="1 day", whatToShow=what, useRTH=use_rth)
                    if not bars or len(bars) < 30:
                        log.warning(f"{cfg['symbol']}: insufficient bars"); continue
                    df = pd.DataFrame([{"Date": b.date, "Open": b.open, "High": b.high, "Low": b.low,
                                        "Close": b.close, "Volume": getattr(b, "volume", 0)} for b in bars])
                    df = compute_indicators(df, cfg)

                    # --- Regime classification ---
                    from helio.regime_router import classify as classify_regime
                    regime_info = classify_regime(df)
                    log.info(f"{cfg['symbol']}: REGIME {regime_info['regime']} (conf={regime_info['confidence']:.3f}) | priority={regime_info['family_priority']} | sizing_mod={regime_info['sizing_modifier']}")

                    # Write heartbeat
                    latest = df.iloc[-1]
                    (inst["log_dir"] / "heartbeat.json").write_text(json.dumps({
                        "ts": now.isoformat(), "system": "helio", "family": "hermes",
                        "stage": cfg.get("deployment", {}).get("stage", "watcher"),
                        "symbol": cfg["symbol"],
                        "position": inst["state"].position, "close": float(latest["Close"]),
                        "atr": float(latest["atr"]), "trade_count": inst["state"].trade_count,
                        "regime": regime_info["regime"],
                        "regime_confidence": regime_info["confidence"],
                    }, indent=2, default=str))

                    # Regime depriority gate — only block new entries in watcher stage
                    _stage = cfg.get("deployment", {}).get("stage", "watcher")
                    if _stage == "watcher" and "hermes" not in regime_info["family_priority"][:2] and inst["state"].position == "FLAT":
                        log.info(f"{cfg['symbol']}: REGIME_DEPRIORITY hermes not in top-2 {regime_info['family_priority'][:2]} — skipping entry eval")
                    else:
                        evaluate(inst["state"], df, cfg, today, inst["log_dir"])
                except Exception as e:
                    log.error(f"{inst['config']['symbol']}: {e}")
    except KeyboardInterrupt:
        log.info("Shutting down...")
        for inst in instruments:
            inst["state"].save()
        ib.disconnect()
        lock.release()
        log.info("Process lock released.")


def _build_contract(cfg: dict):
    from ib_insync import ContFuture, Stock

    if cfg.get("ibkr_sec_type") == "FUT":
        return ContFuture(
            cfg["ibkr_symbol"],
            exchange=cfg.get("ibkr_exchange", "COMEX"),
        )
    return Stock(
        cfg["ibkr_symbol"],
        cfg.get("ibkr_exchange", "SMART"),
        cfg.get("ibkr_currency", "USD"),
    )


def main():
    parser = argparse.ArgumentParser(description="Hermes Momentum Runner")
    parser.add_argument("--configs", nargs="*", default=None)
    args = parser.parse_args()
    configs = [Path(c) for c in args.configs] if args.configs else sorted(CONFIGS_DIR.glob("hermes_*_v1.json"))
    if not configs:
        log.error("No Hermes configs found"); return
    log.info(f"Hermes starting with {len(configs)} instruments")
    run_live(configs)


if __name__ == "__main__":
    main()
