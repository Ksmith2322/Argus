"""ATR-Scaled Stops — generate per-instrument stop/target configs based on ATR.

Reads recent price data from each runner's bar buffer or historical data,
computes ATR, and generates recommended stop/target distances scaled to
each instrument's volatility.

Principle: EURUSD (8-pip range) and GBPJPY (40-pip range) need different
stop distances. ATR-scaling normalizes risk across instruments.

Usage:
    python -m argus_flow.ops.atr_stops                     # analyze all configs
    python -m argus_flow.ops.atr_stops --symbol EURUSD     # single symbol
    python -m argus_flow.ops.atr_stops --apply             # write recommended values to configs
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
LOGS_ROOT = REPO / "argus_flow" / "logs"

# ATR multipliers for stop/target
STOP_ATR_MULT = 2.0     # stop = 2.0x ATR (breathing room)
TARGET_ATR_MULT = 1.5    # target = 1.5x ATR (realistic capture)

# ATR period (bars)
ATR_PERIOD = 14


def _read_recent_bars(log_dir: Path, max_bars: int = 500) -> list[dict]:
    """Read recent bars from signals.csv high/low/close columns."""
    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return []
    rows = []
    try:
        with open(sig_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-max_bars:]
    except Exception:
        return []


def _compute_atr(bars: list[dict], period: int = ATR_PERIOD) -> float | None:
    """Compute Average True Range from bar data."""
    if len(bars) < period + 1:
        return None

    trs = []
    for i in range(1, len(bars)):
        try:
            high = float(bars[i].get("high", bars[i].get("bar_high", 0)))
            low = float(bars[i].get("low", bars[i].get("bar_low", 0)))
            prev_close = float(bars[i - 1].get("close", bars[i - 1].get("bar_close", 0)))
        except (ValueError, TypeError):
            continue

        if high == 0 or low == 0 or prev_close == 0:
            continue

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return None

    # Simple moving average of TR (last `period` values)
    return sum(trs[-period:]) / period


def _price_to_pips(price_delta: float, symbol: str) -> float:
    """Convert a price delta to pips for FX."""
    symbol = symbol.upper()
    if "JPY" in symbol:
        return price_delta / 0.01
    return price_delta / 0.0001


def analyze_instrument(config_path: Path) -> dict | None:
    """Analyze one instrument and recommend ATR-scaled stops."""
    cfg = json.loads(config_path.read_text())
    symbol = cfg.get("symbol", "").upper()
    instrument_type = cfg.get("instrument_type", "forex")

    if instrument_type != "forex":
        return None  # futures handled differently

    risk = cfg.get("risk", {})
    current_stop = risk.get("stop_pips", 0)
    current_target = risk.get("target_pips", 0)

    # Find log dir for this symbol
    log_dir = LOGS_ROOT / symbol.lower()
    if not log_dir.exists():
        # Try alternative naming
        log_dir = LOGS_ROOT / f"live_{symbol.lower()}"

    bars = _read_recent_bars(log_dir)
    atr = _compute_atr(bars)

    if atr is None:
        return {
            "symbol": symbol,
            "config": config_path.name,
            "status": "NO_DATA",
            "current_stop_pips": current_stop,
            "current_target_pips": current_target,
        }

    atr_pips = _price_to_pips(atr, symbol)
    recommended_stop = round(atr_pips * STOP_ATR_MULT, 1)
    recommended_target = round(atr_pips * TARGET_ATR_MULT, 1)

    return {
        "symbol": symbol,
        "config": config_path.name,
        "status": "OK",
        "bars_used": len(bars),
        "atr_price": round(atr, 6),
        "atr_pips": round(atr_pips, 1),
        "current_stop_pips": current_stop,
        "current_target_pips": current_target,
        "recommended_stop_pips": recommended_stop,
        "recommended_target_pips": recommended_target,
        "stop_delta": round(recommended_stop - current_stop, 1),
        "target_delta": round(recommended_target - current_target, 1),
        "stop_atr_mult": STOP_ATR_MULT,
        "target_atr_mult": TARGET_ATR_MULT,
    }


def main():
    parser = argparse.ArgumentParser(description="ATR-scaled stop/target analysis")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Write recommended values to config files")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  ATR-Scaled Stops — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Multipliers: stop={STOP_ATR_MULT}x ATR, target={TARGET_ATR_MULT}x ATR")
    print("=" * 60)

    configs = sorted(CONFIGS_DIR.glob("*_paper_v1.json"))
    if args.symbol:
        target = args.symbol.lower()
        configs = [c for c in configs if target in c.stem.lower()]

    results = []
    for config_path in configs:
        result = analyze_instrument(config_path)
        if result:
            results.append(result)

    if not results:
        print("\n  No instruments analyzed.")
        return

    # Display results
    print(f"\n  {'Symbol':>8s} | {'ATR':>6s} | {'Current':>12s} | {'Recommended':>14s} | {'Delta':>10s}")
    print(f"  {'-' * 8} | {'-' * 6} | {'-' * 12} | {'-' * 14} | {'-' * 10}")

    for r in results:
        if r["status"] == "NO_DATA":
            print(f"  {r['symbol']:>8s} | {'N/A':>6s} | {r['current_stop_pips']:>5.0f}/{r['current_target_pips']:<5.0f} | {'no data':>14s} |")
            continue
        print(
            f"  {r['symbol']:>8s} | {r['atr_pips']:>5.1f}p | "
            f"{r['current_stop_pips']:>5.0f}/{r['current_target_pips']:<5.0f} | "
            f"{r['recommended_stop_pips']:>6.1f}/{r['recommended_target_pips']:<6.1f} | "
            f"{r['stop_delta']:>+5.1f}/{r['target_delta']:<+5.1f}"
        )

    if args.apply:
        print("\n  Applying recommended values...")
        for r in results:
            if r["status"] != "OK":
                continue
            cfg_path = CONFIGS_DIR / r["config"]
            cfg = json.loads(cfg_path.read_text())
            cfg["risk"]["stop_pips"] = r["recommended_stop_pips"]
            cfg["risk"]["target_pips"] = r["recommended_target_pips"]
            cfg["risk"]["_atr_scaled"] = {
                "atr_pips": r["atr_pips"],
                "stop_mult": STOP_ATR_MULT,
                "target_mult": TARGET_ATR_MULT,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }
            cfg_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            print(f"    {r['symbol']}: stop={r['recommended_stop_pips']}, target={r['recommended_target_pips']}")
    else:
        print("\n  Run with --apply to write recommended values to configs.")

    # Save report
    out_path = LOGS_ROOT / "atr_stops_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "multipliers": {"stop": STOP_ATR_MULT, "target": TARGET_ATR_MULT},
        "instruments": results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\n  Report: {out_path}")


if __name__ == "__main__":
    main()
