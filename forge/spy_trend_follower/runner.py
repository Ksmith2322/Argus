"""forge_spy_trend_follower — long-equity-beta sleeve via SPY 50/200 SMA regime.

The fleet's structural gap (90d audit, 5/3): zero long-equity-beta strategies.
That's the literal reason the bot underperformed SPY by 8.34pp over a strong
bull window. This strategy closes that gap.

Logic (regime-based, not crossover):
  - Each day: compute SMA(50) and SMA(200) on SPY daily closes.
  - If SMA(50) > SMA(200) → maintain LONG SPY.
  - If SMA(50) < SMA(200) → flat (sit out the drawdown).
  - State change triggers entry/exit; no intraday churn.

Why regime-based not crossover:
  Crossover-based misses full days while waiting for the daily close to
  cross. Regime-based simply maintains the position any day SMA(50) is above.
  Same expected return, slightly less timing-sensitive.

Backtest expectation (well-documented historical edge):
  - 50/200 SMA regime on SPY: ~85% of buy-and-hold's upside, ~50% of its drawdown
  - Expected PF: 1.3-1.5 over 5+ years; lower in pure bull markets, higher when
    drawdowns happen
  - Trade frequency: VERY low — typically 2-6 regime changes per year
  - This is a STRUCTURAL position, not an active strategy

Risk:
  - In a sustained bull, this strategy buys/holds and basically replicates SPY
    minus a small timing tax. That's the goal.
  - In a 2008-style cliff, the regime flips to flat within ~30 trading days,
    capping max drawdown at ~15-20% of the holding (vs 50% for buy-and-hold).
  - Whipsaw risk in choppy markets: SMA crossings reverse, get stopped, repeat.
    The 50/200 separation reduces this vs faster MAs.

Capital allocation:
  - Default: 30% of fleet anchor at full conviction
  - Single position max ~$10K notional on $32K equity
  - No leverage; no stops other than regime flip

Usage:
    python -m forge.spy_trend_follower.runner --backtest --period 1y
    python -m forge.spy_trend_follower.runner --signal-only --loop
    python -m forge.spy_trend_follower.runner --live
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import logging
# 2026-05-12: was StreamHandler-only (no file). Replaced with setup_logging()
# so the runner now has forge/logs/spy_trend_follower/runner.log AND
# helio.signal_executor + helio.ibkr_execution errors flow to that file.
from forge.logging_setup import setup_logging
log = setup_logging("spy_trend_follower")

LOG_DIR = REPO / "forge" / "logs" / "spy_trend_follower"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_CSV = LOG_DIR / "signals.csv"
TRADES_CSV = LOG_DIR / "trades.csv"
STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
LOCK_FILE = LOG_DIR / "runner.lock"

PARAMS = {
    "version": "v1",
    "ticker": "SPY",
    "fast_sma": 50,
    "slow_sma": 200,
    "allocation_pct_of_anchor": 0.30,  # 30% of fleet anchor at full conviction
    "eval_hour_et": 16,                # evaluate at market close
    "min_history_days": 220,           # need >=200 + buffer for SMA(200)
}

IBKR_CLIENT_ID = 118  # next free in forge range 100-199
IBKR_DEFAULT_PORT = 7497


def _parse_ts(raw: str):
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _normalize_close(close):
    """yfinance returns DataFrame for some symbols; flatten to Series."""
    if hasattr(close, "ndim") and close.ndim == 2:
        return close.iloc[:, 0]
    return close


def compute_regime(closes) -> tuple[str, float, float]:
    """Return (regime, sma_fast, sma_slow). regime ∈ {LONG, FLAT}."""
    if len(closes) < PARAMS["slow_sma"]:
        return ("INSUFFICIENT_DATA", 0.0, 0.0)
    sma_fast = float(closes[-PARAMS["fast_sma"]:].mean())
    sma_slow = float(closes[-PARAMS["slow_sma"]:].mean())
    regime = "LONG" if sma_fast > sma_slow else "FLAT"
    return (regime, sma_fast, sma_slow)


# ── Backtest ──────────────────────────────────────────────────────────────

def run_backtest(period: str = "1y") -> int:
    """Replay 50/200 regime logic on yfinance daily closes. Reports PF, WR,
    max-DD vs buy-and-hold."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        log.error("yfinance + pandas required for backtest")
        return 1

    log.info(f"Backtest mode: SPY 50/200 regime, period={period}")
    df = yf.download(PARAMS["ticker"], period=period, interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        log.error("yfinance returned no data")
        return 1
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    closes = _normalize_close(df["Close"]).values
    dates = df.index

    n = len(closes)
    if n < PARAMS["slow_sma"]:
        log.error(f"Need at least {PARAMS['slow_sma']} daily bars; got {n}")
        return 1

    # Replay
    position = "FLAT"
    entry_px = 0.0
    entry_date = None
    trades = []
    for i in range(PARAMS["slow_sma"], n):
        window = closes[: i + 1]
        regime, sma_fast, sma_slow = compute_regime(window)
        date = dates[i]
        px = float(closes[i])

        if position == "FLAT" and regime == "LONG":
            position = "LONG"
            entry_px = px
            entry_date = date
        elif position == "LONG" and regime == "FLAT":
            pnl_pct = (px - entry_px) / entry_px * 100.0
            trades.append({
                "entry_date": entry_date,
                "exit_date": date,
                "entry_px": entry_px,
                "exit_px": px,
                "pnl_pct": pnl_pct,
                "duration_days": (date - entry_date).days if entry_date else 0,
            })
            position = "FLAT"
            entry_px = 0.0
            entry_date = None

    # Mark-to-market open position at end
    if position == "LONG":
        px = float(closes[-1])
        pnl_pct = (px - entry_px) / entry_px * 100.0
        trades.append({
            "entry_date": entry_date,
            "exit_date": dates[-1],
            "entry_px": entry_px,
            "exit_px": px,
            "pnl_pct": pnl_pct,
            "duration_days": (dates[-1] - entry_date).days if entry_date else 0,
            "open": True,
        })

    if not trades:
        log.info("Backtest: 0 regime changes in window — strategy stayed in single state")
        # Compare to buy-and-hold
        bh = (closes[-1] - closes[PARAMS["slow_sma"]]) / closes[PARAMS["slow_sma"]] * 100.0
        log.info(f"Buy-and-hold over same window: {bh:+.2f}%")
        return 0

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] < 0]
    wr = len(wins) / len(trades) * 100
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    avg_pnl = total_pnl / len(trades)
    avg_duration = sum(t["duration_days"] for t in trades) / len(trades)

    bh_pct = (closes[-1] - closes[PARAMS["slow_sma"]]) / closes[PARAMS["slow_sma"]] * 100.0

    print()
    print("=" * 70)
    print(f"  SPY TREND-FOLLOWER BACKTEST — period={period}")
    print("=" * 70)
    print(f"  trades:           {len(trades)}")
    print(f"  win rate:         {wr:.1f}%")
    print(f"  PF:               {pf:.2f}")
    print(f"  total return:     {total_pnl:+.2f}%")
    print(f"  avg per trade:    {avg_pnl:+.2f}%")
    print(f"  avg duration:     {avg_duration:.0f} days")
    print()
    print(f"  buy-and-hold:     {bh_pct:+.2f}%")
    print(f"  capture ratio:    {(total_pnl / bh_pct * 100) if bh_pct else 0:.1f}%  (regime captured % of buy-and-hold)")
    print()
    print("  Trade detail:")
    for t in trades:
        marker = " (open)" if t.get("open") else ""
        print(f"    {t['entry_date'].strftime('%Y-%m-%d')} -> {t['exit_date'].strftime('%Y-%m-%d')}  "
              f"${t['entry_px']:.2f} -> ${t['exit_px']:.2f}  "
              f"{t['pnl_pct']:+.2f}%  ({t['duration_days']}d){marker}")
    return 0


# ── Live / signal-only ────────────────────────────────────────────────────

def write_heartbeat(mode: str, regime: str, sma_fast: float, sma_slow: float,
                    last_close: float, position: str = "FLAT") -> None:
    hb = {
        "system": "spy_trend_follower",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "mode": mode,
        "ticker": PARAMS["ticker"],
        "regime": regime,
        "sma_fast": round(sma_fast, 2),
        "sma_slow": round(sma_slow, 2),
        "last_close": round(last_close, 2),
        "position": position,
    }
    try:
        HEARTBEAT_PATH.write_text(json.dumps(hb, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"failed to write heartbeat: {e}")


def append_signal_row(date_str: str, last_close: float, sma_fast: float,
                      sma_slow: float, regime: str, action: str) -> None:
    cols = ["ts", "ticker", "last_close", "sma_50", "sma_200", "regime", "action"]
    write_header = not SIGNALS_CSV.exists() or SIGNALS_CSV.stat().st_size == 0
    with SIGNALS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(cols)
        w.writerow([date_str, PARAMS["ticker"], f"{last_close:.4f}",
                    f"{sma_fast:.4f}", f"{sma_slow:.4f}", regime, action])


def evaluate_once(mode: str, dry_run: bool = False) -> None:
    """Single evaluation: fetch daily bars, compute regime, log signal."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance required")
        return

    df = yf.download(PARAMS["ticker"], period="1y", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        log.warning("yfinance returned no data; skipping cycle")
        return
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    closes = _normalize_close(df["Close"]).values
    if len(closes) < PARAMS["slow_sma"]:
        log.error(f"Insufficient history: {len(closes)} bars < {PARAMS['slow_sma']}")
        return

    last_close = float(closes[-1])
    regime, sma_fast, sma_slow = compute_regime(closes)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log.info(f"SPY=${last_close:.2f}  SMA50=${sma_fast:.2f}  SMA200=${sma_slow:.2f}  "
             f"regime={regime}  separation={(sma_fast/sma_slow-1)*100:+.2f}%")

    # Read prior state
    prior_position = "FLAT"
    if STATE_PATH.exists():
        try:
            prior = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            prior_position = prior.get("position", "FLAT")
        except Exception:
            pass

    # Decide action
    desired_position = "LONG" if regime == "LONG" else "FLAT"
    action = "HOLD"
    if prior_position == "FLAT" and desired_position == "LONG":
        action = "ENTER_LONG"
    elif prior_position == "LONG" and desired_position == "FLAT":
        action = "EXIT_LONG"

    append_signal_row(today_str, last_close, sma_fast, sma_slow, regime, action)
    write_heartbeat(mode, regime, sma_fast, sma_slow, last_close, desired_position)

    # Save state
    state = {
        "position": desired_position,
        "regime": regime,
        "last_eval_ts": datetime.now(timezone.utc).isoformat(),
        "last_close": last_close,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

    if dry_run:
        log.info(f"[DRY-RUN] would execute action={action}")
        return

    if mode == "signal_only":
        log.info(f"[SIGNAL-ONLY] action={action} — no IBKR submission")
        return

    if action in ("ENTER_LONG", "EXIT_LONG"):
        _execute_action(action, last_close)


def _execute_action(action: str, last_close: float) -> None:
    """Live IBKR submission. Only LIVE mode."""
    try:
        from helio import ibkr_execution as ibkr
        from helio.fleet_sizing import get_sizing_anchor_usd, get_allocation_factor
    except ImportError as e:
        log.error(f"helio imports failed: {e}")
        return

    # 2026-05-23: respect allocation_factor before computing notional. Previously
    # the runner hardcoded PARAMS["allocation_pct_of_anchor"] (30%) regardless
    # of allocation_factors.json. With the strategy at allocation 0.0 (deallocated
    # per allocation analysis), flipping to --live would have deployed $75K of SPY
    # on a $250K account silently.
    try:
        alloc_factor = float(get_allocation_factor("forge_spy_trend_follower"))
    except Exception as e:
        log.error(f"could not read allocation_factor: {e} — refusing to trade (fail-closed)")
        return
    if alloc_factor <= 0.0:
        log.info(f"[ALLOC-GATE] action={action} blocked: allocation_factor={alloc_factor} "
                 f"(strategy deallocated; no order submitted)")
        return

    try:
        anchor = float(get_sizing_anchor_usd())
    except Exception as e:
        log.error(f"could not read sizing anchor: {e}")
        return

    effective_pct = PARAMS["allocation_pct_of_anchor"] * alloc_factor
    notional = anchor * effective_pct
    shares = int(notional / last_close)
    if shares < 1:
        log.warning(f"computed shares={shares} from notional ${notional:.0f} "
                    f"@ ${last_close:.2f} (alloc_factor={alloc_factor}) — too small to trade")
        return

    log.info(f"[LIVE] {action}: {shares} shares SPY @ ~${last_close:.2f} "
             f"(notional ${shares*last_close:.0f}, "
             f"{effective_pct*100:.1f}% of ${anchor:.0f} "
             f"[base {PARAMS['allocation_pct_of_anchor']*100:.0f}% × alloc_factor {alloc_factor:.2f}])")

    try:
        ib = ibkr.connect(IBKR_CLIENT_ID)
        contract = ibkr.make_contract("SPY", "stock")
        if action == "ENTER_LONG":
            # No bracket here — exit is regime-driven, not stop-driven
            from ib_insync import MarketOrder
            order = MarketOrder("BUY", shares)
            trade = ib.placeOrder(contract, order)
            log.info(f"BUY order submitted: orderId={trade.order.orderId}")
        elif action == "EXIT_LONG":
            from ib_insync import MarketOrder
            position = ibkr.query_position(ib, contract)
            qty = abs(int(position.position)) if position and position.position else 0
            if qty > 0:
                order = MarketOrder("SELL", qty)
                trade = ib.placeOrder(contract, order)
                log.info(f"SELL order submitted (qty={qty}): orderId={trade.order.orderId}")
            else:
                log.warning("EXIT_LONG action but no broker position to close")
        ibkr.disconnect(ib)
    except Exception as e:
        log.error(f"order execution failed: {e}")


def run_loop(mode: str, interval_min: int, dry_run: bool = False) -> int:
    """Continuous loop. mode='signal_only' or 'live'."""
    log.info(f"Starting {mode.upper()} LOOP mode (interval={interval_min}min)")
    while True:
        try:
            evaluate_once(mode, dry_run)
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0
        except Exception as e:
            log.error(f"cycle error: {e}")
        log.info(f"Sleeping {interval_min}min until next eval...")
        time.sleep(interval_min * 60)


# ── CLI ───────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(prog="spy_trend_follower")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest", action="store_true")
    mode.add_argument("--signal-only", action="store_true")
    mode.add_argument("--live", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--period", default="1y")
    p.add_argument("--interval-min", type=int, default=240,  # 4hr — daily strategy doesn't need fast loop
                   help="Loop interval in minutes (default 240 = every 4hr)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.backtest:
        return run_backtest(args.period)
    elif args.signal_only:
        if args.loop:
            return run_loop("signal_only", args.interval_min, args.dry_run)
        else:
            evaluate_once("signal_only", args.dry_run)
            return 0
    elif args.live:
        if args.loop:
            return run_loop("live", args.interval_min, args.dry_run)
        else:
            evaluate_once("live", args.dry_run)
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
