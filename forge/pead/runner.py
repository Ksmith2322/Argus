"""forge.pead — runner + backtest for Post-Earnings Drift strategy.

Modes:
  --backtest         Simulate the strategy over all available earnings events
                     in the universe; report PF/WR/CAGR/maxDD.
  --check            One-shot: print any qualifying earnings events that fired
                     in the last N trading days (default 2) — no orders.
  --evaluate         One cycle: scan universe, take any new entries, manage
                     open positions, save state.
  --loop             Daemon mode: --evaluate at the configured time daily.

Universe is read from apollo/data/core_watchlist.json (16-name PEAD watchlist
curated by the apollo team — used directly, not reinvented)."""
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
from helio.fleet_sizing import max_notional_usd
from helio import ibkr_execution as ibkr
from helio.pead_signal import (
    PEADCandidate, evaluate_pead_signal, should_exit,
    SURPRISE_PCT_MIN, VOLUME_RATIO_MIN, GAP_PCT_MIN, HOLD_DAYS, ATR_STOP_MULT,
)
from helio.pead_research import fetch_earnings_for_ticker

IBKR_CLIENT_ID = 120  # forge range; not in current use per 5/19 grep
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "pead"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"
APOLLO_WATCHLIST = REPO / "apollo" / "data" / "core_watchlist.json"

log = setup_logging("pead")

PARAMS = {
    "version": "v1_20260519",
    "surprise_pct_min": SURPRISE_PCT_MIN,
    "volume_ratio_min": VOLUME_RATIO_MIN,
    "gap_pct_min": GAP_PCT_MIN,
    "hold_days": HOLD_DAYS,
    "atr_stop_mult": ATR_STOP_MULT,
    "atr_period": 14,
    # Position sizing — fraction of stock asset-class cap, per name.
    # 16 names × 0.15 = max 2.4× equity if every single name is on at once
    # (which never happens since earnings are staggered).
    "position_size_fraction": 0.15,
    # Daily evaluation time (UTC): 13:30 = 9:30 ET, just after US open.
    "eval_hour_utc": 13,
    "eval_minute_utc": 35,
    # Lookback window for "did earnings happen recently" check.
    "earnings_recent_lookback_days": 2,
}


# ── Universe ───────────────────────────────────────────────────────────────

def load_universe() -> list[str]:
    """Load Apollo's curated PEAD watchlist (16 names across 3 tiers)."""
    try:
        data = json.loads(APOLLO_WATCHLIST.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for tier in ("tier_1", "tier_2", "tier_3"):
        t = data.get(tier, {}) or {}
        for s in t.get("stocks", []) or []:
            s = str(s).strip().upper()
            if s and s not in out:
                out.append(s)
    return out


# ── Data ───────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l = df["High"], df["Low"]
    pc = df["Close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _fetch_price_history(ticker: str, period: str = "5y") -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval="1d",
                     progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _vol_avg_30d(df: pd.DataFrame, asof_idx: int) -> float | None:
    if asof_idx < 30:
        return None
    lookback = df["Volume"].iloc[asof_idx - 30: asof_idx]
    if lookback.empty:
        return None
    return float(lookback.mean())


# ── State ──────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "open_positions": {},  # ticker -> {entry_ts, entry_px, atr, qty, ...}
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "runtime_start": time.time(),
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


# ── Trade lifecycle (live) ─────────────────────────────────────────────────

TRADE_FIELDS = [
    "ts", "side", "ticker", "px", "qty", "surprise_pct", "vol_ratio", "gap_pct",
    "atr_entry", "stop_px", "pnl_usd", "pnl_pct", "exit_reason", "session_id", "config_version",
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


def _write_heartbeat(state: dict, last_scan: dict | None) -> None:
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "pead",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "open_positions": state.get("open_positions", {}),
        "last_scan": last_scan,
        "universe": load_universe(),
        "version": PARAMS["version"],
    }, indent=2, default=str))


# ── Backtest ───────────────────────────────────────────────────────────────

def backtest(period: str = "5y", verbose: bool = False,
             universe: list[str] | None = None) -> dict:
    """Simulate PEAD across the universe's full earnings history.

    For each ticker:
      1. Fetch earnings history (yfinance Ticker.earnings_dates).
      2. Fetch price history (daily OHLCV).
      3. For each historical earnings event, build a PEADCandidate using
         the announce-day volume, prior close, next-day open, and 14d ATR.
      4. If evaluate_pead_signal returns ENTER_LONG, simulate the trade:
         entry at next-day open, exit on -1.5 ATR stop OR hold_days.
      5. Aggregate into trade list, compute PF/WR/etc.

    If `universe` is None, uses load_universe() (Apollo's curated 16 names).
    Pass a custom universe for generalization tests (e.g., random SPX-30).
    """
    universe = universe if universe is not None else load_universe()
    if not universe:
        return {"error": "empty universe — check apollo/data/core_watchlist.json"}

    trades: list[dict] = []
    per_ticker: dict[str, int] = {}

    for ticker in universe:
        if verbose:
            print(f"  fetching {ticker}...")
        events = fetch_earnings_for_ticker(ticker)
        if not events:
            per_ticker[ticker] = 0
            continue
        try:
            price = _fetch_price_history(ticker, period=period)
        except Exception as exc:
            log.warning("price fetch failed for %s: %s", ticker, exc)
            per_ticker[ticker] = 0
            continue
        if price.empty:
            per_ticker[ticker] = 0
            continue

        atr_series = _atr(price, PARAMS["atr_period"])
        n_trades = 0

        for event in events:
            # Locate the announcement date in price index.
            try:
                ann_dt = pd.to_datetime(event.date, utc=True)
            except Exception:
                continue
            # Find first price bar at or after the announcement.
            idx_after = price.index.searchsorted(ann_dt)
            if idx_after >= len(price) - PARAMS["hold_days"] - 1:
                continue
            # Skip if surprise data missing.
            if event.surprise_pct is None:
                continue
            announce_bar_idx = idx_after  # bar on/after announcement
            next_open_idx = announce_bar_idx + 1
            if next_open_idx >= len(price):
                continue

            announce_volume = float(price["Volume"].iloc[announce_bar_idx])
            avg_vol = _vol_avg_30d(price, announce_bar_idx)
            prior_close = float(price["Close"].iloc[announce_bar_idx])
            next_open = float(price["Open"].iloc[next_open_idx])
            atr_now = atr_series.iloc[announce_bar_idx]
            if pd.isna(atr_now) or atr_now <= 0:
                continue
            atr_at_entry = float(atr_now)

            cand = PEADCandidate(
                ticker=ticker,
                announcement_date=event.date,
                surprise_pct=float(event.surprise_pct),
                eps_actual=event.eps_actual,
                eps_estimate=event.eps_estimate,
                announce_volume=announce_volume,
                avg_volume_30d=avg_vol,
                next_day_open=next_open,
                prior_close=prior_close,
                atr_at_entry=atr_at_entry,
            )
            decision = evaluate_pead_signal(
                cand,
                surprise_pct_min=PARAMS["surprise_pct_min"],
                volume_ratio_min=PARAMS["volume_ratio_min"],
                gap_pct_min=PARAMS["gap_pct_min"],
            )
            if decision.action != "ENTER_LONG":
                continue

            # Simulate exit
            entry_px = next_open
            entry_idx = next_open_idx
            stop_px = entry_px - PARAMS["atr_stop_mult"] * atr_at_entry
            exit_idx = None
            exit_px = None
            exit_reason = ""

            for j in range(entry_idx + 1, min(entry_idx + 1 + PARAMS["hold_days"], len(price))):
                today_low = float(price["Low"].iloc[j])
                today_close = float(price["Close"].iloc[j])
                if today_low <= stop_px:
                    exit_idx = j
                    exit_px = stop_px
                    exit_reason = "atr_stop"
                    break
            if exit_px is None:
                final_idx = min(entry_idx + PARAMS["hold_days"], len(price) - 1)
                exit_idx = final_idx
                exit_px = float(price["Close"].iloc[final_idx])
                exit_reason = "hold_period_end"

            pnl_pct = (exit_px / entry_px - 1.0) * 100.0
            trades.append({
                "ticker": ticker,
                "ann_date": event.date,
                "entry_date": price.index[entry_idx].isoformat(),
                "exit_date": price.index[exit_idx].isoformat(),
                "entry_px": entry_px,
                "exit_px": exit_px,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
                "surprise_pct": event.surprise_pct,
                "hold_bars": exit_idx - entry_idx,
            })
            n_trades += 1

        per_ticker[ticker] = n_trades

    # Stats
    if not trades:
        return {
            "universe_size": len(universe),
            "trades": 0,
            "per_ticker_trades": per_ticker,
            "note": "no qualifying earnings events produced trades — check yfinance earnings_dates availability",
        }

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)

    # Equity curve (sequential, but multiple positions could overlap;
    # for first-cut, treat as if non-overlapping with equal weight).
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0 * PARAMS["position_size_fraction"])
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    # Compute time span for CAGR
    try:
        first_dt = min(pd.to_datetime(t["entry_date"]) for t in trades)
        last_dt = max(pd.to_datetime(t["exit_date"]) for t in trades)
        days = max(1, (last_dt - first_dt).days)
        cagr = (eq ** (365.25 / days) - 1.0) * 100.0
    except Exception:
        cagr = 0.0

    return {
        "universe_size": len(universe),
        "trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "total_pct_unscaled": round(sum(pnls), 2),
        "equity_growth_pct_scaled": round((eq - 1.0) * 100, 2),
        "cagr_pct_scaled": round(cagr, 2),
        "max_drawdown_pct_scaled": round(max_dd * 100, 2),
        "per_ticker_trades": per_ticker,
        "exit_reason_breakdown": _count_reasons(trades),
    }


def _count_reasons(trades: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in trades:
        r = str(t.get("exit_reason", "unknown"))
        out[r] = out.get(r, 0) + 1
    return out


# ── Live cycle ─────────────────────────────────────────────────────────────

def _scan_for_entries(state: dict, ib=None) -> list[dict]:
    """Single scan pass: for each universe ticker, check whether an
    earnings event happened in the recent lookback window. Returns a
    list of fired entries (one dict per ticker that entered)."""
    universe = load_universe()
    lookback = PARAMS["earnings_recent_lookback_days"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback + 1)).date().isoformat()
    fired: list[dict] = []

    for ticker in universe:
        if ticker in state.get("open_positions", {}):
            continue  # already long this name
        try:
            events = fetch_earnings_for_ticker(ticker)
        except Exception:
            continue
        if not events:
            continue
        # Find most recent event within lookback
        recent = [e for e in events if e.date >= cutoff and e.surprise_pct is not None]
        if not recent:
            continue
        latest = max(recent, key=lambda e: e.date)
        try:
            price = _fetch_price_history(ticker, period="60d")
        except Exception:
            continue
        if price.empty:
            continue
        atr_series = _atr(price, PARAMS["atr_period"])

        try:
            ann_dt = pd.to_datetime(latest.date, utc=True)
        except Exception:
            continue
        idx_after = price.index.searchsorted(ann_dt)
        if idx_after >= len(price) - 1:
            continue
        announce_idx = idx_after
        next_open_idx = announce_idx + 1
        atr_now = atr_series.iloc[announce_idx]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        announce_volume = float(price["Volume"].iloc[announce_idx])
        avg_vol = _vol_avg_30d(price, announce_idx)
        prior_close = float(price["Close"].iloc[announce_idx])
        next_open = float(price["Open"].iloc[next_open_idx]) if next_open_idx < len(price) else float(price["Open"].iloc[-1])

        cand = PEADCandidate(
            ticker=ticker,
            announcement_date=latest.date,
            surprise_pct=float(latest.surprise_pct),
            announce_volume=announce_volume,
            avg_volume_30d=avg_vol,
            next_day_open=next_open,
            prior_close=prior_close,
            atr_at_entry=float(atr_now),
        )
        decision = evaluate_pead_signal(cand)
        if decision.action != "ENTER_LONG":
            continue

        # Determine position size
        cap_usd = max_notional_usd("etf", strategy_label="forge_pead")
        target_notional = cap_usd * PARAMS["position_size_fraction"]
        if next_open <= 0:
            continue
        qty = max(int(target_notional // next_open), 0)
        if qty <= 0:
            continue
        stop_px = next_open - PARAMS["atr_stop_mult"] * float(atr_now)

        if ib is not None and not _SIGNAL_ONLY_MODE:
            contract = ibkr.make_contract(ticker, "etf")
            try:
                ib.qualifyContracts(contract)
                target_px = next_open + 10 * float(atr_now)  # huge headroom
                result = ibkr.submit_bracket(
                    ib, contract, direction="long", size=qty,
                    stop_px=stop_px, target_px=target_px, price_decimals=2,
                    est_entry_px=next_open, strategy_label="forge_pead",
                )
                if not result.entry.filled:
                    log.error("REAL_ENTRY FAILED %s: %s", ticker, result.entry.reject_reason)
                    continue
                entry_fill_px = float(result.entry.fill_price)
            except Exception as exc:
                log.error("REAL_ENTRY EXCEPTION %s: %s", ticker, exc, exc_info=True)
                continue
        else:
            entry_fill_px = next_open

        state.setdefault("open_positions", {})[ticker] = {
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "entry_px": entry_fill_px,
            "atr_at_entry": float(atr_now),
            "stop_px": stop_px,
            "qty": qty,
            "ann_date": latest.date,
            "surprise_pct": float(latest.surprise_pct),
            "bars_held": 0,
        }
        state["trade_count"] = state.get("trade_count", 0) + 1
        _append_trade({
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": "ENTRY",
            "ticker": ticker,
            "px": round(entry_fill_px, 2),
            "qty": qty,
            "surprise_pct": round(latest.surprise_pct, 2),
            "atr_entry": round(float(atr_now), 4),
            "stop_px": round(stop_px, 2),
            "session_id": state["session_id"],
            "config_version": PARAMS["version"],
        })
        fired.append({"ticker": ticker, "px": entry_fill_px, "qty": qty,
                      "surprise_pct": latest.surprise_pct})
        log.info("ENTER %s qty=%d @ %.2f stop=%.2f surprise=%.1f%%",
                 ticker, qty, entry_fill_px, stop_px, latest.surprise_pct)

    return fired


def _manage_open_positions(state: dict, ib=None) -> list[dict]:
    """For each open position, check hold-period + ATR stop. Close if needed."""
    closed: list[dict] = []
    for ticker, pos in list(state.get("open_positions", {}).items()):
        try:
            price = _fetch_price_history(ticker, period="60d")
        except Exception:
            continue
        if price.empty:
            continue
        # Last bar's close + low for exit decision
        last_low = float(price["Low"].iloc[-1])
        last_close = float(price["Close"].iloc[-1])
        entry_dt = pd.to_datetime(pos["entry_ts"], utc=True)
        # Count trading bars since entry
        entry_idx = price.index.searchsorted(entry_dt)
        today_idx = len(price) - 1
        should, reason = should_exit(
            entry_px=float(pos["entry_px"]),
            entry_date_idx=entry_idx,
            today_idx=today_idx,
            today_low=last_low,
            today_close=last_close,
            atr_at_entry=float(pos["atr_at_entry"]),
            hold_days=PARAMS["hold_days"],
            stop_mult=PARAMS["atr_stop_mult"],
        )
        if not should:
            continue
        exit_px = float(pos["stop_px"]) if reason == "atr_stop" else last_close
        pnl = (exit_px - float(pos["entry_px"])) * float(pos["qty"])
        pnl_pct = (exit_px / float(pos["entry_px"]) - 1.0) * 100.0

        if ib is not None and not _SIGNAL_ONLY_MODE:
            try:
                contract = ibkr.make_contract(ticker, "etf")
                ib.qualifyContracts(contract)
                ibkr.close_position_market(ib, contract, qty=int(pos["qty"]), direction="long")
            except Exception as exc:
                log.error("REAL_EXIT EXCEPTION %s: %s", ticker, exc, exc_info=True)

        _append_trade({
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": "EXIT",
            "ticker": ticker,
            "px": round(exit_px, 2),
            "qty": int(pos["qty"]),
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 3),
            "exit_reason": reason,
            "session_id": state["session_id"],
            "config_version": PARAMS["version"],
        })
        del state["open_positions"][ticker]
        closed.append({"ticker": ticker, "exit_px": exit_px, "pnl_usd": pnl, "reason": reason})
        log.info("EXIT %s @ %.2f pnl=%+.2f (%+.2f%%) reason=%s", ticker, exit_px, pnl, pnl_pct, reason)

    return closed


def _evaluate_once(state: dict, ib=None) -> dict:
    fired = _scan_for_entries(state, ib=ib)
    closed = _manage_open_positions(state, ib=ib)
    _save_state(state)
    return {
        "fired": fired,
        "closed": closed,
        "open_count": len(state.get("open_positions", {})),
    }


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
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--period", default="5y")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)

    if args.backtest:
        stats = backtest(period=args.period, verbose=args.verbose)
        print(json.dumps(stats, indent=2, default=str))
        return 0

    if args.check:
        universe = load_universe()
        print(f"PEAD universe: {len(universe)} tickers")
        print(f"universe: {universe}")
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
                _write_heartbeat(state, last_scan=result)
                log.info("scan complete: %s", json.dumps(result, default=str))
            except Exception as exc:
                log.error("cycle failed: %s", exc, exc_info=True)

            if not args.loop:
                break
            now = datetime.now(timezone.utc)
            target = now.replace(hour=PARAMS["eval_hour_utc"],
                                 minute=PARAMS["eval_minute_utc"],
                                 second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = max(60, int((target - now).total_seconds()))
            log.info("sleeping %ds until next scan %s", sleep_s, target.isoformat())
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
