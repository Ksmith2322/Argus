"""USO PM Long — hourly intraday runner.

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
from helio.blocker_ledger import record_blocker
from helio.fleet_sizing import compute_risk_usd, max_notional_usd, pnl_pct_of_fleet
from helio import ibkr_execution as ibkr

# 2026-04-24: unique client ID in the forge range (100-199). Must not collide
# with other forge runners (gdx_gld=101).
IBKR_CLIENT_ID = 131
# Set to True via --signal-only CLI flag to skip real-order submission and
# fall back to yfinance-based simulation (for testing/debugging).
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "uso_pm_long"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("uso_pm_long")

_HARDCODED_DEFAULTS = {
    "version": "v1",
    "symbol": "USO",
    "timeframe": "1h",
    "signal_hours_utc": [18, 19, 20],
    "target_atr": 1.0,
    "stop_atr": 0.5,
    "hold_bars": 4,
    "atr_period": 14,
    # risk_pct + model_equity_usd both removed 2026-04-17:
    # sizing now comes from helio.fleet_sizing via tier system (risk_pct
    # auto-sets from measured live performance). See strategy_label
    # "forge_uso_pm_long" in fleet_sizing.json tiers config.
}


def _load_params_from_registry() -> dict:
    """Phase 2 migration: pull edge thresholds from config/strategies.json
    when available. Falls back to _HARDCODED_DEFAULTS on any failure — a
    broken registry cannot break the runner. Phase 1 equivalence test
    (test_registry_equivalence) guarantees the registry values match the
    hardcoded defaults, so this is a no-op behavior change today. The win
    is that future threshold edits happen in ONE file (strategies.json)
    instead of two (strategies.json + runner.py)."""
    try:
        from helio.strategy_registry import load_registry
        reg = load_registry().uso_pm_long
        return {
            "version": _HARDCODED_DEFAULTS["version"],
            "symbol": reg.symbol,
            "timeframe": reg.timeframe,
            "signal_hours_utc": list(reg.signal_hours_utc),
            "target_atr": reg.target_atr,
            "stop_atr": reg.stop_atr,
            "hold_bars": reg.hold_bars,
            "atr_period": reg.atr_period,
        }
    except Exception:
        return dict(_HARDCODED_DEFAULTS)


PARAMS = _load_params_from_registry()

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
    # Delegates to shared helper (2026-04-19 migration). Shape-identical
    # output to the prior local impl; covered by
    # test_strategy_common.TestConfigHash.test_matches_existing_runner_implementations
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
        "system": "uso_pm_long",
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


def _append_signal(ts, action, hour, atr_val) -> None:
    is_new = not SIGNALS_PATH.exists()
    cols = ["ts", "action", "hour_utc", "atr", "config_hash"]
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(cols)
        w.writerow([ts, action, hour, atr_val, _config_hash()])


def fetch_history(period: str = "60d") -> pd.DataFrame:
    df = yf.download("USO", period=period, interval="1h", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no USO data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def evaluate_once() -> None:
    log.info("USO PM Long evaluation starting...")
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    state = _load_state()
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]

    # Open an IBKR connection for this cycle. If it fails (TWS down etc.), we
    # fall through to signal-only mode — still evaluate signals, still update
    # state/ledger via the yfinance replay path, but don't submit real orders.
    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
            log.info("IBKR connected")
        except Exception as exc:
            log.warning("IBKR connect failed, falling back to signal-only: %s", exc)
            ib = None

    try:
        # --- Manage open position -----------------------------------------
        if state.get("open_trade"):
            ot = state["open_trade"]

            # 2026-05-23 self-heal: stale signal_only open_trade from a prior
            # connect-fail wake should NOT block real entries forever. If we
            # now have a real IBKR connection and broker confirms flat,
            # clear the phantom state so the signal evaluator can run.
            if (ib is not None
                and ot.get("execution_venue") == "signal_only"):
                try:
                    _check_contract = ibkr.make_contract("USO", "etf")
                    ib.qualifyContracts(_check_contract)
                    _broker_qty = ibkr.query_position(ib, _check_contract)
                except Exception as exc:
                    log.warning("self-heal broker-position check failed: %s", exc)
                    _broker_qty = None
                if _broker_qty == 0:
                    log.warning(
                        "STALE_SIGNAL_ONLY: clearing phantom open_trade "
                        "(entry_ts=%s, signal-only entry never had real "
                        "broker position) so live signals can resume",
                        ot.get("entry_ts"),
                    )
                    state["open_trade"] = None
                    _save_state(state)
                    ot = None  # fall through to signal evaluation below

            if ot is None:
                pass
            elif ib is not None and ot.get("execution_venue") == "ibkr_paper":
                # Real-order path: ask IBKR whether bracket stop/target filled
                # between cycles, or whether we're still open.
                contract = ibkr.make_contract("USO", "etf")
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
                    # Stop or target hit while we were asleep — record closure
                    _close(state, outcome["fill_ts"], outcome["fill_price"], outcome["reason"])
                else:
                    # Still open — count bars and enforce time-stop
                    entry_idx_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                    forward = df[df.index > entry_idx_ts]
                    bars_held = len(forward)
                    ot["bars_held"] = bars_held
                    if bars_held >= PARAMS["hold_bars"]:
                        log.info("TIME_STOP: %d bars held, closing at market", bars_held)
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
                            # Broker shows flat but we thought we were open —
                            # bracket likely filled but status check missed it.
                            # Record closure at last bar close as best-effort.
                            last_row = df.iloc[last_idx]
                            _close(state, df.index[last_idx], float(last_row["Close"]), "reconcile_flat")
            else:
                # Signal-only fallback: replay yfinance bars like before
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

        # --- Signal evaluation --------------------------------------------
        sig_action = "NO_TRIGGER"
        if state.get("open_trade") is None:
            now_utc = datetime.now(timezone.utc)
            # Don't open new positions when US equity market is closed (weekends).
            # Without this guard, --loop wakes hourly and submits orders that IBKR
            # parks as PreSubmitted/Inactive, which the runner then logs as failures.
            is_weekend = now_utc.weekday() >= 5
            if is_weekend:
                sig_action = "WEEKEND_SKIP"
            elif last_ts.hour in PARAMS["signal_hours_utc"]:
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
    """Open a LONG position. If `ib` is provided, submits real orders to IBKR;
    otherwise records a signal-only entry using the bar close price."""
    plan_entry = float(df["Close"].iloc[idx])  # planned entry for sizing (refined to real fill below)
    target = plan_entry + PARAMS["target_atr"] * a
    stop = plan_entry - PARAMS["stop_atr"] * a
    risk_budget_usd = compute_risk_usd(strategy_label="forge_uso_pm_long")
    # 2026-05-07 audit: same sizing-formula fix as multi_orb / vix_intraday.
    # ATR-based sizing floor prevents tight-stop explosion.
    cap_usd = max_notional_usd("etf", strategy_label="forge_uso_pm_long") if plan_entry > 0 else None
    from helio.strategy_common import safe_position_size
    pos_size, sizing_policy = safe_position_size(
        risk_usd=risk_budget_usd,
        entry_px=plan_entry,
        stop_px=stop,
        atr=a,
        sizing_floor_atr_mult=1.0,
        max_notional_usd=cap_usd if cap_usd and cap_usd > 0 else None,
        point_value_usd=1.0,
    )
    if pos_size <= 0:
        log.warning("SIZE_ZERO: computed size <= 0, skipping entry (policy=%s)", sizing_policy)
        record_blocker(
            "forge_uso_pm_long",
            "SIZE_ZERO",
            symbol="USO",
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
        # Real execution: market entry + protective stop + target limit
        contract = ibkr.make_contract("USO", "etf")
        try:
            ib.qualifyContracts(contract)
            # Guard: don't double up if broker already shows a position
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: GLD qty=%s, skipping entry to avoid doubling", existing)
                record_blocker(
                    "forge_uso_pm_long",
                    "BROKER_POSITION_EXISTS",
                    symbol="USO",
                    stage="entry",
                    action="BUY",
                    qty=pos_size,
                    notional_usd=pos_size * plan_entry,
                    reason=f"broker existing qty={existing}",
                )
                return
            result = ibkr.submit_bracket(
                ib, contract, direction="long", size=pos_size,
                stop_px=stop, target_px=target, price_decimals=2,
                est_entry_px=plan_entry,
                strategy_label="forge_uso_pm_long",
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
                    "forge_uso_pm_long",
                    code,
                    symbol="USO",
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
            # Recompute stop/target relative to the REAL fill price, not the plan
            target = entry_px + PARAMS["target_atr"] * a
            stop = entry_px - PARAMS["stop_atr"] * a
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            record_blocker(
                "forge_uso_pm_long",
                "ENTRY_EXCEPTION",
                symbol="USO",
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
        "bars_held": 0,
    }
    log.info("%s LONG opened @ %.2f target %.2f stop %.2f atr %.4f size %d hour %d UTC",
             execution_venue.upper(), entry_px, target, stop, a, pos_size, df.index[idx].hour)


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    """Record a closed trade. exit_px should be the REAL IBKR fill when available
    (bracket hit, market close fill) — caller is responsible for passing the
    actual fill price rather than an inferred yfinance OHLC value."""
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
            get_effective_risk_pct("forge_uso_pm_long")["risk_pct"] * 100,
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

    # Dual-write to canonical fills so the fleet-wide log sees trades as
    # they close, not only after the nightly backfill. Uses write_fill_typed
    # (helio.domain.Fill path). write_fill_typed is wrapped in its own
    # try/except — a broken canonical log cannot break paper trading here.
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_uso_pm_long",
            symbol="USO",
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
        pass  # never let the canonical log break paper trading

    state["open_trade"] = None


def scan() -> None:
    df = fetch_history()
    a_series = atr(df, PARAMS["atr_period"])
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    print(f"USO 1h — bar {last_ts}")
    print(f"  Close: {df['Close'].iloc[last_idx]:.2f}")
    print(f"  ATR(14): {a_series.iloc[last_idx]:.4f}")
    print(f"  Hour UTC: {last_ts.hour}")
    print(f"  Signal hours: {PARAMS['signal_hours_utc']}")
    print(f"  Would trigger: {'LONG' if last_ts.hour in PARAMS['signal_hours_utc'] else 'no (wrong hour)'}")


def backtest(period: str = "2y") -> None:
    df = yf.download("USO", period=period, interval="1h", progress=False, auto_adjust=False)
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
    log.info("USO PM Long --loop mode started. Signal hours UTC: %s", PARAMS["signal_hours_utc"])
    while True:
        try:
            state = _load_state()
            _write_heartbeat(state, _last_eval_ts_from_heartbeat())
            now = datetime.now(timezone.utc)
            # Find next firing time: top of next signal hour + 30s
            candidates = []
            for h in PARAMS["signal_hours_utc"]:
                fire = now.replace(hour=h, minute=0, second=30, microsecond=0)
                if fire <= now:
                    fire += timedelta(days=1)
                candidates.append(fire)
            # Also wake daily to manage open positions through stop/target hits even
            # outside signal hours (a USO position opened at 20:00 might exit at 21:00)
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
    g.add_argument("--loop", action="store_true", help="Continuous: wake at top of signal hours")
    p.add_argument("--period", default="2y")
    p.add_argument("--signal-only", action="store_true",
                   help="Skip IBKR submission — keep the old yfinance-replay simulation path")
    args = p.parse_args()
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")
    if args.backtest:
        backtest(args.period)
        return
    # Lock is only meaningful for live/loop modes — backtests are
    # one-shot offline computations.
    from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
    try:
        with acquire_runner_lock("forge_uso_pm_long"):
            if args.evaluate:
                evaluate_once()
            elif args.scan:
                scan()
            elif args.loop:
                loop_mode()
    except RunnerAlreadyRunning as exc:
        log.error("REFUSING_DUPLICATE_RUNNER: %s", exc)
        sys.exit(75)  # EX_TEMPFAIL


if __name__ == "__main__":
    main()
