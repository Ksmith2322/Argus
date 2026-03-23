"""Test all 3 new strategies (FVG, Liquidity Sweep, Volume Profile) on NQ and EUR/USD.

Runs the full payoff-first analysis on each combination.

Usage:
    python -m argus_flow.strategies.test_all_strategies
"""
from __future__ import annotations

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
    print("=" * 80)
    print("  STRATEGY COMPARISON: FVG vs Liquidity Sweep vs Volume Profile")
    print("  Testing on NQ (futures) and EUR/USD (forex)")
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

        all_results.sort(key=lambda x: x.get("exp_bps", -999), reverse=True)
        for r in all_results:
            if r.get("status"):
                print(f"  {r['strategy']:>12s} {r['instrument']:>10s}  {r.get('status', '?')}")
            else:
                v = "YES" if r.get("viable") else "no"
                print(f"  {r['strategy']:>12s} {r['instrument']:>10s} {str(r.get('rr','')):>5s} {r.get('timeout',''):>4} "
                      f"{r['signals']:>5} {r['wr']:>6.3f} {r['tgt_rate']:>5.1%} {r['stp_rate']:>5.1%} "
                      f"{r['exp_bps']:>+7.2f}b {v:>5}")

    # Compare against existing strategies
    print(f"\n  --- Existing strategies for reference ---")
    print(f"  {'T4 Full':>12s} {'EUR/USD':>10s}                            +0.98b  (current runner)")
    print(f"  {'Vol Burst':>12s} {'MNQ':>10s}                            +6.41b  (current runner)")
    print(f"  {'Range+Accel':>12s} {'GBP/USD':>10s}                            +0.97b  (current runner)")


if __name__ == "__main__":
    main()