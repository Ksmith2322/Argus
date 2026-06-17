"""Test xs_momentum at faster rebalance cadences across all 7 universes.

Goal: identify (universe, cadence) pairs that survive the disciplined gate
(20y window + 10bps slippage + bootstrap PF CI >= 1.20). Faster cadence =
more trades per year = more live evidence per unit time. The tradeoff is
higher slippage drag, so the disciplined gate is the truth check.

For each universe, test:
  - Monthly (baseline, ~12 picks/yr)
  - Bi-weekly (~26 picks/yr)
  - Weekly (~52 picks/yr)

Output:
  ops/reports/system_audit/xs_momentum_cadence_sweep.{md,json}

A "survivor" passes if:
  - n >= 30 trades (statistical floor)
  - bootstrap PF CI lower bound (95%) >= 1.20 at 10bps slippage
  - Point PF >= 1.30 (margin above floor)

Usage:
    python -m ops.audit.run_xs_momentum_cadence_sweep
    python -m ops.audit.run_xs_momentum_cadence_sweep --years 15
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO / "helio" / "data_yfinance"
_OUT_DIR = _REPO / "ops" / "reports" / "system_audit"


# Universe definitions (mirror forge/xs_momentum/runner._VARIANT_REGISTRY)
UNIVERSES: dict[str, dict] = {
    "broad_8": {
        "tickers": ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT"],
        "top_k": 2,
    },
    "sectors_spdr_11": {
        "tickers": ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
                    "XLB", "XLRE", "XLC"],
        "top_k": 2,
    },
    "style_factors_8": {
        "tickers": ["VTV", "VUG", "VYM", "VIG", "MTUM", "QUAL", "USMV", "VLUE"],
        "top_k": 2,
    },
    "style_top3": {
        "tickers": ["VTV", "VUG", "VYM", "VIG", "MTUM", "QUAL", "USMV", "VLUE"],
        "top_k": 3,
    },
    "legacy_sectors_countries_15": {
        "tickers": ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
                    "XLB", "XLC",
                    "EWJ", "EWG", "EWZ", "INDA", "FXI"],
        "top_k": 3,
    },
    "wide_global_47": {
        "tickers": ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT",
                    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
                    "XLB", "XLRE", "XLC",
                    "VTV", "VUG", "VYM", "VIG", "MTUM", "QUAL", "USMV", "VLUE",
                    "EWJ", "EWG", "EWZ", "INDA", "FXI"],
        "top_k": 6,  # roughly 20% of available tickers
    },
}

CADENCES: dict[str, str] = {
    "monthly": "ME",     # month-end
    "biweekly": "2W-FRI",
    "weekly": "W-FRI",
}

LOOKBACK_DAYS = 252
SKIP_RECENT_DAYS = 21
SLIPPAGE_BPS = 10.0
PROMOTION_PF_FLOOR = 1.20
MIN_TRADES_FLOOR = 30
POINT_PF_MARGIN = 1.30   # require point PF >= this for "survives"


def _load_universe_closes(tickers: list[str]) -> pd.DataFrame:
    frames = {}
    missing = []
    for t in tickers:
        p = _DATA_DIR / f"{t}_daily.csv"
        if not p.exists():
            missing.append(t)
            continue
        df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date").sort_index()
        frames[t] = df["Close"]
    if missing:
        print(f"  warn: missing daily cache for: {missing}")
    if not frames:
        return pd.DataFrame()
    combined = pd.DataFrame(frames).dropna(how="any")
    return combined


def _period_starts(index: pd.DatetimeIndex, freq: str) -> list[pd.Timestamp]:
    """Return rebalance dates for the given pandas frequency string."""
    if freq == "ME":
        # Use first trading day of each month for natural cadence
        months = index.to_series().groupby([index.year, index.month]).first()
        return list(months.values)
    # For weekly / bi-weekly, use last trading day of each period
    return list(index.to_series().resample(freq).last().dropna().values)


def run_xs_momentum_cadence(
    tickers: list[str],
    top_k: int,
    freq: str,
    *,
    closes: pd.DataFrame,
    lookback_days: int = LOOKBACK_DAYS,
    skip_recent_days: int = SKIP_RECENT_DAYS,
    slippage_bps: float = SLIPPAGE_BPS,
) -> list[dict]:
    """Run cross-sectional momentum at the given rebalance frequency.
    Returns a list of per-trade dicts with pnl_pct (after slippage)."""
    if closes.empty:
        return []
    if top_k > len(tickers):
        return []
    rebal_dates = _period_starts(closes.index, freq)
    trades = []
    held: dict[str, tuple[pd.Timestamp, float]] = {}  # ticker -> (entry_dt, entry_px)

    for i, rebal in enumerate(rebal_dates):
        rebal = pd.Timestamp(rebal)
        if rebal not in closes.index:
            # Find nearest prior trading day
            nearest = closes.index[closes.index <= rebal]
            if len(nearest) == 0:
                continue
            rebal = nearest[-1]

        # Close prior positions
        if held and i > 0:
            for t, (entry_dt, entry_px) in held.items():
                if t not in closes.columns:
                    continue
                exit_px = float(closes.at[rebal, t])
                gross = (exit_px - entry_px) / entry_px * 100.0
                pnl_pct = gross - (slippage_bps / 100.0)
                trades.append({
                    "ticker": t, "entry_dt": str(entry_dt.date()),
                    "exit_dt": str(rebal.date()),
                    "pnl_pct": round(pnl_pct, 4),
                })
            held.clear()

        # Compute 12-1 momentum
        rebal_pos = closes.index.get_loc(rebal)
        if rebal_pos < lookback_days:
            continue
        recent_px = closes.iloc[rebal_pos - skip_recent_days]
        old_px = closes.iloc[rebal_pos - lookback_days]
        mom = (recent_px / old_px) - 1.0
        rank_mom = mom.loc[[t for t in tickers if t in mom.index]]
        winners = rank_mom.sort_values(ascending=False).head(top_k).index.tolist()

        # Open positions
        for t in winners:
            entry_px = float(closes.at[rebal, t])
            held[t] = (rebal, entry_px)

    return trades


def evaluate_disciplined_gate(trades: list[dict]) -> dict:
    """Apply bootstrap PF CI floor + min-trades + point-PF gate."""
    if not trades:
        return {
            "n": 0, "verdict": "INSUFFICIENT_DATA", "pf": None,
            "ci_lower_95": None, "win_rate": None, "reason": "no trades",
        }
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    if not losses:
        pf = float("inf")
    else:
        pf = abs(sum(wins) / sum(losses))
    wr = len(wins) / len(pnls)

    # Bootstrap PF CI
    try:
        from helio.bootstrap_stats import bootstrap_profit_factor
        bs = bootstrap_profit_factor(pnls, n_resamples=2000, seed=42)
        ci_lower = bs.ci_lower
        ci_upper = bs.ci_upper
    except Exception as exc:
        ci_lower, ci_upper = None, None

    n = len(trades)
    verdicts = []
    if n < MIN_TRADES_FLOOR:
        verdicts.append(f"n={n} < {MIN_TRADES_FLOOR}")
    if pf < POINT_PF_MARGIN:
        verdicts.append(f"PF={pf:.2f} < {POINT_PF_MARGIN}")
    if ci_lower is not None and ci_lower < PROMOTION_PF_FLOOR:
        verdicts.append(f"CI lower={ci_lower:.2f} < {PROMOTION_PF_FLOOR}")

    # Fail-closed: if bootstrap couldn't compute CI, treat as not-verified
    if ci_lower is None:
        verdicts.append("bootstrap CI unavailable")
    if not verdicts:
        verdict = "SURVIVES"
    elif n >= MIN_TRADES_FLOOR and pf > 1.0 and ci_lower is not None and ci_lower >= 1.10:
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"

    return {
        "n": n,
        "pf": round(pf, 3) if pf != float("inf") else 999.0,
        "win_rate": round(wr, 3),
        "ci_lower_95": round(ci_lower, 3) if ci_lower is not None else None,
        "ci_upper_95": round(ci_upper, 3) if ci_upper is not None else None,
        "verdict": verdict,
        "reason": "; ".join(verdicts) if verdicts else "all gates pass",
    }


def run_sweep(years: int = 20) -> dict:
    end = datetime.now(timezone.utc).date()
    start = pd.Timestamp(end) - pd.DateOffset(years=years)
    results = []

    for uni_name, uni_spec in UNIVERSES.items():
        print(f"\n[{uni_name}] loading data...")
        closes = _load_universe_closes(uni_spec["tickers"])
        if closes.empty:
            print(f"  skipped: no data")
            continue
        # Trim to requested years
        closes = closes.loc[pd.Timestamp(start):]
        if len(closes) < LOOKBACK_DAYS + 30:
            print(f"  skipped: insufficient history ({len(closes)} bars)")
            continue
        actual_years = (closes.index[-1] - closes.index[0]).days / 365.25

        for cadence_name, cadence_freq in CADENCES.items():
            trades = run_xs_momentum_cadence(
                uni_spec["tickers"], uni_spec["top_k"], cadence_freq,
                closes=closes,
            )
            gate = evaluate_disciplined_gate(trades)
            trades_per_year = round(gate["n"] / actual_years, 1) if actual_years > 0 else 0
            row = {
                "universe": uni_name,
                "top_k": uni_spec["top_k"],
                "cadence": cadence_name,
                "years_actual": round(actual_years, 1),
                "trades_per_year": trades_per_year,
                **gate,
            }
            results.append(row)
            verdict_marker = {"SURVIVES": "PASS", "MARGINAL": "MARG", "FAIL": "fail", "INSUFFICIENT_DATA": "-"}.get(gate["verdict"], "?")
            print(f"  {cadence_name:>9s}: n={gate['n']:4d} PF={gate['pf']:5.2f} "
                  f"CI_lo={gate['ci_lower_95']} WR={gate['win_rate']} "
                  f"tpy={trades_per_year:5.1f} -> [{verdict_marker}] {gate['verdict']}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_years_requested": years,
        "slippage_bps": SLIPPAGE_BPS,
        "promotion_pf_floor": PROMOTION_PF_FLOOR,
        "min_trades_floor": MIN_TRADES_FLOOR,
        "point_pf_margin": POINT_PF_MARGIN,
        "lookback_days": LOOKBACK_DAYS,
        "skip_recent_days": SKIP_RECENT_DAYS,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    lines = []
    lines.append("# xs_momentum cadence sweep -- can we get more fills per year?")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Window: {report['window_years_requested']}y  |  Slippage: {report['slippage_bps']}bps  |  "
                 f"Bootstrap CI floor: {report['promotion_pf_floor']}  |  Min n: {report['min_trades_floor']}")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Universe | Top-K | Cadence | n | trades/yr | PF | CI lower 95% | WR | Verdict |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|---|")
    for r in report["results"]:
        ci = f"{r['ci_lower_95']:.2f}" if r['ci_lower_95'] is not None else "—"
        pf = f"{r['pf']:.2f}" if r['pf'] is not None else "—"
        wr = f"{r['win_rate']:.2f}" if r['win_rate'] is not None else "—"
        lines.append(
            f"| {r['universe']} | {r['top_k']} | {r['cadence']} | {r['n']} | "
            f"{r['trades_per_year']} | {pf} | {ci} | {wr} | **{r['verdict']}** |"
        )

    survivors = [r for r in report["results"] if r["verdict"] == "SURVIVES"]
    marginals = [r for r in report["results"] if r["verdict"] == "MARGINAL"]
    lines.append("")
    lines.append(f"## Summary: {len(survivors)} SURVIVES, {len(marginals)} MARGINAL")
    lines.append("")
    if survivors:
        lines.append("**Ship these as new variants** (passed all disciplined-gate layers):")
        for s in survivors:
            lines.append(
                f"- `{s['universe']}` @ {s['cadence']} -> "
                f"+{s['trades_per_year']} fills/year, "
                f"PF {s['pf']:.2f}, CI lower {s['ci_lower_95']:.2f}, WR {s['win_rate']:.2f}"
            )
        total_new = sum(r["trades_per_year"] for r in survivors)
        lines.append("")
        lines.append(f"**Estimated additional fills/year**: {total_new:.0f}  "
                     f"(~{total_new/52:.1f}/week)")
    if marginals:
        lines.append("")
        lines.append("**Marginal candidates** (CI lower 1.10-1.20; not quite gate-pass but close):")
        for m in marginals:
            lines.append(
                f"- `{m['universe']}` @ {m['cadence']} -> "
                f"n={m['n']}, PF {m['pf']:.2f}, CI lower {m['ci_lower_95']:.2f}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    print(f"=== xs_momentum cadence sweep ===")
    print(f"Testing {len(UNIVERSES)} universes x {len(CADENCES)} cadences = "
          f"{len(UNIVERSES) * len(CADENCES)} cells")
    print(f"Disciplined gate: {args.years}y, slip={SLIPPAGE_BPS}bps, "
          f"CI lower >= {PROMOTION_PF_FLOOR}, point PF >= {POINT_PF_MARGIN}, "
          f"min n={MIN_TRADES_FLOOR}")

    report = run_sweep(years=args.years)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / "xs_momentum_cadence_sweep.json"
    md_path = _OUT_DIR / "xs_momentum_cadence_sweep.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print()
        print(md)
        print()
        print(f"Wrote {md_path}")
        print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
