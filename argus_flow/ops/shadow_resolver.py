"""Shadow Outcome Resolver — compute what blocked/filtered opportunities would have done.

Reads opportunity events logged by runner_unified.py and resolves counterfactual
outcomes using historical bar data.

Usage:
    python -m argus_flow.ops.shadow_resolver
    python -m argus_flow.ops.shadow_resolver --symbol EURUSD
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"


def resolve_opportunity(opp: dict, bars: pd.DataFrame, pip_size: float = 0.0001) -> dict:
    """Resolve one opportunity event into a counterfactual outcome."""
    result = {
        "opportunity_id": opp.get("opportunity_id", ""),
        "resolved": False,
        "shadow_valid": False,
    }

    ts_str = opp.get("ts", "")
    direction = opp.get("direction", "")
    price = float(opp.get("price", 0))
    stop_pips = float(opp.get("stop_pips", 30))
    target_pips = float(opp.get("target_pips", 30))
    timeout_min = int(opp.get("timeout_minutes", 75))

    if not ts_str or not direction or price <= 0:
        result["shadow_invalid_reason"] = "missing_fields"
        return result

    try:
        entry_time = pd.Timestamp(ts_str)
        if entry_time.tzinfo is None:
            entry_time = entry_time.tz_localize("UTC")
    except Exception:
        result["shadow_invalid_reason"] = "bad_timestamp"
        return result

    # Find entry bar index (next bar after signal = next-bar-open model)
    entry_mask = bars["ts"] > entry_time
    if entry_mask.sum() == 0:
        result["shadow_invalid_reason"] = "no_bars_after_signal"
        return result

    entry_idx = entry_mask.idxmax()
    entry_px = float(bars.loc[entry_idx, "open"])

    # Compute stops
    stop_dist = stop_pips * pip_size
    target_dist = target_pips * pip_size
    if direction == "long":
        stop_px = entry_px - stop_dist
        target_px = entry_px + target_dist
    else:
        stop_px = entry_px + stop_dist
        target_px = entry_px - target_dist

    timeout_time = entry_time + timedelta(minutes=timeout_min)

    # Walk forward through bars
    exit_reason = None
    exit_px = 0
    exit_idx = entry_idx

    horizons = [5, 10, 15, 30, 45, 60, 75, 90]
    path_mfe = {}
    path_mae = {}
    path_pnl = {}
    running_mfe = 0
    running_mae = 0

    for i in range(entry_idx, min(entry_idx + timeout_min + 5, len(bars))):
        bar = bars.iloc[i]
        bar_time = bar["ts"]

        if direction == "long":
            fav = (float(bar["high"]) - entry_px) / pip_size
            adv = (entry_px - float(bar["low"])) / pip_size
            bar_pnl = (float(bar["close"]) - entry_px) / pip_size
        else:
            fav = (entry_px - float(bar["low"])) / pip_size
            adv = (float(bar["high"]) - entry_px) / pip_size
            bar_pnl = (entry_px - float(bar["close"])) / pip_size

        running_mfe = max(running_mfe, fav)
        running_mae = max(running_mae, adv)

        elapsed = i - entry_idx
        if elapsed in horizons:
            path_mfe[elapsed] = round(running_mfe, 2)
            path_mae[elapsed] = round(running_mae, 2)
            path_pnl[elapsed] = round(bar_pnl, 2)

        # Check exit conditions (pessimistic: stop first)
        if direction == "long":
            if float(bar["low"]) <= stop_px:
                exit_reason = "stop"
                exit_px = stop_px
                break
            if float(bar["high"]) >= target_px:
                exit_reason = "target"
                exit_px = target_px
                break
        else:
            if float(bar["high"]) >= stop_px:
                exit_reason = "stop"
                exit_px = stop_px
                break
            if float(bar["low"]) <= target_px:
                exit_reason = "target"
                exit_px = target_px
                break

        if bar_time >= timeout_time:
            exit_reason = "timeout"
            exit_px = float(bar["close"])
            break

        exit_idx = i

    if not exit_reason:
        exit_reason = "timeout"
        exit_px = float(bars.iloc[min(exit_idx, len(bars) - 1)]["close"])

    # Compute PnL
    if direction == "long":
        shadow_pnl = (exit_px - entry_px) / pip_size
    else:
        shadow_pnl = (entry_px - exit_px) / pip_size

    reached_5 = running_mfe >= 5
    reached_10 = running_mfe >= 10

    result.update({
        "resolved": True,
        "shadow_valid": True,
        "shadow_entry_px": round(entry_px, 6),
        "shadow_exit_px": round(exit_px, 6),
        "shadow_exit_reason": exit_reason,
        "shadow_pnl": round(shadow_pnl, 2),
        "shadow_mfe": round(running_mfe, 2),
        "shadow_mae": round(running_mae, 2),
        "reached_5": reached_5,
        "reached_10": reached_10,
        "path_mfe": path_mfe,
        "path_mae": path_mae,
        "path_pnl": path_pnl,
        "giveback_after_10": reached_10 and shadow_pnl <= 0,
    })

    return result


def resolve_symbol(symbol: str) -> dict:
    """Resolve all opportunity events for one symbol."""
    log_dir = LOGS / symbol.lower()
    opp_file = log_dir / "opportunities.jsonl"

    if not opp_file.exists():
        return {"symbol": symbol, "opportunities": 0, "resolved": 0, "message": "no opportunity file"}

    # Load bars for this symbol
    data_candidates = [
        REPO / f"argus_flow/data/ibkr_{symbol.lower()}_1m.csv",
    ]
    bars = None
    for dp in data_candidates:
        if dp.exists():
            bars = pd.read_csv(dp)
            bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
            break

    if bars is None:
        return {"symbol": symbol, "opportunities": 0, "resolved": 0, "message": "no bar data"}

    # Load opportunities
    opps = []
    with open(opp_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    opps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Resolve each
    results = []
    for opp in opps:
        pip_size = 0.01 if "JPY" in symbol.upper() else 0.0001
        result = resolve_opportunity(opp, bars, pip_size)
        results.append(result)

    # Write resolved outcomes
    out_file = log_dir / "shadow_outcomes.jsonl"
    with open(out_file, "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")

    # Summary
    resolved = [r for r in results if r.get("resolved")]
    positive = [r for r in resolved if r.get("shadow_pnl", 0) > 0]

    return {
        "symbol": symbol,
        "opportunities": len(opps),
        "resolved": len(resolved),
        "positive": len(positive),
        "shadow_expectancy": round(sum(r.get("shadow_pnl", 0) for r in resolved) / len(resolved), 2) if resolved else 0,
        "shadow_net": round(sum(r.get("shadow_pnl", 0) for r in resolved), 1) if resolved else 0,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Shadow Outcome Resolver")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "EURJPY", "CADJPY", "AUDJPY", "AUDUSD"]

    print(f"\n{'='*60}")
    print(f"  SHADOW RESOLVER — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*60}\n")

    for sym in symbols:
        result = resolve_symbol(sym)
        print(f"  {sym}: {result.get('opportunities', 0)} opportunities, {result.get('resolved', 0)} resolved")
        if result.get("resolved", 0) > 0:
            print(f"    shadow_exp={result['shadow_expectancy']:+.2f} shadow_net={result['shadow_net']:+.1f} positive={result['positive']}")
        elif result.get("message"):
            print(f"    {result['message']}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
