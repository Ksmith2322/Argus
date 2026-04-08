"""Argus Flow Backtest Engine — replay historical bars through live strategy logic.

Execution model (IMPORTANT — differs from naive same-bar entry):
- Signal is evaluated after bar i closes (features computed from bars up to i)
- Entry executes at bar i+1 OPEN price (not bar i close)
- This matches live behavior where signal → next available price
- Stops/targets checked against intra-bar high/low with PESSIMISTIC ordering
  (stop checked first for the position's direction)
- Trailing stop uses bar close (matches live mid-based logic)

Usage:
    python -m argus_flow.backtest.engine --config argus_flow/configs/eurusd_t4_paper_v1.json --data argus_flow/data/ibkr_eurusd_1m.csv
    python -m argus_flow.backtest.engine --config argus_flow/configs/mes_range_paper_v1.json --data argus_flow/data/mes_1m.csv --output results/
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import math

import numpy as np
import pandas as pd

# Import EXACT same logic as the live runner
from argus_flow.runner_unified import (
    BarBuffer,
    classify_regime,
    compute_features_fx,
    compute_features_futures,
    check_trigger_fx,
    check_trigger_futures,
    _in_session,
)
from argus_flow.strategies.fvg_detector import detect_fvgs, find_fvg_fill_entries
from argus_flow.strategies.liquidity_sweep import find_swing_points, detect_sweeps
from argus_flow.strategies.volume_profile import calculate_volume_profile

REPO = Path(__file__).resolve().parents[2]


@dataclass
class BacktestTrade:
    entry_time: str
    direction: str
    entry_price: float
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    regime: str = ""
    efficiency_ratio: float = 0.0
    trend_strength: float = 0.0
    duration_min: float = 0.0


@dataclass
class BacktestState:
    position: str = "FLAT"
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    entry_time: Optional[datetime] = None
    timeout_time: Optional[datetime] = None
    last_signal_time: Optional[datetime] = None
    trade_count: int = 0
    direction: str = ""
    initial_risk: float = 0.0
    # Regime at entry (stamped on close)
    entry_regime: str = ""
    entry_efficiency: float = 0.0
    entry_trend: float = 0.0


def run_backtest(
    config: dict,
    bars: pd.DataFrame,
    override_params: dict | None = None,
    *,
    slippage_pips: float = 1.0,
    commission_per_lot_usd: float = 2.0,
    friday_close: bool = True,
    initial_equity: float = 10000.0,
) -> dict:
    """Run a single backtest.

    Args:
        config: instrument config dict (same format as *_paper_v1.json)
        bars: DataFrame with ts, open, high, low, close, volume columns
        override_params: optional dict to override config values (for sweeps)

    Returns:
        dict with trades, metrics, and metadata
    """
    cfg = json.loads(json.dumps(config))  # deep copy
    if override_params:
        for key, val in override_params.items():
            parts = key.split(".")
            target = cfg
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            target[parts[-1]] = val

    symbol = cfg.get("symbol", "???")
    instrument_type = cfg.get("instrument_type", "forex")
    strategy = cfg.get("strategy", "range_accel")
    trigger = cfg.get("trigger", {})
    risk = cfg.get("risk", {})

    is_fx = instrument_type == "forex"
    if is_fx:
        stop_pips = risk.get("stop_pips", 20)
        target_pips = risk.get("target_pips", 40)
        pip_size = 0.01 if "JPY" in symbol.upper() else 0.0001
    else:
        stop_bps = risk.get("stop_bps", 30)
        target_bps = risk.get("target_bps", 60)

    timeout_min = risk.get("timeout_minutes", 60)
    min_gap = risk.get("min_signal_gap_minutes", 15)
    regime_gate = cfg.get("regime_gate", "LOG_ONLY")
    atr_stop_mult = risk.get("atr_stop_mult", 0)
    atr_target_mult = risk.get("atr_target_mult", 0)

    from argus_flow.runner_unified import InstrumentRunner
    regime_compatible = InstrumentRunner._regime_compatible

    # Backtest cost model: slippage applied to fills, commission deducted from PnL
    bt_cfg = cfg.get("backtest", {})
    _slippage_pips = bt_cfg.get("slippage_pips", slippage_pips)
    _commission_usd = bt_cfg.get("commission_per_lot_usd", commission_per_lot_usd)
    _friday_close = bt_cfg.get("friday_close", friday_close)
    _initial_equity = bt_cfg.get("initial_equity", initial_equity)
    _FRIDAY_CLOSE_MINUTE = 20 * 60 + 45  # 20:45 UTC — matches live runner

    def _apply_slippage(price: float, direction: str, side: str) -> float:
        """Worsen fill price by slippage. side='entry' or 'exit'."""
        if _slippage_pips <= 0:
            return price
        slip = _slippage_pips * pip_size if is_fx else price * (_slippage_pips / 10000)
        if side == "entry":
            return price + slip if direction == "long" else price - slip
        else:  # exit
            return price - slip if direction == "long" else price + slip

    buf = BarBuffer(maxlen=300)
    state = BacktestState()
    trades: list[BacktestTrade] = []
    signals_log: list[dict] = []
    skipped_bars = 0
    gap_count = 0

    bars = bars.reset_index(drop=True)
    total_bars = len(bars)

    # Candle gap detection
    if total_bars > 1:
        try:
            ts_series = pd.to_datetime(bars["ts"], utc=True)
            diffs = ts_series.diff().dt.total_seconds()
            gaps = diffs[diffs > 120]  # gaps > 2 minutes
            gap_count = len(gaps)
            if gap_count > 0:
                import logging
                _bt_log = logging.getLogger("argus.backtest")
                for idx, gap_s in list(gaps.items())[:10]:
                    _bt_log.warning(f"CANDLE_GAP: {gap_s:.0f}s gap at row {idx} ({bars['ts'].iloc[idx]})")
                if gap_count > 10:
                    _bt_log.warning(f"... and {gap_count - 10} more gaps")
        except Exception:
            pass

    def compute_features():
        if instrument_type in ("future", "crypto"):
            return compute_features_futures(buf)
        return compute_features_fx(buf)

    def check_trigger_fn(features):
        if strategy == "fvg":
            return _bt_check_fvg(buf, features, trigger)
        elif strategy == "liquidity_sweep":
            return _bt_check_sweep(buf, features, trigger)
        elif strategy == "volume_profile":
            return _bt_check_vp(buf, features, trigger)
        if instrument_type in ("future", "crypto"):
            return check_trigger_futures(features, cfg)
        return check_trigger_fx(features, cfg)

    def compute_atr():
        """Same ATR logic as live runner._compute_atr() — proper True Range."""
        df = buf.to_df()
        if len(df) < 15:
            return 0.0
        highs = df["high"].astype(float).iloc[-14:]
        lows = df["low"].astype(float).iloc[-14:]
        closes = df["close"].astype(float).iloc[-15:-1]
        if len(closes) < 14:
            return float((highs - lows).mean())
        tr = pd.concat([
            highs - lows,
            (highs - closes).abs(),
            (lows - closes).abs(),
        ], axis=1).max(axis=1)
        return float(tr.mean())

    def compute_stops(entry_px, direction):
        if atr_stop_mult > 0:
            atr = compute_atr()
            if atr > 0:
                sd = atr * atr_stop_mult
                td = atr * (atr_target_mult if atr_target_mult > 0 else atr_stop_mult * 2)
                if direction == "long":
                    return entry_px - sd, entry_px + td
                return entry_px + sd, entry_px - td
        if is_fx:
            sd = stop_pips * pip_size
            td = target_pips * pip_size
        else:
            sd = entry_px * (stop_bps / 10000)
            td = entry_px * (target_bps / 10000)
        if direction == "long":
            return entry_px - sd, entry_px + td
        return entry_px + sd, entry_px - td

    # Pending signal: evaluated on bar i, executed at bar i+1 OPEN
    pending_direction: str | None = None
    pending_regime_info: dict = {}

    for i in range(total_bars):
        row = bars.iloc[i]
        try:
            bar = {
                "ts": str(row["ts"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            }
        except (ValueError, TypeError):
            skipped_bars += 1
            continue
        # Price validation
        if bar["high"] < bar["low"] or bar["close"] <= 0 or bar["open"] <= 0:
            skipped_bars += 1
            continue
        buf.add(bar)

        try:
            bar_time = pd.Timestamp(row["ts"])
            if bar_time.tzinfo is None:
                bar_time = bar_time.tz_localize("UTC")
            now = bar_time.to_pydatetime()
        except Exception:
            continue

        # ── Execute pending entry at this bar's OPEN ──
        # Signal was from PRIOR bar. Entry at THIS bar's open = no look-ahead.
        if pending_direction is not None and state.position == "FLAT":
            entry_px = _apply_slippage(bar["open"], pending_direction, "entry")
            direction = pending_direction
            stop_px, target_px = compute_stops(entry_px, direction)

            state.position = direction.upper()
            state.entry_price = entry_px
            state.stop_price = stop_px
            state.target_price = target_px
            state.direction = direction
            state.entry_time = now
            state.timeout_time = now + timedelta(minutes=timeout_min)
            state.last_signal_time = now
            state.trade_count += 1
            state.initial_risk = abs(entry_px - stop_px)
            state.entry_regime = pending_regime_info.get("regime", "")
            state.entry_efficiency = pending_regime_info.get("efficiency_ratio", 0)
            state.entry_trend = pending_regime_info.get("trend_strength", 0)

            signals_log.append({
                "bar": i, "action": "ENTRY", "direction": direction,
                "regime": state.entry_regime, "price": entry_px,
                "eff": state.entry_efficiency,
            })
            pending_direction = None
            pending_regime_info = {}

        # ── Position management (pessimistic stop ordering) ──
        if state.position != "FLAT":
            exit_reason = None
            exit_px = bar["close"]

            # Pessimistic: check stop FIRST (worst case for position)
            if state.position == "LONG":
                if bar["low"] <= state.stop_price:
                    exit_reason = "stop"
                    exit_px = state.stop_price
                elif bar["high"] >= state.target_price:
                    exit_reason = "target"
                    exit_px = state.target_price
            elif state.position == "SHORT":
                if bar["high"] >= state.stop_price:
                    exit_reason = "stop"
                    exit_px = state.stop_price
                elif bar["low"] <= state.target_price:
                    exit_reason = "target"
                    exit_px = state.target_price

            if state.timeout_time and now >= state.timeout_time:
                exit_reason = "timeout"
                exit_px = bar["close"]

            # FX Friday close: force-exit before weekend (matches live runner)
            if not exit_reason and _friday_close and is_fx:
                if now.weekday() == 4:  # Friday
                    utc_min = now.hour * 60 + now.minute
                    if utc_min >= _FRIDAY_CLOSE_MINUTE:
                        exit_reason = "friday_close"
                        exit_px = bar["close"]

            # Trailing stop (uses close, matching live mid-based logic)
            if not exit_reason and state.initial_risk > 0:
                risk_dist = state.initial_risk
                if state.position == "LONG":
                    fav = bar["close"] - state.entry_price
                    if fav >= risk_dist * 1.5:
                        new_stop = state.entry_price + risk_dist * 0.5
                        state.stop_price = max(state.stop_price, new_stop)
                    elif fav >= risk_dist:
                        state.stop_price = max(state.stop_price, state.entry_price)
                elif state.position == "SHORT":
                    fav = state.entry_price - bar["close"]
                    if fav >= risk_dist * 1.5:
                        new_stop = state.entry_price - risk_dist * 0.5
                        state.stop_price = min(state.stop_price, new_stop)
                    elif fav >= risk_dist:
                        state.stop_price = min(state.stop_price, state.entry_price)

            if exit_reason:
                # Apply slippage to exit (stop/target fill at stated price + slip)
                if exit_reason not in ("stop", "target"):
                    exit_px = _apply_slippage(exit_px, state.direction, "exit")
                if is_fx:
                    pnl = (exit_px - state.entry_price) / pip_size if state.position == "LONG" else (state.entry_price - exit_px) / pip_size
                    # Deduct commission (convert USD commission to pips for a standard 100K lot)
                    # $2 commission on EURUSD 100K lot: 1 pip = $10, so $2 = 0.2 pips
                    if _commission_usd > 0:
                        pnl -= _commission_usd / (pip_size * 100000)
                else:
                    pnl = exit_px - state.entry_price if state.position == "LONG" else state.entry_price - exit_px

                dur = (now - state.entry_time).total_seconds() / 60 if state.entry_time else 0
                trades.append(BacktestTrade(
                    entry_time=str(state.entry_time),
                    direction=state.direction,
                    entry_price=state.entry_price,
                    exit_price=exit_px,
                    exit_time=str(now),
                    exit_reason=exit_reason,
                    pnl=round(pnl, 4),
                    regime=state.entry_regime,
                    efficiency_ratio=state.entry_efficiency,
                    trend_strength=state.entry_trend,
                    duration_min=round(dur, 1),
                ))
                state.position = "FLAT"
                state.entry_time = None
                state.timeout_time = None
                state.initial_risk = 0
            continue

        # ── Signal evaluation (FLAT, bar closed) ──
        if len(buf) < 60:
            continue

        features = compute_features()
        if features is None:
            continue

        direction = check_trigger_fn(features)

        # Regime gate
        regime = features.get("regime", "UNKNOWN")
        if direction and regime_gate == "GATE":
            if not regime_compatible(regime, strategy):
                signals_log.append({"bar": i, "action": "REGIME_BLOCKED", "regime": regime, "direction": direction})
                direction = None

        # Min gap
        if direction and state.last_signal_time:
            gap = (now - state.last_signal_time).total_seconds() / 60
            if gap < min_gap:
                direction = None

        # Queue for next-bar execution (no same-bar entry)
        if direction:
            pending_direction = direction
            pending_regime_info = {
                "regime": regime,
                "efficiency_ratio": features.get("efficiency_ratio", 0),
                "trend_strength": features.get("trend_strength", 0),
            }

    # ── Compute metrics ──
    if not trades:
        return {
            "symbol": symbol, "strategy": strategy, "trades": [],
            "metrics": {"count": 0}, "bars_processed": total_bars,
            "override_params": override_params or {},
        }

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net = sum(pnls)
    wr = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    # PF: wins/losses ratio. No losses = infinity (strong), not 0
    if losses and sum(losses) != 0:
        pf = sum(wins) / abs(sum(losses))
    elif wins:
        pf = float('inf')
    else:
        pf = 0.0
    expectancy = net / len(pnls)

    exit_counts = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    # Sharpe and Sortino ratios (annualized, using trade returns)
    sharpe = 0.0
    sortino = 0.0
    if len(pnls) >= 2:
        pnl_arr = np.array(pnls, dtype=float)
        mean_ret = float(np.mean(pnl_arr))
        std_ret = float(np.std(pnl_arr, ddof=1))
        if std_ret > 0:
            sharpe = (mean_ret / std_ret) * math.sqrt(252)
        downside = pnl_arr[pnl_arr < 0]
        if len(downside) > 0:
            downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else abs(float(downside[0]))
            if downside_std > 0:
                sortino = (mean_ret / downside_std) * math.sqrt(252)

    # Equity-based max drawdown percentage
    max_dd_pct = (max_dd / _initial_equity * 100) if _initial_equity > 0 else 0.0

    metrics = {
        "count": len(trades),
        "net_pnl": round(net, 2),
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 4) if pf != float('inf') else 999.0,
        "expectancy": round(expectancy, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "exits": exit_counts,
        "avg_duration_min": round(sum(t.duration_min for t in trades) / len(trades), 1),
        "slippage_pips": _slippage_pips,
        "commission_per_lot_usd": _commission_usd,
        "initial_equity": _initial_equity,
        "gap_count": gap_count,
        "skipped_bars": skipped_bars,
    }

    return {
        "symbol": symbol,
        "strategy": strategy,
        "config": cfg,
        "trades": [t.__dict__ for t in trades],
        "metrics": metrics,
        "bars_processed": total_bars,
        "override_params": override_params or {},
        "signals_log": signals_log,
    }


# ── Strategy-specific backtest triggers ──

def _bt_check_fvg(buf: BarBuffer, features: dict, trigger: dict) -> Optional[str]:
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
        return None
    df = buf.to_df()
    if len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    fvgs = detect_fvgs(df, min_displacement_mult=trigger.get("min_displacement_mult", 1.5))
    entries = find_fvg_fill_entries(df, fvgs, max_wait_bars=trigger.get("max_wait_bars", 60),
                                    session_start=trigger.get("session_start_utc", 8),
                                    session_end=trigger.get("session_end_utc", 20))
    if entries:
        return entries[-1]["direction"]
    return None


def _bt_check_sweep(buf: BarBuffer, features: dict, trigger: dict) -> Optional[str]:
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
        return None
    df = buf.to_df()
    if len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    sh, sl = find_swing_points(df, lookback=trigger.get("swing_lookback", 20))
    sweeps = detect_sweeps(df, sh, sl, wick_ratio_min=trigger.get("wick_ratio_min", 0.3),
                            session_start=trigger.get("session_start_utc", 8),
                            session_end=trigger.get("session_end_utc", 20))
    if sweeps:
        return sweeps[-1]["direction"]
    return None


def _bt_check_vp(buf: BarBuffer, features: dict, trigger: dict) -> Optional[str]:
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
        return None
    df = buf.to_df()
    lookback = trigger.get("vp_lookback", 240)
    if len(df) < lookback:
        return None
    df = df.reset_index(drop=True)
    profile = calculate_volume_profile(df, len(df) - lookback, len(df))
    if not profile:
        return None
    price = features["price"]
    buffer = trigger.get("entry_buffer_pct", 0.0002)
    if price >= profile["vah"] * (1 - buffer):
        return "short"
    elif price <= profile["val"] * (1 + buffer):
        return "long"
    return None


# ── Parameter Sweep ──

def run_sweep(config: dict, bars: pd.DataFrame, param_grid: dict) -> list[dict]:
    """Run a grid sweep over parameter combinations.

    param_grid format: {"risk.stop_pips": [15, 20, 25], "risk.target_pips": [30, 40, 50]}
    Returns list of results sorted by profit_factor descending.
    """
    import itertools

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))

    results = []
    total = len(combos)
    print(f"Running {total} parameter combinations...")

    for idx, combo in enumerate(combos):
        override = dict(zip(keys, combo))
        result = run_backtest(config, bars, override_params=override)
        m = result["metrics"]
        results.append({
            "params": override,
            "count": m["count"],
            "net_pnl": m.get("net_pnl", 0),
            "win_rate": m.get("win_rate", 0),
            "profit_factor": m.get("profit_factor", 0),
            "expectancy": m.get("expectancy", 0),
            "max_drawdown": m.get("max_drawdown", 0),
            "exits": m.get("exits", {}),
        })
        if (idx + 1) % 10 == 0 or idx + 1 == total:
            print(f"  {idx+1}/{total} complete")

    results.sort(key=lambda r: r["profit_factor"], reverse=True)
    return results


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="Argus Flow Backtest Engine")
    parser.add_argument("--config", required=True, help="Config JSON path")
    parser.add_argument("--data", required=True, help="Historical bars CSV path")
    parser.add_argument("--output", default=None, help="Output directory (default: print to console)")
    parser.add_argument("--regime-gate", choices=["LOG_ONLY", "GATE"], default=None,
                        help="Override regime gate mode")
    parser.add_argument("--sweep", default=None, help="JSON file with param_grid for sweep")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = REPO / config_path
    config = json.loads(config_path.read_text())

    if args.regime_gate:
        config["regime_gate"] = args.regime_gate

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = REPO / data_path
    bars = pd.read_csv(data_path)
    print(f"Loaded {len(bars)} bars from {data_path.name}")

    if args.sweep:
        sweep_path = Path(args.sweep)
        if not sweep_path.is_absolute():
            sweep_path = REPO / sweep_path
        param_grid = json.loads(sweep_path.read_text())
        results = run_sweep(config, bars, param_grid)

        print(f"\n{'='*80}")
        print(f"SWEEP RESULTS — {config.get('symbol', '?')} / {config.get('strategy', '?')}")
        print(f"{'='*80}")
        print(f"{'PF':>8} {'WR':>6} {'Exp':>8} {'Trades':>6} {'DD':>8}  Params")
        print("-" * 80)
        for r in results[:20]:
            p = " ".join(f"{k}={v}" for k, v in r["params"].items())
            print(f"{r['profit_factor']:>8.3f} {r['win_rate']:>5.1%} {r['expectancy']:>+8.3f} "
                  f"{r['count']:>6} {r['max_drawdown']:>8.2f}  {p}")

        if args.output:
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"sweep_{config.get('symbol', 'unknown').lower()}.json"
            out_file.write_text(json.dumps(results, indent=2) + "\n")
            print(f"\nSaved to {out_file}")
    else:
        t0 = time.time()
        result = run_backtest(config, bars)
        elapsed = time.time() - t0

        m = result["metrics"]
        print(f"\n{'='*60}")
        print(f"BACKTEST: {result['symbol']} / {result['strategy']}")
        print(f"{'='*60}")
        print(f"Bars: {result['bars_processed']:,}  |  Time: {elapsed:.1f}s")
        print(f"Trades: {m['count']}")
        if m["count"] > 0:
            print(f"Net PnL: {m['net_pnl']:+.2f}")
            print(f"Win Rate: {m['win_rate']:.1%}")
            print(f"Profit Factor: {m['profit_factor']:.3f}")
            print(f"Expectancy: {m['expectancy']:+.3f}")
            print(f"Avg Win: {m['avg_win']:.2f}  |  Avg Loss: {m['avg_loss']:.2f}")
            print(f"Max Drawdown: {m['max_drawdown']:.2f}")
            print(f"Avg Duration: {m['avg_duration_min']:.0f} min")
            print(f"Exits: {m['exits']}")

        if args.output:
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"bt_{result['symbol'].lower()}_{result['strategy']}.json"
            out_file.write_text(json.dumps(result, indent=2, default=str) + "\n")
            trades_file = out_dir / f"bt_{result['symbol'].lower()}_{result['strategy']}_trades.csv"
            if result["trades"]:
                with open(trades_file, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=result["trades"][0].keys())
                    w.writeheader()
                    w.writerows(result["trades"])
            print(f"\nSaved to {out_dir}/")


if __name__ == "__main__":
    main()
