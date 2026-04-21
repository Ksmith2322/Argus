"""forge_aud_asian_breakout — 1H AUDUSD opening-range breakout at Tokyo AM.

Hypothesis: Tokyo 01:00-02:00 UTC opening range on AUDUSD (commodity/risk
currency). Break above the 01:00 hour's high in the next 2 hours triggers
a long; break below the low triggers a short. Time-exit 4 bars (4h) later
or stop/target.

Expected cadence: 8-12 signals/week (2 session-days × 4-6 setups).
Complexity: ~200 LOC.

Usage:
    python -m forge.aud_asian_breakout.runner --evaluate
    python -m forge.aud_asian_breakout.runner --loop
    python -m forge.aud_asian_breakout.runner --scan
    python -m forge.aud_asian_breakout.runner --backtest --period 180d
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

log = setup_logging("aud_asian_breakout")

LOG_DIR = REPO / "forge" / "logs" / "aud_asian_breakout"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = LOG_DIR / "state.json"
TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"

PARAMS = {
    "version": "v1",
    "ticker": "AUDUSD=X",
    "symbol": "AUDUSD",
    "timeframe": "1h",
    "range_start_hour_utc": 1,     # Tokyo open hour
    "range_end_hour_utc": 2,       # 01:00 bar close
    "breakout_window_hours": 4,    # window after range close to detect breakout
    "hold_bars": 4,                # max 4h hold after entry
    "atr_period": 14,
    "target_atr_mult": 1.0,
    "stop_atr_mult": 0.5,
    "min_range_pips": 15,          # skip narrow ranges (noise)
    "max_range_pips": 120,         # skip fat ranges (overnight news)
    "risk_pct_default": 0.005,
}

TRADE_FIELDS = [
    "ts", "symbol", "direction", "entry_px", "exit_px", "pnl_pips", "exit_reason",
    "duration_min", "trade_num", "pnl_usd", "position_size", "risk_usd",
    "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "stop_px", "target_px", "range_high", "range_low",
]


def _ensure_trade_csv():
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
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "open_trade": None, "trade_count": 0,
        "session_id": os.urandom(4).hex(),
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _write_heartbeat(state: dict, last_ts: str) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "aud_asian_breakout", "family": "forge", "mode": "paper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_signal_ts": last_ts,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str), encoding="utf-8")


def _append_signal(ts, action: str, rng_hi: float, rng_lo: float, atr_val: float) -> None:
    first_write = not SIGNALS_PATH.exists()
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first_write:
            w.writerow(["ts", "action", "range_high", "range_low", "atr", "hour_utc", "config_hash"])
        w.writerow([str(ts), action, f"{rng_hi:.5f}", f"{rng_lo:.5f}",
                    f"{atr_val:.5f}", pd.to_datetime(ts).hour, _config_hash()])


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def fetch_history(period: str = "7d") -> pd.DataFrame:
    df = yf.download(PARAMS["ticker"], period=period, interval="1h",
                     progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _range_for_date(df: pd.DataFrame, date_utc) -> tuple[float, float, int] | None:
    """Find the 01:00 UTC bar for the given date. Return (high, low, bar_idx) or None."""
    mask = (df.index.year == date_utc.year) & (df.index.month == date_utc.month) & (df.index.day == date_utc.day) & (df.index.hour == PARAMS["range_start_hour_utc"])
    bars = df[mask]
    if bars.empty:
        return None
    row = bars.iloc[0]
    idx = df.index.get_loc(bars.index[0])
    return float(row["High"]), float(row["Low"]), int(idx)


def signal_check(df: pd.DataFrame) -> tuple[str, dict]:
    """Return (direction, ctx). direction in {'long', 'short', 'none'}."""
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    today = last_ts.date()

    rng = _range_for_date(df, last_ts)
    if rng is None:
        return "none", {"reason": "no_range_bar_today"}
    rng_hi, rng_lo, rng_idx = rng
    pip = 0.0001
    rng_pips = (rng_hi - rng_lo) / pip
    if rng_pips < PARAMS["min_range_pips"] or rng_pips > PARAMS["max_range_pips"]:
        return "none", {"reason": f"range_pips_outside_band_{rng_pips:.0f}", "range_high": rng_hi, "range_low": rng_lo}

    # Window: last bar must be within breakout window
    bars_since_range = last_idx - rng_idx
    if bars_since_range < 1 or bars_since_range > PARAMS["breakout_window_hours"]:
        return "none", {"reason": f"outside_breakout_window_{bars_since_range}", "range_high": rng_hi, "range_low": rng_lo}

    atr_val = float(_atr(df, PARAMS["atr_period"]).iloc[last_idx])
    if not (np.isfinite(atr_val) and atr_val > 0):
        return "none", {"reason": "atr_nan"}

    last_close = float(df["Close"].iloc[last_idx])
    if last_close > rng_hi:
        return "long", {"entry": last_close, "atr": atr_val, "range_high": rng_hi, "range_low": rng_lo}
    if last_close < rng_lo:
        return "short", {"entry": last_close, "atr": atr_val, "range_high": rng_hi, "range_low": rng_lo}
    return "none", {"reason": "within_range", "range_high": rng_hi, "range_low": rng_lo, "last": last_close}


def _open(state: dict, df: pd.DataFrame, direction: str, ctx: dict) -> None:
    entry = ctx["entry"]; a = ctx["atr"]
    if direction == "long":
        target = entry + PARAMS["target_atr_mult"] * a
        stop = entry - PARAMS["stop_atr_mult"] * a
    else:
        target = entry - PARAMS["target_atr_mult"] * a
        stop = entry + PARAMS["stop_atr_mult"] * a

    stop_pips = abs(stop - entry) / 0.0001
    pip_value_per_lot = 10.0  # $10 per pip per 100K lot for AUDUSD
    try:
        from helio.fleet_sizing import compute_risk_usd, max_notional_usd
        from argus_flow.sizing import fx_notional_per_unit_usd
        risk_budget_usd = compute_risk_usd(strategy_label="forge_aud_asian_breakout")
    except Exception:
        risk_budget_usd = 500.0

    pos_size = max(1, int(risk_budget_usd / max(stop_pips * pip_value_per_lot / 100_000, 1e-6)))
    try:
        npu = fx_notional_per_unit_usd(PARAMS["symbol"], quote_price=entry)
        cap_units = int(max_notional_usd("fx") / max(npu, 1e-9))
        if pos_size > cap_units:
            pos_size = cap_units
    except Exception:
        pass
    risk_usd = stop_pips * pip_value_per_lot * (pos_size / 100_000)

    state["open_trade"] = {
        "entry_ts": str(df.index[-1]), "direction": direction.lower(),
        "entry_px": entry, "stop_px": stop, "target_px": target,
        "atr_entry": a, "position_size": pos_size, "risk_usd": risk_usd,
        "range_high": ctx["range_high"], "range_low": ctx["range_low"],
        "bars_held": 0,
        "config_hash": _config_hash(), "git_sha": _git_sha(),
        "session_id": state["session_id"],
    }
    log.info("PAPER %s @ %.5f target %.5f stop %.5f atr %.5f units %d risk $%.0f",
             direction.upper(), entry, target, stop, a, pos_size, risk_usd)


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    pip = 0.0001
    if ot["direction"] == "long":
        pnl_pips = (exit_px - ot["entry_px"]) / pip
    else:
        pnl_pips = (ot["entry_px"] - exit_px) / pip
    pnl_usd = pnl_pips * 10.0 * (ot["position_size"] / 100_000)
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60

    _append_trade({
        "ts": ot["entry_ts"], "symbol": PARAMS["symbol"], "direction": ot["direction"],
        "entry_px": ot["entry_px"], "exit_px": exit_px,
        "pnl_pips": round(pnl_pips, 1), "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"], "pnl_usd": round(pnl_usd, 2),
        "position_size": ot["position_size"], "risk_usd": round(ot["risk_usd"], 2),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "tokyo_orb", "experiment_valid": "true",
        "invalid_reason": "", "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state.get("runtime_start", time.time())),
        "git_sha": ot["git_sha"], "atr_entry": ot["atr_entry"],
        "stop_px": ot["stop_px"], "target_px": ot["target_px"],
        "range_high": ot["range_high"], "range_low": ot["range_low"],
    })
    log.info("PAPER %s closed (%s) @ %.5f — pnl %+.1fp ($%+.2f)",
             ot["direction"].upper(), reason, exit_px, pnl_pips, pnl_usd)

    # Dual-write canonical
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_aud_asian_breakout", symbol=PARAMS["symbol"],
            direction=ot["direction"], side="EXIT",
            entry_ts=str(ot["entry_ts"]), exit_ts=str(exit_ts),
            entry_px=float(ot["entry_px"]), exit_px=float(exit_px),
            size=float(ot["position_size"]), risk_usd=float(ot["risk_usd"]),
            pnl_usd=round(pnl_usd, 2), exit_reason=reason,
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")

    state["open_trade"] = None


def evaluate_once() -> None:
    log.info("aud_asian_breakout eval starting")
    df = fetch_history()
    state = _load_state()

    if state.get("open_trade"):
        ot = state["open_trade"]
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
            _open(state, df, direction, ctx)
            _append_signal(last_ts, f"ENTRY_{direction.upper()}",
                           ctx.get("range_high", 0), ctx.get("range_low", 0), ctx.get("atr", 0))
        else:
            _append_signal(last_ts, f"NO_TRIGGER_{ctx.get('reason','?')}",
                           ctx.get("range_high", 0), ctx.get("range_low", 0), 0)

    _save_state(state)
    _write_heartbeat(state, str(last_ts))


def loop_mode() -> None:
    log.info("aud_asian_breakout loop starting")
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Wake at :05 past each hour during relevant window (01-07 UTC)
            next_fire = (now + timedelta(hours=1)).replace(minute=5, second=0, microsecond=0)
            sleep_s = max(30, (next_fire - now).total_seconds())
            log.info("next eval at %s (sleep %.0fs)", next_fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as e:
                log.error("eval failed: %s", e, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            return


def scan() -> None:
    df = fetch_history()
    last_ts = df.index[-1]
    print(f"AUDUSD 1h — bar {last_ts}")
    print(f"  Close: {df['Close'].iloc[-1]:.5f}")
    direction, ctx = signal_check(df)
    print(f"  Signal: {direction} | ctx={ctx}")


def backtest(period: str = "180d") -> None:
    df = fetch_history(period=period)
    atr_series = _atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    pip = 0.0001

    trades = []
    # Group by date, find 01:00 bar per day
    for d in sorted(set(df.index.date)):
        ts_for_day = [i for i, ts in enumerate(df.index) if ts.date() == d and ts.hour == PARAMS["range_start_hour_utc"]]
        if not ts_for_day:
            continue
        rng_idx = ts_for_day[0]
        rng_hi, rng_lo = float(df["High"].iloc[rng_idx]), float(df["Low"].iloc[rng_idx])
        rng_pips = (rng_hi - rng_lo) / pip
        if rng_pips < PARAMS["min_range_pips"] or rng_pips > PARAMS["max_range_pips"]:
            continue

        # Look for breakout in next N hours
        entry_idx = None; direction = None
        for j in range(rng_idx + 1, min(rng_idx + 1 + PARAMS["breakout_window_hours"], len(df))):
            if C[j] > rng_hi:
                entry_idx = j; direction = "long"; break
            if C[j] < rng_lo:
                entry_idx = j; direction = "short"; break
        if entry_idx is None:
            continue
        a = atr_series[entry_idx]
        if not np.isfinite(a) or a <= 0: continue
        entry = C[entry_idx]
        if direction == "long":
            target = entry + PARAMS["target_atr_mult"] * a
            stop = entry - PARAMS["stop_atr_mult"] * a
        else:
            target = entry - PARAMS["target_atr_mult"] * a
            stop = entry + PARAMS["stop_atr_mult"] * a

        exit_px = None; exit_reason = "time"
        for k in range(entry_idx + 1, min(entry_idx + 1 + PARAMS["hold_bars"], len(df))):
            if direction == "long":
                if L[k] <= stop: exit_px = stop; exit_reason = "stop"; break
                if H[k] >= target: exit_px = target; exit_reason = "target"; break
            else:
                if H[k] >= stop: exit_px = stop; exit_reason = "stop"; break
                if L[k] <= target: exit_px = target; exit_reason = "target"; break
        if exit_px is None: exit_px = C[min(entry_idx + PARAMS["hold_bars"], len(df) - 1)]
        pnl_pips = (exit_px - entry) / pip if direction == "long" else (entry - exit_px) / pip
        trades.append({"ts": df.index[entry_idx], "direction": direction,
                       "entry": entry, "exit": exit_px, "pnl_pips": pnl_pips,
                       "exit_reason": exit_reason})

    if not trades:
        print(f"0 trades in {period}"); return
    td = pd.DataFrame(trades)
    wins = td[td["pnl_pips"] > 0]; losses = td[td["pnl_pips"] <= 0]
    pf = wins["pnl_pips"].sum() / abs(losses["pnl_pips"].sum()) if len(losses) else float("inf")
    print(f"{len(td)} trades over {period}")
    print(f"  WR: {len(wins)/len(td)*100:.1f}%  PF: {pf:.2f}")
    print(f"  Total pips: {td['pnl_pips'].sum():+.1f}  Exit reasons: {td['exit_reason'].value_counts().to_dict()}")
    print(f"  Direction split: {td['direction'].value_counts().to_dict()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--backtest", action="store_true")
    ap.add_argument("--period", default="180d")
    args = ap.parse_args()
    if args.evaluate: evaluate_once()
    elif args.loop: loop_mode()
    elif args.scan: scan()
    elif args.backtest: backtest(args.period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
