"""forge_multi_orb — opening-range breakout on 4 instruments simultaneously.

Tickers: SPY, QQQ, IWM, GLD. Range defined by the 14:30 UTC 5-minute bar
(NY open). Break above range high → LONG, below → SHORT. Target 1.0 ATR,
stop 0.5 ATR, max hold 12 bars (60 min).

Cadence target: ~1 signal/instrument/day = 20 signals/week fleet-wide.

Usage:
    python -m forge.multi_orb.runner --evaluate
    python -m forge.multi_orb.runner --loop
    python -m forge.multi_orb.runner --backtest --period 60d
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

log = setup_logging("multi_orb")

LOG_DIR = REPO / "forge" / "logs" / "multi_orb"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = LOG_DIR / "state.json"
TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"

PARAMS = {
    "version": "v1",
    "tickers": ["SPY", "QQQ", "IWM", "GLD"],
    "timeframe": "5m",
    "range_start_utc_hour": 14,      # NY open hour
    "range_start_utc_min": 30,       # 14:30 UTC = 9:30 ET
    "breakout_window_bars": 24,      # 2 hours (24 × 5m) to detect breakout after range close
    "atr_period": 14,
    "hold_bars": 12,                 # 60 min
    "target_atr_mult": 1.0,
    "stop_atr_mult": 0.5,
    "min_atr_pct": 0.0005,
    "risk_pct_default": 0.003,
}

TRADE_FIELDS = [
    "ts", "symbol", "direction", "entry_px", "exit_px", "pnl_pct", "pnl_usd",
    "exit_reason", "duration_min", "trade_num", "position_size", "risk_usd",
    "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "range_high", "range_low", "stop_px", "target_px",
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


def _load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception: pass
    # open_trades keyed by ticker — allows parallel positions
    return {"open_trades": {}, "trade_count": 0,
            "session_id": os.urandom(4).hex(), "runtime_start": time.time()}


def _save_state(s): STATE_PATH.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(s, last_ts):
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "multi_orb", "family": "forge", "mode": "paper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_count": s.get("trade_count", 0),
        "open_trades": list((s.get("open_trades") or {}).keys()),
        "last_signal_ts": last_ts,
        "config_hash": _config_hash(), "git_sha": _git_sha(),
    }, indent=2, default=str), encoding="utf-8")


def _append_signal(ts, sym, action, rng_hi, rng_lo, atr_val):
    first = not SIGNALS_PATH.exists()
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first: w.writerow(["ts", "symbol", "action", "range_high", "range_low", "atr"])
        w.writerow([str(ts), sym, action, f"{rng_hi:.4f}", f"{rng_lo:.4f}", f"{atr_val:.4f}"])


def _append_trade(row):
    _ensure_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _atr(df, n):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def fetch_history(ticker, period="7d"):
    df = yf.download(ticker, period=period, interval="5m",
                     progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _today_range_bar(df) -> tuple[float, float, int] | None:
    """Find today's 14:30 UTC bar. Return (high, low, idx) or None."""
    last = df.index[-1]
    today = last.date()
    mask = [(ts.date() == today and ts.hour == PARAMS["range_start_utc_hour"]
             and ts.minute == PARAMS["range_start_utc_min"])
            for ts in df.index]
    if not any(mask):
        return None
    idx = mask.index(True)
    return float(df["High"].iloc[idx]), float(df["Low"].iloc[idx]), idx


def signal_check_one(ticker, df) -> tuple[str, dict]:
    r = _today_range_bar(df)
    if r is None:
        return "none", {"reason": "no_range_bar_today"}
    rng_hi, rng_lo, rng_idx = r
    last_idx = len(df) - 1
    if last_idx <= rng_idx:
        return "none", {"reason": "still_in_range_bar"}
    if (last_idx - rng_idx) > PARAMS["breakout_window_bars"]:
        return "none", {"reason": "breakout_window_expired"}
    atr_val = float(_atr(df, PARAMS["atr_period"]).iloc[last_idx])
    if not (np.isfinite(atr_val) and atr_val > 0):
        return "none", {"reason": "atr_nan"}
    close = float(df["Close"].iloc[last_idx])
    if atr_val / close < PARAMS["min_atr_pct"]:
        return "none", {"reason": "atr_too_small"}
    if close > rng_hi:
        return "long", {"entry": close, "atr": atr_val, "range_high": rng_hi, "range_low": rng_lo}
    if close < rng_lo:
        return "short", {"entry": close, "atr": atr_val, "range_high": rng_hi, "range_low": rng_lo}
    return "none", {"reason": "within_range", "range_high": rng_hi, "range_low": rng_lo}


def _open(state, ticker, df, direction, ctx):
    entry = ctx["entry"]; a = ctx["atr"]
    if direction == "long":
        target = entry + PARAMS["target_atr_mult"] * a
        stop = entry - PARAMS["stop_atr_mult"] * a
    else:
        target = entry - PARAMS["target_atr_mult"] * a
        stop = entry + PARAMS["stop_atr_mult"] * a
    try:
        from helio.fleet_sizing import compute_risk_usd, max_notional_usd
        risk_budget = compute_risk_usd(strategy_label="forge_multi_orb")
    except Exception:
        risk_budget = 300.0
    stop_dollars = abs(entry - stop)
    shares = max(1, int(risk_budget / max(stop_dollars, 0.01)))
    try:
        cap = max_notional_usd("stock")
        if cap > 0 and entry * shares > cap:
            shares = max(1, int(cap / max(entry, 1e-6)))
    except Exception:
        pass
    risk_usd = stop_dollars * shares
    state.setdefault("open_trades", {})[ticker] = {
        "entry_ts": str(df.index[-1]), "direction": direction,
        "entry_px": entry, "stop_px": stop, "target_px": target,
        "atr_entry": a, "position_size": shares, "risk_usd": risk_usd,
        "range_high": ctx["range_high"], "range_low": ctx["range_low"],
        "bars_held": 0, "config_hash": _config_hash(),
        "git_sha": _git_sha(), "session_id": state["session_id"],
    }
    log.info("PAPER %s %s @ %.2f target %.2f stop %.2f atr %.3f shares %d",
             direction.upper(), ticker, entry, target, stop, a, shares)


def _close(state, ticker, exit_ts, exit_px, reason):
    ot = state["open_trades"][ticker]
    if ot["direction"] == "long":
        pnl_pct = (exit_px - ot["entry_px"]) / ot["entry_px"] * 100
    else:
        pnl_pct = (ot["entry_px"] - exit_px) / ot["entry_px"] * 100
    pnl_usd = pnl_pct / 100 * ot["entry_px"] * ot["position_size"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60
    _append_trade({
        "ts": ot["entry_ts"], "symbol": ticker, "direction": ot["direction"],
        "entry_px": ot["entry_px"], "exit_px": exit_px,
        "pnl_pct": round(pnl_pct, 3), "pnl_usd": round(pnl_usd, 2),
        "exit_reason": reason, "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"],
        "position_size": ot["position_size"], "risk_usd": round(ot["risk_usd"], 2),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "ny_orb", "experiment_valid": "true",
        "invalid_reason": "", "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state.get("runtime_start", time.time())),
        "git_sha": ot["git_sha"], "atr_entry": ot["atr_entry"],
        "range_high": ot["range_high"], "range_low": ot["range_low"],
        "stop_px": ot["stop_px"], "target_px": ot["target_px"],
    })
    log.info("PAPER %s %s closed (%s) @ %.2f — pnl %+.2f%% ($%+.2f)",
             ot["direction"].upper(), ticker, reason, exit_px, pnl_pct, pnl_usd)
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_multi_orb", symbol=ticker,
            direction=ot["direction"], side="EXIT",
            entry_ts=str(ot["entry_ts"]), exit_ts=str(exit_ts),
            entry_px=float(ot["entry_px"]), exit_px=float(exit_px),
            size=float(ot["position_size"]), risk_usd=float(ot["risk_usd"]),
            pnl_usd=round(pnl_usd, 2), exit_reason=reason,
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")
    del state["open_trades"][ticker]


def evaluate_once():
    log.info("multi_orb eval starting across %s", PARAMS["tickers"])
    state = _load_state()
    for ticker in PARAMS["tickers"]:
        try:
            df = fetch_history(ticker)
        except Exception as e:
            log.warning("fetch failed for %s: %s", ticker, e)
            continue
        if df.empty: continue

        # Manage open trade on this ticker
        if ticker in state.get("open_trades", {}):
            ot = state["open_trades"][ticker]
            entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
            forward = df[df.index > entry_ts]
            closed = False
            for ts, row in forward.iterrows():
                ot["bars_held"] = ot.get("bars_held", 0) + 1
                hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
                if ot["direction"] == "long":
                    if lo <= ot["stop_px"]: _close(state, ticker, ts, ot["stop_px"], "stop"); closed = True; break
                    if hi >= ot["target_px"]: _close(state, ticker, ts, ot["target_px"], "target"); closed = True; break
                else:
                    if hi >= ot["stop_px"]: _close(state, ticker, ts, ot["stop_px"], "stop"); closed = True; break
                    if lo <= ot["target_px"]: _close(state, ticker, ts, ot["target_px"], "target"); closed = True; break
                if ot["bars_held"] >= PARAMS["hold_bars"]:
                    _close(state, ticker, ts, cl, "time"); closed = True; break
            # If still open, carry state

        # New entry for this ticker
        if ticker not in state.get("open_trades", {}):
            direction, ctx = signal_check_one(ticker, df)
            if direction in ("long", "short"):
                _open(state, ticker, df, direction, ctx)
                _append_signal(df.index[-1], ticker, f"ENTRY_{direction.upper()}",
                               ctx.get("range_high", 0), ctx.get("range_low", 0), ctx.get("atr", 0))
            else:
                _append_signal(df.index[-1], ticker, f"NO_TRIGGER_{ctx.get('reason', '?')}",
                               ctx.get("range_high", 0) or 0, ctx.get("range_low", 0) or 0, 0)

    _save_state(state)
    # Last-ts for heartbeat is just 'now' since we iterate multiple tickers
    _write_heartbeat(state, datetime.now(timezone.utc).isoformat())


def loop_mode():
    log.info("multi_orb loop starting")
    while True:
        try:
            now = datetime.now(timezone.utc)
            minutes_to_next = 5 - (now.minute % 5)
            next_fire = (now + timedelta(minutes=minutes_to_next)).replace(second=20, microsecond=0)
            sleep_s = max(10, (next_fire - now).total_seconds())
            log.info("next eval at %s (sleep %.0fs)", next_fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try: evaluate_once()
            except Exception as e:
                log.error("eval failed: %s", e, exc_info=True)
                time.sleep(60)
        except KeyboardInterrupt: return


def scan():
    for ticker in PARAMS["tickers"]:
        try:
            df = fetch_history(ticker)
        except Exception as e:
            print(f"  {ticker}: fetch failed: {e}"); continue
        direction, ctx = signal_check_one(ticker, df)
        print(f"  {ticker}: {direction} | {ctx}")


def backtest(period="60d"):
    all_trades = []
    for ticker in PARAMS["tickers"]:
        try:
            df = fetch_history(ticker, period=period)
        except Exception as e:
            print(f"  {ticker}: fetch failed: {e}"); continue
        atr_s = _atr(df, PARAMS["atr_period"]).values
        H, L, C = df["High"].values, df["Low"].values, df["Close"].values

        # Find each day's 14:30 UTC bar
        trades_this = []
        day_by_idx = [ts.date() for ts in df.index]
        by_date = {}
        for i, ts in enumerate(df.index):
            if ts.hour == PARAMS["range_start_utc_hour"] and ts.minute == PARAMS["range_start_utc_min"]:
                by_date.setdefault(ts.date(), i)

        for d, rng_idx in by_date.items():
            rng_hi, rng_lo = float(df["High"].iloc[rng_idx]), float(df["Low"].iloc[rng_idx])
            entry_idx = None; direction = None
            for j in range(rng_idx + 1, min(rng_idx + 1 + PARAMS["breakout_window_bars"], len(df))):
                if C[j] > rng_hi: entry_idx = j; direction = "long"; break
                if C[j] < rng_lo: entry_idx = j; direction = "short"; break
            if entry_idx is None: continue
            a = atr_s[entry_idx]
            if not np.isfinite(a) or a <= 0: continue
            entry = C[entry_idx]
            if a / entry < PARAMS["min_atr_pct"]: continue
            if direction == "long":
                target = entry + PARAMS["target_atr_mult"] * a
                stop = entry - PARAMS["stop_atr_mult"] * a
            else:
                target = entry - PARAMS["target_atr_mult"] * a
                stop = entry + PARAMS["stop_atr_mult"] * a
            exit_px = None; reason = "time"
            for k in range(entry_idx + 1, min(entry_idx + 1 + PARAMS["hold_bars"], len(df))):
                if direction == "long":
                    if L[k] <= stop: exit_px = stop; reason = "stop"; break
                    if H[k] >= target: exit_px = target; reason = "target"; break
                else:
                    if H[k] >= stop: exit_px = stop; reason = "stop"; break
                    if L[k] <= target: exit_px = target; reason = "target"; break
            if exit_px is None: exit_px = C[min(entry_idx + PARAMS["hold_bars"], len(df) - 1)]
            pnl_pct = (exit_px - entry) / entry * 100 if direction == "long" else (entry - exit_px) / entry * 100
            trades_this.append({"ticker": ticker, "ts": df.index[entry_idx], "direction": direction,
                                "entry": entry, "exit": exit_px, "pnl_pct": pnl_pct, "exit_reason": reason})
        all_trades.extend(trades_this)
        print(f"  {ticker}: {len(trades_this)} trades")
    if not all_trades:
        print(f"0 trades in {period}"); return
    td = pd.DataFrame(all_trades)
    wins = td[td["pnl_pct"] > 0]; losses = td[td["pnl_pct"] <= 0]
    pf = wins["pnl_pct"].sum() / abs(losses["pnl_pct"].sum()) if len(losses) and losses["pnl_pct"].sum() != 0 else float("inf")
    days = (td["ts"].max() - td["ts"].min()).days or 1
    print(f"\n{len(td)} trades over {period} ({len(td) / max(days, 1) * 7:.1f}/week across all tickers)")
    print(f"  WR: {len(wins)/len(td)*100:.1f}%  PF: {pf:.2f}")
    print(f"  Total %: {td['pnl_pct'].sum():+.2f}%  Avg: {td['pnl_pct'].mean():+.3f}%")
    print(f"  Per-ticker: {td['ticker'].value_counts().to_dict()}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--backtest", action="store_true")
    ap.add_argument("--period", default="60d")
    args = ap.parse_args()
    if args.evaluate: evaluate_once()
    elif args.loop: loop_mode()
    elif args.scan: scan()
    elif args.backtest: backtest(args.period)
    return 0


if __name__ == "__main__": sys.exit(main())
