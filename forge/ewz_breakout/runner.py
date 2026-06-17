"""EWZ Monthly Breakout -- daily runner.

Strategy: long EWZ when daily close crosses its prior 21-day high.
Exit: -2% stop or 5-day timeout (no target).

Backtest gate: 22.6 years, n=82, PF=1.73, CI lower=1.275, WR=0.50,
avg +0.59% per trade, ~3.6 fills/year. Survived breakout sweep
2026-05-25 at 6bps round-trip slippage.

Modes:
  --backtest   Run on cached daily history, print stats
  --scan       Print current bar's breakout status
  --evaluate   One cycle: evaluate at end of day, manage open position
  --loop       Continuous: sleep until next end-of-day, then evaluate
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.blocker_ledger import record_blocker
from helio.fleet_sizing import compute_risk_usd, max_notional_usd, pnl_pct_of_fleet
from helio import ibkr_execution as ibkr

IBKR_CLIENT_ID = 132
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "ewz_breakout"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("ewz_breakout")

PARAMS = {
    "version": "v1",
    "symbol": "EWZ",
    "timeframe": "1d",
    # Signal: today's close > rolling max of prior 21 closes
    "breakout_lookback": 21,
    # Exit: -2% stop OR 5-day timeout (no target)
    "stop_pct": 0.02,
    "hold_days": 5,
    # Evaluate once per day, ~30 min after US equity close (20:30 UTC = 16:30 ET DST)
    "signal_hour_utc": 20,
    "atr_period": 14,  # informational only
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
    ot = state.get("open_trade")
    open_days_held = None
    open_wall_minutes = None
    if ot:
        try:
            entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
            last_ts_dt = pd.to_datetime(last_eval_ts, utc=True)
            open_wall_minutes = round((last_ts_dt - entry_ts).total_seconds() / 60.0, 1)
            open_days_held = int(ot.get("days_held", 0))
        except Exception:
            pass
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "ewz_breakout",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": ot,
        "open_days_held": open_days_held,
        "open_wall_minutes": open_wall_minutes,
        "last_eval_ts": last_eval_ts,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str))


def _last_eval_ts_from_heartbeat() -> str:
    try:
        if HEARTBEAT_PATH.exists():
            data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
            return str(data.get("last_eval_ts") or "")
    except Exception:
        pass
    return ""


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _append_signal(ts, action, prior_high, close_px) -> None:
    is_new = not SIGNALS_PATH.exists()
    cols = ["ts", "action", "prior_21d_high", "close", "config_hash"]
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(cols)
        w.writerow([ts, action, prior_high, close_px, _config_hash()])


def fetch_history(period: str = "120d") -> pd.DataFrame:
    df = yf.download(PARAMS["symbol"], period=period, interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no {PARAMS['symbol']} data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def evaluate_once() -> None:
    log.info("EWZ breakout evaluation starting...")
    df = fetch_history()
    state = _load_state()
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    a_series = atr(df, PARAMS["atr_period"])
    # Prior N-day high excluding today
    roll_high = df["Close"].rolling(PARAMS["breakout_lookback"]).max()
    prior_high_series = roll_high.shift(1)

    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
            log.info("IBKR connected")
        except Exception as exc:
            log.warning("IBKR connect failed, falling back to signal-only: %s", exc)
            ib = None

    try:
        # --- Manage open position ---
        if state.get("open_trade"):
            ot = state["open_trade"]

            # Self-heal stale signal-only state
            if (ib is not None
                and ot.get("execution_venue") == "signal_only"):
                try:
                    _check_contract = ibkr.make_contract(PARAMS["symbol"], "etf")
                    ib.qualifyContracts(_check_contract)
                    _broker_qty = ibkr.query_position(ib, _check_contract)
                except Exception as exc:
                    log.warning("self-heal broker-position check failed: %s", exc)
                    _broker_qty = None
                if _broker_qty == 0:
                    log.warning(
                        "STALE_SIGNAL_ONLY: clearing phantom open_trade "
                        "(entry_ts=%s) so live signals can resume",
                        ot.get("entry_ts"),
                    )
                    state["open_trade"] = None
                    _save_state(state)
                    ot = None

            if ot is None:
                pass
            elif ib is not None and ot.get("execution_venue") == "ibkr_paper":
                contract = ibkr.make_contract(PARAMS["symbol"], "etf")
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
                    days_held = len(forward)
                    ot["days_held"] = days_held
                    if days_held >= PARAMS["hold_days"]:
                        log.info("TIME_STOP: %d days held, closing at market", days_held)
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
                # Signal-only replay
                entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                forward = df[df.index > entry_idx_ts]
                days_held = 0
                for ts, row in forward.iterrows():
                    days_held += 1
                    if row["Low"] <= ot["stop_px"]:
                        _close(state, ts, ot["stop_px"], "stop")
                        break
                    if days_held >= PARAMS["hold_days"]:
                        _close(state, ts, row["Close"], "time")
                        break

        # --- Signal evaluation ---
        sig_action = "NO_TRIGGER"
        if state.get("open_trade") is None:
            now_utc = datetime.now(timezone.utc)
            is_weekend = now_utc.weekday() >= 5
            prior_high = prior_high_series.iloc[last_idx]
            close_px = float(df["Close"].iloc[last_idx])
            if is_weekend:
                sig_action = "WEEKEND_SKIP"
            elif not np.isfinite(prior_high):
                sig_action = "WARMUP"
            elif close_px > float(prior_high):
                a = float(a_series.iloc[last_idx]) if np.isfinite(a_series.iloc[last_idx]) else 0.0
                _open(state, df, last_idx, a, ib=ib)
                sig_action = "ENTRY_LONG" if state.get("open_trade") else "ENTRY_REJECTED"
            _append_signal(last_ts, sig_action,
                           float(prior_high) if np.isfinite(prior_high) else None,
                           close_px)

        _save_state(state)
        _write_heartbeat(state, str(last_ts))
        log.info("Cycle done. open=%s trades_total=%d action=%s",
                 "yes" if state.get("open_trade") else "no",
                 state.get("trade_count", 0), sig_action)
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def _open(state: dict, df: pd.DataFrame, idx: int, a: float, ib=None) -> None:
    """Open LONG EWZ at signal close. Stop = -2% from entry. No target."""
    plan_entry = float(df["Close"].iloc[idx])
    stop = plan_entry * (1.0 - PARAMS["stop_pct"])
    target = None  # No target -- exit on stop or time
    risk_budget_usd = compute_risk_usd(strategy_label="forge_ewz_breakout")
    cap_usd = max_notional_usd("etf", strategy_label="forge_ewz_breakout") if plan_entry > 0 else None
    from helio.strategy_common import safe_position_size
    pos_size, sizing_policy = safe_position_size(
        risk_usd=risk_budget_usd,
        entry_px=plan_entry,
        stop_px=stop,
        atr=a if a > 0 else (plan_entry * PARAMS["stop_pct"]),
        sizing_floor_atr_mult=1.0,
        max_notional_usd=cap_usd if cap_usd and cap_usd > 0 else None,
        point_value_usd=1.0,
    )
    if pos_size <= 0:
        log.warning("SIZE_ZERO: computed size <= 0, skipping entry (policy=%s)", sizing_policy)
        record_blocker(
            "forge_ewz_breakout",
            "SIZE_ZERO",
            symbol="EWZ",
            stage="entry",
            action="BUY",
            reason=f"safe_position_size returned {pos_size}",
            context={"sizing_policy": sizing_policy},
        )
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract("EWZ", "etf")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: EWZ qty=%s, skipping entry to avoid doubling", existing)
                record_blocker(
                    "forge_ewz_breakout",
                    "BROKER_POSITION_EXISTS",
                    symbol="EWZ",
                    stage="entry",
                    action="BUY",
                    qty=pos_size,
                    notional_usd=pos_size * plan_entry,
                    reason=f"broker existing qty={existing}",
                )
                return
            # No target: submit stop-only protective order via wide synthetic target
            # check_bracket_filled treats target as optional; pass a far-OTM target so
            # the bracket helper still wires both legs (avoid one-leg adoption issues).
            synthetic_target = plan_entry * 10.0  # effectively unreachable
            result = ibkr.submit_bracket(
                ib, contract, direction="long", size=pos_size,
                stop_px=stop, target_px=synthetic_target, price_decimals=2,
                est_entry_px=plan_entry,
                strategy_label="forge_ewz_breakout",
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED: %s", result.entry.reject_reason)
                reject = str(result.entry.reject_reason or "unknown")
                code = "ORDER_REJECTED"
                if "cluster_cap" in reject or "oversized_order" in reject:
                    code = "CAP_EXCEEDED"
                elif "market_closed" in reject:
                    code = "MARKET_CLOSED"
                elif "real_money_boundary" in reject:
                    code = "REAL_MONEY_BOUNDARY"
                record_blocker(
                    "forge_ewz_breakout",
                    code,
                    symbol="EWZ",
                    stage="entry",
                    action="BUY",
                    qty=pos_size,
                    notional_usd=pos_size * plan_entry,
                    reason=reject,
                )
                return
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            stop = entry_px * (1.0 - PARAMS["stop_pct"])
            target = entry_px * 10.0  # synthetic
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            record_blocker(
                "forge_ewz_breakout",
                "ENTRY_EXCEPTION",
                symbol="EWZ",
                stage="entry",
                action="BUY",
                qty=pos_size,
                notional_usd=pos_size * plan_entry,
                reason=str(exc),
            )
            return

    risk_usd = pos_size * max(entry_px - stop, 0.0)
    state["open_trade"] = {
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
        "signal_hour_utc": int(df.index[idx].hour),
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
        "days_held": 0,
    }
    log.info("%s LONG opened @ %.2f stop %.2f (-%.1f%%) size %d hold %dd",
             execution_venue.upper(), entry_px, stop, PARAMS["stop_pct"] * 100,
             pos_size, PARAMS["hold_days"])


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    pnl_pts = exit_px - ot["entry_px"]
    pnl_usd = pnl_pts * ot["position_size"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    try:
        pnl_pct_value = round(pnl_pct_of_fleet(pnl_usd), 4)
    except Exception as exc:
        log.warning("PNL_PCT_UNAVAILABLE: %s", exc)
        pnl_pct_value = ""
    try:
        from helio.fleet_sizing import get_effective_risk_pct
        risk_pct_value = round(
            get_effective_risk_pct("forge_ewz_breakout")["risk_pct"] * 100,
            3,
        )
    except Exception as exc:
        log.warning("RISK_PCT_UNAVAILABLE: %s", exc)
        risk_pct_value = ""
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
        "pnl_pct_of_fleet": pnl_pct_value,
        "position_size": ot["position_size"],
        "risk_usd": ot["risk_usd"],
        "risk_pct_of_fleet": risk_pct_value,
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "breakout_21d_high",
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
    log.info("PAPER LONG closed (%s) @ %.2f -- pnl %+.4f pts ($%+.2f)", reason, exit_px, pnl_pts, pnl_usd)

    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_ewz_breakout",
            symbol="EWZ",
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
    except Exception:
        pass

    state["open_trade"] = None


def scan() -> None:
    df = fetch_history()
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    roll_high = df["Close"].rolling(PARAMS["breakout_lookback"]).max()
    prior_high = roll_high.shift(1).iloc[last_idx]
    close_px = df["Close"].iloc[last_idx]
    print(f"EWZ 1d -- bar {last_ts.date()}")
    print(f"  Close: {close_px:.2f}")
    print(f"  Prior {PARAMS['breakout_lookback']}d high: {prior_high:.2f}" if np.isfinite(prior_high) else "  Prior high: --")
    print(f"  Breakout: {'YES' if np.isfinite(prior_high) and close_px > prior_high else 'no'}")
    print(f"  Stop pct: -{PARAMS['stop_pct'] * 100:.1f}% | Hold: {PARAMS['hold_days']}d")


def backtest(period: str = "5y") -> None:
    df = yf.download(PARAMS["symbol"], period=period, interval="1d", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    if len(df) < PARAMS["breakout_lookback"] + 30:
        print(f"Not enough bars ({len(df)})")
        return
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    roll_high = df["Close"].rolling(PARAMS["breakout_lookback"]).max().values
    prior_high = pd.Series(roll_high).shift(1).values

    trades = []
    open_until = -1
    for i in range(PARAMS["breakout_lookback"] + 1, len(df)):
        if i <= open_until:
            continue
        if not np.isfinite(prior_high[i]):
            continue
        if C[i] <= prior_high[i]:
            continue
        entry = C[i]
        stop_px = entry * (1.0 - PARAMS["stop_pct"])
        exit_px = None; exit_idx = None; exit_reason = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_days"], len(df))):
            if L[j] <= stop_px:
                exit_px = stop_px; exit_idx = j; exit_reason = "stop"; break
        if exit_px is None:
            exit_idx = min(i + PARAMS["hold_days"], len(df) - 1)
            exit_px = C[exit_idx]; exit_reason = "time"
        pnl_pct = (exit_px - entry) / entry * 100.0
        trades.append({"date": df.index[i], "entry": entry, "exit": exit_px,
                       "pnl_pct": pnl_pct, "exit_reason": exit_reason,
                       "held_days": int(exit_idx - i)})
        open_until = exit_idx
    td = pd.DataFrame(trades)
    if td.empty:
        print("No trades.")
        return
    wr = (td.pnl_pct > 0).mean() * 100
    pf = td[td.pnl_pct > 0].pnl_pct.sum() / abs(td[td.pnl_pct < 0].pnl_pct.sum() or 1)
    print(f"Trades: {len(td)} | WR: {wr:.1f}% | PF: {pf:.2f} | Avg: {td.pnl_pct.mean():+.2f}% | Period: {td.date.min().date()} -> {td.date.max().date()}")


def loop_mode():
    """Sleep until 30s after the daily signal hour, then evaluate.

    EWZ uses end-of-day signals: one evaluation per day at ~20:30 UTC.
    """
    log.info("EWZ breakout --loop mode started. Signal hour UTC: %d", PARAMS["signal_hour_utc"])
    while True:
        try:
            state = _load_state()
            _write_heartbeat(state, _last_eval_ts_from_heartbeat())
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=PARAMS["signal_hour_utc"], minute=30, second=0, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            # Skip Saturday/Sunday evals (US market closed; would just no-op)
            while fire.weekday() >= 5:
                fire += timedelta(days=1)
            sleep_s = max(5, (fire - now).total_seconds())
            log.info("Next eval at %s UTC (sleep %.0fs)", fire.isoformat(), sleep_s)
            remaining = sleep_s
            while remaining > 0:
                chunk = min(300.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
                state = _load_state()
                _write_heartbeat(state, _last_eval_ts_from_heartbeat())
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
    g.add_argument("--loop", action="store_true", help="Continuous: wake at daily signal hour")
    p.add_argument("--period", default="5y")
    p.add_argument("--signal-only", action="store_true",
                   help="Skip IBKR submission -- yfinance-replay simulation path")
    args = p.parse_args()
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")
    if args.backtest:
        backtest(args.period)
        return
    from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
    try:
        with acquire_runner_lock("forge_ewz_breakout"):
            if args.evaluate:
                evaluate_once()
            elif args.scan:
                scan()
            elif args.loop:
                loop_mode()
    except RunnerAlreadyRunning as exc:
        log.error("REFUSING_DUPLICATE_RUNNER: %s", exc)
        sys.exit(75)


if __name__ == "__main__":
    main()
