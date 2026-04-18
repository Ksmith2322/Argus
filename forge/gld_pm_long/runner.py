"""GLD PM Long — hourly intraday runner.

Strategy: see STRATEGY_SPEC.md.

Modes:
  --backtest   Run on cached 1h history, print stats
  --scan       Print current hour's evaluation status
  --evaluate   One cycle: evaluate signal at top of hour, manage open position
  --loop       Continuous: sleep until next top-of-hour, then evaluate

Designed to run continuously OR via --evaluate from a scheduler that fires
at the top of each hour during US session.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
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
LOG_DIR = REPO / "forge" / "logs" / "gld_pm_long"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("gld_pm_long")

PARAMS = {
    "version": "v1",
    "symbol": "GLD",
    "timeframe": "1h",
    "signal_hours_utc": [18, 19, 20],
    "target_atr": 1.0,
    "stop_atr": 0.5,
    "hold_bars": 4,
    "atr_period": 14,
    # risk_pct + model_equity_usd both removed 2026-04-17:
    # sizing now comes from helio.fleet_sizing via tier system (risk_pct
    # auto-sets from measured live performance). See strategy_label
    # "forge_gld_pm_long" in fleet_sizing.json tiers config.
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
    return hashlib.sha256(json.dumps(PARAMS, sort_keys=True).encode()).hexdigest()[:16]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO), stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return "unknown"


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
    ot = state.get("open_trade")
    open_bars_held = None
    open_wall_minutes = None
    if ot:
        try:
            entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
            last_ts_dt = pd.to_datetime(last_eval_ts, utc=True)
            open_wall_minutes = round((last_ts_dt - entry_ts).total_seconds() / 60.0, 1)
            open_bars_held = int(ot.get("bars_held", 0))
        except Exception:
            pass
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "gld_pm_long",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": ot,
        # Use bars_held for open-trade SLO, not wall-clock minutes: equity hourly
        # bars gap overnight so a held position can show 18+ wall-clock hours
        # while only occupying 2-3 market bars (designed-normal for this strategy).
        "open_bars_held": open_bars_held,
        "open_wall_minutes": open_wall_minutes,
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
    df = yf.download("GLD", period=period, interval="1h", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no GLD data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def evaluate_once() -> None:
    log.info("GLD PM Long evaluation starting...")
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    state = _load_state()
    last_idx = len(df) - 1
    last_bar = df.iloc[last_idx]
    last_ts = df.index[last_idx]

    # Manage open position — replay every bar since entry
    if state.get("open_trade"):
        ot = state["open_trade"]
        entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
        # Find bars after entry
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

    # Signal evaluation: only at top of designated hours, only if flat
    sig_action = "NO_TRIGGER"
    if state.get("open_trade") is None:
        if last_ts.hour in PARAMS["signal_hours_utc"]:
            a = float(a_series.iloc[last_idx])
            if np.isfinite(a) and a > 0:
                _open(state, df, last_idx, a)
                sig_action = "ENTRY_LONG"
        _append_signal(last_ts, sig_action, last_ts.hour, float(a_series.iloc[last_idx]) if np.isfinite(a_series.iloc[last_idx]) else 0)

    _save_state(state)
    _write_heartbeat(state, str(last_ts))
    log.info("Cycle done. open=%s trades_total=%d hour=%d action=%s",
             "yes" if state.get("open_trade") else "no",
             state.get("trade_count", 0), last_ts.hour, sig_action)


def _open(state: dict, df: pd.DataFrame, idx: int, a: float) -> None:
    entry = float(df["Close"].iloc[idx])  # fill at hour close (paper)
    target = entry + PARAMS["target_atr"] * a
    stop = entry - PARAMS["stop_atr"] * a
    risk_budget_usd = compute_risk_usd(strategy_label="forge_gld_pm_long")
    pos_size = int(risk_budget_usd / max(entry - stop, 0.01))
    cap_shares = int(max_notional_usd("etf") / entry) if entry > 0 else pos_size
    if cap_shares > 0 and pos_size > cap_shares:
        log.warning("NOTIONAL_CAP: GLD shares %d > cap %d", pos_size, cap_shares)
        pos_size = cap_shares
    risk_usd = pos_size * max(entry - stop, 0.0)
    state["open_trade"] = {
        "entry_ts": str(df.index[idx]),
        "entry_px": entry,
        "target_px": target,
        "stop_px": stop,
        "atr_entry": a,
        "position_size": pos_size,
        "risk_usd": risk_usd,
        "session_id": state["session_id"],
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
        "signal_hour_utc": int(df.index[idx].hour),
    }
    log.info("PAPER LONG opened @ %.2f target %.2f stop %.2f atr %.4f size %d hour %d UTC",
             entry, target, stop, a, pos_size, df.index[idx].hour)


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    pnl_pts = exit_px - ot["entry_px"]
    pnl_usd = pnl_pts * ot["position_size"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    _append_trade({
        "ts": ot["entry_ts"],
        "direction": "long",
        "entry_px": ot["entry_px"],
        "exit_px": exit_px,
        "pnl_pts": round(pnl_pts, 4),
        "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct_of_fleet": round(pnl_pct_of_fleet(pnl_usd), 4),
        "position_size": ot["position_size"],
        "risk_usd": ot["risk_usd"],
        "risk_pct_of_fleet": round(__import__("helio.fleet_sizing", fromlist=["get_effective_risk_pct"]).get_effective_risk_pct("forge_gld_pm_long")["risk_pct"] * 100, 3),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "pm_session",
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
    log.info("PAPER LONG closed (%s) @ %.2f — pnl %+.4f pts ($%+.2f)", reason, exit_px, pnl_pts, pnl_usd)
    state["open_trade"] = None


def scan() -> None:
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    print(f"GLD 1h — bar {last_ts}")
    print(f"  Close: {df['Close'].iloc[last_idx]:.2f}")
    print(f"  ATR(14): {a_series.iloc[last_idx]:.4f}")
    print(f"  Hour UTC: {last_ts.hour}")
    print(f"  Signal hours: {PARAMS['signal_hours_utc']}")
    print(f"  Would trigger: {'LONG' if last_ts.hour in PARAMS['signal_hours_utc'] else 'no (wrong hour)'}")


def backtest(period: str = "2y") -> None:
    df = yf.download("GLD", period=period, interval="1h", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    a_arr = atr(df, PARAMS["atr_period"]).values
    H, L, C, O = df["High"].values, df["Low"].values, df["Close"].values, df["Open"].values

    trades = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue  # already in a trade
        if df.index[i].hour not in PARAMS["signal_hours_utc"]:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]  # signal bar close
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
    """Sleep until ~30 seconds after the next signal hour boundary, then evaluate.

    Why offset 30s: yfinance bar for hour H usually populates a few seconds after
    the hour ends. Waiting briefly ensures the latest bar is in.
    """
    log.info("GLD PM Long --loop mode started. Signal hours UTC: %s", PARAMS["signal_hours_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Find next firing time: top of next signal hour + 30s
            candidates = []
            for h in PARAMS["signal_hours_utc"]:
                fire = now.replace(hour=h, minute=0, second=30, microsecond=0)
                if fire <= now:
                    fire += timedelta(days=1)
                candidates.append(fire)
            # Also wake daily to manage open positions through stop/target hits even
            # outside signal hours (a GLD position opened at 20:00 might exit at 21:00)
            for h in range(24):
                if h in PARAMS["signal_hours_utc"]:
                    continue
                fire = now.replace(hour=h, minute=5, second=0, microsecond=0)
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
    g.add_argument("--loop", action="store_true", help="Continuous: wake at top of signal hours")
    p.add_argument("--period", default="2y")
    args = p.parse_args()
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
