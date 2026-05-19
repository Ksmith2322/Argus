"""JPY PM Short — short USDJPY + CADJPY at 19 UTC.

Strategy: see STRATEGY_SPEC.md.

Modes: --backtest, --scan, --evaluate, --loop.
Manages two parallel positions (one per pair) — same entry hour, independent exits.
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
from argus_flow.sizing import fx_notional_per_unit_usd
from helio.fleet_sizing import compute_risk_usd, max_notional_usd, pnl_pct_of_fleet

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "jpy_pm_short"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("jpy_pm_short")

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr
IBKR_CLIENT_ID = 103
_SIGNAL_ONLY_MODE = False

PAIRS = {"USDJPY": "USDJPY=X", "CADJPY": "CADJPY=X"}

PARAMS = {
    "version": "v1",
    "pairs": list(PAIRS.keys()),
    "timeframe": "1h",
    "signal_hour_utc": 19,
    "target_atr": 1.0,
    "stop_atr": 0.5,
    "hold_bars": 4,
    "atr_period": 14,
    # risk_pct_per_pair + model_equity_usd removed 2026-04-17 — sourced
    # from helio.fleet_sizing tier system (strategy_label="forge_jpy_pm_short").
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
    "ts", "symbol", "direction", "entry_px", "exit_px", "pnl_pips", "exit_reason",
    "duration_min", "trade_num", "pnl_usd", "pnl_pct_of_fleet", "position_size", "risk_usd",
    "risk_pct_of_fleet", "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "stop_px", "target_px",
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
        "open_trades": {},  # symbol -> open trade dict
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _write_heartbeat(state: dict, last_eval_ts: str) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "jpy_pm_short",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trades": state.get("open_trades", {}),
        "last_eval_ts": last_eval_ts,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str))


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _append_signal(ts, sym, action, atr_val) -> None:
    is_new = not SIGNALS_PATH.exists()
    cols = ["ts", "symbol", "action", "atr", "config_hash"]
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(cols)
        w.writerow([ts, sym, action, atr_val, _config_hash()])


def fetch_history(ticker: str, period: str = "60d") -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval="1h", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no {ticker} data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def evaluate_once() -> None:
    log.info("JPY PM Short evaluation starting...")
    state = _load_state()
    last_ts_overall = None

    # Single IBKR connection for the whole cycle — both pairs share it
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
            log.info("IBKR connected")
        except Exception as exc:
            log.warning("IBKR connect failed, falling back to signal-only: %s", exc)
            ib = None

    try:
        for sym, ticker in PAIRS.items():
            try:
                df = fetch_history(ticker)
            except Exception as e:
                log.error("Failed to fetch %s: %s", sym, e)
                continue
            a_series = atr(df, PARAMS["atr_period"])
            last_idx = len(df) - 1
            last_ts = df.index[last_idx]
            last_ts_overall = last_ts

            # --- Manage existing position for this pair -------------------
            if sym in state["open_trades"]:
                ot = state["open_trades"][sym]

                if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                    contract = ibkr.make_contract(sym, "fx")
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
                        log.error("bracket check failed %s: %s", sym, exc)
                        outcome = None

                    if outcome is not None:
                        _close(state, sym, outcome["fill_ts"], outcome["fill_price"], outcome["reason"])
                    else:
                        entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                        forward = df[df.index > entry_idx_ts]
                        bars_held = len(forward)
                        ot["bars_held"] = bars_held
                        if bars_held >= PARAMS["hold_bars"]:
                            log.info("TIME_STOP %s: %d bars held, closing at market", sym, bars_held)
                            try:
                                broker_qty = ibkr.query_position(ib, contract)
                            except Exception:
                                broker_qty = 0
                            if broker_qty < 0:  # SHORT means negative broker qty
                                fill = ibkr.close_position_market(
                                    ib, contract, direction="short", size=abs(broker_qty),
                                    stop_order_id=ot.get("stop_order_id"),
                                    target_order_id=ot.get("target_order_id"),
                                )
                                if fill.filled:
                                    _close(state, sym, fill.fill_ts, fill.fill_price, "time")
                                else:
                                    log.error("TIME_STOP close failed %s: %s", sym, fill.reject_reason)
                            else:
                                last_row = df.iloc[last_idx]
                                _close(state, sym, df.index[last_idx], float(last_row["Close"]), "reconcile_flat")
                else:
                    # Signal-only fallback: yfinance replay like before
                    entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                    forward = df[df.index > entry_idx_ts]
                    bars_held = 0
                    for ts, row in forward.iterrows():
                        bars_held += 1
                        if row["High"] >= ot["stop_px"]:
                            _close(state, sym, ts, ot["stop_px"], "stop")
                            break
                        if row["Low"] <= ot["target_px"]:
                            _close(state, sym, ts, ot["target_px"], "target")
                            break
                        if bars_held >= PARAMS["hold_bars"]:
                            _close(state, sym, ts, row["Close"], "time")
                            break

            # --- Signal evaluation ----------------------------------------
            sig_action = "NO_TRIGGER"
            if sym not in state["open_trades"] and last_ts.hour == PARAMS["signal_hour_utc"]:
                a = float(a_series.iloc[last_idx])
                if np.isfinite(a) and a > 0:
                    _open(state, sym, df, last_idx, a, ib=ib)
                    sig_action = "ENTRY_SHORT" if sym in state["open_trades"] else "ENTRY_REJECTED"
            _append_signal(last_ts, sym, sig_action,
                           float(a_series.iloc[last_idx]) if np.isfinite(a_series.iloc[last_idx]) else 0)

        _save_state(state)
        _write_heartbeat(state, str(last_ts_overall) if last_ts_overall else "")
        log.info("Cycle done. open=%s trades_total=%d",
                 list(state.get("open_trades", {}).keys()),
                 state.get("trade_count", 0))
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def _open(state: dict, sym: str, df: pd.DataFrame, idx: int, a: float, ib=None) -> None:
    plan_entry = float(df["Close"].iloc[idx])
    target = plan_entry - PARAMS["target_atr"] * a  # SHORT: target below
    stop = plan_entry + PARAMS["stop_atr"] * a       # SHORT: stop above
    risk_budget_usd = compute_risk_usd(strategy_label="forge_jpy_pm_short")
    # 2026-05-07 audit P3: safe_position_size + ATR floor. USDJPY pip = 0.01.
    # point_value_usd=0.01 because per_unit_risk = stop_distance * 0.01 (algebra
    # from existing formula with pip_value=$10/lot and stop_pips=(stop-entry)*100).
    is_jpy = "JPY" in sym
    pt_usd = 0.01 if is_jpy else 1.0
    abs_floor = 0.01 if is_jpy else 0.0001
    from helio.strategy_common import safe_position_size
    pos_size, sizing_policy = safe_position_size(
        risk_usd=risk_budget_usd,
        entry_px=plan_entry,
        stop_px=stop,
        atr=a,
        sizing_floor_atr_mult=1.0,
        abs_floor_per_unit=abs_floor,
        point_value_usd=pt_usd,
    )
    usd_jpy_ref = plan_entry if sym == "USDJPY" else None
    notional_per_unit = fx_notional_per_unit_usd(sym, quote_price=plan_entry, usd_jpy_price=usd_jpy_ref)
    cap_units = int(max_notional_usd("fx") / max(notional_per_unit, 1e-9))
    if cap_units > 0 and pos_size > cap_units:
        log.warning("NOTIONAL_CAP: %s units %d > cap %d", sym, pos_size, cap_units)
        pos_size = cap_units
    if pos_size <= 0:
        log.warning("SIZE_ZERO: %s computed size <= 0, skipping entry (policy=%s)", sym, sizing_policy)
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract(sym, "fx")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: %s qty=%s, skipping to avoid doubling", sym, existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction="short", size=pos_size,
                stop_px=stop, target_px=target, price_decimals=5,
                est_entry_px=plan_entry,
                strategy_label="forge_jpy_pm_short",
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED %s: %s", sym, result.entry.reject_reason)
                return
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            target = entry_px - PARAMS["target_atr"] * a
            stop = entry_px + PARAMS["stop_atr"] * a
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION %s: %s", sym, exc, exc_info=True)
            return

    risk_usd = abs(stop - entry_px) * (100 if "JPY" in sym else 10000) * pip_value * (pos_size / 100_000)
    state["open_trades"][sym] = {
        "entry_ts": str(df.index[idx]),
        "entry_px": entry_px,
        "target_px": target,
        "stop_px": stop,
        "atr_entry": a,
        "position_size": pos_size,
        "risk_usd": risk_usd,
        "session_id": state["session_id"],
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
        "bars_held": 0,
    }
    log.info("%s SHORT %s @ %.5f target %.5f stop %.5f atr %.5f size %d",
             execution_venue.upper(), sym, entry_px, target, stop, a, pos_size)


def _close(state: dict, sym: str, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trades"][sym]
    # SHORT: pnl = entry - exit
    pip_mult = 100 if "JPY" in sym else 10000
    pnl_pips = (ot["entry_px"] - exit_px) * pip_mult
    pip_value = 10.0
    pnl_usd = pnl_pips * pip_value * (ot["position_size"] / 100_000)
    state["trade_count"] += 1

    # Dual-write to canonical_fills.jsonl for fleet-wide aggregation.
    # Without this, new trades show up in the per-strategy trades.csv but
    # are invisible to fleet_perf_summary, broker_equity_curve, and holdout
    # evaluation — causing the dashboard's recent_trades vs canonical-fills
    # divergence observed 2026-04-21.
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_jpy_pm_short",
            symbol=sym,
            direction="short",
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
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    _append_trade({
        "ts": ot["entry_ts"],
        "symbol": sym,
        "direction": "short",
        "entry_px": ot["entry_px"],
        "exit_px": exit_px,
        "pnl_pips": round(pnl_pips, 1),
        "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct_of_fleet": round(pnl_pct_of_fleet(pnl_usd), 4),
        "position_size": ot["position_size"],
        "risk_usd": ot["risk_usd"],
        "risk_pct_of_fleet": round(__import__("helio.fleet_sizing", fromlist=["get_effective_risk_pct"]).get_effective_risk_pct("forge_jpy_pm_short")["risk_pct"] * 100, 3),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "ny_pm",
        "experiment_valid": "true",
        "invalid_reason": "",
        "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state["runtime_start"]),
        "git_sha": ot["git_sha"],
        "atr_entry": ot["atr_entry"],
        "stop_px": ot["stop_px"],
        "target_px": ot["target_px"],
    })
    log.info("PAPER SHORT %s closed (%s) @ %.5f — pnl %+.1f pips ($%+.2f)", sym, reason, exit_px, pnl_pips, pnl_usd)
    del state["open_trades"][sym]


def scan() -> None:
    for sym, ticker in PAIRS.items():
        df = fetch_history(ticker)
        a_series = atr(df, PARAMS["atr_period"])
        last_idx = len(df) - 1
        last_ts = df.index[last_idx]
        print(f"{sym} 1h — bar {last_ts}")
        print(f"  Close: {df['Close'].iloc[last_idx]:.5f}")
        print(f"  ATR(14): {a_series.iloc[last_idx]:.5f}")
        print(f"  Hour UTC: {last_ts.hour} (signal hour: {PARAMS['signal_hour_utc']})")
        print(f"  Would trigger: {'SHORT' if last_ts.hour == PARAMS['signal_hour_utc'] else 'no'}")
        print()


def backtest(period: str = "2y") -> None:
    for sym, ticker in PAIRS.items():
        df = yf.download(ticker, period=period, interval="1h", progress=False, auto_adjust=False)
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
            if df.index[i].hour != PARAMS["signal_hour_utc"]:
                continue
            a = a_arr[i]
            if not np.isfinite(a) or a <= 0:
                continue
            entry = C[i]
            target = entry - PARAMS["target_atr"] * a
            stop = entry + PARAMS["stop_atr"] * a
            exit_px = None; exit_idx = None
            for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
                if H[j] >= stop:
                    exit_px = stop; exit_idx = j; break
                if L[j] <= target:
                    exit_px = target; exit_idx = j; break
            if exit_px is None:
                exit_idx = min(i + PARAMS["hold_bars"], len(df) - 1)
                exit_px = C[exit_idx]
            trades.append({"date": df.index[i], "entry": entry, "exit": exit_px, "pnl_atr": (entry - exit_px) / a})
            open_until = exit_idx
        td = pd.DataFrame(trades)
        if td.empty:
            print(f"{sym}: No trades.")
            continue
        wr = (td.pnl_atr > 0).mean() * 100
        pf = td[td.pnl_atr > 0].pnl_atr.sum() / abs(td[td.pnl_atr <= 0].pnl_atr.sum() or 1)
        print(f"{sym}: Trades: {len(td)} | WR: {wr:.1f}% | PF: {pf:.2f} | Exp: {td.pnl_atr.mean():+.4f} ATR/trade")


def loop_mode():
    log.info("JPY PM Short --loop started. Signal hour UTC: %s", PARAMS["signal_hour_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            candidates = []
            for h in range(24):  # wake hourly for management
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
