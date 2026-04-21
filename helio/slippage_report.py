"""Per-pair + fleet-level slippage analysis from trades.csv.

Slippage in pips = |entry_price - signal_mid| on the fill side the trade went
against us. Runner writes signal_mid and slippage columns on every trade
(argus_flow/runner_unified.py:1794-1822).

Reality check for paper mode: when execution_mode=="paper", entry_price is
set directly to signal_mid at entry (no broker fill simulation), so slippage
columns read 0. This module surfaces that fact so the reader doesn't mistake
"zero slippage in backtest" for "zero slippage in production."

Real slippage measurement requires one of:
  1. stage=paper + execution_mode=real (IBKR paper sim with spread), OR
  2. Synthetic slippage model applied at paper-fill time (future work —
     see docs/PAPER_SLIPPAGE_MODEL.md proposal).

Usage:
    python -m helio.slippage_report
    python -m helio.slippage_report --pair GBPUSD
    python -m helio.slippage_report --json
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

LOGS = _REPO / "argus_flow" / "logs"
OUT_PATH = LOGS / "slippage_report.json"


def _load_trades(pair: str) -> list[dict]:
    path = LOGS / pair.lower() / "trades.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _float(v) -> float | None:
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def _pair_stats(pair: str, rows: list[dict]) -> dict:
    if not rows:
        return {"pair": pair, "n": 0, "note": "no trades"}

    slips = []
    latencies = []
    zero_slip_count = 0
    for r in rows:
        # Historical schema had "slippage"; current schema is "slippage_pips"
        raw = r.get("slippage_pips") if r.get("slippage_pips") not in ("", None) else r.get("slippage")
        s = _float(raw)
        if s is None:
            continue
        slips.append(s)
        if s == 0:
            zero_slip_count += 1
        lat = _float(r.get("fill_latency_ms"))
        if lat is not None:
            latencies.append(lat)

    if not slips:
        return {"pair": pair, "n": len(rows), "note": "no slippage column populated"}

    slip_mean = statistics.mean(slips)
    slip_med = statistics.median(slips)
    slip_max = max(slips)
    zero_frac = zero_slip_count / len(slips)

    note = None
    if zero_frac > 0.95:
        note = ("PAPER_MODE_ARTIFACT: >95% of trades show 0 pip slippage — "
                "consistent with execution_mode=paper (entry_price == signal_mid). "
                "Real-world slippage is NOT being measured here. See "
                "helio/slippage_report.py docstring for the fix path.")

    return {
        "pair": pair,
        "n": len(slips),
        "slippage_pips": {
            "mean": round(slip_mean, 3),
            "median": round(slip_med, 3),
            "max": round(slip_max, 3),
            "zero_fraction": round(zero_frac, 3),
        },
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "note": note,
    }


def build_report(pairs: list[str] | None = None) -> dict:
    from datetime import datetime, timezone

    if pairs is None:
        # Auto-discover all log dirs under argus_flow/logs/<symbol>/
        pairs = []
        for p in LOGS.iterdir():
            if p.is_dir() and (p / "trades.csv").exists() and not p.name.startswith("_"):
                pairs.append(p.name.upper())

    per_pair = [_pair_stats(pair, _load_trades(pair)) for pair in sorted(pairs)]

    # Fleet aggregate (only over pairs with populated slippage)
    slips_all = []
    for p in per_pair:
        sp = p.get("slippage_pips")
        if sp and p["n"] > 0:
            # Approximate by mean weighted by n (we don't retain raw list)
            slips_all.append((sp["mean"], p["n"]))

    fleet = None
    if slips_all:
        total_n = sum(n for _, n in slips_all)
        weighted_mean = sum(m * n for m, n in slips_all) / total_n if total_n else None
        fleet = {
            "total_trades_with_slippage_data": total_n,
            "weighted_mean_slippage_pips": round(weighted_mean, 3) if weighted_mean is not None else None,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fleet": fleet,
        "per_pair": per_pair,
        "warning_paper_mode": (
            "execution_mode=paper means entry fills are set to signal_mid; "
            "slippage will read ~0 until either (a) execution_mode flipped "
            "to real, or (b) a synthetic slippage model is wired at fill "
            "time. This report is honest about that — see notes on each pair."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="Single pair (e.g., GBPUSD)")
    ap.add_argument("--json", action="store_true", help="Print JSON, don't save")
    args = ap.parse_args()

    report = build_report([args.pair.upper()] if args.pair else None)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print()
    for p in report["per_pair"]:
        if p["n"] == 0:
            print(f"  {p['pair']:10s} no trades")
            continue
        sp = p.get("slippage_pips")
        lat = p.get("latency_ms") or {}
        if not sp or sp.get("mean") is None:
            print(f"  {p['pair']:10s} n={p['n']:3d}  no slippage data  ({p.get('note') or 'slippage column empty'})")
            continue
        print(f"  {p['pair']:10s} n={p['n']:3d}  "
              f"slip mean={sp['mean']:>5.2f} med={sp['median']:>5.2f} max={sp['max']:>5.2f}  "
              f"zero_frac={sp['zero_fraction']:>4.2f}  "
              f"lat_ms={lat.get('mean')}")
        if p.get("note"):
            print(f"    NOTE: {p['note']}")
    fleet = report.get("fleet")
    if fleet:
        print()
        print(f"  FLEET: {fleet['total_trades_with_slippage_data']} trades, "
              f"weighted-mean slippage = {fleet['weighted_mean_slippage_pips']} pips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
