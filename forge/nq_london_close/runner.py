"""forge_nq_london_close — 5m NQ mean-reversion at London close (16:00 UTC).

Hypothesis: at 16:00 UTC the London session closes; option gamma hedges roll,
algos take profit before US afternoon volatility expansion. If NQ=F has
pushed too far in the London session (RSI stretched, price > 20-bar SMA +
1.2 ATR), the late-London move tends to fade in the first 30-60 min of
the US afternoon.

Expected cadence: 4-5 signals per London close session → 15-20/week.
Complexity: ~200 LOC (this file).

Usage:
    python -m forge.nq_london_close.runner --evaluate    # one cycle
    python -m forge.nq_london_close.runner --loop        # continuous
    python -m forge.nq_london_close.runner --backtest    # historical
    python -m forge.nq_london_close.runner --scan        # read-only status
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

log = setup_logging("nq_london_close")

LOG_DIR = REPO / "forge" / "logs" / "nq_london_close"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = LOG_DIR / "state.json"
TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"

RESEARCH_ONLY = True  # 2026-04-21: 60d backtest PF 0.90 / 4.6 trades/wk — negative
# edge. Params need tuning (overshoot threshold too low? RSI window wrong?) before
# this is more than research. Runner still produces paper trades; flag the artifact.
# Re-enable for production by flipping to False after PF > 1.2 on a 90d+ backtest.

# 2026-04-24: IBKR execution. Unique client_id in the forge range 100-199.
from helio import ibkr_execution as ibkr
IBKR_CLIENT_ID = 107
_SIGNAL_ONLY_MODE = False

PARAMS = {
    "version": "v1",
    "ticker": "NQ=F",
    "timeframe": "5m",
    "signal_hour_utc": 16,       # London close hour
    "signal_lookback_bars": 12,  # last hour of London session
    "atr_period": 20,
    "sma_period": 20,
    "rsi_period": 9,
    "rsi_overbought": 65,
    "overshoot_atr_mult": 1.2,   # price must be > SMA + 1.2*ATR to qualify
    "min_volume_percentile": 0.5,
    "hold_bars": 5,               # 25 min max
    "target_atr_mult": 0.5,
    "stop_atr_mult": 0.3,
    "risk_pct_default": 0.005,    # 0.5% of anchor
    "point_value_usd": 2.0,       # MNQ: $2 per pt; NQ: $20 per pt
}

TRADE_FIELDS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pts", "exit_reason",
    "duration_min", "trade_num", "pnl_usd", "position_size", "risk_usd",
    "sizing_policy", "entry_regime", "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
    "atr_entry", "stop_px", "target_px",
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
        "system": "nq_london_close", "family": "forge", "mode": "paper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_signal_ts": last_ts,
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
    }, indent=2, default=str), encoding="utf-8")


def _append_signal(ts, action: str, rsi: float, overshoot: float, atr_val: float) -> None:
    first_write = not SIGNALS_PATH.exists()
    with open(SIGNALS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first_write:
            w.writerow(["ts", "action", "rsi_9", "overshoot_atr", "atr", "hour_utc", "config_hash"])
        w.writerow([str(ts), action, f"{rsi:.2f}", f"{overshoot:.3f}",
                    f"{atr_val:.3f}", pd.to_datetime(ts).hour, _config_hash()])


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


# ── indicators ──
def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).rolling(n, min_periods=n).mean()
    dn = (-d.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def fetch_history(period: str = "7d") -> pd.DataFrame:
    df = yf.download(PARAMS["ticker"], period=period, interval="5m",
                     progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def signal_check(df: pd.DataFrame) -> tuple[bool, dict]:
    """Return (should_enter_short, context)."""
    last_idx = len(df) - 1
    last_ts = df.index[last_idx]
    if last_ts.hour != PARAMS["signal_hour_utc"]:
        return False, {"reason": "not_signal_hour", "hour": last_ts.hour}

    atr_series = _atr(df, PARAMS["atr_period"])
    sma = df["Close"].rolling(PARAMS["sma_period"]).mean()
    rsi = _rsi(df["Close"], PARAMS["rsi_period"])

    a = float(atr_series.iloc[last_idx])
    s = float(sma.iloc[last_idx])
    close = float(df["Close"].iloc[last_idx])
    r = float(rsi.iloc[last_idx])
    if not (np.isfinite(a) and np.isfinite(s) and np.isfinite(r) and a > 0):
        return False, {"reason": "indicator_nan"}

    overshoot = (close - s) / max(a, 1e-9)
    if r < PARAMS["rsi_overbought"] and overshoot < PARAMS["overshoot_atr_mult"]:
        return False, {"reason": "not_stretched", "rsi": r, "overshoot": overshoot, "atr": a}

    # Require a 2-bar pullback after spike (reversal confirmation)
    if last_idx < 3:
        return False, {"reason": "not_enough_bars"}
    c0 = float(df["Close"].iloc[last_idx])
    c1 = float(df["Close"].iloc[last_idx - 1])
    c2 = float(df["Close"].iloc[last_idx - 2])
    if not (c1 > c2 and c0 < c1):
        return False, {"reason": "no_reversal_bar", "rsi": r, "overshoot": overshoot}

    return True, {"rsi": r, "overshoot": overshoot, "atr": a, "entry": close}


def _open(state: dict, df: pd.DataFrame, ctx: dict, ib=None) -> None:
    plan_entry = ctx["entry"]
    a = ctx["atr"]
    target = plan_entry - PARAMS["target_atr_mult"] * a
    stop = plan_entry + PARAMS["stop_atr_mult"] * a

    try:
        from helio.fleet_sizing import compute_risk_usd, max_notional_usd
        risk_budget_usd = compute_risk_usd(strategy_label="forge_nq_london_close")
    except Exception:
        risk_budget_usd = 1000.0 * PARAMS["risk_pct_default"] * 1000

    # 2026-05-07 audit: replaced raw `risk_budget / stop_dist` with safe_position_size
    # helper. Previous formula produced a phantom 417-contract NQ order on 5/5
    # ($11.7M notional on $33K equity) because stop_pts=10pts × pt_usd=$2 = $20/contract
    # × 25 contracts buffer somehow misaligned. ATR-based sizing floor prevents
    # tight-stop explosion. point_value_usd is in PARAMS (5.0 for MNQ — 1pt = $5/contract).
    pt_usd = PARAMS["point_value_usd"]
    try:
        cap_usd = max_notional_usd("micro_future")
        # cap is in USD notional; need to convert via pt_usd to share-equivalent
        # for safe_position_size (which expects a notional cap). Notional per contract
        # = entry_price * pt_usd, so size cap = cap_usd / (entry_price * pt_usd).
        # Pass cap_usd directly; helper computes size cap = cap / entry_px which
        # is wrong for futures (uses point_value). Pass max_size instead.
        max_contracts_from_cap = int(cap_usd / max(plan_entry * pt_usd, 1e-6)) if cap_usd > 0 else None
    except Exception:
        max_contracts_from_cap = None

    from helio.strategy_common import safe_position_size
    pos_size, sizing_policy = safe_position_size(
        risk_usd=risk_budget_usd,
        entry_px=plan_entry,
        stop_px=stop,
        atr=a,
        sizing_floor_atr_mult=1.0,  # never size as if stop tighter than 1×ATR
        max_size=max_contracts_from_cap,
        point_value_usd=pt_usd,
    )
    if pos_size <= 0:
        log.warning("SIZE_ZERO: skipping entry (policy=%s)", sizing_policy)
        return

    entry_px = plan_entry
    stop_order_id = None
    target_order_id = None
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.qualify_front_month_future(ib, "MNQ")
        try:
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION: MNQ qty=%s, skipping", existing)
                return
            result = ibkr.submit_bracket(
                ib, contract, direction="short", size=pos_size,
                stop_px=stop, target_px=target, price_decimals=2,
                est_entry_px=plan_entry,
                strategy_label="forge_nq_london_close",
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED: %s", result.entry.reject_reason)
                return
            entry_px = float(result.entry.fill_price)
            stop_order_id = result.stop_order_id
            target_order_id = result.target_order_id
            target = entry_px - PARAMS["target_atr_mult"] * a
            stop = entry_px + PARAMS["stop_atr_mult"] * a
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            return

    risk_usd = abs(stop - entry_px) * pt_usd * pos_size

    state["open_trade"] = {
        "entry_ts": str(df.index[-1]),
        "entry_px": entry_px, "stop_px": stop, "target_px": target,
        "atr_entry": a, "position_size": pos_size, "risk_usd": risk_usd,
        "bars_held": 0,
        "config_hash": _config_hash(), "git_sha": _git_sha(),
        "session_id": state["session_id"],
        "execution_venue": execution_venue,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
    }
    log.info("%s SHORT MNQ @ %.2f target %.2f stop %.2f atr %.3f contracts %d risk $%.0f",
             execution_venue.upper(), entry_px, target, stop, a, pos_size, risk_usd)


def _close(state: dict, exit_ts, exit_px: float, reason: str) -> None:
    ot = state["open_trade"]
    pnl_pts = ot["entry_px"] - exit_px  # SHORT
    pt_usd = PARAMS["point_value_usd"]
    pnl_usd = pnl_pts * pt_usd * ot["position_size"]
    state["trade_count"] += 1
    duration_min = (pd.to_datetime(exit_ts, utc=True) - pd.to_datetime(ot["entry_ts"], utc=True)).total_seconds() / 60

    _append_trade({
        "ts": ot["entry_ts"], "direction": "short",
        "entry_px": ot["entry_px"], "exit_px": exit_px,
        "pnl_pts": round(pnl_pts, 2), "exit_reason": reason,
        "duration_min": round(duration_min, 1),
        "trade_num": state["trade_count"], "pnl_usd": round(pnl_usd, 2),
        "position_size": ot["position_size"], "risk_usd": round(ot["risk_usd"], 2),
        "sizing_policy": "fleet_anchored_risk_pct",
        "entry_regime": "london_close", "experiment_valid": "true",
        "invalid_reason": "", "config_hash": ot["config_hash"],
        "session_id": ot["session_id"],
        "runtime_epoch": int(time.time() - state.get("runtime_start", time.time())),
        "git_sha": ot["git_sha"], "atr_entry": ot["atr_entry"],
        "stop_px": ot["stop_px"], "target_px": ot["target_px"],
    })
    log.info("PAPER SHORT closed (%s) @ %.2f — pnl %+.2fpts ($%+.2f)",
             reason, exit_px, pnl_pts, pnl_usd)

    # Dual-write to canonical_fills
    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        write_fill_typed(Fill(
            strategy="forge_nq_london_close", symbol="MNQ", direction="short",
            side="EXIT", entry_ts=str(ot["entry_ts"]), exit_ts=str(exit_ts),
            entry_px=float(ot["entry_px"]), exit_px=float(exit_px),
            size=float(ot["position_size"]), risk_usd=float(ot["risk_usd"]),
            pnl_usd=round(pnl_usd, 2), exit_reason=reason,
        ))
    except Exception as e:
        log.warning(f"canonical dual-write failed (non-fatal): {e}")

    state["open_trade"] = None


def evaluate_once() -> None:
    log.info("nq_london_close eval starting")
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
        # Manage open trade
        if state.get("open_trade"):
            ot = state["open_trade"]
            if ib is not None and ot.get("execution_venue") == "ibkr_paper":
                contract = ibkr.qualify_front_month_future(ib, "MNQ")
                try:
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
                                ib, contract, direction="short", size=abs(broker_qty),
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
                    if row["High"] >= ot["stop_px"]:
                        _close(state, ts, ot["stop_px"], "stop"); break
                    if row["Low"] <= ot["target_px"]:
                        _close(state, ts, ot["target_px"], "target"); break
                    if ot["bars_held"] >= PARAMS["hold_bars"]:
                        _close(state, ts, float(row["Close"]), "time"); break

        # New entry
        last_ts = df.index[-1]
        if state.get("open_trade") is None:
            should, ctx = signal_check(df)
            if should:
                _open(state, df, ctx, ib=ib)
                _append_signal(last_ts, "ENTRY_SHORT",
                               ctx.get("rsi", 0), ctx.get("overshoot", 0), ctx.get("atr", 0))
            else:
                _append_signal(last_ts, f"NO_TRIGGER_{ctx.get('reason','?')}",
                               ctx.get("rsi", 0), ctx.get("overshoot", 0), ctx.get("atr", 0))

        _save_state(state)
        _write_heartbeat(state, str(last_ts))
    finally:
        if ib is not None:
            ibkr.disconnect(ib)


def loop_mode() -> None:
    log.info("nq_london_close loop starting — fires :05 past each 5m boundary during %d UTC", PARAMS["signal_hour_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Wake at start of next 5m bar + 5s buffer
            minutes_to_next = 5 - (now.minute % 5)
            next_fire = (now + timedelta(minutes=minutes_to_next)).replace(second=5, microsecond=0)
            sleep_s = max(5, (next_fire - now).total_seconds())
            log.info("next eval at %s (sleep %.0fs)", next_fire.isoformat(), sleep_s)
            time.sleep(sleep_s)
            try:
                evaluate_once()
            except Exception as e:
                log.error("eval failed: %s", e, exc_info=True)
                time.sleep(60)
        except KeyboardInterrupt:
            return


def scan() -> None:
    df = fetch_history()
    last_ts = df.index[-1]
    print(f"NQ=F 5m — bar {last_ts}")
    print(f"  Close: {df['Close'].iloc[-1]:.2f}")
    atr_series = _atr(df, PARAMS["atr_period"])
    sma = df["Close"].rolling(PARAMS["sma_period"]).mean()
    rsi = _rsi(df["Close"], PARAMS["rsi_period"])
    print(f"  ATR({PARAMS['atr_period']}): {atr_series.iloc[-1]:.2f}")
    print(f"  SMA({PARAMS['sma_period']}): {sma.iloc[-1]:.2f}")
    print(f"  RSI({PARAMS['rsi_period']}): {rsi.iloc[-1]:.2f}")
    print(f"  Hour UTC: {last_ts.hour} (signal hour: {PARAMS['signal_hour_utc']})")
    should, ctx = signal_check(df)
    reason = ctx.get("reason", "?")
    label = "SHORT" if should else f"no ({reason})"
    print(f"  Signal: {label} | {ctx}")


def backtest(period: str = "30d") -> None:
    df = fetch_history(period=period)
    atr_series = _atr(df, PARAMS["atr_period"]).values
    sma = df["Close"].rolling(PARAMS["sma_period"]).mean().values
    rsi = _rsi(df["Close"], PARAMS["rsi_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    trades = []
    i = 0
    while i < len(df) - PARAMS["hold_bars"]:
        ts = df.index[i]
        if ts.hour != PARAMS["signal_hour_utc"]:
            i += 1; continue
        if not (np.isfinite(atr_series[i]) and np.isfinite(sma[i]) and np.isfinite(rsi[i])): i += 1; continue
        overshoot = (C[i] - sma[i]) / max(atr_series[i], 1e-9)
        if rsi[i] < PARAMS["rsi_overbought"] and overshoot < PARAMS["overshoot_atr_mult"]: i += 1; continue
        if not (i >= 2 and C[i-1] > C[i-2] and C[i] < C[i-1]): i += 1; continue

        entry = C[i]; a = atr_series[i]
        target = entry - PARAMS["target_atr_mult"] * a
        stop = entry + PARAMS["stop_atr_mult"] * a
        exit_px = None; exit_reason = "time"
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if H[j] >= stop: exit_px = stop; exit_reason = "stop"; break
            if L[j] <= target: exit_px = target; exit_reason = "target"; break
        if exit_px is None: exit_px = C[min(i + PARAMS["hold_bars"], len(df) - 1)]
        trades.append({"ts": ts, "entry": entry, "exit": exit_px,
                       "pnl_pts": entry - exit_px, "exit_reason": exit_reason})
        i += PARAMS["hold_bars"] + 1

    if not trades:
        print(f"0 trades in {period}"); return
    td = pd.DataFrame(trades)
    wins = td[td["pnl_pts"] > 0]; losses = td[td["pnl_pts"] <= 0]
    pf = wins["pnl_pts"].sum() / abs(losses["pnl_pts"].sum()) if len(losses) else float("inf")
    print(f"{len(td)} trades over {period}")
    print(f"  WR: {len(wins)/len(td)*100:.1f}%  PF: {pf:.2f}")
    print(f"  Total pts: {td['pnl_pts'].sum():+.1f}  Exit reasons: {td['exit_reason'].value_counts().to_dict()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--backtest", action="store_true")
    ap.add_argument("--period", default="30d")
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


if __name__ == "__main__":
    sys.exit(main())
