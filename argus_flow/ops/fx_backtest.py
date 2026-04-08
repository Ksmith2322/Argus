"""FX Backtest Harness — replay historical data through runner_unified strategy logic.

Tests configs offline without IBKR connection. Uses exact same feature computation
and trigger logic as the live runner.

Usage:
    python -m argus_flow.ops.fx_backtest --config CONFIG --data DATA_CSV
    python -m argus_flow.ops.fx_backtest --config CONFIG --data DATA_CSV --pyramid
    python -m argus_flow.ops.fx_backtest --config CONFIG --data DATA_CSV --output results.csv
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from argus_flow.runner_unified import BarBuffer, compute_features_fx, compute_features_futures, check_trigger_fx, check_trigger_futures

# Optional MTF imports (only needed for --mtf mode)
try:
    from argus_flow.strategies.mtf_engine import MTFStrategyEngine
    from argus_flow.strategies.ai_overlay import AdaptiveOverlay, MarketState
    _MTF_AVAILABLE = True
except ImportError:
    _MTF_AVAILABLE = False


def load_bars(csv_path: Path) -> list[dict]:
    """Load historical bars from CSV."""
    bars = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            bars.append({
                "ts": row["ts"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })
    return bars


class BacktestRunner:
    def __init__(self, config: dict, enable_pyramid: bool = False, enable_mtf: bool = False):
        self.cfg = config
        self.buf = BarBuffer(300)
        self.symbol = config["symbol"]
        self.instrument_type = config.get("instrument_type", "forex")

        risk = config.get("risk", {})
        self.stop_pips = risk.get("stop_pips", 0)
        self.target_pips = risk.get("target_pips", 0)
        self.stop_bps = risk.get("stop_bps", 0)
        self.target_bps = risk.get("target_bps", 0)
        self.timeout_min = risk.get("timeout_minutes", 60)
        self.min_gap = risk.get("min_signal_gap_minutes", 15)

        self.pip_size = 0.01 if "JPY" in self.symbol.upper() else 0.0001
        self.uses_pips = self.instrument_type == "forex" and self.stop_pips > 0

        # Pyramiding
        pyramid = config.get("pyramid", {})
        self.pyramid_enabled = enable_pyramid and pyramid.get("enabled", False)
        self.pyramid_trigger_pips = pyramid.get("trigger_pips", 0)
        self.pyramid_max_adds = pyramid.get("max_adds", 1)
        self.pyramid_move_stop_be = pyramid.get("move_stop_breakeven", True)

        # MTF strategy + AI overlay
        self.mtf_enabled = enable_mtf and _MTF_AVAILABLE and config.get("strategy") == "mtf_trend"
        self._mtf_engine = None
        self._ai_overlay = None
        self._mtf_min_confidence = 0.5
        self._bars_since_last_trade = 999
        self._consecutive_losses = 0
        if self.mtf_enabled:
            mtf_cfg = config.get("mtf", {})
            self._mtf_engine = MTFStrategyEngine(
                symbol=self.symbol,
                pip_size=self.pip_size,
                trend_ema_fast=mtf_cfg.get("trend_ema_fast", 8),
                trend_ema_slow=mtf_cfg.get("trend_ema_slow", 21),
                rsi_period=mtf_cfg.get("rsi_period", 14),
                min_trend_strength=mtf_cfg.get("min_trend_strength", 0.5),
            )
            self._mtf_min_confidence = mtf_cfg.get("min_confidence", 0.5)
            self._ai_overlay = AdaptiveOverlay(
                symbol=self.symbol,
                pip_size=self.pip_size,
                state_dir=Path("argus_flow/data/backtest_results"),
            )

        # Hour filter
        hf = config.get("hour_filter", {})
        self.hour_filter_enabled = hf.get("enabled", False)
        self.profitable_hours = set(hf.get("hours", []))

        # State
        self.position = "FLAT"
        self.entry_price = 0
        self.stop_price = 0
        self.target_price = 0
        self.entry_time = None
        self.timeout_time = None
        self.last_signal_time = None
        self.pyramid_adds = 0
        self.avg_entry = 0
        self._entry_direction = None
        self._last_entry_features = {}

        # Results
        self.trades = []
        self.signals = []

    def _compute_stops(self, entry_px, direction):
        if self.uses_pips:
            stop_dist = self.stop_pips * self.pip_size
            target_dist = self.target_pips * self.pip_size
        else:
            stop_dist = entry_px * (self.stop_bps / 10000)
            target_dist = entry_px * (self.target_bps / 10000)
        if direction == "long":
            return entry_px - stop_dist, entry_px + target_dist
        else:
            return entry_px + stop_dist, entry_px - target_dist

    def _compute_features(self):
        if self.instrument_type in ("future", "crypto"):
            return compute_features_futures(self.buf)
        return compute_features_fx(self.buf)

    def _check_trigger(self, features):
        if self.instrument_type in ("future", "crypto"):
            return check_trigger_futures(features, self.cfg)
        return check_trigger_fx(features, self.cfg)

    def _check_pyramid(self, mid):
        if not self.pyramid_enabled or self.pyramid_adds >= self.pyramid_max_adds:
            return
        trigger_dist = self.pyramid_trigger_pips * self.pip_size if self.uses_pips else self.entry_price * (self.pyramid_trigger_pips / 10000)
        if trigger_dist <= 0:
            return
        if self.position == "LONG":
            fav = mid - self.entry_price
        else:
            fav = self.entry_price - mid
        if fav < trigger_dist:
            return
        n = self.pyramid_adds + 1
        self.avg_entry = (self.entry_price * n + mid) / (n + 1)
        self.pyramid_adds += 1
        if self.pyramid_move_stop_be and self.avg_entry > 0:
            if self.position == "LONG":
                self.stop_price = max(self.stop_price, self.avg_entry - self.pip_size)
            else:
                self.stop_price = min(self.stop_price, self.avg_entry + self.pip_size)

    def process_bar(self, bar: dict):
        self.buf.add(bar)
        mid = bar["close"]

        try:
            bar_time = datetime.fromisoformat(bar["ts"].replace("Z", "+00:00"))
        except Exception:
            bar_time = datetime.now(timezone.utc)

        # Feed MTF engine every 1m bar
        if self._mtf_engine is not None:
            self._mtf_engine.update_bar(bar)

        # Exit check
        if self.position != "FLAT":
            exit_reason = None
            if self.position == "LONG":
                if mid <= self.stop_price: exit_reason = "stop"
                elif mid >= self.target_price: exit_reason = "target"
            else:
                if mid >= self.stop_price: exit_reason = "stop"
                elif mid <= self.target_price: exit_reason = "target"
            if self.timeout_time and bar_time >= self.timeout_time:
                exit_reason = "timeout"

            if exit_reason:
                if self.uses_pips:
                    pnl = ((mid - self.entry_price) / self.pip_size) if self.position == "LONG" else ((self.entry_price - mid) / self.pip_size)
                else:
                    pnl = (mid - self.entry_price) if self.position == "LONG" else (self.entry_price - mid)
                dur = (bar_time - self.entry_time).total_seconds() / 60 if self.entry_time else 0
                trade_record = {
                    "ts": bar_time.isoformat(),
                    "direction": self.position.lower(),
                    "entry_px": self.entry_price,
                    "exit_px": mid,
                    "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "duration_min": round(dur, 1),
                    "pyramid_adds": self.pyramid_adds,
                }
                # Capture MTF features at exit for governor training data
                if self._last_entry_features:
                    trade_record.update(self._last_entry_features)
                trade_record["win"] = 1 if pnl > 0 else 0
                self.trades.append(trade_record)

                # AI overlay learning
                if self._ai_overlay is not None:
                    self._ai_overlay.learn(pnl)
                    self._bars_since_last_trade = 0
                    self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0

                self.position = "FLAT"
                self.pyramid_adds = 0
                self._entry_direction = None
                return

            self._check_pyramid(mid)
            self._bars_since_last_trade += 1
            return

        self._bars_since_last_trade += 1

        # Signal evaluation
        if len(self.buf) < 60:
            return

        features = self._compute_features()
        if features is None:
            return

        direction = None
        mtf_features = {}

        # MTF strategy path
        if self.mtf_enabled and self._mtf_engine is not None:
            mtf_signal = self._mtf_engine.evaluate(mid)
            if mtf_signal is not None and mtf_signal.confidence >= self._mtf_min_confidence:
                direction = mtf_signal.direction
                mtf_features = {
                    "mtf_trend_4h": mtf_signal.trend_4h,
                    "mtf_setup_1h": mtf_signal.setup_1h,
                    "mtf_trigger_5m": mtf_signal.trigger_5m,
                    "mtf_confidence": round(mtf_signal.confidence, 3),
                    "mtf_support": round(mtf_signal.support, 5),
                    "mtf_resistance": round(mtf_signal.resistance, 5),
                    "mtf_rsi_1h": round(mtf_signal.rsi_1h, 1),
                }

                # AI overlay gate
                if self._ai_overlay is not None and direction:
                    recent_trades = self.trades[-10:]
                    recent_wr = sum(1 for t in recent_trades if t["pnl"] > 0) / max(len(recent_trades), 1)
                    ai_state = MarketState(
                        price=mid,
                        atr_14=features.get("atr_14", 0.0),
                        rsi_14=features.get("rsi_14", 50.0),
                        spread_pips=0.5,  # simulated tight spread for backtest
                        volume_ratio=features.get("vol_z", 0.0) + 1.0,
                        dist_from_high_20=1.0 - features.get("dist_from_low", 0.5),
                        dist_from_low_20=features.get("dist_from_low", 0.5),
                        ema_8_slope=features.get("trend_strength", 0.0),
                        ema_21_slope=features.get("efficiency_ratio", 0.0),
                        hour=bar_time.hour,
                        day_of_week=bar_time.weekday(),
                        bars_since_last_trade=self._bars_since_last_trade,
                        recent_win_rate=recent_wr,
                        recent_pnl=sum(t["pnl"] for t in recent_trades),
                        consecutive_losses=self._consecutive_losses,
                        trend_strength_4h=abs(mtf_signal.ema_8_4h - mtf_signal.ema_21_4h) / self.pip_size if self.pip_size > 0 else 0.0,
                        rsi_1h=mtf_signal.rsi_1h,
                        confidence_mtf=mtf_signal.confidence,
                    )
                    overlay_decision = self._ai_overlay.evaluate(direction, ai_state)
                    mtf_features["ai_action"] = overlay_decision.action
                    mtf_features["ai_consensus"] = round(overlay_decision.consensus_score, 3)
                    mtf_features["ai_confidence"] = round(overlay_decision.confidence, 3)
                    if overlay_decision.action == "SKIP":
                        direction = None
        else:
            # Fallback: original range_accel trigger
            direction = self._check_trigger(features)

        # Hour filter
        if direction and self.hour_filter_enabled and self.profitable_hours:
            if bar_time.hour not in self.profitable_hours:
                direction = None

        if direction and self.last_signal_time:
            gap = (bar_time - self.last_signal_time).total_seconds() / 60
            if gap < self.min_gap:
                direction = None

        self.signals.append({"ts": bar_time.isoformat(), "action": "ENTRY" if direction else "NO_TRIGGER", "direction": direction or ""})

        if direction:
            self.entry_price = mid
            self.avg_entry = mid
            self.stop_price, self.target_price = self._compute_stops(mid, direction)
            self.entry_time = bar_time
            self.timeout_time = bar_time + timedelta(minutes=self.timeout_min)
            self.last_signal_time = bar_time
            self.position = direction.upper()
            self._entry_direction = direction
            self.pyramid_adds = 0
            # Capture MTF features at entry for governor training data
            self._last_entry_features = {
                **mtf_features,
                "range_pct": features.get("range_pct", 0),
                "vol_z": features.get("vol_z", 0),
                "range_accel": features.get("range_accel", 0),
                "dist_from_low": features.get("dist_from_low", 0),
                "hour": bar_time.hour,
                "regime": features.get("regime", ""),
                "trend_strength": features.get("trend_strength", 0),
                "efficiency_ratio": features.get("efficiency_ratio", 0),
            }

    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0}
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        exits = [str(t.get("exit_reason", "")).lower() for t in self.trades]

        peak = 0
        equity = 0
        max_dd = 0
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        unit = "pips" if self.uses_pips else "pts"
        entries = [s for s in self.signals if s["action"] == "ENTRY"]

        return {
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 999,
            "expectancy": round(sum(pnls) / len(pnls), 3),
            "total_pnl": round(sum(pnls), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "max_drawdown": round(max_dd, 2),
            "unit": unit,
            "total_signals": len(self.signals),
            "total_entries": len(entries),
            "trigger_rate": round(len(entries) / len(self.signals) * 100, 2) if self.signals else 0,
            "stop_rate": round(exits.count("stop") / len(self.trades) * 100, 2),
            "target_rate": round(exits.count("target") / len(self.trades) * 100, 2),
            "timeout_rate": round(exits.count("timeout") / len(self.trades) * 100, 2),
        }


def main():
    parser = argparse.ArgumentParser(description="FX Backtest Harness")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--data", required=True, help="Path to bar data CSV")
    parser.add_argument("--pyramid", action="store_true", help="Enable pyramiding")
    parser.add_argument("--mtf", action="store_true", help="Enable MTF strategy + AI overlay (for mtf_trend configs)")
    parser.add_argument("--output", help="Output trades CSV path")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    bars = load_bars(Path(args.data))

    print(f"Config: {config['symbol']} / {config['strategy']}")
    print(f"Data: {len(bars)} bars from {args.data}")
    if args.pyramid:
        print(f"Pyramiding: ENABLED")
    if args.mtf:
        if not _MTF_AVAILABLE:
            print("ERROR: MTF modules not available")
            return
        print(f"MTF Strategy + AI Overlay: ENABLED")

    bt = BacktestRunner(config, enable_pyramid=args.pyramid, enable_mtf=args.mtf)

    for i, bar in enumerate(bars):
        bt.process_bar(bar)
        if (i + 1) % 10000 == 0:
            print(f"  Processed {i + 1}/{len(bars)} bars...")

    s = bt.summary()
    print(f"\n{'=' * 50}")
    print(f"  BACKTEST RESULTS — {config['symbol']}")
    print(f"{'=' * 50}")

    if s["trades"] == 0:
        print("  No trades generated.")
        print(json.dumps(s))
        return

    print(f"  Trades:      {s['trades']} ({s['wins']}W / {s['losses']}L)")
    print(f"  Win Rate:    {s['win_rate']}%")
    print(f"  PF:          {s['profit_factor']}")
    print(f"  Expectancy:  {s['expectancy']} {s['unit']}/trade")
    print(f"  Total PnL:   {s['total_pnl']} {s['unit']}")
    print(f"  Avg Win:     {s['avg_win']} {s['unit']}")
    print(f"  Avg Loss:    {s['avg_loss']} {s['unit']}")
    print(f"  Max DD:      {s['max_drawdown']} {s['unit']}")
    print(f"  Signals:     {s['total_signals']} ({s['total_entries']} entries, {s['trigger_rate']}% trigger rate)")

    # Save trades
    out_dir = REPO / "argus_flow" / "data" / "backtest_results"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_path = out_dir / f"{config['symbol'].lower()}_{ts}.csv"

    with open(out_path, "w", newline="") as f:
        if bt.trades:
            w = csv.DictWriter(f, fieldnames=bt.trades[0].keys())
            w.writeheader()
            w.writerows(bt.trades)
    print(f"\n  Trades saved: {out_path}")

    # Export training data with MTF features for governor retraining
    if args.mtf and bt.trades:
        train_path = out_dir / f"{config['symbol'].lower()}_train_{ts}.csv"
        # Filter to only trades with MTF features
        train_trades = [t for t in bt.trades if t.get("mtf_confidence")]
        if train_trades:
            with open(train_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=train_trades[0].keys())
                w.writeheader()
                w.writerows(train_trades)
            print(f"  Training data: {train_path} ({len(train_trades)} trades with MTF features)")

    print(json.dumps(s))


if __name__ == "__main__":
    main()
