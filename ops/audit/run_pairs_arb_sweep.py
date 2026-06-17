"""Sweep: pairs / stat-arb mean-reversion on ETF pairs.

Hypothesis: pairs of correlated ETFs occasionally diverge from their
historical spread relationship. Trading the convergence is a different
edge family from momentum (which is what xs_momentum + USO + opening
drive all attempt).

MECHANISM:
  1. Compute the rolling 60-day Z-score of (log(A) - hedge_ratio * log(B))
  2. When Z > +2.0: short the spread (short A, long B)
  3. When Z < -2.0: long the spread (long A, short B)
  4. Exit on Z crosses 0 OR after 20 trading days OR -3 sigma stop

PAIRS TESTED:
  - XLK / XLF  (tech vs financials -- often diverge on rate moves)
  - XLE / XOP  (integrated energy vs producers -- well-known pair)
  - GLD / SLV  (precious metals)
  - TLT / IEF  (long vs intermediate duration treasuries)
  - SPY / IWM  (large vs small cap)
  - EWJ / EWG  (Japan vs Germany)
  - XLY / XLP  (consumer discretionary vs staples)
  - QQQ / SPY  (tech vs broad)

Output:
  ops/reports/system_audit/pairs_arb_sweep.{md,json}
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
_DATA_DIR = _REPO / "helio" / "data_yfinance"

# Each entry: (ticker_a, ticker_b)
PAIRS = [
    ("XLK", "XLF"),
    ("XLE", "USO"),
    ("GLD", "TLT"),
    ("TLT", "IEF"),
    ("SPY", "IWM"),
    ("EWJ", "EWG"),
    ("XLY", "XLP"),
    ("QQQ", "SPY"),
    ("XLK", "QQQ"),
    ("EEM", "EFA"),
]

# Strategy params
ZSCORE_LOOKBACK_DAYS = 60
ENTRY_Z = 2.0      # absolute z-score to enter
EXIT_Z = 0.0       # exit when z crosses 0
STOP_Z = 3.0       # stop if z keeps blowing out
MAX_HOLD_DAYS = 20

# Disciplined gate
SLIPPAGE_BPS_ROUND_TRIP = 8.0  # 2x ETFs traded simultaneously, ~4bps each
PROMOTION_PF_FLOOR = 1.20
MIN_TRADES_FLOOR = 30
POINT_PF_MARGIN = 1.30


def _load_daily(ticker: str) -> pd.Series:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    s = df["Close"].astype(float)
    s.name = ticker
    return s


def _try_load_or_skip(ticker: str) -> Optional[pd.Series]:
    s = _load_daily(ticker)
    if s.empty:
        return None
    return s


def backtest_pair(a_ser: pd.Series, b_ser: pd.Series, name_a: str, name_b: str) -> list[dict]:
    """Compute spread z-score on log prices, generate trades on z-cross."""
    # Align
    df = pd.DataFrame({name_a: a_ser, name_b: b_ser}).dropna()
    if len(df) < ZSCORE_LOOKBACK_DAYS + 30:
        return []
    log_a = np.log(df[name_a])
    log_b = np.log(df[name_b])
    # Rolling regression for hedge ratio (alternatively, use OLS over full window).
    # For simplicity use rolling correlation-implied hedge ratio:
    # hedge_ratio = cov(A,B) / var(B) over rolling window.
    cov = log_a.rolling(ZSCORE_LOOKBACK_DAYS).cov(log_b)
    var_b = log_b.rolling(ZSCORE_LOOKBACK_DAYS).var()
    hedge_ratio = (cov / var_b).fillna(1.0)
    spread = log_a - hedge_ratio * log_b
    sp_mean = spread.rolling(ZSCORE_LOOKBACK_DAYS).mean()
    sp_std = spread.rolling(ZSCORE_LOOKBACK_DAYS).std()
    z = (spread - sp_mean) / sp_std

    trades = []
    position = 0  # 0=flat, +1 = long spread (long A, short B), -1 = short spread
    entry_idx = None
    entry_z = None
    entry_a = None
    entry_b = None
    entry_hr = None

    for i in range(ZSCORE_LOOKBACK_DAYS + 1, len(df)):
        zi = z.iloc[i]
        if not np.isfinite(zi):
            continue

        if position == 0:
            # Look for entry
            if zi <= -ENTRY_Z:
                position = +1   # long spread (long A, short hedge*B)
                entry_idx = i
                entry_z = zi
                entry_a = df[name_a].iloc[i]
                entry_b = df[name_b].iloc[i]
                entry_hr = hedge_ratio.iloc[i]
            elif zi >= ENTRY_Z:
                position = -1   # short spread (short A, long hedge*B)
                entry_idx = i
                entry_z = zi
                entry_a = df[name_a].iloc[i]
                entry_b = df[name_b].iloc[i]
                entry_hr = hedge_ratio.iloc[i]
        else:
            held_days = i - entry_idx
            should_exit = False
            exit_reason = None
            # Mean reversion crossed 0 — take profit
            if position == +1 and zi >= EXIT_Z:
                should_exit = True
                exit_reason = "mean_revert"
            elif position == -1 and zi <= EXIT_Z:
                should_exit = True
                exit_reason = "mean_revert"
            # Spread blew out further — stop
            elif position == +1 and zi <= -STOP_Z:
                should_exit = True
                exit_reason = "stop"
            elif position == -1 and zi >= STOP_Z:
                should_exit = True
                exit_reason = "stop"
            # Time stop
            elif held_days >= MAX_HOLD_DAYS:
                should_exit = True
                exit_reason = "timeout"

            if should_exit:
                exit_a = df[name_a].iloc[i]
                exit_b = df[name_b].iloc[i]
                # PnL on long-spread (position=+1): long A return - hedge*short B return
                # = (exit_a/entry_a - 1) - hedge*(exit_b/entry_b - 1)
                ret_a = (exit_a - entry_a) / entry_a
                ret_b = (exit_b - entry_b) / entry_b
                gross_pct = position * (ret_a - entry_hr * ret_b) * 100.0
                # Slippage applied per round trip (entry + exit on both legs)
                slip_pct = SLIPPAGE_BPS_ROUND_TRIP / 100.0
                pnl_pct = gross_pct - slip_pct
                trades.append({
                    "entry_dt": str(df.index[entry_idx].date()),
                    "exit_dt": str(df.index[i].date()),
                    "entry_z": float(entry_z),
                    "exit_z": float(zi),
                    "position": position,
                    "held_days": int(held_days),
                    "pnl_pct": float(pnl_pct),
                    "exit_reason": exit_reason,
                })
                position = 0
                entry_idx = None
    return trades


def evaluate_gate(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "verdict": "INSUFFICIENT_DATA", "reason": "no trades"}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(pnls)
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
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "ci_upper_95": round(ci_upper, 3) if ci_upper is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


def run_sweep() -> dict:
    results = []
    for (a, b) in PAIRS:
        a_ser = _try_load_or_skip(a)
        b_ser = _try_load_or_skip(b)
        if a_ser is None or b_ser is None:
            print(f"  skipped {a}/{b}: missing daily cache")
            results.append({"pair": f"{a}/{b}", "verdict": "NO_DATA", "n": 0,
                            "reason": f"missing {a if a_ser is None else b}"})
            continue
        common_start = max(a_ser.index.min(), b_ser.index.min())
        common_end = min(a_ser.index.max(), b_ser.index.max())
        actual_years = (common_end - common_start).days / 365.25
        trades = backtest_pair(a_ser, b_ser, a, b)
        gate = evaluate_gate(trades)
        tpy = round(gate["n"] / actual_years, 1) if actual_years > 0 else 0
        row = {"pair": f"{a}/{b}", "years_actual": round(actual_years, 1),
               "trades_per_year": tpy, **gate}
        results.append(row)
        marker = {"SURVIVES": "PASS", "MARGINAL": "MARG",
                  "FAIL": "fail", "INSUFFICIENT_DATA": "-"}.get(gate["verdict"], "?")
        print(f"  {a:>4s}/{b:<4s}: n={gate['n']:3d} PF={gate['pf']:5.2f} "
              f"CI_lo={gate['ci_lower_95']} WR={gate['win_rate']} "
              f"avg={gate.get('avg_pnl_pct')}% tpy={tpy:4.1f} -> [{marker}] {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "params": {
            "lookback_days": ZSCORE_LOOKBACK_DAYS,
            "entry_z": ENTRY_Z, "exit_z": EXIT_Z, "stop_z": STOP_Z,
            "max_hold_days": MAX_HOLD_DAYS,
        },
        "pairs_tested": [f"{a}/{b}" for (a,b) in PAIRS],
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Pairs / stat-arb sweep")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT (both legs)  |  "
                 f"CI floor: {report['promotion_pf_floor']}  |  "
                 f"Point PF margin: {report['point_pf_margin']}")
    lines.append("")
    lines.append("Params: " + json.dumps(report['params']))
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Pair | Years | n | trades/yr | PF | CI lower | WR | Avg pnl | Verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in report["results"]:
        if r.get("verdict") == "NO_DATA":
            lines.append(f"| {r['pair']} | — | — | — | — | — | — | — | **NO_DATA** |")
            continue
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "—"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "—"
        wr = f"{r['win_rate']:.2f}" if r.get('win_rate') is not None else "—"
        avg = f"{r['avg_pnl_pct']:.2f}%" if r.get('avg_pnl_pct') is not None else "—"
        years = f"{r['years_actual']:.1f}" if r.get('years_actual') else "—"
        lines.append(
            f"| {r['pair']} | {years} | {r['n']} | {r['trades_per_year']} | "
            f"{pf} | {ci} | {wr} | {avg} | **{r['verdict']}** |"
        )
    survivors = [r for r in report["results"] if r.get("verdict") == "SURVIVES"]
    marginals = [r for r in report["results"] if r.get("verdict") == "MARGINAL"]
    lines.append("")
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL")
    lines.append("")
    if survivors:
        lines.append("**Ship**:")
        for s in survivors:
            tag = s["pair"].replace("/", "_").lower()
            lines.append(
                f"- `forge_pair_{tag}` -> +{s['trades_per_year']:.0f} fills/yr, "
                f"PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}"
            )
    if marginals:
        lines.append("")
        lines.append("**Marginals**:")
        for m in marginals:
            tag = m["pair"].replace("/", "_").lower()
            lines.append(
                f"- `forge_pair_{tag}` -> n={m['n']}, PF {m['pf']:.2f}, "
                f"CI lower {m['ci_lower_95']:.2f}"
            )
    if not survivors and not marginals:
        lines.append("**No survivors.** Pairs/stat-arb on these ETF pairs "
                     "does not survive the disciplined gate at 8bps round-trip.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    print("=== Pairs/stat-arb sweep ===")
    print(f"{len(PAIRS)} pairs tested")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, CI >= {PROMOTION_PF_FLOOR}, "
          f"PF >= {POINT_PF_MARGIN}, min n={MIN_TRADES_FLOOR}")
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "pairs_arb_sweep.json"
    md_path = _OUT_DIR / "pairs_arb_sweep.md"
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
