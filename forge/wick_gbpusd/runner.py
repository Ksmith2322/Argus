"""Wick GBPUSD daily runner — paper-mode signal/trade tracker.

Strategy: see STRATEGY_SPEC.md.

Modes:
  --backtest       Run on cached daily history, print stats
  --scan           One-shot: print today's signal status (no trade)
  --evaluate       One daily cycle: check for signal, manage open paper positions
  --loop           Daily eval loop (sleeps 1hr between checks; only acts on close)

Designed to run as a daily scheduled task (e.g. via run_cohort_report.ps1).
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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from argus_flow.sizing import fx_notional_per_unit_usd
from helio.fleet_sizing import compute_risk_usd, max_notional_usd, pnl_pct_of_fleet

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "wick_gbpusd"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = setup_logging("wick_gbpusd")

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr  # noqa: E402
IBKR_CLIENT_ID = 112
_SIGNAL_ONLY_MODE = False

# Strategy parameters (frozen — change requires version bump in STRATEGY_SPEC.md)
# Loaded via registry with hardcoded fallback (Phase 2 pattern — see
# forge/gld_pm_long/runner.py for the template).
_HARDCODED_DEFAULTS = {
    "version": "v1",
    "symbol": "GBPUSD",
    "timeframe": "1d",
    "uw_min": 0.6,
    "cp_max": 0.3,
    "bb_width_quantile_max": 0.33,  # below 33rd percentile
    "chop_quantile_min": 0.67,  # above 67th percentile
    "regime_window": 60,  # bars for quantile computation
    "target_atr": 2.0,
    "stop_atr": 1.0,
    "hold_bars": 20,
    "atr_period": 14,
    # risk_pct + model_equity_usd removed 2026-04-17 — sourced from
    # helio.fleet_sizing tier system (strategy_label="forge_wick_gbpusd").
}


def _load_params_from_registry() -> dict:
    """Pull from config/strategies.json with fallback. See
    forge/gld_pm_long/runner.py for the full rationale."""
    try:
        from helio.strategy_registry import load_registry
        reg = load_registry().wick_gbpusd
        return {
            "version": _HARDCODED_DEFAULTS["version"],
            "symbol": reg.symbol,
            "timeframe": reg.timeframe,
            "uw_min": reg.uw_min,
            "cp_max": reg.cp_max,
            "bb_width_quantile_max": reg.bb_width_quantile_max,
            "chop_quantile_min": reg.chop_quantile_min,
            "regime_window": reg.regime_window,
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


# ────────────── Indicators ──────────────

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    rng = (h - l).replace(0, np.nan)
    f["upper_wick_pct"] = (h - np.maximum(o, c)) / rng
    f["close_pos_in_range"] = (c - l) / rng
    f["atr"] = atr(df, PARAMS["atr_period"])

    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    f["bb_width_pct"] = (4 * bb_std) / bb_mid

    atr_sum = f["atr"].rolling(14).sum()
    h14 = h.rolling(14).max()
    l14 = l.rolling(14).min()
    f["choppiness_14"] = 100 * np.log10(atr_sum / (h14 - l14).replace(0, np.nan)) / np.log10(14)

    # Rolling quantiles for regime filters (avoids look-ahead)
    win = PARAMS["regime_window"]
    f["bb_width_q33"] = f["bb_width_pct"].rolling(win, min_periods=win).quantile(PARAMS["bb_width_quantile_max"])
    f["chop_q67"] = f["choppiness_14"].rolling(win, min_periods=win).quantile(PARAMS["chop_quantile_min"])
    return f


def signal_long(df: pd.DataFrame, feats: pd.DataFrame) -> pd.Series:
    return (
        (feats["upper_wick_pct"] > PARAMS["uw_min"])
        & (feats["close_pos_in_range"] < PARAMS["cp_max"])
        & (feats["bb_width_pct"] < feats["bb_width_q33"])
        & (feats["choppiness_14"] > feats["chop_q67"])
        & feats["atr"].notna()
        & (feats["atr"] > 0)
    )


# ────────────── Cohort tagging ──────────────

def _config_hash() -> str:
    # Delegates to shared helper (2026-04-19 migration).
    from helio.strategy_common import config_hash
    return config_hash(PARAMS)


def _git_sha() -> str:
    from helio.strategy_common import git_sha
    return git_sha(REPO)


# ────────────── Persistence ──────────────

TRADE_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pips", "exit_reason",
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
        "open_trade": None,
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _write_heartbeat(state: dict, last_signal_ts: str | None, today_evaluated: bool) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "wick_gbpusd",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_signal_ts": last_signal_ts,
        "today_evaluated": today_evaluated,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str))


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _append_signal(ts, action, features_row) -> None:
    is_new = not SIGNALS_PATH.exists()
    cols = ["ts", "action", "upper_wick_pct", "close_pos_in_range", "bb_width_pct",
            "choppiness_14", "atr", "config_hash"]
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(cols)
        w.writerow([ts, action,
                    features_row.get("upper_wick_pct"),
                    features_row.get("close_pos_in_range"),
                    features_row.get("bb_width_pct"),
                    features_row.get("choppiness_14"),
                    features_row.get("atr"),
                    _config_hash()])


# ────────────── Data fetching ──────────────

def fetch_history(period: str = "180d") -> pd.DataFrame:
    """Pull GBPUSD daily bars via yfinance (last 180 days = ~120 trading days
    — enough for the 60-bar regime quantile + 20-bar ATR + signal evaluation)."""
    df = yf.download("GBPUSD=X", period=period, interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no GBPUSD data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# ────────────── Position management ──────────────

def _check_exit(open_trade: dict, today_bar: dict, today_idx: int, entry_idx: int | None = None) -> tuple[str, float] | None:
    """Returns (reason, exit_px) if exit triggered today, else None.
    entry_idx must be passed (resolved fresh from entry_ts) to compute
    bars_held accurately after the 2026-04-23 phantom-close fix."""
    # Back-compat: if caller didn't pass entry_idx, fall back to stored value
    if entry_idx is None:
        entry_idx = open_trade.get("entry_idx", today_idx)
    bars_held = today_idx - entry_idx
    if today_bar["Low"] <= open_trade["stop_px"]:
        return "stop", open_trade["stop_px"]
    if today_bar["High"] >= open_trade["target_px"]:
        return "target", open_trade["target_px"]
    if bars_held >= PARAMS["hold_bars"]:
        return "time", today_bar["Close"]
    return None


def _open_paper_trade(state: dict, df: pd.DataFrame, feats: pd.DataFrame, signal_idx: int, ib=None) -> None:
    """Signal fired at bar `signal_idx`. With real IBKR execution: submit market
    BUY immediately + bracket. Without (signal-only fallback): mark as pending-fill
    and settle next cycle (legacy behavior)."""
    a = float(feats["atr"].iloc[signal_idx])
    entry_anchor = float(df["Close"].iloc[signal_idx])  # planned entry
    target = entry_anchor + PARAMS["target_atr"] * a
    stop = entry_anchor - PARAMS["stop_atr"] * a
    risk_budget_usd = compute_risk_usd(strategy_label="forge_wick_gbpusd")
    # 2026-05-07 audit P3: safe_position_size + ATR floor. GBPUSD pip = 0.0001.
    from helio.strategy_common import safe_position_size
    pos_size, sizing_policy = safe_position_size(
        risk_usd=risk_budget_usd,
        entry_px=entry_anchor,
        stop_px=stop,
        atr=a,
        sizing_floor_atr_mult=1.0,
        abs_floor_per_unit=0.0001,
        point_value_usd=1.0,
    )
    stop_distance_pips = (entry_anchor - stop) / 0.0001  # kept for downstream risk_usd record
    notional_per_unit = fx_notional_per_unit_usd("GBPUSD", quote_price=entry_anchor)
    cap_units = int(max_notional_usd("fx") / max(notional_per_unit, 1e-9)) if entry_anchor > 0 else pos_size
    if cap_units > 0 and pos_size > cap_units:
        log.warning("NOTIONAL_CAP: GBPUSD units %d > cap %d", pos_size, cap_units)
        pos_size = cap_units
    if pos_size <= 0:
        log.warning("SIZE_ZERO: skipping entry (policy=%s)", sizing_policy)
        return

    entry_px = entry_anchor
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"
    pending_fill = True  # legacy default for signal-only path

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract("GBPUSD", "fx")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: GBPUSD qty=%s, skipping", existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction="long", size=pos_size,
                stop_px=stop, target_px=target, price_decimals=5,
                est_entry_px=entry_anchor,
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
            pending_fill = False  # real fill landed, no need to settle next cycle
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            return

    # entry_ts: for real, set now; for legacy signal-only, point at next bar
    if execution_venue == "ibkr_paper":
        entry_ts = str(df.index[signal_idx])
    else:
        entry_ts = str(df.index[signal_idx + 1]) if signal_idx + 1 < len(df) else None

    state["open_trade"] = {
        "signal_ts": str(df.index[signal_idx]),
        "signal_close": entry_anchor,
        "entry_anchor": entry_anchor,
        "entry_px": entry_px if not pending_fill else None,
        "target_px": target,
        "stop_px": stop,
        "atr_entry": a,
        "entry_ts": entry_ts,
        "position_size": pos_size,
        "risk_usd": stop_distance_pips * pip_value * (pos_size / 100_000),
        "session_id": state["session_id"],
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
        "pending_fill": pending_fill,
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
    }
    log.info("%s LONG opened — signal at %s, entry %.5f, target %.5f stop %.5f atr %.5f size %d",
             execution_venue.upper(), df.index[signal_idx], entry_px, target, stop, a, pos_size)


def _resolve_idx_from_ts(df: pd.DataFrame, ts_str: str | None) -> int | None:
    """Resolve a timestamp string to its current integer position in df.
    Returns None if the ts isn't in df (e.g., it's in the future or df was
    rolled). This is the phantom-close-bug fix — never trust a stale
    integer index across runs."""
    if not ts_str:
        return None
    try:
        ts = pd.Timestamp(ts_str)
        if ts in df.index:
            return int(df.index.get_loc(ts))
    except Exception:
        pass
    return None


def _settle_pending_fill(state: dict, df: pd.DataFrame) -> None:
    """The day after signal, fill at the open of that day."""
    ot = state["open_trade"]
    if not ot or not ot.get("pending_fill"):
        return
    # Back-compat: older state.json may have entry_idx (integer). Prefer entry_ts
    # when present; fall back to legacy entry_idx only if ts isn't available.
    entry_idx = _resolve_idx_from_ts(df, ot.get("entry_ts"))
    if entry_idx is None:
        entry_idx = ot.get("entry_idx")  # legacy fallback (buggy but better than nothing)
        if entry_idx is None or entry_idx >= len(df):
            return
    if entry_idx >= len(df):
        return
    actual_entry = float(df["Open"].iloc[entry_idx])
    a = ot["atr_entry"]
    ot["entry_px"] = actual_entry
    ot["target_px"] = actual_entry + PARAMS["target_atr"] * a
    ot["stop_px"] = actual_entry - PARAMS["stop_atr"] * a
    # Update entry_ts now that we've resolved it (in case it was empty on signal-day)
    if not ot.get("entry_ts"):
        ot["entry_ts"] = str(df.index[entry_idx])
    ot["pending_fill"] = False
    log.info("PAPER LONG filled at %.5f — target %.5f stop %.5f", actual_entry, ot["target_px"], ot["stop_px"])


def _close_paper_trade(state: dict, df: pd.DataFrame, exit_idx: int, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    entry_px = ot["entry_px"]
    pnl_pips = (exit_px - entry_px) / 0.0001
    pnl_usd = pnl_pips * 10.0 * (ot["position_size"] / 100_000)
    state["trade_count"] += 1
    # Resolve entry idx fresh from entry_ts (phantom-close-bug fix)
    entry_idx = _resolve_idx_from_ts(df, ot.get("entry_ts")) or ot.get("entry_idx") or 0
    entry_ts_str = ot.get("entry_ts") or str(df.index[entry_idx])
    duration_min = (df.index[exit_idx] - pd.Timestamp(entry_ts_str)).total_seconds() / 60
    _append_trade({
        "ts": entry_ts_str,
        "direction": "long",
        "entry_px": entry_px,
        "exit_px": exit_px,
        "pnl_pips": round(pnl_pips, 1),
        "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct_of_fleet": round(pnl_pct_of_fleet(pnl_usd), 4),
        "position_size": ot["position_size"],
        "risk_usd": ot["risk_usd"],
        "risk_pct_of_fleet": round(__import__("helio.fleet_sizing", fromlist=["get_effective_risk_pct"]).get_effective_risk_pct("forge_wick_gbpusd")["risk_pct"] * 100, 3),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "range",
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
    log.info("PAPER LONG closed (%s) at %.5f — pnl %+.1f pips ($%+.2f)", reason, exit_px, pnl_pips, pnl_usd)

    # Dual-write to canonical fills (additive — same pattern as gld_pm_long).
    # Runs after the strategy-local CSV is written. Internal write_fill_typed
    # try/except means a broken canonical log cannot break paper trading.
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_wick_gbpusd",
            symbol="GBPUSD",
            direction="long",
            side="EXIT",
            entry_ts=entry_ts_str,
            exit_ts=str(df.index[exit_idx]),
            entry_px=float(entry_px),
            exit_px=float(exit_px),
            size=float(ot["position_size"]),
            risk_usd=float(ot.get("risk_usd") or 0.0),
            pnl_usd=round(pnl_usd, 2),
            exit_reason=reason,
        ))
    except Exception:
        pass  # never let the canonical log break paper trading

    state["open_trade"] = None


# ────────────── Modes ──────────────

def evaluate_once() -> None:
    log.info("Wick GBPUSD daily evaluation starting...")
    df = fetch_history()
    feats = compute_features(df)
    state = _load_state()
    today_idx = len(df) - 1
    today_ts = df.index[today_idx]

    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
        # Settle any pending fill from yesterday's signal (legacy signal-only path)
        _settle_pending_fill(state, df)

        # Manage open position
        if state.get("open_trade") and not state["open_trade"].get("pending_fill"):
            ot = state["open_trade"]
            if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                contract = ibkr.make_contract("GBPUSD", "fx")
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
                    # Bracket filled — record closure (need a bar idx for compatibility)
                    _close_paper_trade(state, df, today_idx, outcome["fill_price"], outcome["reason"])
                else:
                    # Still open — check time stop via legacy entry_idx logic
                    entry_idx = _resolve_idx_from_ts(df, ot.get("entry_ts"))
                    if entry_idx is not None and (today_idx - entry_idx) >= PARAMS.get("hold_days", 5):
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
                                _close_paper_trade(state, df, today_idx, fill.fill_price, "time")
                            else:
                                log.error("TIME close failed: %s", fill.reject_reason)
            else:
                # Signal-only fallback: legacy bar replay
                entry_idx = _resolve_idx_from_ts(df, ot.get("entry_ts"))
                if entry_idx is None:
                    entry_idx = ot.get("entry_idx")
                exit_event = None
                if entry_idx is not None and entry_idx < len(df):
                    for j in range(entry_idx, today_idx + 1):
                        bar = df.iloc[j].to_dict()
                        ev = _check_exit(ot, bar, j, entry_idx=entry_idx)
                        if ev:
                            exit_event = (j, ev[0], ev[1])
                            break
                if exit_event:
                    j, reason, exit_px = exit_event
                    _close_paper_trade(state, df, j, exit_px, reason)

        # New signal evaluation only if flat
        last_signal_ts = None
        today_evaluated = True
        if state.get("open_trade") is None:
            sig = signal_long(df, feats)
            if bool(sig.iloc[today_idx]):
                log.info("SIGNAL fired today (%s)", today_ts)
                _open_paper_trade(state, df, feats, today_idx, ib=ib)
                last_signal_ts = str(today_ts)
                _append_signal(today_ts, "ENTRY_LONG", feats.iloc[today_idx].to_dict())
            else:
                _append_signal(today_ts, "NO_TRIGGER", feats.iloc[today_idx].to_dict())

        _save_state(state)
        _write_heartbeat(state, last_signal_ts, today_evaluated)
        log.info("Cycle complete. open=%s trades_total=%d",
                 "yes" if state.get("open_trade") else "no", state.get("trade_count", 0))
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def scan() -> None:
    """Read-only: show today's evaluation without trading."""
    df = fetch_history()
    feats = compute_features(df)
    today_idx = len(df) - 1
    sig = signal_long(df, feats)
    print(f"GBPUSD daily — bar {df.index[today_idx]}")
    print(f"  Close: {df['Close'].iloc[today_idx]:.5f}")
    print(f"  upper_wick_pct: {feats['upper_wick_pct'].iloc[today_idx]:.3f} (need > {PARAMS['uw_min']})")
    print(f"  close_pos_in_range: {feats['close_pos_in_range'].iloc[today_idx]:.3f} (need < {PARAMS['cp_max']})")
    print(f"  bb_width_pct: {feats['bb_width_pct'].iloc[today_idx]:.5f} (need < {feats['bb_width_q33'].iloc[today_idx]:.5f})")
    print(f"  choppiness_14: {feats['choppiness_14'].iloc[today_idx]:.2f} (need > {feats['chop_q67'].iloc[today_idx]:.2f})")
    print(f"  ATR: {feats['atr'].iloc[today_idx]:.5f}")
    print(f"  SIGNAL: {'LONG' if bool(sig.iloc[today_idx]) else 'no'}")


def backtest(period: str = "5y") -> None:
    df = yf.download("GBPUSD=X", period=period, interval="1d", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    feats = compute_features(df)
    sig = signal_long(df, feats).values
    H, L, C, O = df["High"].values, df["Low"].values, df["Close"].values, df["Open"].values
    a_arr = feats["atr"].values

    trades = []
    for i in np.where(sig)[0]:
        if i + 1 >= len(df):
            continue
        a = a_arr[i]
        entry = O[i + 1]
        target = entry + PARAMS["target_atr"] * a
        stop = entry - PARAMS["stop_atr"] * a
        exit_px = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if L[j] <= stop:
                exit_px = stop; break
            if H[j] >= target:
                exit_px = target; break
        if exit_px is None:
            exit_px = C[min(i + PARAMS["hold_bars"], len(df) - 1)]
        trades.append({"date": df.index[i], "entry": entry, "exit": exit_px, "pnl_atr": (exit_px - entry) / a})
    td = pd.DataFrame(trades)
    if td.empty:
        print("No trades.")
        return
    wr = (td.pnl_atr > 0).mean() * 100
    pf = td[td.pnl_atr > 0].pnl_atr.sum() / abs(td[td.pnl_atr <= 0].pnl_atr.sum() or 1)
    print(f"Trades: {len(td)} | WR: {wr:.1f}% | PF: {pf:.2f} | Exp: {td.pnl_atr.mean():+.4f} ATR/trade")
    print(f"Period: {td.date.min()} -> {td.date.max()}")


def main_loop() -> int:
    """Daemon mode — evaluate_once() per cycle. Added 2026-04-23; pairs with
    phantom-close fix above. Prior to these two changes, wick_gbpusd had
    0 trades in 5 months because (a) nothing was running continuously and
    (b) the stale-entry-idx bug invalidated the few trades that did happen."""
    import time
    interval_s = 3600  # once/hour is plenty for a daily-bar strategy
    log.info("wick_gbpusd --loop started (interval=%ds)", interval_s)
    cycle = 0
    while True:
        cycle += 1
        try:
            evaluate_once()
            log.info("cycle %d complete", cycle)
        except Exception as e:
            log.error("cycle %d FAILED: %s", cycle, e)
        time.sleep(interval_s)


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true", help="One daily eval cycle")
    g.add_argument("--scan", action="store_true", help="Read-only: print today's status")
    g.add_argument("--backtest", action="store_true", help="Run backtest on cached/recent data")
    g.add_argument("--loop", action="store_true", help="Continuous daemon: re-evaluate every hour")
    p.add_argument("--period", default="5y", help="Backtest period (default 5y)")
    p.add_argument("--signal-only", action="store_true",
                   help="Skip IBKR submission — keep legacy pending_fill simulation path")
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
        main_loop()


if __name__ == "__main__":
    main()
