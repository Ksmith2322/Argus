"""forge.vix_carry — daily-check runner for VIX term-structure carry.

Modes:
  --check       One-shot signal evaluation against current data; print only.
  --evaluate    One cycle: fetch, evaluate, act on signal, write state.
  --loop        Continuous: sleep until next daily evaluation time, then evaluate.
  --backtest    Run the signal logic over a multi-year history; print stats.
  --signal-only Disable real-order submission (use for paper / shadow only).

The runner is intentionally low-frequency: it fires at most once per
trading day, and most days produce HOLD or WAIT, not ENTER/EXIT. That
profile fits a 1-hour/day operator budget; it does not require a
daemon — `--evaluate` from a scheduled task is sufficient.

Decision logic lives in helio.vix_term_structure.evaluate_carry_signal
so it can be tested as a pure function (see test_vix_carry.py).
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

import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.fleet_sizing import compute_risk_usd, max_notional_usd
from helio import ibkr_execution as ibkr
from helio.vix_term_structure import (
    compute_term_structure,
    evaluate_carry_signal,
    realized_vol_annualized,
)

# Reuses the killed forge_vix_intraday client_id slot (intraday strategy
# was killed 5/12 with no_restart=True; vix_carry is its conceptual
# replacement, not a coexisting strategy).
IBKR_CLIENT_ID = 119  # forge range 101-199; not in current use per 5/19 sweep
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "vix_carry"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"
SIGNALS_PATH = LOG_DIR / "signals.csv"

log = setup_logging("vix_carry")

PARAMS = {
    "version": "v1_20260519",
    "symbol": "SVXY",
    # Term-structure thresholds (Architect audit 2026-05-19 spec):
    "entry_contango_min": 1.05,
    "entry_vix_max": 20.0,
    "entry_realized_vol_max": 15.0,
    "exit_contango_max": 1.00,
    "exit_vix_min": 25.0,
    "hard_stop_pct": 0.08,         # -8% from entry triggers stop
    # Position sizing (fraction of max_notional_usd("etf")):
    "position_size_fraction": 0.5,  # SVXY position ≤ 50% of ETF cap
    # Realized vol window:
    "realized_vol_lookback_days": 5,
    # Daily evaluation time (UTC): 19:30 = 3:30pm ET, before US close.
    "eval_hour_utc": 19,
    "eval_minute_utc": 30,
}


# ── Data ───────────────────────────────────────────────────────────────────

def _fetch_history(period: str = "5y") -> pd.DataFrame:
    """Download daily closes for ^VIX, ^VIX3M, SPY, SVXY. Returns a single
    DataFrame indexed by date with columns: vix, vix3m, spy, svxy."""
    tickers = ["^VIX", "^VIX3M", "SPY", "SVXY"]
    out = yf.download(tickers, period=period, interval="1d",
                      progress=False, auto_adjust=False, group_by="ticker")
    rows = []
    for t in tickers:
        if (t, "Close") in out.columns:
            rows.append((t.replace("^", "").lower(), out[(t, "Close")]))
    df = pd.DataFrame({name: series for name, series in rows})
    df = df.dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# ── State ──────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "open_trade": None,   # {entry_ts, entry_px, position_size, stop_px, ...}
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _write_heartbeat(state: dict, last_signal: dict | None) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "vix_carry",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "open_trade": state.get("open_trade"),
        "last_signal": last_signal,
        "version": PARAMS["version"],
    }, indent=2, default=str))


# ── Trade lifecycle ────────────────────────────────────────────────────────

TRADE_FIELDS = [
    "ts", "side", "svxy_px", "vix", "vix3m", "contango_ratio", "spy_realized_vol_pct",
    "position_size", "stop_px", "pnl_usd", "pnl_pct", "exit_reason",
    "session_id", "config_version",
]


def _ensure_trade_csv():
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in TRADE_FIELDS}
        )


def _open_position(state: dict, df: pd.DataFrame, signal, ib=None) -> None:
    last = df.iloc[-1]
    entry_px = float(last["svxy"])
    cap_usd = max_notional_usd("etf", strategy_label="forge_vix_carry")
    if cap_usd <= 0:
        log.warning("max_notional_usd returned %s for forge_vix_carry; skipping entry", cap_usd)
        return

    target_notional = cap_usd * PARAMS["position_size_fraction"]
    if entry_px <= 0:
        log.warning("entry_px <= 0 (%s); skipping entry", entry_px)
        return
    pos_size = max(int(target_notional // entry_px), 0)
    if pos_size <= 0:
        log.warning("pos_size computed to 0 (cap=%.0f frac=%.2f entry=%.2f); skipping",
                    cap_usd, PARAMS["position_size_fraction"], entry_px)
        return

    stop_px = entry_px * (1.0 - PARAMS["hard_stop_pct"])
    target_px = entry_px * 1.50  # huge headroom; real exit driven by signal logic
    execution_venue = "signal_only"

    if ib is not None and not _SIGNAL_ONLY_MODE:
        contract = ibkr.make_contract(PARAMS["symbol"], "etf")
        try:
            ib.qualifyContracts(contract)
            result = ibkr.submit_bracket(
                ib, contract, direction="long", size=pos_size,
                stop_px=stop_px, target_px=target_px, price_decimals=2,
                est_entry_px=entry_px,
                strategy_label="forge_vix_carry",
            )
            if not result.entry.filled:
                log.error("REAL_ENTRY FAILED: %s", result.entry.reject_reason)
                return
            entry_px = float(result.entry.fill_price)
            stop_px = entry_px * (1.0 - PARAMS["hard_stop_pct"])
            execution_venue = "ibkr_paper"
        except Exception as exc:
            log.error("REAL_ENTRY EXCEPTION: %s", exc, exc_info=True)
            return

    state["open_trade"] = {
        "entry_ts": last.name.isoformat() if hasattr(last.name, "isoformat") else str(last.name),
        "entry_px": entry_px,
        "position_size": pos_size,
        "stop_px": stop_px,
        "execution_venue": execution_venue,
        "entry_contango": signal.contango_ratio,
        "entry_vix": signal.vix_level,
    }
    state["trade_count"] = state.get("trade_count", 0) + 1
    _save_state(state)

    _append_trade({
        "ts": state["open_trade"]["entry_ts"],
        "side": "ENTRY",
        "svxy_px": round(entry_px, 2),
        "vix": round(signal.vix_level, 2),
        "vix3m": round(signal.vix_level * signal.contango_ratio, 2),
        "contango_ratio": round(signal.contango_ratio, 4),
        "spy_realized_vol_pct": round(signal.spy_realized_vol_pct, 2),
        "position_size": pos_size,
        "stop_px": round(stop_px, 2),
        "session_id": state["session_id"],
        "config_version": PARAMS["version"],
    })
    log.info("ENTER_LONG_SVXY @ %.2f size=%d stop=%.2f contango=%.3f vix=%.1f",
             entry_px, pos_size, stop_px, signal.contango_ratio, signal.vix_level)


def _close_position(state: dict, df: pd.DataFrame, exit_reason: str, ib=None) -> None:
    ot = state.get("open_trade")
    if not ot:
        return
    last = df.iloc[-1]
    exit_px = float(last["svxy"])
    entry_px = float(ot["entry_px"])
    pos_size = int(ot["position_size"])
    pnl_usd = (exit_px - entry_px) * pos_size
    pnl_pct = (exit_px / entry_px) - 1.0

    if ib is not None and not _SIGNAL_ONLY_MODE and ot.get("execution_venue") == "ibkr_paper":
        try:
            contract = ibkr.make_contract(PARAMS["symbol"], "etf")
            ib.qualifyContracts(contract)
            ibkr.close_position_market(ib, contract, qty=pos_size, direction="long")
        except Exception as exc:
            log.error("REAL_EXIT EXCEPTION: %s", exc, exc_info=True)

    _append_trade({
        "ts": datetime.now(timezone.utc).isoformat(),
        "side": "EXIT",
        "svxy_px": round(exit_px, 2),
        "vix": round(float(last["vix"]), 2),
        "vix3m": round(float(last["vix3m"]), 2),
        "contango_ratio": round(float(last["vix3m"]) / float(last["vix"]), 4),
        "position_size": pos_size,
        "stop_px": round(float(ot["stop_px"]), 2),
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct * 100, 3),
        "exit_reason": exit_reason,
        "session_id": state["session_id"],
        "config_version": PARAMS["version"],
    })
    log.info("EXIT @ %.2f reason=%s pnl=%+.2f (%+.2f%%) held_from %s",
             exit_px, exit_reason, pnl_usd, pnl_pct * 100, ot["entry_ts"])
    state["open_trade"] = None
    _save_state(state)


# ── Cycle ──────────────────────────────────────────────────────────────────

def _evaluate_once(state: dict, ib=None) -> dict:
    """One evaluation cycle. Fetches data, computes signal, acts. Returns
    a dict describing what was decided (for heartbeat + logs)."""
    df = _fetch_history(period="60d")  # enough for realized-vol window + buffer
    last = df.iloc[-1]
    spy_window = df["spy"].iloc[-(PARAMS["realized_vol_lookback_days"] + 1):].tolist()
    rv_pct = realized_vol_annualized(spy_window)

    ts_obj = compute_term_structure(
        vix_close=float(last["vix"]),
        vix3m_close=float(last["vix3m"]),
        as_of=last.name if hasattr(last.name, "tzinfo") else datetime.now(timezone.utc),
    )
    has_pos = state.get("open_trade") is not None
    signal = evaluate_carry_signal(
        ts_obj,
        spy_realized_vol_pct=rv_pct,
        has_open_position=has_pos,
        entry_contango_min=PARAMS["entry_contango_min"],
        entry_vix_max=PARAMS["entry_vix_max"],
        entry_realized_vol_max=PARAMS["entry_realized_vol_max"],
        exit_contango_max=PARAMS["exit_contango_max"],
        exit_vix_min=PARAMS["exit_vix_min"],
    )

    # Hard stop also gets a chance to fire on every cycle, regardless of signal.
    if has_pos:
        ot = state["open_trade"]
        if float(last["svxy"]) <= float(ot["stop_px"]):
            _close_position(state, df, exit_reason="hard_stop_8pct", ib=ib)
            return {"action": "EXIT", "reason": "hard_stop_8pct", "svxy_px": float(last["svxy"])}

    if signal.action == "ENTER_LONG_SVXY":
        _open_position(state, df, signal, ib=ib)
    elif signal.action == "EXIT":
        _close_position(state, df, exit_reason=signal.reason, ib=ib)

    return {
        "action": signal.action,
        "reason": signal.reason,
        "contango_ratio": round(signal.contango_ratio, 4),
        "vix": round(signal.vix_level, 2),
        "spy_realized_vol_pct": round(signal.spy_realized_vol_pct, 2),
        "svxy_px": round(float(last["svxy"]), 2),
    }


# ── Backtest ───────────────────────────────────────────────────────────────

def backtest(period: str = "5y") -> dict:
    """Simulate the strategy bar-by-bar over historical data. Reports
    trades, win rate, profit factor, max drawdown, Sharpe. Used to
    validate the edge BEFORE going live — there is no point trading a
    strategy that doesn't backtest cleanly."""
    df = _fetch_history(period=period)
    if df.empty:
        return {"error": "no data fetched"}

    open_trade = None
    trades: list[dict] = []
    lb = PARAMS["realized_vol_lookback_days"]

    for i in range(lb + 1, len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        spy_window = df["spy"].iloc[i - lb - 1: i + 1].tolist()
        rv = realized_vol_annualized(spy_window)
        ts_obj = compute_term_structure(
            vix_close=float(row["vix"]),
            vix3m_close=float(row["vix3m"]),
            as_of=ts,
        )
        signal = evaluate_carry_signal(
            ts_obj, spy_realized_vol_pct=rv,
            has_open_position=open_trade is not None,
            entry_contango_min=PARAMS["entry_contango_min"],
            entry_vix_max=PARAMS["entry_vix_max"],
            entry_realized_vol_max=PARAMS["entry_realized_vol_max"],
            exit_contango_max=PARAMS["exit_contango_max"],
            exit_vix_min=PARAMS["exit_vix_min"],
        )

        # Hard stop first
        if open_trade is not None and float(row["svxy"]) <= open_trade["stop_px"]:
            entry_px = open_trade["entry_px"]
            exit_px = float(row["svxy"])
            trades.append({
                "entry_ts": open_trade["entry_ts"], "exit_ts": ts.isoformat(),
                "entry_px": entry_px, "exit_px": exit_px,
                "pnl_pct": (exit_px / entry_px - 1.0) * 100,
                "exit_reason": "hard_stop_8pct",
                "hold_days": (ts - open_trade["entry_ts_obj"]).days,
            })
            open_trade = None
            continue

        if signal.action == "ENTER_LONG_SVXY" and open_trade is None:
            open_trade = {
                "entry_ts": ts.isoformat(), "entry_ts_obj": ts,
                "entry_px": float(row["svxy"]),
                "stop_px": float(row["svxy"]) * (1.0 - PARAMS["hard_stop_pct"]),
            }
        elif signal.action == "EXIT" and open_trade is not None:
            entry_px = open_trade["entry_px"]
            exit_px = float(row["svxy"])
            trades.append({
                "entry_ts": open_trade["entry_ts"], "exit_ts": ts.isoformat(),
                "entry_px": entry_px, "exit_px": exit_px,
                "pnl_pct": (exit_px / entry_px - 1.0) * 100,
                "exit_reason": signal.reason,
                "hold_days": (ts - open_trade["entry_ts_obj"]).days,
            })
            open_trade = None

    # Stats
    if not trades:
        return {"trades": 0, "note": "no trades generated over period"}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    total_pct = sum(pnls)

    # Equity curve + max drawdown (sequential, compounded)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        equity *= (1.0 + p / 100.0)
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)

    days = (pd.to_datetime(trades[-1]["exit_ts"]) - pd.to_datetime(trades[0]["entry_ts"])).days or 1
    cagr = (equity ** (365.25 / days) - 1.0) * 100.0

    return {
        "period": period,
        "trades": len(trades),
        "win_rate": round(win_rate, 3),
        "profit_factor": round(pf, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "total_return_pct": round((equity - 1.0) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "first_entry": trades[0]["entry_ts"],
        "last_exit": trades[-1]["exit_ts"],
        "exit_reason_breakdown": _count_reasons(trades),
    }


def _count_reasons(trades: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for t in trades:
        r = str(t.get("exit_reason", "")).split("_")[0]  # bucket first token
        counts[r] = counts.get(r, 0) + 1
    return counts


# ── Main ───────────────────────────────────────────────────────────────────

def _connect_ib():
    import os
    from ib_insync import IB
    host = "127.0.0.1"
    port = int(os.getenv("IBKR_PORT", "7497"))
    ib = IB()
    ib.connect(host, port, clientId=IBKR_CLIENT_ID, timeout=15)
    return ib


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="One-shot signal evaluation; print only, no orders.")
    parser.add_argument("--evaluate", action="store_true",
                        help="One cycle: fetch, evaluate, act, save state.")
    parser.add_argument("--loop", action="store_true",
                        help="Daemon mode: sleep until eval_hour_utc, then evaluate.")
    parser.add_argument("--backtest", action="store_true",
                        help="Run historical simulation; print stats.")
    parser.add_argument("--period", default="5y",
                        help="Lookback for --backtest (yfinance period, default 5y).")
    parser.add_argument("--signal-only", action="store_true",
                        help="Disable real-order submission.")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)

    if args.backtest:
        stats = backtest(period=args.period)
        print(json.dumps(stats, indent=2, default=str))
        return 0

    if args.check:
        df = _fetch_history(period="60d")
        last = df.iloc[-1]
        spy_window = df["spy"].iloc[-(PARAMS["realized_vol_lookback_days"] + 1):].tolist()
        rv = realized_vol_annualized(spy_window)
        ts_obj = compute_term_structure(float(last["vix"]), float(last["vix3m"]))
        signal = evaluate_carry_signal(
            ts_obj, spy_realized_vol_pct=rv,
            has_open_position=_load_state().get("open_trade") is not None,
            entry_contango_min=PARAMS["entry_contango_min"],
            entry_vix_max=PARAMS["entry_vix_max"],
            entry_realized_vol_max=PARAMS["entry_realized_vol_max"],
            exit_contango_max=PARAMS["exit_contango_max"],
            exit_vix_min=PARAMS["exit_vix_min"],
        )
        print(json.dumps({
            "ts": str(last.name),
            "vix": float(last["vix"]),
            "vix3m": float(last["vix3m"]),
            "contango_ratio": ts_obj.contango_ratio,
            "spy_realized_vol_pct": rv,
            "svxy_px": float(last["svxy"]),
            "action": signal.action,
            "reason": signal.reason,
        }, indent=2, default=str))
        return 0

    if args.evaluate or args.loop:
        state = _load_state()
        ib = None
        if not _SIGNAL_ONLY_MODE:
            try:
                ib = _connect_ib()
            except Exception as exc:
                log.error("IB connect failed: %s — falling back to signal_only", exc)

        while True:
            try:
                result = _evaluate_once(state, ib=ib)
                _write_heartbeat(state, last_signal=result)
                log.info("cycle result: %s", json.dumps(result, default=str))
            except Exception as exc:
                log.error("cycle failed: %s", exc, exc_info=True)

            if not args.loop:
                break

            # Sleep until next eval_hour_utc:eval_minute_utc.
            now = datetime.now(timezone.utc)
            target = now.replace(
                hour=PARAMS["eval_hour_utc"], minute=PARAMS["eval_minute_utc"],
                second=0, microsecond=0,
            )
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = max(60, int((target - now).total_seconds()))
            log.info("sleeping %ds until next evaluation %s", sleep_s, target.isoformat())
            time.sleep(sleep_s)

        if ib is not None:
            try:
                ibkr.disconnect(ib)
            except Exception:
                pass
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
