"""forge_vix_intraday — 15m UVXY mean-reversion during NY session.

Adds volatility as a distinct asset class. UVXY is a leveraged vol ETN
that mean-reverts strongly on short timeframes once a spike subsides.

Hypothesis: RSI(2) > 85 on 15m UVXY during NY session → short the pop.
Rare but high-conviction; 2-5 setups per week.

Usage:
    python -m forge.vix_intraday.runner --evaluate
    python -m forge.vix_intraday.runner --loop
    python -m forge.vix_intraday.runner --backtest --period 60d
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from forge.logging_setup import setup_logging  # noqa: E402

log = setup_logging("vix_intraday")

RESEARCH_ONLY = True  # ship after backtest validates

LOG_DIR = REPO / "forge" / "logs" / "vix_intraday"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = LOG_DIR / "state.json"
TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr
IBKR_CLIENT_ID = 106
_SIGNAL_ONLY_MODE = False

PARAMS = {
    "version": "v1",
    "ticker": "UVXY",
    "timeframe": "15m",
    "session_start_utc": 14,
    "session_end_utc": 20,
    "rsi_period": 2,
    "rsi_short_threshold": 85,   # short on pop
    "rsi_long_threshold": 10,    # long on washout (rare)
    "atr_period": 14,
    "min_atr_pct": 0.005,        # UVXY is volatile; require real move
    "hold_bars": 4,              # 1 hour
    "target_atr_mult": 0.6,
    "stop_atr_mult": 0.5,
    "risk_pct_default": 0.003,
}

TRADE_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pct", "pnl_usd",
    "exit_reason", "duration_min", "trade_num", "position_size", "risk_usd",
    "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "rsi_entry", "stop_px", "target_px",
]


def _ensure_csv():
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _config_hash() -> str:
    from helio.strategy_common import config_hash
    return config_hash(PARAMS)


def _git_sha() -> str:
    from helio.strategy_common import git_sha
    return git_sha(REPO)


def _load_state() -> dict:
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception: pass
    return {"open_trade": None, "trade_count": 0,
            "session_id": os.urandom(4).hex(), "runtime_start": time.time()}


def _save_state(s): STATE_PATH.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(s, last_ts: str) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "vix_intraday", "family": "forge", "mode": "paper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_count": s.get("trade_count", 0), "open_trade": s.get("open_trade"),
        "last_signal_ts": last_ts, "config_hash": _config_hash(), "git_sha": _git_sha(),
    }, indent=2, default=str), encoding="utf-8")


def _append_signal(ts, action, rsi_val, atr_val):
    first = not SIGNALS_PATH.exists()
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first: w.writerow(["ts", "action", "rsi_2", "atr", "hour_utc"])
        w.writerow([str(ts), action, f"{rsi_val:.2f}", f"{atr_val:.4f}", pd.to_datetime(ts).hour])


def _append_trade(row):
    _ensure_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).rolling(n, min_periods=n).mean()
    dn = (-d.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(df, n):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def fetch_history(period="10d"):
    df = yf.download(PARAMS["ticker"], period=period, interval="15m",
                     progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def signal_check(df):
    i = len(df) - 1
    ts = df.index[i]
    if ts.hour < PARAMS["session_start_utc"] or ts.hour >= PARAMS["session_end_utc"]:
        return "none", {"reason": f"outside_session_{ts.hour}"}
    rsi = _rsi(df["Close"], PARAMS["rsi_period"])
    atr_s = _atr(df, PARAMS["atr_period"])
    r = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else None
    a = float(atr_s.iloc[i]) if not pd.isna(atr_s.iloc[i]) else None
    close = float(df["Close"].iloc[i])
    if r is None or a is None or a <= 0:
        return "none", {"reason": "indicator_nan"}
    if a / close < PARAMS["min_atr_pct"]:
        return "none", {"reason": "atr_too_small"}
    if r > PARAMS["rsi_short_threshold"]:
        return "short", {"entry": close, "atr": a, "rsi": r}
    if r < PARAMS["rsi_long_threshold"]:
        return "long", {"entry": close, "atr": a, "rsi": r}
    return "none", {"reason": f"rsi_neutral_{r:.1f}"}


def _open(state, df, direction, ctx, ib=None):
    plan_entry = ctx["entry"]; a = ctx["atr"]
    if direction == "long":
        target = plan_entry + PARAMS["target_atr_mult"] * a
        stop = plan_entry - PARAMS["stop_atr_mult"] * a
    else:
        target = plan_entry - PARAMS["target_atr_mult"] * a
        stop = plan_entry + PARAMS["stop_atr_mult"] * a
    try:
        from helio.fleet_sizing import compute_risk_usd, max_notional_usd
        risk_budget = compute_risk_usd(strategy_label="forge_vix_intraday")
    except Exception:
        risk_budget = 300.0
    stop_dollars = abs(plan_entry - stop)
    shares = max(1, int(risk_budget / max(stop_dollars, 0.01)))
    try:
        cap = max_notional_usd("stock")
        if cap > 0 and plan_entry * shares > cap:
            shares = max(1, int(cap / max(plan_entry, 1e-6)))
    except Exception:
        pass
    if shares <= 0:
        log.warning("SIZE_ZERO: skipping entry")
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract("UVXY", "stock")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: UVXY qty=%s, skipping", existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction=direction, size=shares,
                stop_px=stop, target_px=target, price_decimals=2,
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED: %s", result.entry.reject_reason)
                return
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            if direction == "long":
                target = entry_px + PARAMS["target_atr_mult"] * a
                stop = entry_px - PARAMS["stop_atr_mult"] * a
            else:
                target = entry_px - PARAMS["target_atr_mult"] * a
                stop = entry_px + PARAMS["stop_atr_mult"] * a
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            return

    risk_usd = abs(entry_px - stop) * shares
    state["open_trade"] = {
        "entry_ts": str(df.index[-1]), "direction": direction,
        "entry_px": entry_px, "stop_px": stop, "target_px": target,
        "atr_entry": a, "rsi_entry": ctx.get("rsi", 0),
        "position_size": shares, "risk_usd": risk_usd,
        "bars_held": 0, "config_hash": _config_hash(),
        "git_sha": _git_sha(), "session_id": state["session_id"],
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
    }
    log.info("%s %s UVXY @ %.2f target %.2f stop %.2f rsi %.1f shares %d",
             execution_venue.upper(), direction.upper(), entry_px, target, stop,
             ctx.get("rsi", 0), shares)


def _close(state, exit_ts, exit_px, reason):
    ot = state["open_trade"]
    if ot["direction"] == "long":
        pnl_pct = (exit_px - ot["entry_px"]) / ot["entry_px"] * 100
    else:
        pnl_pct = (ot["entry_px"] - exit_px) / ot["entry_px"] * 100
    pnl_usd = pnl_pct / 100 * ot["entry_px"] * ot["position_size"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    _append_trade({
        "ts": ot["entry_ts"], "direction": ot["direction"],
        "entry_px": ot["entry_px"], "exit_px": exit_px,
        "pnl_pct": round(pnl_pct, 3), "pnl_usd": round(pnl_usd, 2),
        "exit_reason": reason, "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "position_size": ot["position_size"], "risk_usd": round(ot["risk_usd"], 2),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "ny_vol", "experiment_valid": "true",
        "invalid_reason": "", "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state.get("runtime_start", time.time())),
        "git_sha": ot["git_sha"], "atr_entry": ot["atr_entry"],
        "rsi_entry": ot["rsi_entry"], "stop_px": ot["stop_px"], "target_px": ot["target_px"],
    })
    log.info("PAPER %s UVXY closed (%s) @ %.2f — pnl %+.2f%% ($%+.2f)",
             ot["direction"].upper(), reason, exit_px, pnl_pct, pnl_usd)
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_vix_intraday", symbol="UVXY",
            direction=ot["direction"], side="EXIT",
            entry_ts=str(ot["entry_ts"]), exit_ts=str(exit_ts),
            entry_px=float(ot["entry_px"]), exit_px=float(exit_px),
            size=float(ot["position_size"]), risk_usd=float(ot["risk_usd"]),
            pnl_usd=round(pnl_usd, 2), exit_reason=reason,
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")
    state["open_trade"] = None


def evaluate_once():
    log.info("vix_intraday eval starting")
    df = fetch_history()
    state = _load_state()

    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
        if state.get("open_trade"):
            ot = state["open_trade"]
            if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                contract = ibkr.make_contract("UVXY", "stock")
                try:
                    ib.qualifyContracts(contract)
                    outcome = ibkr.check_bracket_filled(
                        ib, contract, ot.get("stop_order_id"), ot.get("target_order_id"),
                    )
                except Exception as exc:
                    log.error("bracket check failed: %s", exc)
                    outcome = None
                if outcome is not None:
                    _close(state, outcome["fill_ts"], outcome["fill_price"], outcome["reason"])
                else:
                    entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                    forward = df[df.index > entry_ts]
                    ot["bars_held"] = len(forward)
                    if ot["bars_held"] >= PARAMS["hold_bars"]:
                        try:
                            broker_qty = ibkr.query_position(ib, contract)
                        except Exception:
                            broker_qty = 0
                        if broker_qty != 0:
                            fill = ibkr.close_position_market(
                                ib, contract, direction=ot["direction"], size=abs(broker_qty),
                                stop_order_id=ot.get("stop_order_id"),
                                target_order_id=ot.get("target_order_id"),
                            )
                            if fill.filled:
                                _close(state, fill.fill_ts, fill.fill_price, "time")
                            else:
                                log.error("TIME_STOP close failed: %s", fill.reject_reason)
                        else:
                            _close(state, df.index[-1], float(df.iloc[-1]["Close"]), "reconcile_flat")
            else:
                entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                forward = df[df.index > entry_ts]
                for ts, row in forward.iterrows():
                    ot["bars_held"] = ot.get("bars_held", 0) + 1
                    hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
                    if ot["direction"] == "long":
                        if lo <= ot["stop_px"]: _close(state, ts, ot["stop_px"], "stop"); break
                        if hi >= ot["target_px"]: _close(state, ts, ot["target_px"], "target"); break
                    else:
                        if hi >= ot["stop_px"]: _close(state, ts, ot["stop_px"], "stop"); break
                        if lo <= ot["target_px"]: _close(state, ts, ot["target_px"], "target"); break
                    if ot["bars_held"] >= PARAMS["hold_bars"]:
                        _close(state, ts, cl, "time"); break

        last_ts = df.index[-1]
        if state.get("open_trade") is None:
            direction, ctx = signal_check(df)
            if direction in ("long", "short"):
                _open(state, df, direction, ctx, ib=ib)
                _append_signal(last_ts, f"ENTRY_{direction.upper()}", ctx.get("rsi", 0), ctx.get("atr", 0))
            else:
                _append_signal(last_ts, f"NO_TRIGGER_{ctx.get('reason', '?')}", ctx.get("rsi", 0) or 0, 0)
        _save_state(state)
        _write_heartbeat(state, str(last_ts))
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def loop_mode():
    log.info("vix_intraday loop starting")
    while True:
        try:
            now = datetime.now(timezone.utc)
            minutes_to_next = 15 - (now.minute % 15)
            next_fire = (now + timedelta(minutes=minutes_to_next)).replace(second=15, microsecond=0)
            sleep_s = max(10, (next_fire - now).total_seconds())
            log.info("next eval at %s (sleep %.0fs)", next_fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try: evaluate_once()
            except Exception as e:
                log.error("eval failed: %s", e, exc_info=True)
                time.sleep(60)
        except KeyboardInterrupt: return


def scan():
    df = fetch_history()
    rsi = _rsi(df["Close"], PARAMS["rsi_period"]).iloc[-1]
    a = _atr(df, PARAMS["atr_period"]).iloc[-1]
    print(f"UVXY 15m — {df.index[-1]}")
    print(f"  Close: {df['Close'].iloc[-1]:.2f}  RSI(2): {rsi:.2f}  ATR: {a:.4f}")
    direction, ctx = signal_check(df)
    print(f"  Signal: {direction} | {ctx}")


def backtest(period="60d"):
    df = fetch_history(period=period)
    rsi = _rsi(df["Close"], PARAMS["rsi_period"]).values
    atr_s = _atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    trades = []
    i = 0
    while i < len(df) - PARAMS["hold_bars"]:
        ts = df.index[i]
        if ts.hour < PARAMS["session_start_utc"] or ts.hour >= PARAMS["session_end_utc"]:
            i += 1; continue
        r, a = rsi[i], atr_s[i]
        if not (np.isfinite(r) and np.isfinite(a) and a > 0): i += 1; continue
        if a / C[i] < PARAMS["min_atr_pct"]: i += 1; continue
        direction = None
        if r > PARAMS["rsi_short_threshold"]: direction = "short"
        elif r < PARAMS["rsi_long_threshold"]: direction = "long"
        else: i += 1; continue
        entry = C[i]
        if direction == "long":
            target = entry + PARAMS["target_atr_mult"] * a
            stop = entry - PARAMS["stop_atr_mult"] * a
        else:
            target = entry - PARAMS["target_atr_mult"] * a
            stop = entry + PARAMS["stop_atr_mult"] * a
        exit_px = None; reason = "time"
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if direction == "long":
                if L[j] <= stop: exit_px = stop; reason = "stop"; break
                if H[j] >= target: exit_px = target; reason = "target"; break
            else:
                if H[j] >= stop: exit_px = stop; reason = "stop"; break
                if L[j] <= target: exit_px = target; reason = "target"; break
        if exit_px is None: exit_px = C[min(i + PARAMS["hold_bars"], len(df) - 1)]
        pnl_pct = (exit_px - entry) / entry * 100 if direction == "long" else (entry - exit_px) / entry * 100
        trades.append({"ts": ts, "direction": direction, "entry": entry, "exit": exit_px,
                       "pnl_pct": pnl_pct, "exit_reason": reason})
        i += PARAMS["hold_bars"] + 1
    if not trades:
        print(f"0 trades in {period}"); return
    td = pd.DataFrame(trades)
    wins = td[td["pnl_pct"] > 0]; losses = td[td["pnl_pct"] <= 0]
    pf = wins["pnl_pct"].sum() / abs(losses["pnl_pct"].sum()) if len(losses) and losses["pnl_pct"].sum() != 0 else float("inf")
    days = (td["ts"].iloc[-1] - td["ts"].iloc[0]).days or 1
    print(f"{len(td)} trades over {period} ({len(td) / max(days, 1) * 7:.1f}/week)")
    print(f"  WR: {len(wins)/len(td)*100:.1f}%  PF: {pf:.2f}")
    print(f"  Total %: {td['pnl_pct'].sum():+.2f}%  Exits: {td['exit_reason'].value_counts().to_dict()}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--backtest", action="store_true")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--signal-only", action="store_true",
                    help="Skip IBKR submission — keep the yfinance-replay simulation path")
    args = ap.parse_args()
    if args.signal_only:
        global _SIGNAL_ONLY_MODE
        _SIGNAL_ONLY_MODE = True
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")
    if args.evaluate: evaluate_once()
    elif args.loop: loop_mode()
    elif args.scan: scan()
    elif args.backtest: backtest(args.period)
    return 0


if __name__ == "__main__": sys.exit(main())
