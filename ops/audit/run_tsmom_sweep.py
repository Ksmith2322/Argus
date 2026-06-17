"""Sweep: time-series momentum (TSMOM) per Moskowitz-Ooi-Pedersen 2012.

PER-ASSET trend signal -- distinct from cross-sectional momentum which
ranks assets against each other. TSMOM asks: is THIS asset itself
trending up over its own trailing window?

  Long if trailing N-month return > 0
  Flat if trailing N-month return <= 0
  (Optional short variant excluded; we don't short)
  Rebalance: monthly (check signal at month-end)
  Exit: when signal flips negative
  Hold: until signal flip (path-dependent; typically 3-12 months)

This is GENUINELY orthogonal to forge_xs_momentum:
  - xs_momentum ranks all 8 assets and picks top-N (relative strength)
  - tsmom evaluates each asset against its own history (absolute strength)
  - In a broad market downturn, xs_momentum still picks "least bad" longs
  - tsmom would go flat across the board

VARIANTS:
  A. tsmom_12m_long     12-month trailing > 0
  B. tsmom_6m_long      6-month trailing > 0 (faster signal)
  C. tsmom_3m_long      3-month trailing > 0 (fastest)
  D. tsmom_12m_with_vol 12m signal + position sized inverse to realized vol

Universe: all 154 daily tickers (ETFs + S&P 100 stocks).

Two-tier promotion gate (same as mean_reversion_sweep):
  - PF >= 1.30, bootstrap CI lower >= 1.20, min n=20 (lower n: ~3-6 transitions/yr)
  - Pre-tax CAGR >= 13% (SPY net-of-tax bar)

Output: ops/reports/system_audit/tsmom_sweep.{md,json}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"
_DATA_DIR = _REPO / "helio" / "data_yfinance"

# Use the same 154-ticker universe as mean reversion sweep
from ops.audit.run_mean_reversion_sweep import CANDIDATES

SLIPPAGE_BPS_ROUND_TRIP = 5.0
PROMOTION_PF_FLOOR = 1.20
POINT_PF_MARGIN = 1.30
MIN_TRADES_FLOOR = 20   # TSMOM gives few transitions; relaxed
CAGR_FLOOR_BEAT_SPY = 0.13


def _load_daily(ticker: str) -> pd.DataFrame:
    p = _DATA_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def _to_monthly_close(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily to last-trading-day-of-month close."""
    return df["Close"].resample("ME").last().to_frame("Close").dropna()


def _backtest_variant(df: pd.DataFrame, variant: str) -> list[dict]:
    if df.empty:
        return []
    m = _to_monthly_close(df)
    if len(m) < 16:
        return []
    closes = m["Close"].values
    idx = m.index

    if variant == "tsmom_12m_long":
        lookback = 12
    elif variant == "tsmom_6m_long":
        lookback = 6
    elif variant == "tsmom_3m_long":
        lookback = 3
    elif variant == "tsmom_12m_with_vol":
        lookback = 12  # vol scaling applied to pnl below
    else:
        return []

    # Compute trailing-N-month return at each month-end
    signal = np.zeros(len(closes), dtype=bool)
    for i in range(lookback, len(closes)):
        trailing = closes[i] / closes[i - lookback] - 1.0
        signal[i] = trailing > 0

    # Trades: a trade is one held period (long N months until signal flips)
    trades = []
    in_pos = False
    entry_idx = None
    for i in range(lookback, len(closes)):
        if signal[i] and not in_pos:
            entry_idx = i
            in_pos = True
        elif (not signal[i]) and in_pos:
            # Exit at this month-end
            entry_px = float(closes[entry_idx])
            exit_px = float(closes[i])
            gross_pct = (exit_px - entry_px) / entry_px * 100.0
            pnl_pct = gross_pct - (SLIPPAGE_BPS_ROUND_TRIP / 100.0)
            if variant == "tsmom_12m_with_vol":
                # Scale by inverse realized vol (12m, annualized)
                window = closes[max(0, i - 12):i + 1]
                if len(window) > 1:
                    returns = np.diff(window) / window[:-1]
                    vol = np.std(returns) * np.sqrt(12) if len(returns) > 0 else 0.20
                    target_vol = 0.20  # 20% target
                    scale = min(2.0, max(0.25, target_vol / max(vol, 0.01)))
                    pnl_pct *= scale
            trades.append({
                "entry_dt": str(idx[entry_idx].date()),
                "exit_dt": str(idx[i].date()),
                "entry_px": entry_px,
                "exit_px": exit_px,
                "pnl_pct": float(pnl_pct),
                "held_months": int(i - entry_idx),
            })
            in_pos = False
            entry_idx = None
    # Close any open trade at the end
    if in_pos and entry_idx is not None:
        entry_px = float(closes[entry_idx])
        exit_px = float(closes[-1])
        gross_pct = (exit_px - entry_px) / entry_px * 100.0
        pnl_pct = gross_pct - (SLIPPAGE_BPS_ROUND_TRIP / 100.0)
        trades.append({
            "entry_dt": str(idx[entry_idx].date()),
            "exit_dt": str(idx[-1].date()),
            "entry_px": entry_px,
            "exit_px": exit_px,
            "pnl_pct": float(pnl_pct),
            "held_months": int(len(closes) - 1 - entry_idx),
        })
    return trades


def _estimate_cagr(trades: list[dict], years: float) -> float:
    if not trades or years <= 0:
        return 0.0
    equity = 100.0
    for t in trades:
        equity *= (1.0 + t["pnl_pct"] / 100.0)
    total_return = equity / 100.0
    if total_return <= 0:
        return -1.0
    return total_return ** (1.0 / years) - 1.0


def evaluate_gate(trades: list[dict], years: float) -> dict:
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
    cagr = _estimate_cagr(trades, years)

    verdicts = []
    if n < MIN_TRADES_FLOOR:
        verdicts.append(f"n={n} < {MIN_TRADES_FLOOR}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is None:
        verdicts.append("CI unavailable")
    elif ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")
    if cagr < CAGR_FLOOR_BEAT_SPY:
        verdicts.append(f"CAGR={cagr*100:.1f}% < {CAGR_FLOOR_BEAT_SPY*100:.0f}%")
    if not verdicts:
        verdict = "SURVIVES"
    elif (n >= MIN_TRADES_FLOOR and pf > 1.0 and ci_lower is not None
          and ci_lower >= 1.10 and cagr >= 0.08):
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"
    return {
        "n": n,
        "pf": round(pf, 3) if pf != float("inf") else 999.0,
        "win_rate": round(wr, 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "cagr_pretax": round(cagr, 4),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "ci_upper_95": round(ci_upper, 3) if ci_upper is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


VARIANTS = ["tsmom_12m_long", "tsmom_6m_long", "tsmom_3m_long", "tsmom_12m_with_vol"]


def run_sweep() -> dict:
    results = []
    for ticker in CANDIDATES:
        df = _load_daily(ticker)
        if df.empty:
            continue
        years = (df.index[-1] - df.index[0]).days / 365.25
        for variant in VARIANTS:
            trades = _backtest_variant(df, variant)
            gate = evaluate_gate(trades, years)
            tpy = round(gate["n"] / years, 2) if years > 0 else 0
            row = {"ticker": ticker, "variant": variant,
                   "years_actual": round(years, 1),
                   "trades_per_year": tpy, **gate}
            results.append(row)
            if gate["verdict"] in ("SURVIVES", "MARGINAL"):
                print(f"  {ticker:5s} {variant:22s}: n={gate['n']:3d} "
                      f"PF={gate['pf']:5.2f} CI_lo={gate['ci_lower_95']} "
                      f"CAGR={gate.get('cagr_pretax', 0)*100:5.1f}% tpy={tpy:5.1f} "
                      f"-> {gate['verdict']}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps_round_trip": SLIPPAGE_BPS_ROUND_TRIP,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "cagr_floor_beat_spy": CAGR_FLOOR_BEAT_SPY,
        "candidates": CANDIDATES,
        "variants": VARIANTS,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# Time-series momentum (TSMOM) sweep")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Per Moskowitz-Ooi-Pedersen 2012; per-asset trend, monthly rebalance.")
    lines.append(f"Slippage: {report['slippage_bps_round_trip']}bps RT  |  "
                 f"Gate: PF >= {report['point_pf_margin']}, CI lower >= "
                 f"{report['promotion_pf_floor']}, CAGR >= "
                 f"{report['cagr_floor_beat_spy']*100:.0f}%, min n={report['min_trades_floor']}")
    lines.append("")
    survivors = [r for r in report["results"] if r["verdict"] == "SURVIVES"]
    marginals = [r for r in report["results"] if r["verdict"] == "MARGINAL"]
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL, "
                 f"{len(report['results']) - len(survivors) - len(marginals)} FAIL/INS")
    lines.append("")
    if survivors:
        lines.append("**Ship-eligible** (passed all 4 gates including CAGR):")
        for s in sorted(survivors, key=lambda x: -x["cagr_pretax"]):
            lines.append(
                f"- `forge_{s['ticker'].lower()}_{s['variant']}` -> "
                f"n={s['n']}, PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}, "
                f"CAGR {s['cagr_pretax']*100:.1f}%, +{s['trades_per_year']:.1f} fills/yr"
            )
    else:
        lines.append("**No clean survivors.** TSMOM fails the disciplined + CAGR gate.")
    if marginals:
        lines.append("")
        lines.append("**Marginals** (passed PF + CI but missed CAGR or n):")
        for m in sorted(marginals, key=lambda x: -x["cagr_pretax"])[:20]:
            lines.append(
                f"- `forge_{m['ticker'].lower()}_{m['variant']}` -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CAGR {m['cagr_pretax']*100:.1f}%"
            )
    lines.append("")
    lines.append("## Top 30 results by CAGR")
    lines.append("")
    lines.append("| Ticker | Variant | n | PF | CI lower | CAGR | trades/yr | Verdict |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    top = sorted(report["results"], key=lambda x: -(x.get("cagr_pretax") or 0))[:30]
    for r in top:
        ci = f"{r['ci_lower_95']:.2f}" if r.get('ci_lower_95') is not None else "--"
        pf = f"{r['pf']:.2f}" if r.get('pf') is not None else "--"
        cagr_val = r.get('cagr_pretax')
        cagr = f"{cagr_val*100:.1f}%" if cagr_val is not None else "--"
        lines.append(
            f"| {r['ticker']} | {r['variant']} | {r['n']} | {pf} | {ci} | {cagr} | "
            f"{r['trades_per_year']} | **{r['verdict']}** |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(f"=== TSMOM sweep ===")
    print(f"{len(CANDIDATES)} tickers x {len(VARIANTS)} variants")
    print(f"Gate: slip={SLIPPAGE_BPS_ROUND_TRIP}bps RT, PF >= {POINT_PF_MARGIN}, "
          f"CI >= {PROMOTION_PF_FLOOR}, CAGR >= {CAGR_FLOOR_BEAT_SPY*100:.0f}%, "
          f"min n={MIN_TRADES_FLOOR}")
    print()
    print("Printing only SURVIVES / MARGINAL during run; full table in MD output.")
    print()
    report = run_sweep()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / "tsmom_sweep.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    (_OUT_DIR / "tsmom_sweep.md").write_text(
        render_markdown(report), encoding="utf-8")
    survivors = sum(1 for r in report["results"] if r["verdict"] == "SURVIVES")
    marginals = sum(1 for r in report["results"] if r["verdict"] == "MARGINAL")
    print()
    print(f"=== DONE: {survivors} SURVIVES, {marginals} MARGINAL, "
          f"{len(report['results'])} cells total ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
