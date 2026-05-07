"""forge_multi_orb — opening-range breakout, QQQ-only (scoped down 2026-05-01).

Originally traded SPY/QQQ/IWM/GLD. 5/1 ceremony REWORK verdict scoped this
down to QQQ-only based on the drilldown finding:
  - QQQ short  (n=10, PF 1.99, +$9.15)   ← keep
  - QQQ long   (n=14, PF 1.82, +$71.85)  ← keep
  - SPY long   (n=11, PF 0.95)           dropped
  - SPY short  (n=9,  PF 0.68)           dropped
  - IWM short  (n=11, PF 1.01)           dropped (neutral)
  - GLD short  (n=11, PF 0.44, -$63.30)  dropped (bleeder)
  - IWM long   (n=6,  PF 0.28, -$71.24)  dropped (bleeder)
  - GLD long   (n=4,  PF 0.37, -$9.76)   dropped (bleeder)

Net effect: QQQ-only would have made +$81 on n=24 over the same period
the broad strategy lost -$73 over n=99. Re-evaluate at 5/15 — if QQQ-only
PF stays >= 1.30 over n>=15 post-filter, promote. If no improvement by
5/31, auto-convert to KILL per ceremony spec.

Range defined by the 14:30 UTC 5-minute bar (NY open). Break above
range high → LONG, below → SHORT. Target 1.0 ATR, stop 0.5 ATR,
max hold 12 bars (60 min).

Cadence target post-scope-down: ~1 signal/day = 5 signals/week.

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

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr
IBKR_CLIENT_ID = 109
_SIGNAL_ONLY_MODE = False

PARAMS = {
    "version": "v4_qqq_window24_revert",  # 2026-05-07 audit: v3 window=32 was a NET LOSS (PF 0.78->0.28, -64%). Revert to 24.
    "tickers": ["QQQ"],  # was ["SPY", "QQQ", "IWM", "GLD"]; QQQ-only per 5/1 REWORK verdict
    "timeframe": "5m",
    "range_start_utc_hour": 14,      # NY open hour
    "range_start_utc_min": 30,       # 14:30 UTC = 9:30 ET
    "breakout_window_bars": 24,      # 2hr. v3 tried 32 per V2 counterfactual evidence; live trades disagreed strongly with V2 prediction. Revert to 24 baseline; revisit if V2 model is corrected.
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


def _open(state, ticker, df, direction, ctx, ib=None):
    plan_entry = ctx["entry"]; a = ctx["atr"]
    if direction == "long":
        target = plan_entry + PARAMS["target_atr_mult"] * a
        stop = plan_entry - PARAMS["stop_atr_mult"] * a
    else:
        target = plan_entry - PARAMS["target_atr_mult"] * a
        stop = plan_entry + PARAMS["stop_atr_mult"] * a
    try:
        from helio.fleet_sizing import compute_risk_usd, max_notional_usd
        risk_budget = compute_risk_usd(strategy_label="forge_multi_orb")
    except Exception:
        risk_budget = 300.0
    # 2026-05-07 audit: replaced raw `int(risk_budget / stop_dollars)` with
    # the safe_position_size helper. Old formula produced phantom 1419-share
    # QQQ orders ($965K notional) on 5/5 because tight stops divided the
    # risk budget into explosive share counts. New helper uses an ATR-based
    # sizing floor so a temporarily-compressed stop can't blow up size.
    try:
        cap = max_notional_usd("stock") / max(len(PARAMS["tickers"]), 1)
    except Exception:
        cap = None
    from helio.strategy_common import safe_position_size
    shares, sizing_policy = safe_position_size(
        risk_usd=risk_budget,
        entry_px=plan_entry,
        stop_px=stop,
        atr=a,
        sizing_floor_atr_mult=1.0,  # never size as if stop were tighter than 1×ATR
        max_notional_usd=cap if cap and cap > 0 else None,
        point_value_usd=1.0,
    )
    if shares <= 0:
        log.warning("SIZE_ZERO: skipping entry for %s (policy=%s)", ticker, sizing_policy)
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract(ticker, "etf")
        try:
            ib.qualifyContracts(contract)
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: %s qty=%s, skipping", ticker, existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction=direction, size=shares,
                stop_px=stop, target_px=target, price_decimals=2,
                est_entry_px=plan_entry,
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED %s: %s", ticker, result.entry.reject_reason)
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
            log.error("REAL_ENTRY EXCEPTION %s: %s", ticker, exc, exc_info=True)
            return

    risk_usd = abs(stop - entry_px) * shares
    state.setdefault("open_trades", {})[ticker] = {
        "entry_ts": str(df.index[-1]), "direction": direction,
        "entry_px": entry_px, "stop_px": stop, "target_px": target,
        "atr_entry": a, "position_size": shares, "risk_usd": risk_usd,
        "range_high": ctx["range_high"], "range_low": ctx["range_low"],
        "bars_held": 0, "config_hash": _config_hash(),
        "git_sha": _git_sha(), "session_id": state["session_id"],
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
    }
    log.info("%s %s %s @ %.2f target %.2f stop %.2f atr %.3f shares %d",
             execution_venue.upper(), direction.upper(), ticker, entry_px, target, stop, a, shares)


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

    ib = None
    if not _SIGNAL_ONLY_MODE:
        try:
            ib = ibkr.connect(IBKR_CLIENT_ID)
        except Exception as exc:
            log.warning("IBKR connect failed, signal-only fallback: %s", exc)
            ib = None

    try:
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
                if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                    contract = ibkr.make_contract(ticker, "etf")
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
                        log.error("bracket check failed %s: %s", ticker, exc)
                        outcome = None
                    if outcome is not None:
                        _close(state, ticker, outcome["fill_ts"], outcome["fill_price"], outcome["reason"])
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
                                    _close(state, ticker, fill.fill_ts, fill.fill_price, "time")
                                else:
                                    log.error("TIME_STOP close failed %s: %s", ticker, fill.reject_reason)
                            else:
                                _close(state, ticker, df.index[-1], float(df.iloc[-1]["Close"]), "reconcile_flat")
                else:
                    entry_ts = pd.to_datetime(ot["entry_ts"], utc=True)
                    forward = df[df.index > entry_ts]
                    for ts, row in forward.iterrows():
                        ot["bars_held"] = ot.get("bars_held", 0) + 1
                        hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
                        if ot["direction"] == "long":
                            if lo <= ot["stop_px"]: _close(state, ticker, ts, ot["stop_px"], "stop"); break
                            if hi >= ot["target_px"]: _close(state, ticker, ts, ot["target_px"], "target"); break
                        else:
                            if hi >= ot["stop_px"]: _close(state, ticker, ts, ot["stop_px"], "stop"); break
                            if lo <= ot["target_px"]: _close(state, ticker, ts, ot["target_px"], "target"); break
                        if ot["bars_held"] >= PARAMS["hold_bars"]:
                            _close(state, ticker, ts, cl, "time"); break

            # New entry for this ticker
            if ticker not in state.get("open_trades", {}):
                direction, ctx = signal_check_one(ticker, df)
                if direction in ("long", "short"):
                    _open(state, ticker, df, direction, ctx, ib=ib)
                    _append_signal(df.index[-1], ticker, f"ENTRY_{direction.upper()}",
                                   ctx.get("range_high", 0), ctx.get("range_low", 0), ctx.get("atr", 0))
                else:
                    _append_signal(df.index[-1], ticker, f"NO_TRIGGER_{ctx.get('reason', '?')}",
                                   ctx.get("range_high", 0) or 0, ctx.get("range_low", 0) or 0, 0)

        _save_state(state)
        _write_heartbeat(state, datetime.now(timezone.utc).isoformat())
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


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
