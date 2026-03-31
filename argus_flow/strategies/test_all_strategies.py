"""Test all 3 new strategies (FVG, Liquidity Sweep, Volume Profile) on NQ and EUR/USD.

Runs the full payoff-first analysis on each combination.

Usage:
    python -m argus_flow.strategies.test_all_strategies
    python -m argus_flow.strategies.test_all_strategies --json-out argus_flow/strategy_compare.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.strategies.fvg_detector import detect_fvgs, find_fvg_fill_entries, simulate_fvg_trades
from argus_flow.strategies.liquidity_sweep import find_swing_points, detect_sweeps, simulate_sweep_trades
from argus_flow.strategies.volume_profile import find_vp_entries, simulate_vp_trades

DATA_DIR = Path("argus_flow/data")

INSTRUMENTS = [
    {
        "name": "MNQ",
        "file": "ibkr_MNQ_nasdaq_micro_1m.csv",
        "fee_rt": 0.00003,  # 3bps
        "session_start": 13,
        "session_end": 20,
        "has_volume": True,
    },
    {
        "name": "EUR/USD",
        "file": "ibkr_eurusd_1m_extended.csv",
        "fee_rt": 0.00002,  # 2bps
        "session_start": 8,
        "session_end": 19,
        "has_volume": False,
    },
]


def _load_benchmarks(path: str | None) -> list[dict]:
    """Load optional external benchmark rows for display-only comparison."""
    if not path:
        return []

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("benchmarks", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("benchmarks JSON must be a list or an object with a 'benchmarks' list")

    benchmarks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        benchmarks.append(
            {
                "strategy": str(row.get("strategy", "benchmark")),
                "instrument": str(row.get("instrument", "")),
                "exp_bps": row.get("exp_bps"),
                "note": str(row.get("note", "")),
            }
        )
    return benchmarks


def _result_sort_key(row: dict) -> tuple[int, float, int]:
    """Sort computed results with viable/high expectancy rows first."""
    if row.get("status"):
        return (0, float("-inf"), 0)
    return (
        2 if row.get("viable") else 1,
        float(row.get("exp_bps", float("-inf"))),
        int(row.get("signals", 0)),
    )


def _jsonable(value):
    """Convert pandas/numpy scalars and nested containers into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return _jsonable(value.item())
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _build_summary(all_results: list[dict], benchmarks: list[dict]) -> dict:
    computed = [r for r in all_results if not r.get("status")]
    viable = [r for r in computed if r.get("viable")]
    sorted_results = sorted(all_results, key=_result_sort_key, reverse=True)

    return _jsonable({
        "result_count": len(all_results),
        "computed_count": len(computed),
        "viable_count": len(viable),
        "viable_ratio": (len(viable) / len(computed)) if computed else 0.0,
        "best_result": sorted(computed, key=lambda r: r.get("exp_bps", float("-inf")), reverse=True)[0]
        if computed
        else None,
        "results": sorted_results,
        "benchmarks": benchmarks,
        "notes": [
            "All computed rows are generated from the current in-sample run.",
            "Benchmark rows are optional display-only context loaded from --benchmarks-json.",
        ],
    })


def test_fvg(df, name, fee_rt, session_start, session_end, **kwargs):
    """Test Fair Value Gap strategy."""
    fvgs = detect_fvgs(df)
    entries = find_fvg_fill_entries(df, fvgs, session_start=session_start, session_end=session_end)

    if not entries:
        return {"strategy": "FVG", "instrument": name, "signals": 0, "status": "NO_SIGNALS"}

    # Test multiple RR ratios
    best = None
    for rr in [1.5, 2.0, 3.0]:
        for timeout in [30, 60]:
            results = simulate_fvg_trades(df, entries, rr_mult=rr, timeout_bars=timeout, fee_rt=fee_rt)
            if results.empty:
                continue
            exp = results["net_pnl_pct"].mean() * 10000
            wr = (results["net_pnl_pct"] > 0).mean()
            tgt = (results["outcome"] == "target").mean()
            stp = (results["outcome"] == "stop").mean()
            tmo = (results["outcome"] == "timeout").mean()

            row = {"strategy": "FVG", "instrument": name, "rr": rr, "timeout": timeout,
                   "signals": len(results), "wr": wr, "tgt_rate": tgt, "stp_rate": stp,
                   "tmo_rate": tmo, "exp_bps": exp, "viable": exp > 0}

            if best is None or exp > best["exp_bps"]:
                best = row

    return best if best else {"strategy": "FVG", "instrument": name, "signals": 0, "status": "NO_VIABLE"}


def test_sweep(df, name, fee_rt, session_start, session_end, **kwargs):
    """Test Liquidity Sweep strategy."""
    highs, lows = find_swing_points(df, lookback=20)
    sweeps = detect_sweeps(df, highs, lows, session_start=session_start, session_end=session_end)

    if not sweeps:
        return {"strategy": "Liq Sweep", "instrument": name, "signals": 0, "status": "NO_SIGNALS"}

    best = None
    for rr in [1.5, 2.0, 3.0]:
        for timeout in [30, 60]:
            results = simulate_sweep_trades(df, sweeps, rr_mult=rr, timeout_bars=timeout, fee_rt=fee_rt)
            if results.empty:
                continue
            exp = results["net_pnl_pct"].mean() * 10000
            wr = (results["net_pnl_pct"] > 0).mean()
            tgt = (results["outcome"] == "target").mean()
            stp = (results["outcome"] == "stop").mean()
            tmo = (results["outcome"] == "timeout").mean()

            row = {"strategy": "Liq Sweep", "instrument": name, "rr": rr, "timeout": timeout,
                   "signals": len(results), "wr": wr, "tgt_rate": tgt, "stp_rate": stp,
                   "tmo_rate": tmo, "exp_bps": exp, "viable": exp > 0}

            if best is None or exp > best["exp_bps"]:
                best = row

    return best if best else {"strategy": "Liq Sweep", "instrument": name, "signals": 0, "status": "NO_VIABLE"}


def test_volume_profile(df, name, fee_rt, session_start, session_end, has_volume=True, **kwargs):
    """Test Volume Profile strategy."""
    if not has_volume:
        # Use bar range as volume proxy for FX
        df = df.copy()
        df["volume"] = df["high"] - df["low"]

    entries = find_vp_entries(df, session_start=session_start, session_end=session_end)

    if not entries:
        return {"strategy": "Vol Profile", "instrument": name, "signals": 0, "status": "NO_SIGNALS"}

    best = None
    for timeout in [30, 60, 90]:
        results = simulate_vp_trades(df, entries, timeout_bars=timeout, fee_rt=fee_rt)
        if results.empty:
            continue
        exp = results["net_pnl_pct"].mean() * 10000
        wr = (results["net_pnl_pct"] > 0).mean()
        tgt = (results["outcome"] == "target").mean()
        stp = (results["outcome"] == "stop").mean()
        tmo = (results["outcome"] == "timeout").mean()

        row = {"strategy": "Vol Profile", "instrument": name, "rr": "POC", "timeout": timeout,
               "signals": len(results), "wr": wr, "tgt_rate": tgt, "stp_rate": stp,
               "tmo_rate": tmo, "exp_bps": exp, "viable": exp > 0}

        if best is None or exp > best["exp_bps"]:
            best = row

    return best if best else {"strategy": "Vol Profile", "instrument": name, "signals": 0, "status": "NO_VIABLE"}


def main():
    parser = argparse.ArgumentParser(description="Compare argus_flow strategy variants on available instruments")
    parser.add_argument("--json-out", default=None, help="Optional path to write machine-readable summary JSON")
    parser.add_argument(
        "--benchmarks-json",
        default=None,
        help="Optional JSON file with external benchmark rows to display for reference",
    )
    args = parser.parse_args()
    benchmarks = _load_benchmarks(args.benchmarks_json)

    print("=" * 80)
    print("  STRATEGY COMPARISON: FVG vs Liquidity Sweep vs Volume Profile")
    print("  Testing on NQ (futures) and EUR/USD (forex)")
    print("  Computed rows are in-sample probes, not walk-forward validation.")
    print("=" * 80)

    all_results = []

    for inst in INSTRUMENTS:
        csv_path = DATA_DIR / inst["file"]
        if not csv_path.exists():
            print(f"\n  {inst['name']}: data not found ({csv_path}), skipping")
            continue

        df = pd.read_csv(csv_path)
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400 if "ts" in df.columns else len(df) / 1440

        print(f"\n{'#' * 70}")
        print(f"  {inst['name']} — {len(df):,} bars, {n_days:.1f} days")
        print(f"{'#' * 70}")

        for test_fn, label in [(test_fvg, "FVG"), (test_sweep, "Liq Sweep"), (test_volume_profile, "Vol Profile")]:
            print(f"\n  --- {label} ---")
            try:
                kw = {k: v for k, v in inst.items() if k not in ("name", "file")}
                result = test_fn(df, inst["name"], **kw)
                all_results.append(result)

                if result.get("status"):
                    print(f"    {result['status']}")
                else:
                    v = "VIABLE" if result.get("viable") else "dead"
                    print(f"    Best: RR={result.get('rr','?')} timeout={result.get('timeout','?')}m")
                    print(f"    n={result['signals']} WR={result['wr']:.3f} tgt={result['tgt_rate']:.1%} "
                          f"stp={result['stp_rate']:.1%} tmo={result['tmo_rate']:.1%}")
                    print(f"    exp={result['exp_bps']:+.2f}bps  [{v}]")
            except Exception as e:
                print(f"    ERROR: {e}")
                all_results.append({"strategy": label, "instrument": inst["name"], "signals": 0, "status": f"ERROR: {e}"})

    # Grand comparison
    print(f"\n\n{'=' * 80}")
    print("  GRAND COMPARISON — Best config per strategy per instrument")
    print(f"{'=' * 80}")

    viable = [r for r in all_results if r.get("viable")]
    print(f"\n  Total tested: {len(all_results)}")
    print(f"  Viable: {len(viable)}")

    if all_results:
        print(f"\n  {'Strategy':>12s} {'Instrument':>10s} {'RR':>5s} {'Tmo':>4s} {'n':>5s} {'WR':>6s} {'Tgt%':>6s} {'Stp%':>6s} {'Exp':>8s} {'V':>5s}")
        print(f"  {'-' * 72}")

        all_results.sort(key=_result_sort_key, reverse=True)
        for r in all_results:
            if r.get("status"):
                print(f"  {r['strategy']:>12s} {r['instrument']:>10s}  {r.get('status', '?')}")
            else:
                v = "YES" if r.get("viable") else "no"
                print(f"  {r['strategy']:>12s} {r['instrument']:>10s} {str(r.get('rr','')):>5s} {r.get('timeout',''):>4} "
                      f"{r['signals']:>5} {r['wr']:>6.3f} {r['tgt_rate']:>5.1%} {r['stp_rate']:>5.1%} "
                      f"{r['exp_bps']:>+7.2f}b {v:>5}")

    if benchmarks:
        print(f"\n  --- External benchmarks (display only) ---")
        for row in benchmarks:
            exp = row.get("exp_bps")
            exp_text = f"{float(exp):+7.2f}b" if exp is not None else "   n/a"
            note = row.get("note", "")
            print(f"  {row['strategy']:>12s} {row['instrument']:>10s} {exp_text:>12s}  {note}")
    else:
        print("\n  No external benchmark file supplied; only computed results are shown.")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(_build_summary(all_results, benchmarks), indent=2),
            encoding="utf-8",
        )
        print(f"\nSaved summary: {out_path}")


if __name__ == "__main__":
    main()
