"""gld_pm_long signal-hours sweep.

Live strategy fires on UTC hours [18, 19, 20] (~14:00-16:00 ET).
Sweep other hour windows to find if a different PM window has
stronger edge.

USAGE
-----
    python -m ops.audit.run_gld_pm_signal_hours_sweep --json

Each variant fires on a different set of hours, all other PARAMS
identical to the live strategy. Slippage 5bp (matches production
baseline methodology).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# Candidate signal-hour windows to test
HOUR_WINDOWS = {
    "baseline_18_19_20": [18, 19, 20],
    "early_17_18_19":    [17, 18, 19],
    "late_19_20_21":     [19, 20, 21],
    "full_pm_17_to_21":  [17, 18, 19, 20, 21],
    "narrow_18_19":      [18, 19],
    "narrow_19_20":      [19, 20],
    "single_18":         [18],
    "single_19":         [19],
    "single_20":         [20],
}


def _backtest_gld_hours(hours: list[int], *, period: str = "2y",
                        slippage_bps: float = 5.0) -> dict:
    """Replay gld_pm_long signal on GLD with custom signal_hours_utc."""
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
        from forge.gld_pm_long.runner import PARAMS, atr
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"error": f"import: {exc}"}

    try:
        df = yf.download("GLD", period=period, interval="1h",
                         progress=False, auto_adjust=False)
    except Exception as exc:
        return {"error": f"yfinance: {exc}"}
    if df is None or df.empty:
        return {"error": "empty"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    needed = ["Open", "High", "Low", "Close"]
    if not all(c in df.columns for c in needed):
        return {"error": "missing cols"}
    df = df[needed].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    a_arr = atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    hours_set = set(hours)
    trades: list[float] = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue
        if df.index[i].hour not in hours_set:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        target = entry + PARAMS["target_atr"] * a
        stop = entry - PARAMS["stop_atr"] * a
        exit_px = None; exit_idx = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if L[j] <= stop:
                exit_px = stop; exit_idx = j; break
            if H[j] >= target:
                exit_px = target; exit_idx = j; break
        if exit_px is None:
            exit_idx = min(i + PARAMS["hold_bars"], len(df) - 1)
            exit_px = C[exit_idx]
        pnl_pct = (exit_px - entry) / entry * 100.0
        trades.append(pnl_pct)
        open_until = exit_idx

    if not trades:
        return {"error": "no trades"}

    drag = 2.0 * (slippage_bps / 100.0)
    pnls = [p - drag for p in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / len(pnls)
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    try:
        boot = bootstrap_profit_factor(pnls, n_resamples=2000)
        return {
            "n": len(trades),
            "wr": round(wr, 3),
            "pf": round(pf, 2),
            "ci_lower": round(boot.ci_lower, 2),
            "ci_upper": round(boot.ci_upper, 2),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        }
    except Exception as exc:
        return {"error": f"bootstrap: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="2y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = []
    for name, hours in HOUR_WINDOWS.items():
        print(f"  {name} hours={hours}...", file=sys.stderr, end=" ",
              flush=True)
        r = _backtest_gld_hours(hours, period=args.period)
        r["window"] = name
        r["hours"] = hours
        rows.append(r)
        if r.get("error"):
            print(f"ERR {r['error']}", file=sys.stderr)
        else:
            print(
                f"n={r['n']} PF={r['pf']:.2f} CI_lo={r['ci_lower']:.2f}",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "gld_pm_signal_hours_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print()
    print("# gld_pm_long signal-hours sweep")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"Slippage: 5 bps. Survivor: CI lower >= 1.05 (DEFENSE floor)")
    print()
    print("| Window | Hours | n | WR | PF | CI lower | Avg% |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            print(f"| {r['window']} | {r['hours']} | — | — | — | — | ERR |")
            continue
        survivor = "[SURVIVOR]" if r["ci_lower"] >= 1.05 else ""
        print(
            f"| {r['window']} | {r['hours']} | {r['n']} | "
            f"{r['wr']*100:.1f}% | {r['pf']:.2f} | {r['ci_lower']:.2f} {survivor} | "
            f"{r['avg_pnl_pct']:+.3f}% |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
