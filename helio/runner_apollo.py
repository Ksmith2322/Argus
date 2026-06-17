"""Apollo Mean Reversion Runner — daily evaluation, counter-trend entries on overextension.

Enters when price extends > Nx ATR from EMA with RSI confirmation.
Exits on reversion to EMA, stop, or timeout.

Usage:
    python -m helio.runner_apollo
    python -m helio.runner_apollo --configs helio/configs/apollo_audjpy_v1.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
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
    format="%(asctime)s [%(levelname)s] apollo | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("apollo")


class ApolloState:
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
        self.direction = ""

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
    ema_period = cfg.get("timeframe", {}).get("ema_period", 20)
    df["ema"] = df["Close"].ewm(span=ema_period, adjust=False).mean()
    df["tr"] = np.maximum(df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))))
    df["atr"] = df["tr"].rolling(14).mean()
    df["dist_from_ema"] = df["Close"] - df["ema"]
    df["dist_atr"] = abs(df["dist_from_ema"]) / df["atr"]
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def evaluate(state: ApolloState, row, cfg: dict, today_str: str, log_dir: Path):
    entry_cfg = cfg.get("entry", {})
    risk_cfg = cfg.get("risk", {})
    ext_mult = entry_cfg.get("extension_atr_mult", 2.0)
    rsi_ob = entry_cfg.get("rsi_overbought", 70)
    rsi_os = entry_cfg.get("rsi_oversold", 30)
    stop_mult = risk_cfg.get("atr_stop_mult", 0.5)
    target_mult = risk_cfg.get("reversion_target_mult", 0.5)
    max_hold = risk_cfg.get("max_hold_bars", risk_cfg.get("max_hold_days", 5))

    atr = float(row["atr"])
    ema = float(row["ema"])

    # Position management
    if state.position != "FLAT":
        state.bars_held += 1
        exit_reason = None
        exit_price = float(row["Close"])

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
            log.info(f"{cfg['symbol']}: EXIT {state.position} @ {exit_price:.5f} | {exit_reason} | PnL={pnl:+.3f}%")
            state.position = "FLAT"
            state.bars_held = 0
            state.save()
            return

        log.info(f"{cfg['symbol']}: HOLD {state.position} | bars={state.bars_held} | stop={state.stop_price:.5f}")
        state.save()
        return

    # Entry check
    overextended = row["dist_atr"] > ext_mult
    if not overextended:
        return

    close = float(row["Close"])

    # Determine entry direction (if any)
    _entry_dir = None
    if row["dist_from_ema"] > 0 and row["rsi"] > rsi_ob:
        _entry_dir = "SHORT"
    elif row["dist_from_ema"] < 0 and row["rsi"] < rsi_os:
        _entry_dir = "LONG"

    if _entry_dir is None:
        return

    # Portfolio guard — check cross-family limits before new entry
    if _pg_check_new_entry is not None:
        pg_check = _pg_check_new_entry("apollo", cfg["symbol"], _entry_dir)
        if not pg_check.allowed:
            log.info(f"{cfg['symbol']}: BLOCKED by portfolio guard — {pg_check.reason}")
            return
        if pg_check.warnings:
            log.info(f"{cfg['symbol']}: portfolio guard warnings: {pg_check.warnings}")

    if _entry_dir == "SHORT":
        # SHORT — overextended up
        stop = close + atr * stop_mult
        target = ema + atr * target_mult
        state.position = "SHORT"
        state.entry_price = close
        state.stop_price = stop
        state.target_price = target
        state.entry_date = today_str
        state.bars_held = 0
        state.trade_count += 1
        state.save()
        _log_signal(log_dir, today_str, "SHORT", close, stop, target, row)
        log.info(f"{cfg['symbol']}: ENTRY SHORT @ {close:.5f} | stop={stop:.5f} target={target:.5f} | RSI={row['rsi']:.0f} dist={row['dist_atr']:.1f}ATR")

    else:  # LONG
        # LONG — overextended down
        stop = close - atr * stop_mult
        target = ema - atr * target_mult
        state.position = "LONG"
        state.entry_price = close
        state.stop_price = stop
        state.target_price = target
        state.entry_date = today_str
        state.bars_held = 0
        state.trade_count += 1
        state.save()
        _log_signal(log_dir, today_str, "LONG", close, stop, target, row)
        log.info(f"{cfg['symbol']}: ENTRY LONG @ {close:.5f} | stop={stop:.5f} target={target:.5f} | RSI={row['rsi']:.0f} dist={row['dist_atr']:.1f}ATR")


def _log_trade(log_dir, state, exit_price, exit_reason, exit_date, pnl):
    f = log_dir / "trades.csv"
    header = ["entry_date", "exit_date", "direction", "entry_px", "exit_px", "pnl_pct", "bars_held", "exit_reason", "trade_num"]
    write_h = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        if write_h: w.writeheader()
        w.writerow({"entry_date": state.entry_date, "exit_date": exit_date, "direction": state.position,
                     "entry_px": f"{state.entry_price:.5f}", "exit_px": f"{exit_price:.5f}",
                     "pnl_pct": f"{pnl:.3f}", "bars_held": state.bars_held, "exit_reason": exit_reason,
                     "trade_num": state.trade_count})


def _log_signal(log_dir, ts, direction, entry, stop, target, row):
    f = log_dir / "signals.csv"
    header = ["ts", "direction", "entry_px", "stop_px", "target_px", "rsi", "dist_atr", "ema", "atr"]
    write_h = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        if write_h: w.writeheader()
        w.writerow({"ts": ts, "direction": direction, "entry_px": f"{entry:.5f}", "stop_px": f"{stop:.5f}",
                     "target_px": f"{target:.5f}", "rsi": f"{row['rsi']:.1f}", "dist_atr": f"{row['dist_atr']:.2f}",
                     "ema": f"{float(row['ema']):.5f}", "atr": f"{float(row['atr']):.5f}"})


def _eval_instrument(ib, inst: dict, bars, now: datetime, cfg: dict):
    """Evaluate one Apollo instrument given its historical bars."""
    df = pd.DataFrame([{"Date": b.date, "Open": b.open, "High": b.high, "Low": b.low,
                        "Close": b.close, "Volume": getattr(b, "volume", 0)} for b in bars])
    df = compute_indicators(df, cfg)

    from helio.regime_router import classify as classify_regime
    regime_info = classify_regime(df)
    log.info(f"{cfg['symbol']}: REGIME {regime_info['regime']} (conf={regime_info['confidence']:.3f}) | priority={regime_info['family_priority']} | sizing_mod={regime_info['sizing_modifier']}")

    latest = df.iloc[-1]
    today_str = now.strftime("%Y-%m-%d")

    # Write heartbeat
    (inst["log_dir"] / "heartbeat.json").write_text(json.dumps({
        "ts": now.isoformat(), "system": "helio", "family": "apollo",
        "stage": cfg.get("deployment", {}).get("stage", "watcher"),
        "symbol": cfg["symbol"], "position": inst["state"].position,
        "close": float(latest["Close"]), "rsi": float(latest["rsi"]),
        "dist_atr": float(latest["dist_atr"]), "trade_count": inst["state"].trade_count,
        "regime": regime_info["regime"], "regime_confidence": regime_info["confidence"],
    }, indent=2, default=str))

    # --- Watcher stage: observe-only, no synthetic positions ---
    _stage = cfg.get("deployment", {}).get("stage", "watcher")
    if _stage == "watcher":
        log.info(f"{cfg['symbol']}: WATCHER mode — observe-only, skipping entry/exit eval")
        return

    # Regime depriority gate (paper/real stages only)
    if "apollo" not in regime_info["family_priority"][:2] and inst["state"].position == "FLAT":
        log.info(f"{cfg['symbol']}: REGIME_DEPRIORITY — skipping entry eval")
        return

    evaluate(inst["state"], latest, cfg, today_str, inst["log_dir"])


def run_live(configs: list[Path]):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ops.process_lock import ProcessLock, ProcessLockError, build_runner_lock_name
    from ib_insync import IB, Forex

    # --- Process lock: prevent duplicate launches ---
    lock_name = build_runner_lock_name(
        client_id=220,
        config_paths=[str(c) for c in configs],
        exclude=None,
    )
    lock = ProcessLock(lock_name)
    try:
        lock.acquire(metadata={"family": "apollo", "strategy": "mean_reversion", "configs": [str(c) for c in configs]})
    except ProcessLockError as e:
        log.error(f"Cannot start: {e}")
        return
    log.info(f"Process lock acquired: {lock_name}")

    # --- Stage enforcement ---
    first_cfg = json.loads(configs[0].read_text())
    stage = first_cfg.get("deployment", {}).get("stage", "watcher")
    log.info(f"Stage: {stage} | execution_allowed={stage in ('paper', 'real')}")

    ib = IB()
    ib.connect(os.getenv("IBKR_HOST", "127.0.0.1"), int(os.getenv("IBKR_PORT", "7497")), clientId=220, timeout=15)
    log.info(f"Connected. Account: {ib.managedAccounts()}")

    instruments = []
    for cfg_path in configs:
        cfg = json.loads(cfg_path.read_text())
        sym = cfg["symbol"]
        log_dir = LOGS_ROOT / f"apollo_{sym.lower()}"
        log_dir.mkdir(parents=True, exist_ok=True)
        state = ApolloState(log_dir / "state.json")
        state.load()
        contract = Forex(sym[:3] + sym[3:])
        ib.qualifyContracts(contract)
        instruments.append({"config": cfg, "state": state, "contract": contract, "log_dir": log_dir})
        log.info(f"  {sym}: {state.position} | trades={state.trade_count}")

    # Group instruments by timeframe for different eval intervals
    hourly_instruments = [i for i in instruments if i["config"].get("timeframe", {}).get("primary") == "1h"]
    daily_instruments = [i for i in instruments if i["config"].get("timeframe", {}).get("primary") != "1h"]
    log.info(f"Apollo running: {len(hourly_instruments)} hourly + {len(daily_instruments)} daily instruments")
    last_daily_eval = ""
    last_hourly_eval = ""

    try:
        while True:
            ib.sleep(60)
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            hour_key = now.strftime("%Y-%m-%d-%H")

            # --- Hourly evaluation (1H instruments) ---
            if hourly_instruments and hour_key != last_hourly_eval and now.minute >= 5:
                last_hourly_eval = hour_key
                log.info(f"=== Apollo hourly evaluation {hour_key} ===")
                for inst in hourly_instruments:
                    try:
                        cfg = inst["config"]
                        bars = ib.reqHistoricalData(inst["contract"], endDateTime="", durationStr="30 D",
                                                    barSizeSetting="1 hour", whatToShow="MIDPOINT", useRTH=False)
                        if not bars or len(bars) < 40:
                            log.warning(f"{cfg['symbol']}: insufficient hourly bars"); continue
                        _eval_instrument(ib, inst, bars, now, cfg)
                    except Exception as e:
                        log.error(f"Apollo hourly {inst['config']['symbol']}: {e}")
                    ib.sleep(1)

            # --- Liveness refresh for daily instruments ---
            # Daily pairs only re-evaluate once per day (after 17:00 UTC), but the
            # fleet monitor flags any heartbeat older than ~hours as STALE. Touch
            # each daily instrument's heartbeat once per hour with the current ts.
            if daily_instruments and now.minute < 2:
                for inst in daily_instruments:
                    hb_path = inst["log_dir"] / "heartbeat.json"
                    try:
                        if hb_path.exists():
                            payload = json.loads(hb_path.read_text())
                            payload["ts"] = now.isoformat()
                            hb_path.write_text(json.dumps(payload, indent=2, default=str))
                    except Exception as e:
                        log.warning(f"heartbeat refresh failed for {inst['config']['symbol']}: {e}")

            # --- Daily evaluation (daily instruments) ---
            if daily_instruments and today != last_daily_eval and now.hour >= 17:
                last_daily_eval = today
                log.info(f"=== Apollo daily evaluation {today} ===")
                for inst in daily_instruments:
                    try:
                        cfg = inst["config"]
                        bars = ib.reqHistoricalData(inst["contract"], endDateTime="", durationStr="120 D",
                                                    barSizeSetting="1 day", whatToShow="MIDPOINT", useRTH=False)
                        if not bars or len(bars) < 40:
                            log.warning(f"{cfg['symbol']}: insufficient daily bars"); continue
                        _eval_instrument(ib, inst, bars, now, cfg)
                    except Exception as e:
                        log.error(f"Apollo daily {inst['config']['symbol']}: {e}")
                    ib.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        for inst in instruments:
            inst["state"].save()
        ib.disconnect()
        lock.release()
        log.info("Process lock released.")


def main():
    parser = argparse.ArgumentParser(description="Apollo Mean Reversion Runner")
    parser.add_argument("--configs", nargs="*", default=None)
    args = parser.parse_args()
    configs = [Path(c) for c in args.configs] if args.configs else sorted(CONFIGS_DIR.glob("apollo_*_v1.json"))
    if not configs:
        log.error("No Apollo configs found"); return
    log.info(f"Apollo starting with {len(configs)} instruments")
    run_live(configs)


if __name__ == "__main__":
    main()
