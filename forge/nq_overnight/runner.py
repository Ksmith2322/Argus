"""NQ Overnight Long — hourly runner.

Strategy: long NQ at top of overnight hours (20-23 UTC + 00 UTC), exit OCO 1:2.
See STRATEGY_SPEC.md for full rationale and walk-forward results.

Modes:
  --backtest    Run on cached 1h history
  --scan        Print current hour status
  --evaluate    One cycle: signal eval + position management
  --loop        Continuous: wake at top of each hour
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.fleet_sizing import compute_risk_usd, max_notional_usd, pnl_pct_of_fleet

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "nq_overnight"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("nq_overnight")

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr
IBKR_CLIENT_ID = 104
_SIGNAL_ONLY_MODE = False

PARAMS = {
    "version": "v1",
    "symbol": "NQ=F",
    "timeframe": "1h",
    "signal_hours_utc": [20, 21, 22, 23, 0],
    "target_atr": 1.0,
    "stop_atr": 0.5,
    "hold_bars": 4,
    "atr_period": 14,
    # risk_pct + model_equity_usd removed 2026-04-17 — sourced from
    # helio.fleet_sizing tier system (strategy_label="forge_nq_overnight").
    "point_value_usd": 2.0,  # MNQ
}

TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"
STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _config_hash() -> str:
    # Delegates to shared helper (2026-04-19 migration).
    from helio.strategy_common import config_hash
    return config_hash(PARAMS)


def _git_sha() -> str:
    from helio.strategy_common import git_sha
    return git_sha(REPO)


TRADE_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pts", "exit_reason",
    "duration_min", "trade_num", "pnl_usd", "pnl_pct_of_fleet", "position_size", "risk_usd",
    "risk_pct_of_fleet", "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "stop_px", "target_px", "signal_hour_utc",
]


def _ensure_trade_csv():
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "open_trade": None,
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _write_heartbeat(state: dict, last_eval_ts: str) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "nq_overnight",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_eval_ts": last_eval_ts,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str))


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _append_signal(ts, action, hour, atr_val) -> None:
    is_new = not SIGNALS_PATH.exists()
    cols = ["ts", "action", "hour_utc", "atr", "config_hash"]
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(cols)
        w.writerow([ts, action, hour, atr_val, _config_hash()])


def fetch_history(period: str = "60d") -> pd.DataFrame:
    df = yf.download("NQ=F", period=period, interval="1h", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no NQ=F data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def evaluate_once() -> None:
    log.info("NQ Overnight evaluation starting...")
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    state = _load_state()
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]

    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
            log.info("IBKR connected")
        except Exception as exc:
            log.warning("IBKR connect failed, falling back to signal-only: %s", exc)
            ib = None

    try:
        if state.get("open_trade"):
            ot = state["open_trade"]

            if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                contract = ibkr.make_contract("MNQ", "micro_future")
                try:
                    ib.qualifyContracts(contract)
                    outcome = ibkr.check_bracket_filled(
                        ib, contract, ot.get("stop_order_id"), ot.get("target_order_id"),
                        entry_direction=ot.get("direction"),
                        entry_size=ot.get("position_size") or ot.get("size"),
                        stop_px=ot.get("stop_px"),
                        target_px=ot.get("target_px"),
                    )
                except Exception as exc:
                    log.error("bracket check failed: %s", exc)
                    outcome = None
                if outcome is not None:
                    _close(state, outcome["fill_ts"], outcome["fill_price"], outcome["reason"])
                else:
                    entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                    forward = df[df.index > entry_idx_ts]
                    bars_held = len(forward)
                    ot["bars_held"] = bars_held
                    if bars_held >= PARAMS["hold_bars"]:
                        log.info("TIME_STOP: %d bars held, closing MNQ at market", bars_held)
                        try:
                            broker_qty = ibkr.query_position(ib, contract)
                        except Exception:
                            broker_qty = 0
                        if broker_qty > 0:
                            fill = ibkr.close_position_market(
                                ib, contract, direction="long", size=broker_qty,
                                stop_order_id=ot.get("stop_order_id"),
                                target_order_id=ot.get("target_order_id"),
                            )
                            if fill.filled:
                                _close(state, fill.fill_ts, fill.fill_price, "time")
                            else:
                                log.error("TIME_STOP close failed: %s", fill.reject_reason)
                        else:
                            last_row = df.iloc[last_idx]
                            _close(state, df.index[last_idx], float(last_row["Close"]), "reconcile_flat")
            else:
                entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                forward = df[df.index > entry_idx_ts]
                bars_held = 0
                for ts, row in forward.iterrows():
                    bars_held += 1
                    if row["Low"] <= ot["stop_px"]:
                        _close(state, ts, ot["stop_px"], "stop")
                        break
                    if row["High"] >= ot["target_px"]:
                        _close(state, ts, ot["target_px"], "target")
                        break
                    if bars_held >= PARAMS["hold_bars"]:
                        _close(state, ts, row["Close"], "time")
                        break

        sig_action = "NO_TRIGGER"
        if state.get("open_trade") is None:
            if last_ts.hour in PARAMS["signal_hours_utc"]:
                a = float(a_series.iloc[last_idx])
                if np.isfinite(a) and a > 0:
                    _open(state, df, last_idx, a, ib=ib)
                    sig_action = "ENTRY_LONG" if state.get("open_trade") else "ENTRY_REJECTED"
            _append_signal(last_ts, sig_action, last_ts.hour,
                           float(a_series.iloc[last_idx]) if np.isfinite(a_series.iloc[last_idx]) else 0)

        _save_state(state)
        _write_heartbeat(state, str(last_ts))
        log.info("Cycle done. open=%s trades_total=%d hour=%d action=%s",
                 "yes" if state.get("open_trade") else "no",
                 state.get("trade_count", 0), last_ts.hour, sig_action)
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def _open(state: dict, df: pd.DataFrame, idx: int, a: float, ib=None) -> None:
    plan_entry = float(df["Close"].iloc[idx])
    target = plan_entry + PARAMS["target_atr"] * a
    stop = plan_entry - PARAMS["stop_atr"] * a
    risk_budget_usd = compute_risk_usd(strategy_label="forge_nq_overnight")
    stop_dist_pts = plan_entry - stop
    contracts = max(1, int(risk_budget_usd / (stop_dist_pts * PARAMS["point_value_usd"])))
    cap_contracts = int(max_notional_usd("micro_future") / (plan_entry * PARAMS["point_value_usd"])) if plan_entry > 0 else contracts
    if cap_contracts > 0 and contracts > cap_contracts:
        log.warning("NOTIONAL_CAP: MNQ contracts %d > cap %d", contracts, cap_contracts)
        contracts = cap_contracts
    if contracts <= 0:
        log.warning("SIZE_ZERO: computed contracts <= 0, skipping entry")
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract("MNQ", "micro_future")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: MNQ qty=%s, skipping to avoid doubling", existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction="long", size=contracts,
                stop_px=stop, target_px=target, price_decimals=2,
                est_entry_px=plan_entry,
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED: %s", result.entry.reject_reason)
                return
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            target = entry_px + PARAMS["target_atr"] * a
            stop = entry_px - PARAMS["stop_atr"] * a
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            return

    risk_usd = (entry_px - stop) * PARAMS["point_value_usd"] * contracts
    state["open_trade"] = {
        "entry_ts": str(df.index[idx]),
        "entry_px": entry_px,
        "target_px": target,
        "stop_px": stop,
        "atr_entry": a,
        "position_size": contracts,
        "risk_usd": risk_usd,
        "session_id": state["session_id"],
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
        "signal_hour_utc": int(df.index[idx].hour),
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
        "bars_held": 0,
    }
    log.info("%s LONG MNQ @ %.2f target %.2f stop %.2f atr %.2f contracts %d hour %d UTC",
             execution_venue.upper(), entry_px, target, stop, a, contracts, df.index[idx].hour)


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    pnl_pts = exit_px - ot["entry_px"]
    pnl_usd = pnl_pts * ot["position_size"] * PARAMS["point_value_usd"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    _append_trade({
        "ts": ot["entry_ts"],
        "direction": "long",
        "entry_px": ot["entry_px"],
        "exit_px": exit_px,
        "pnl_pts": round(pnl_pts, 2),
        "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct_of_fleet": round(pnl_pct_of_fleet(pnl_usd), 4),
        "position_size": ot["position_size"],
        "risk_usd": ot["risk_usd"],
        "risk_pct_of_fleet": round(__import__("helio.fleet_sizing", fromlist=["get_effective_risk_pct"]).get_effective_risk_pct("forge_nq_overnight")["risk_pct"] * 100, 3),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "overnight",
        "experiment_valid": "true",
        "invalid_reason": "",
        "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state["runtime_start"]),
        "git_sha": ot["git_sha"],
        "atr_entry": ot["atr_entry"],
        "stop_px": ot["stop_px"],
        "target_px": ot["target_px"],
        "signal_hour_utc": ot["signal_hour_utc"],
    })
    log.info("PAPER LONG closed (%s) @ %.2f — pnl %+.2f pts ($%+.2f)", reason, exit_px, pnl_pts, pnl_usd)

    # Dual-write to canonical_fills for fleet-wide aggregation.
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_nq_overnight",
            symbol="MNQ",
            direction="long",
            side="EXIT",
            entry_ts=str(ot["entry_ts"]),
            exit_ts=str(exit_ts),
            entry_px=float(ot["entry_px"]),
            exit_px=float(exit_px),
            size=float(ot["position_size"]),
            risk_usd=float(ot.get("risk_usd") or 0.0),
            pnl_usd=round(pnl_usd, 2),
            exit_reason=reason,
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")

    state["open_trade"] = None


def scan() -> None:
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    print(f"NQ=F 1h — bar {last_ts}")
    print(f"  Close: {df['Close'].iloc[last_idx]:.2f}")
    print(f"  ATR(14): {a_series.iloc[last_idx]:.2f}")
    print(f"  Hour UTC: {last_ts.hour}")
    print(f"  Signal hours: {PARAMS['signal_hours_utc']}")
    print(f"  Would trigger: {'LONG' if last_ts.hour in PARAMS['signal_hours_utc'] else 'no'}")


def backtest(period: str = "2y") -> None:
    df = yf.download("NQ=F", period=period, interval="1h", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    a_arr = atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    trades = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue
        if df.index[i].hour not in PARAMS["signal_hours_utc"]:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        target = entry + PARAMS["target_atr"] * a
        stop = entry - PARAMS["stop_atr"] * a
        exit_px = None; exit_idx = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if L[j] <= stop:
                exit_px = stop; exit_idx = j; break
            if H[j] >= target:
                exit_px = target; exit_idx = j; break
        if exit_px is None:
            exit_idx = min(i + PARAMS["hold_bars"], len(df) - 1)
            exit_px = C[exit_idx]
        trades.append({"date": df.index[i], "hour": int(df.index[i].hour), "entry": entry, "exit": exit_px, "pnl_pts": exit_px - entry, "pnl_atr": (exit_px - entry) / a})
        open_until = exit_idx
    td = pd.DataFrame(trades)
    if td.empty:
        print("No trades.")
        return
    wr = (td.pnl_atr > 0).mean() * 100
    pf = td[td.pnl_atr > 0].pnl_atr.sum() / abs(td[td.pnl_atr <= 0].pnl_atr.sum() or 1)
    print(f"Trades: {len(td)} | WR: {wr:.1f}% | PF: {pf:.2f} | Exp: {td.pnl_atr.mean():+.4f} ATR/trade")
    print(f"Period: {td.date.min()} -> {td.date.max()}")
    print("\nBy hour:")
    print(td.groupby("hour").agg(n=("pnl_atr","count"), wr=("pnl_atr", lambda x: (x>0).mean()), exp=("pnl_atr","mean")).to_string())


def loop_mode():
    log.info("NQ Overnight --loop mode started. Signal hours UTC: %s", PARAMS["signal_hours_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            candidates = []
            for h in range(24):  # wake every hour for position management
                fire = now.replace(hour=h, minute=0, second=30, microsecond=0)
                if fire <= now:
                    fire += timedelta(days=1)
                candidates.append(fire)
            next_fire = min(candidates)
            sleep_s = max(5, (next_fire - now).total_seconds())
            log.info("Next eval at %s UTC (sleep %.0fs)", next_fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as e:
                log.error("Evaluation failed: %s", e)
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Loop stopped by user")
            return


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--backtest", action="store_true")
    g.add_argument("--loop", action="store_true")
    p.add_argument("--period", default="2y")
    p.add_argument("--signal-only", action="store_true",
                   help="Skip IBKR submission — keep the yfinance-replay simulation path")
    args = p.parse_args()
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")
    if args.evaluate:
        evaluate_once()
    elif args.scan:
        scan()
    elif args.backtest:
        backtest(args.period)
    elif args.loop:
        loop_mode()


if __name__ == "__main__":
    main()
