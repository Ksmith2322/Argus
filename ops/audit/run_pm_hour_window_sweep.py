"""Sweep: gld_pm_long PM-window pattern at DIFFERENT hour windows.

The live forge_gld_pm_long + forge_uso_pm_long use signal_hours_utc =
[18, 19, 20] (last hour of US equity session). This sweep tests
whether the same ATR-bracketed long pattern has edge at OTHER hours:

HOUR WINDOWS TESTED:
  open_hour     [14]              -- first hour of US session
  am_session    [14, 15]          -- first two hours
  midday        [15, 16, 17]      -- middle three hours
  pm_baseline   [18, 19, 20]      -- existing (control / sanity check)
  last_hour     [20]              -- final hour only
  full_day      [14..20]          -- entire RTH

Disciplined gate (same as run_pm_pattern_sweep.py):
  5bps RT slippage, bootstrap PF CI >= 1.20, PF point >= 1.30, min n=100.

Reuses the 1h cache populated by run_pm_pattern_sweep.py
(helio/data_yfinance_1h/<TICKER>_1h.csv).

Output: ops/reports/system_audit/pm_hour_window_sweep.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Reuse pm_pattern_sweep's helpers + cache + gate
from ops.audit.run_pm_pattern_sweep import (
    _fetch_1h, _atr, apply_slippage_atr, evaluate_gate,
    TARGET_ATR, STOP_ATR, HOLD_BARS, ATR_PERIOD,
    SLIPPAGE_BPS_ROUND_TRIP, PROMOTION_PF_FLOOR, POINT_PF_MARGIN,
    MIN_TRADES_FLOOR,
)

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"

CANDIDATES = [
    "GLD", "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV",
    "TLT", "USO",
    "MTUM", "QUAL", "EEM",
]

HOUR_WINDOWS = {
    "open_hour":   [14],
    "am_session":  [14, 15],
    "midday":      [15, 16, 17],
    "pm_baseline": [18, 19, 20],   # control / sanity
    "last_hour":   [20],
    "full_day":    [14, 15, 16, 17, 18, 19, 20],
}


def backtest_at_hours(df: pd.DataFrame, signal_hours: list[int]) -> list[dict]:
    """Mirror gld_pm_long.backtest() loop with custom signal hours."""
    if df.empty or len(df) < ATR_PERIOD + HOLD_BARS + 10:
        return []
    a_arr = _atr(df, ATR_PERIOD).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    trades = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue
        if df.index[i].hour not in signal_hours:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        target = entry + TARGET_ATR * a
        stop = entry - STOP_ATR * a
        exit_px = None
        exit_idx = None
        for j in range(i + 1, min(i + 1 + HOLD_BARS, len(df))):
            if L[j] <= stop:
                exit_px = stop
                exit_idx = j
                break
            if H[j] >= target:
                exit_px = target
                exit_idx = j
                break
        if exit_px is None:
            exit_idx = min(i + HOLD_BARS, len(df) - 1)
            exit_px = C[exit_idx]
        pnl_atr = (exit_px - entry) / a
        trades.append({
            "entry_dt": str(df.index[i]),
            "exit_dt": str(df.index[exit_idx]),
            "entry_px": float(entry),
            "exit_px": float(exit_px),
            "pnl_atr": float(pnl_atr),
        })
        open_until = exit_idx
    return trades


def run_sweep() -> dict:
    print(f"=== PM-hour-window sweep ===")
    print(f"{len(CANDIDATES)} tickers x {len(HOUR_WINDOWS)} hour windows")
    print(f"Gate: 5bps RT, CI >= 1.20, PF >= 1.30, min n=100")
    print()
    results = []
    for ticker in CANDIDATES:
        df = _fetch_1h(ticker)
        if df.empty:
            print(f"  {ticker:6s}: no 1h data, skipping")
            continue
        actual_days = (df.index[-1] - df.index[0]).days
        for win_name, hours in HOUR_WINDOWS.items():
            trades = backtest_at_hours(df, hours)
            apply_slippage_atr(trades, SLIPPAGE_BPS_ROUND_TRIP)
            gate = evaluate_gate(trades, use_net=True)
            tpy = round(gate["n"] / (actual_days / 365.25), 2) if actual_days > 0 else 0
            row = {"ticker": ticker, "hour_window": win_name, "hours": hours,
                   "days_actual": actual_days, "trades_per_year": tpy, **gate}
            results.append(row)
            if gate["verdict"] in ("SURVIVES", "MARGINAL"):
                print(f"  {ticker:5s} {win_name:12s}: n={gate['n']:4d} "
                      f"PF={gate['pf']:5.2f} CI_lo={gate['ci_lower_95']} "
                      f"WR={gate['win_rate']} tpy={tpy:6.1f} -> {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "candidates": CANDIDATES,
        "hour_windows": HOUR_WINDOWS,
        "target_atr": TARGET_ATR,
        "stop_atr": STOP_ATR,
        "hold_bars": HOLD_BARS,
        "atr_period": ATR_PERIOD,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# PM-hour-window sweep -- gld_pm_long pattern at different hours")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"Gate: CI >= {report['promotion_pf_floor']}, PF >= "
                 f"{report['point_pf_margin']}, min n={report['min_trades_floor']}")
    lines.append("")
    survivors = [r for r in report["results"] if r["verdict"] == "SURVIVES"]
    marginals = [r for r in report["results"] if r["verdict"] == "MARGINAL"]
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
                 f"{len(report['results']) - len(survivors) - len(marginals)} FAIL/INS")
    lines.append("")
    if survivors:
        lines.append("**Ship-eligible**:")
        for s in survivors:
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['hour_window']}` (hours {s['hours']}) -> "
                f"n={s['n']}, PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}, "
                f"+{s['trades_per_year']:.0f} fills/yr"
            )
    if marginals:
        lines.append("")
        lines.append("**Marginals**:")
        for m in marginals:
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['hour_window']}` -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CI lower {m['ci_lower_95']:.2f}"
            )
    lines.append("")
    lines.append("## All results")
    lines.append("")
    lines.append("| Ticker | Window | Hours | n | PF | CI lower | WR | trades/yr | Verdict |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---|")
    for r in sorted(report["results"], key=lambda x: -(x.get("pf") or 0)):
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "--"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "--"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "--"
        lines.append(
            f"| {r['ticker']} | {r['hour_window']} | {r['hours']} | {r['n']} | "
            f"{pf} | {ci} | {wr} | {r['trades_per_year']} | **{r['verdict']}** |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / "pm_hour_window_sweep.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    (_OUT_DIR / "pm_hour_window_sweep.md").write_text(
        render_markdown(report), encoding="utf-8")
    survivors = sum(1 for r in report["results"] if r["verdict"] == "SURVIVES")
    marginals = sum(1 for r in report["results"] if r["verdict"] == "MARGINAL")
    print()
    print(f"=== DONE: {survivors} SURVIVES, {marginals} MARGINAL, "
          f"{len(report['results'])} cells total ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
