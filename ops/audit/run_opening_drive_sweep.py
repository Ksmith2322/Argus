"""Sweep: opening-drive momentum strategies on liquid US ETFs.

Hypothesis: directional moves in the first 1-2 hours of US session
often continue. Tests three variants of the pattern on liquid ETFs to
find which (instrument, variant) pairs survive the disciplined gate.

VARIANTS TESTED:
  A. first_bar_continuation
     At 15 UTC (after first 1h bar of session 14:30-15:30 UTC), if
     that bar closed UP, enter LONG. ATR-based stop/target.

  B. gap_continuation
     At 14:30 UTC bar close, if today's open gapped UP > 0.3% vs prior
     close, enter LONG. Ride the gap.

  C. first_bar_reversal
     At 15 UTC, if first bar closed DOWN by more than 0.5%, enter LONG
     (mean-revert / fade the open dump).

Each variant runs the same exit machinery as gld_pm_long:
  - Target: +1.5 ATR
  - Stop: -1.0 ATR
  - Hold: up to 6 bars
  - Exit at first hit OR timeout

Disciplined gate (same calibration as PM-pattern sweep):
  - 5bps round-trip slippage
  - Bootstrap PF CI lower >= 1.20
  - Point PF >= 1.30
  - Min n=100

Output:
  ops/reports/system_audit/opening_drive_sweep.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"
_CACHE_DIR = _REPO / "helio" / "data_yfinance_1h"

CANDIDATES = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE",
    "MTUM", "QUAL", "USO",
]

# Strategy params (different from PM-pattern -- wider stops, longer hold)
TARGET_ATR = 1.5
STOP_ATR = 1.0
HOLD_BARS = 6
ATR_PERIOD = 14
GAP_THRESHOLD_PCT = 0.30   # for gap_continuation
REVERSAL_THRESHOLD_PCT = 0.50  # for first_bar_reversal

# Disciplined gate
SLIPPAGE_BPS_ROUND_TRIP = 5.0
PROMOTION_PF_FLOOR = 1.20
MIN_TRADES_FLOOR = 100
POINT_PF_MARGIN = 1.30


def _atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _fetch_1h(ticker: str, period_days: int = 720) -> pd.DataFrame:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"{ticker}_1h.csv"
    if cache_path.exists():
        age_s = (datetime.now(timezone.utc).timestamp() - cache_path.stat().st_mtime)
        if age_s < 86400:
            try:
                df = pd.read_csv(cache_path, parse_dates=["Datetime"]).set_index("Datetime")
                if not df.empty:
                    if df.index.tz is None:
                        df.index = df.index.tz_localize("UTC")
                    return df
            except Exception:
                pass
    try:
        import yfinance as yf
        df = yf.download(ticker, period=f"{period_days}d", interval="1h",
                         progress=False, auto_adjust=False)
    except Exception as exc:
        print(f"  fetch failed for {ticker}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "Datetime"
    keep_cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep_cols].copy().dropna()
    df.to_csv(cache_path)
    return df


def _backtest_variant(df: pd.DataFrame, variant: str) -> list[dict]:
    """Run the chosen variant on 1h bars. Returns ATR-unit pnl trades."""
    if df.empty or len(df) < ATR_PERIOD + HOLD_BARS + 10:
        return []
    a_arr = _atr(df, ATR_PERIOD).values
    H = df["High"].values
    L = df["Low"].values
    C = df["Close"].values
    O = df["Open"].values
    idx = df.index
    trades = []
    open_until = -1

    for i in range(1, len(df)):
        if i <= open_until:
            continue
        ts = idx[i]
        if ts.weekday() >= 5:
            continue
        # Signal logic per variant. All variants enter LONG.
        signal = False
        if variant == "first_bar_continuation":
            # Signal at 15 UTC bar (first 1h bar ending after open).
            # yfinance 1h bars for US ETFs start at 14:30 UTC; the 15:30
            # UTC bar represents the 15:00-16:00 trading period. We use
            # bar at hour 15 = the bar OPENING at 15:00 UTC.
            if ts.hour == 15:
                # Look back one bar — the first bar of session
                if i >= 1 and C[i-1] > O[i-1]:  # first bar was UP
                    signal = True
        elif variant == "gap_continuation":
            # At first bar of new session, check overnight gap.
            if ts.hour == 14 and ts.minute == 30:
                # Find prior session's close
                prev_close_idx = i - 1
                while prev_close_idx > 0 and idx[prev_close_idx].date() == ts.date():
                    prev_close_idx -= 1
                if prev_close_idx >= 0:
                    prev_close = C[prev_close_idx]
                    gap_pct = (O[i] - prev_close) / prev_close * 100.0
                    if gap_pct > GAP_THRESHOLD_PCT:
                        signal = True
        elif variant == "first_bar_reversal":
            # At 15 UTC, if first bar was DOWN by > threshold, enter long (fade)
            if ts.hour == 15 and i >= 1:
                first_bar_pct = (C[i-1] - O[i-1]) / O[i-1] * 100.0
                if first_bar_pct < -REVERSAL_THRESHOLD_PCT:
                    signal = True
        if not signal:
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
            "entry_dt": str(idx[i]), "exit_dt": str(idx[exit_idx]),
            "entry_px": float(entry), "exit_px": float(exit_px),
            "pnl_atr": float(pnl_atr),
        })
        open_until = exit_idx
    return trades


def _apply_slippage(trades: list[dict], bps: float) -> None:
    for t in trades:
        pnl_pct = (t["exit_px"] - t["entry_px"]) / t["entry_px"] * 100.0
        atr_pct = abs(pnl_pct / t["pnl_atr"]) if t["pnl_atr"] != 0 else 0
        slip_pct = bps / 100.0
        slip_atr = slip_pct / atr_pct if atr_pct > 0 else 0
        t["pnl_atr_net"] = t["pnl_atr"] - slip_atr


def evaluate_gate(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "verdict": "INSUFFICIENT_DATA", "reason": "no trades"}
    pnls = [t.get("pnl_atr_net", t["pnl_atr"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(pnls)
    expectancy = sum(pnls) / len(pnls)
    try:
        from helio.bootstrap_stats import bootstrap_profit_factor
        bs = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
        ci_lower = bs.ci_lower
        ci_upper = bs.ci_upper
    except Exception:
        ci_lower, ci_upper = None, None
    n = len(trades)
    verdicts = []
    if n < MIN_TRADES_FLOOR:
        verdicts.append(f"n={n} < {MIN_TRADES_FLOOR}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is None:
        verdicts.append("bootstrap CI unavailable")
    elif ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")
    if not verdicts:
        verdict = "SURVIVES"
    elif (n >= MIN_TRADES_FLOOR and pf > 1.0 and ci_lower is not None
          and ci_lower >= 1.10):
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"
    return {
        "n": n,
        "pf": round(pf, 3) if pf != float("inf") else 999.0,
        "win_rate": round(wr, 3),
        "expectancy_atr": round(expectancy, 4),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "ci_upper_95": round(ci_upper, 3) if ci_upper is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


VARIANTS = ["first_bar_continuation", "gap_continuation", "first_bar_reversal"]


def run_sweep() -> dict:
    results = []
    for ticker in CANDIDATES:
        print(f"\n[{ticker}] fetch...")
        df = _fetch_1h(ticker)
        if df.empty:
            print(f"  skipped: no data")
            continue
        actual_years = (df.index[-1] - df.index[0]).days / 365.25
        for variant in VARIANTS:
            trades = _backtest_variant(df, variant)
            if not trades:
                continue
            _apply_slippage(trades, SLIPPAGE_BPS_ROUND_TRIP)
            gate = evaluate_gate(trades)
            tpy = round(gate["n"] / actual_years, 1) if actual_years > 0 else 0
            row = {
                "ticker": ticker, "variant": variant,
                "years_actual": round(actual_years, 2),
                "trades_per_year": tpy, **gate,
            }
            results.append(row)
            marker = {"SURVIVES": "PASS", "MARGINAL": "MARG",
                      "FAIL": "fail", "INSUFFICIENT_DATA": "-"}.get(gate["verdict"], "?")
            print(f"  {ticker:5s} {variant:28s}: n={gate['n']:4d} PF={gate['pf']:5.2f} "
                  f"CI_lo={gate['ci_lower_95']} WR={gate['win_rate']} "
                  f"tpy={tpy:5.1f} -> [{marker}] {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "params": {
            "target_atr": TARGET_ATR, "stop_atr": STOP_ATR,
            "hold_bars": HOLD_BARS, "atr_period": ATR_PERIOD,
            "gap_threshold_pct": GAP_THRESHOLD_PCT,
            "reversal_threshold_pct": REVERSAL_THRESHOLD_PCT,
        },
        "candidates": CANDIDATES,
        "variants": VARIANTS,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Opening-drive sweep -- new intraday edge family")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"CI floor: {report['promotion_pf_floor']}  |  "
                 f"Point PF margin: {report['point_pf_margin']}  |  "
                 f"Min n: {report['min_trades_floor']}")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Ticker | Variant | Years | n | trades/yr | PF | CI lower | WR | Verdict |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in report["results"]:
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "—"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "—"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "—"
        years = f"{r['years_actual']:.1f}" if r.get('years_actual') else "—"
        lines.append(
            f"| {r['ticker']} | {r['variant']} | {years} | {r['n']} | "
            f"{r['trades_per_year']} | {pf} | {ci} | {wr} | **{r['verdict']}** |"
        )
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    lines.append("")
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL")
    lines.append("")
    if survivors:
        lines.append("**Ship these**:")
        for s in survivors:
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['variant']}` -> "
                f"+{s['trades_per_year']:.0f} fills/yr, PF {s['pf']:.2f}, "
                f"CI lower {s['ci_lower_95']:.2f}"
            )
    if marginals:
        lines.append("")
        lines.append("**Marginal candidates**:")
        for m in marginals:
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['variant']}` -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CI lower {m['ci_lower_95']:.2f}"
            )
    if not survivors and not marginals:
        lines.append("**No survivors.** Opening-drive momentum on these "
                     "instruments + variants does not survive the disciplined "
                     "gate at 5bps round-trip slippage.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    print("=== Opening-drive sweep ===")
    print(f"{len(CANDIDATES)} candidates x {len(VARIANTS)} variants = "
          f"{len(CANDIDATES) * len(VARIANTS)} cells")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, "
          f"CI >= {PROMOTION_PF_FLOOR}, PF >= {POINT_PF_MARGIN}, min n={MIN_TRADES_FLOOR}")
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "opening_drive_sweep.json"
    md_path = _OUT_DIR / "opening_drive_sweep.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")
    print()
    print(md)
    print()
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
