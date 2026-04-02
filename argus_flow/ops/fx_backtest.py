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
    def __init__(self, config: dict, enable_pyramid: bool = False):
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
                self.trades.append({
                    "ts": bar_time.isoformat(),
                    "direction": self.position.lower(),
                    "entry_px": self.entry_price,
                    "exit_px": mid,
                    "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "duration_min": round(dur, 1),
                    "pyramid_adds": self.pyramid_adds,
                })
                self.position = "FLAT"
                self.pyramid_adds = 0
                return

            self._check_pyramid(mid)
            return

        # Signal evaluation
        if len(self.buf) < 60:
            return

        features = self._compute_features()
        if features is None:
            return

        direction = self._check_trigger(features)

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
            self.pyramid_adds = 0

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
    parser.add_argument("--output", help="Output trades CSV path")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    bars = load_bars(Path(args.data))

    print(f"Config: {config['symbol']} / {config['strategy']}")
    print(f"Data: {len(bars)} bars from {args.data}")
    if args.pyramid:
        print(f"Pyramiding: ENABLED")

    bt = BacktestRunner(config, enable_pyramid=args.pyramid)

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
    print(json.dumps(s))


if __name__ == "__main__":
    main()
