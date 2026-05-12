"""Per-strategy ROI proof engine.

Builds on ``helio.roi_proof_core`` and ``helio.spy_benchmark`` to produce
a per-strategy verdict matrix:

* annualized return on deployed capital + bootstrap CI
* SPY benchmark over strategy-active timestamps only
* Sortino ratio, Information Ratio
* Trade-removed concentration test
* Friction-adjusted expectancy
* Verdict label (SCALE_CANDIDATE / PROMISING_LOW_SAMPLE / REPAIR_EXIT /
  REPAIR_SIZING / SHADOW_ONLY / FAILS_SPY_BENCHMARK / KILL /
  INSUFFICIENT_SAMPLE)

Read-only. Writes outputs under ``ops/reports/system_audit/``.

Usage::

    python -m ops.audit.run_strategy_roi_proof          # post-reset window
    python -m ops.audit.run_strategy_roi_proof --window all  # entire history

By default this evaluates only post-reset trades (>= ``POST_RESET``); the
``--window all`` flag scopes to the entire trade history (useful for
sanity checks, NOT for promotion decisions — pre-reset evidence must
never drive capital).

Per ``project_2026_05_07_evening_audit.md`` and the 5/31 reset plan:
the engine is built now, but its outputs only become decision-grade
once accumulated against post-reset clean-data trades. Until then, the
``--window post_reset`` outputs label everything ``INSUFFICIENT_SAMPLE``
on purpose.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helio import roi_proof_core as core
from helio import spy_benchmark as bench

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
CANONICAL_FILLS = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
ALLOCATION_FACTORS = REPO / "argus_flow" / "configs" / "allocation_factors.json"

POST_RESET = datetime(2026, 4, 23, tzinfo=timezone.utc)

# Per-strategy asset class for friction model lookup.
STRATEGY_ASSET_CLASS = {
    "forge_vix_intraday":         "leveraged_etf",
    "forge_gld_pm_long":          "etf",
    "forge_jpy_pm_short":         "forex",
    "forge_nq_overnight":         "micro_future",
    "forge_nq_london_close":      "micro_future",
    "forge_aud_asian_breakout":   "forex",
    "forge_multi_orb":            "etf",
    "forge_fomc_drift":           "etf",
    "forge_tom_international":    "etf",
    "forge_wick_gbpusd":          "forex",
    "forge_vix_revert":           "etf",
    "forge_mamba":                "micro_future",
    "forge_tori":                 "micro_future",
    "forge_cuebanks":             "micro_future",
    "forge_gdx_gld":              "etf",
    "forge_spy_trend_follower":   "etf",
    "forge_spy_mean_rev":         "etf",
    "forge_nq_london":            "micro_future",
    "argus_cadjpy":               "forex",
    "argus_gbpusd":               "forex",
    "argus_usdjpy":               "forex",
}

# Verdict thresholds — locked here so they're greppable. Per the 5/9
# session synthesis: portfolio target 25-40% annualized, but per-strategy
# floor is risk-adjusted (Sortino + IR) not raw return.
PROMOTION_PORTFOLIO_ROI_TARGET = 0.30  # 30% annualized portfolio target
PROMOTION_SORTINO_FLOOR = 0.8
PROMOTION_IR_FLOOR = 0.5
PROMOTION_PF_FLOOR = 1.25
KILL_PF_CEILING = 0.95
KILL_NEGATIVE_IR_THRESHOLD = -0.5
HIGH_CONCENTRATION_THRESHOLD = 0.5  # >50% of profit from single trade = fragile


def _load_canonical_fills(window: str) -> list[dict]:
    """Read closed-trade rows from canonical_fills, filtered to the
    requested window. Returns [] if file missing."""
    if not CANONICAL_FILLS.exists():
        return []
    rows: list[dict] = []
    with CANONICAL_FILLS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("side") != "EXIT":
                continue
            ts = _parse_iso(d.get("exit_ts") or d.get("ts") or "")
            if ts is None:
                continue
            if window == "post_reset" and ts < POST_RESET:
                continue
            rows.append(d)
    return rows


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _load_killed_strategies() -> set[str]:
    """Strategies at allocation factor 0.0 are pre-killed; the ROI proof
    keeps them in the report but labels them ALREADY_KILLED so they
    don't waste verdict-engine cycles."""
    if not ALLOCATION_FACTORS.exists():
        return set()
    try:
        d = json.loads(ALLOCATION_FACTORS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    factors = d.get("factors") or {}
    return {name for name, f in factors.items() if float(f or 0) <= 0.0}


def _group_by_strategy(rows: list[dict]) -> dict[str, list[dict]]:
    """Bucket fills by strategy label. Strategies absent from
    ``STRATEGY_ASSET_CLASS`` still get evaluated — they just default to
    'stock' friction."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        name = str(r.get("strategy") or "").strip()
        if not name:
            continue
        out.setdefault(name, []).append(r)
    for name in out:
        out[name].sort(key=lambda r: str(r.get("exit_ts") or r.get("ts") or ""))
    return out


def _trade_pnls_and_notionals(rows: list[dict]) -> tuple[list[float], list[float]]:
    pnls: list[float] = []
    notionals: list[float] = []
    for r in rows:
        try:
            pnl = float(r.get("pnl_usd") or 0)
        except (TypeError, ValueError):
            continue
        size = float(r.get("size") or 0) or 0.0
        entry_px = float(r.get("entry_px") or 0) or 0.0
        notional = abs(size * entry_px) if (size and entry_px) else 0.0
        pnls.append(pnl)
        notionals.append(notional)
    return pnls, notionals


def _strategy_window(rows: list[dict]) -> tuple[datetime | None, datetime | None]:
    if not rows:
        return None, None
    starts = [_parse_iso(r.get("entry_ts") or "") for r in rows]
    ends = [_parse_iso(r.get("exit_ts") or "") for r in rows]
    starts = [s for s in starts if s is not None]
    ends = [e for e in ends if e is not None]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def _verdict(stats: dict, killed: bool) -> tuple[str, str]:
    """Apply the verdict matrix. Returns (verdict, reason)."""
    if killed:
        return ("ALREADY_KILLED", "allocation_factor 0.0 in allocation_factors.json")

    n = int(stats.get("n", 0))
    if n < core.CONTINUATION_N:
        return (
            "INSUFFICIENT_SAMPLE",
            f"n={n} below continuation gate ({core.CONTINUATION_N}); no metric is reportable",
        )

    pf = float(stats.get("profit_factor") or 0.0)
    sortino = stats.get("sortino")
    ir = stats.get("information_ratio")
    concentration = float(stats.get("concentration_score") or 0.0)
    friction_adjusted_expectancy = float(stats.get("friction_adj_expectancy") or 0.0)
    excess_return_vs_spy = stats.get("mean_excess_return_vs_spy")
    spy_coverage = float(stats.get("spy_coverage_pct") or 0.0)

    if pf <= KILL_PF_CEILING and friction_adjusted_expectancy < 0:
        return (
            "KILL",
            f"PF={pf:.2f} <= {KILL_PF_CEILING} and friction-adjusted expectancy is negative",
        )

    if isinstance(ir, (int, float)) and ir < KILL_NEGATIVE_IR_THRESHOLD and spy_coverage >= 0.5:
        return (
            "FAILS_SPY_BENCHMARK",
            f"IR={ir:.2f} (worse than SPY by significant margin, coverage={spy_coverage:.0%})",
        )

    if concentration >= HIGH_CONCENTRATION_THRESHOLD:
        return (
            "REPAIR_SIZING",
            f"single best trade is {concentration:.0%} of total PnL — fragile; review sizing/exit",
        )

    if n < core.SERIOUS_N:
        return (
            "PROMISING_LOW_SAMPLE",
            f"n={n} below serious threshold ({core.SERIOUS_N}); hold paper, do not promote",
        )

    if isinstance(sortino, (int, float)) and isinstance(ir, (int, float)):
        if sortino >= PROMOTION_SORTINO_FLOOR and ir >= PROMOTION_IR_FLOOR and pf >= PROMOTION_PF_FLOOR:
            return (
                "SCALE_CANDIDATE",
                f"PF={pf:.2f} Sortino={sortino:.2f} IR={ir:.2f}: meets all promotion gates",
            )

    if pf < 1.10:
        return (
            "REPAIR_EXIT",
            f"PF={pf:.2f} marginal — exit-rule study warranted (MFE/giveback analysis)",
        )

    return (
        "SHADOW_ONLY",
        f"PF={pf:.2f} positive but doesn't clear Sortino/IR floors — keep observing",
    )


def _summarize_strategy(
    name: str, rows: list[dict], spy_bars: list[bench.SpyBar], killed: bool
) -> dict[str, Any]:
    """Compute the full ROI-proof statistic bundle for one strategy."""
    pnls, notionals = _trade_pnls_and_notionals(rows)
    n = len(pnls)
    asset_class = STRATEGY_ASSET_CLASS.get(name, "stock")
    start, end = _strategy_window(rows)
    days = ((end - start).total_seconds() / 86400.0) if (start and end) else 0.0

    avg_notional = (sum(notionals) / n) if n else 0.0
    deployed_capital = avg_notional or 1.0  # avoid divide-by-zero, scaled later

    total_pnl = float(sum(pnls)) if pnls else 0.0
    ann_ret = core.annualized_return(pnls, deployed_capital, days) if days > 0 else 0.0
    pnl_ci = core.bootstrap_ci(
        pnls, statistic_fn=lambda xs: sum(xs), n_resamples=500, seed=42,
    )

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    pf_serializable = "inf" if pf == float("inf") else round(pf, 4)
    expectancy = (total_pnl / n) if n else 0.0
    win_rate = (len(wins) / n) if n else 0.0

    dd = core.max_drawdown(pnls)
    rod = core.return_over_drawdown(pnls)
    stress = core.trade_removed_stress(pnls)

    friction_adj = core.friction_adjusted_pnls(pnls, notionals, asset_class)
    friction_total = sum(friction_adj)
    friction_expectancy = (friction_total / n) if n else 0.0

    # Per-trade returns for risk-adjusted ratios. We compute strategy
    # return as pnl / notional and compare against SPY return over the
    # same trade window (when SPY data covers the window).
    trade_benches = []
    strat_returns: list[float] = []
    aligned_bench_returns: list[float] = []
    for r, p, nt in zip(rows, pnls, notionals):
        if nt <= 0:
            continue
        tb = bench.benchmark_trade(
            spy_bars,
            entry_ts=str(r.get("entry_ts") or ""),
            exit_ts=str(r.get("exit_ts") or ""),
            strategy_pnl=p,
            notional=nt,
        )
        trade_benches.append(tb)
        if tb.spy_data_available and tb.spy_return_frac is not None:
            strat_returns.append(tb.strategy_return_frac)
            aligned_bench_returns.append(tb.spy_return_frac)
    bench_summary = bench.aggregate_benchmark(trade_benches)

    # Sortino on per-trade strategy returns. periods_per_year is derived
    # from observed trade frequency: trades / year-fraction.
    if days > 0 and n > 0:
        ppy = n * (365.0 / days)
    else:
        ppy = 252
    sortino = core.sortino_ratio(strat_returns, target=0.0, periods_per_year=int(ppy)) \
        if strat_returns else core.InsufficientSample(actual_n=0, required_n=core.CONTINUATION_N)
    ir = core.information_ratio(strat_returns, aligned_bench_returns, periods_per_year=int(ppy)) \
        if strat_returns and aligned_bench_returns else \
        core.InsufficientSample(actual_n=len(strat_returns), required_n=core.CONTINUATION_N)
    # Low-sample companion ratio. The smoke stage of the capital ladder
    # is allowed to read this when n is in [SMOKE_N, CONTINUATION_N); any
    # stage above smoke must still gate on the strict ``information_ratio``.
    ir_low_sample = core.information_ratio(
        strat_returns,
        aligned_bench_returns,
        periods_per_year=int(ppy),
        min_n=core.SMOKE_N,
    ) if strat_returns and aligned_bench_returns else \
        core.InsufficientSample(actual_n=len(strat_returns), required_n=core.SMOKE_N)

    out: dict[str, Any] = {
        "strategy": name,
        "asset_class": asset_class,
        "n": n,
        "first_exit": start.isoformat() if start else "",
        "last_exit": end.isoformat() if end else "",
        "days_observed": round(days, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "pnl_ci_low": round(pnl_ci[0], 2),
        "pnl_ci_high": round(pnl_ci[1], 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": pf_serializable,
        "expectancy_per_trade_usd": round(expectancy, 4),
        "max_drawdown_usd": round(dd, 2),
        "return_over_drawdown": round(rod, 4),
        "annualized_return_on_avg_notional": round(ann_ret, 4),
        "avg_notional_usd": round(avg_notional, 2),
        "concentration_score": stress.get("concentration_score", 0.0),
        "net_remove_best_1": round(float(stress.get("net_remove_best_1", 0.0)), 2),
        "net_remove_best_3": round(float(stress.get("net_remove_best_3", 0.0)), 2),
        "friction_adj_total_pnl": round(friction_total, 2),
        "friction_adj_expectancy": round(friction_expectancy, 4),
        "sortino": _serialize_metric(sortino),
        "information_ratio": _serialize_metric(ir),
        "information_ratio_low_sample": _serialize_metric(ir_low_sample),
        "spy_n_with_data": bench_summary["n_with_spy_data"],
        "spy_coverage_pct": bench_summary["coverage_pct"],
        "mean_strategy_return": bench_summary["mean_strategy_return"],
        "mean_spy_return": bench_summary["mean_spy_return"],
        "mean_excess_return_vs_spy": bench_summary["mean_excess_return"],
        "wins_vs_spy": bench_summary["wins_vs_spy"],
        "losses_vs_spy": bench_summary["losses_vs_spy"],
    }
    verdict, reason = _verdict(out, killed=killed)
    out["verdict"] = verdict
    out["verdict_reason"] = reason
    return out


def _serialize_metric(val: Any) -> Any:
    """Convert ``InsufficientSample`` to a string sentinel for CSV/JSON
    serialization; numbers pass through rounded."""
    if isinstance(val, core.InsufficientSample):
        return f"INSUFFICIENT_SAMPLE(n={val.actual_n}/required={val.required_n})"
    if isinstance(val, (int, float)):
        return round(float(val), 4)
    return val


def _portfolio_summary(per_strategy: list[dict]) -> dict[str, Any]:
    """Roll per-strategy verdicts into a portfolio-level read."""
    candidates = [s for s in per_strategy if s["verdict"] == "SCALE_CANDIDATE"]
    promising = [s for s in per_strategy if s["verdict"] == "PROMISING_LOW_SAMPLE"]
    repair = [s for s in per_strategy if s["verdict"] in ("REPAIR_EXIT", "REPAIR_SIZING")]
    shadow = [s for s in per_strategy if s["verdict"] == "SHADOW_ONLY"]
    kill = [s for s in per_strategy if s["verdict"] == "KILL"]
    fails_spy = [s for s in per_strategy if s["verdict"] == "FAILS_SPY_BENCHMARK"]
    insufficient = [s for s in per_strategy if s["verdict"] == "INSUFFICIENT_SAMPLE"]
    already_killed = [s for s in per_strategy if s["verdict"] == "ALREADY_KILLED"]

    return {
        "total_strategies": len(per_strategy),
        "scale_candidates": [s["strategy"] for s in candidates],
        "promising_low_sample": [s["strategy"] for s in promising],
        "repair": [s["strategy"] for s in repair],
        "shadow_only": [s["strategy"] for s in shadow],
        "fails_spy_benchmark": [s["strategy"] for s in fails_spy],
        "kill_recommended": [s["strategy"] for s in kill],
        "insufficient_sample": [s["strategy"] for s in insufficient],
        "already_killed": [s["strategy"] for s in already_killed],
        "portfolio_target_annualized_roi": PROMOTION_PORTFOLIO_ROI_TARGET,
        "promotion_floors": {
            "sortino": PROMOTION_SORTINO_FLOOR,
            "information_ratio": PROMOTION_IR_FLOOR,
            "profit_factor": PROMOTION_PF_FLOOR,
        },
    }


def _roi_gap_row(stat: dict) -> dict:
    """Compute the ROI gap to the 25% and 40% annualized targets, and the
    expectancy-per-trade lift required to close those gaps. Surfaces
    whether a 25-40% ROI sleeve is *plausible* for this strategy or
    requires implausible per-trade expectancy growth."""
    n = int(stat.get("n", 0))
    days = float(stat.get("days_observed") or 0)
    avg_notional = float(stat.get("avg_notional_usd") or 0)
    expectancy = float(stat.get("expectancy_per_trade_usd") or 0)
    current_ann = float(stat.get("annualized_return_on_avg_notional") or 0)

    if days <= 0 or n <= 0 or avg_notional <= 0:
        return {
            "strategy": stat["strategy"],
            "n": n,
            "current_ann_roi": current_ann,
            "trades_per_year_estimate": 0,
            "gap_to_25pct": None,
            "gap_to_40pct": None,
            "expectancy_required_for_25pct_usd": None,
            "expectancy_required_for_40pct_usd": None,
            "current_expectancy_usd": expectancy,
            "feasibility_25pct": "UNCOMPUTABLE",
            "feasibility_40pct": "UNCOMPUTABLE",
            "verdict": stat.get("verdict", ""),
        }

    trades_per_year = n * (365.0 / days)
    target_pnl_25 = 0.25 * avg_notional
    target_pnl_40 = 0.40 * avg_notional
    expectancy_required_25 = (target_pnl_25 / trades_per_year) if trades_per_year > 0 else 0
    expectancy_required_40 = (target_pnl_40 / trades_per_year) if trades_per_year > 0 else 0

    gap_25 = expectancy_required_25 - expectancy
    gap_40 = expectancy_required_40 - expectancy

    def _feasibility(gap: float) -> str:
        # Heuristic: lift needed > 50% of current expectancy → IMPLAUSIBLE
        if expectancy <= 0:
            return "BLOCKED_NEGATIVE_BASE"
        ratio = gap / expectancy
        if ratio <= 0:
            return "ALREADY_AT_OR_ABOVE"
        if ratio <= 0.20:
            return "PLAUSIBLE_WITH_TUNE"
        if ratio <= 0.50:
            return "REACHABLE_WITH_REPAIR"
        if ratio <= 1.00:
            return "AGGRESSIVE_TARGET"
        return "IMPLAUSIBLE_WITHOUT_THESIS_CHANGE"

    return {
        "strategy": stat["strategy"],
        "n": n,
        "current_ann_roi": round(current_ann, 4),
        "trades_per_year_estimate": round(trades_per_year, 1),
        "gap_to_25pct": round(0.25 - current_ann, 4),
        "gap_to_40pct": round(0.40 - current_ann, 4),
        "expectancy_required_for_25pct_usd": round(expectancy_required_25, 2),
        "expectancy_required_for_40pct_usd": round(expectancy_required_40, 2),
        "current_expectancy_usd": round(expectancy, 2),
        "feasibility_25pct": _feasibility(gap_25),
        "feasibility_40pct": _feasibility(gap_40),
        "verdict": stat.get("verdict", ""),
    }


def _write_outputs(per_strategy: list[dict], window: str, summary: dict, ts: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not per_strategy:
        (OUT_DIR / "strategy_roi_proof.csv").write_text("strategy,verdict\n", encoding="utf-8")
        (OUT_DIR / "roi_proof_report.md").write_text(
            f"# ROI Proof Report ({ts})\n\nNo strategies had post-reset trades to evaluate.\n",
            encoding="utf-8",
        )
        return

    fields = list(per_strategy[0].keys())
    with (OUT_DIR / "strategy_roi_proof.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in per_strategy:
            w.writerow(row)

    bench_fields = [
        "strategy", "n", "spy_n_with_data", "spy_coverage_pct",
        "mean_strategy_return", "mean_spy_return", "mean_excess_return_vs_spy",
        "wins_vs_spy", "losses_vs_spy", "verdict",
    ]
    with (OUT_DIR / "strategy_vs_spy_benchmark.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=bench_fields, extrasaction="ignore")
        w.writeheader()
        for row in per_strategy:
            w.writerow(row)

    matrix_fields = [
        "strategy", "verdict", "verdict_reason", "n",
        "profit_factor", "sortino", "information_ratio",
        "annualized_return_on_avg_notional", "concentration_score",
        "friction_adj_expectancy",
    ]
    with (OUT_DIR / "strategy_kill_or_scale_matrix.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=matrix_fields, extrasaction="ignore")
        w.writeheader()
        for row in per_strategy:
            w.writerow(row)

    # ROI gap to 25%/40% targets — surfaces feasibility per strategy.
    gap_rows = [_roi_gap_row(row) for row in per_strategy]
    gap_fields = list(gap_rows[0].keys()) if gap_rows else ["strategy"]
    with (OUT_DIR / "strategy_roi_gap_to_25_40.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=gap_fields)
        w.writeheader()
        for row in gap_rows:
            w.writerow(row)

    md_lines = [
        f"# ROI Proof Report ({ts})",
        "",
        f"Window: **{window}** (post_reset cutoff: {POST_RESET.isoformat()})",
        f"Total strategies evaluated: **{summary['total_strategies']}**",
        "",
        "## Portfolio target",
        f"- Annualized ROI target: **{summary['portfolio_target_annualized_roi']:.0%}**",
        f"- Per-strategy floors: Sortino ≥ {summary['promotion_floors']['sortino']}, "
        f"IR ≥ {summary['promotion_floors']['information_ratio']}, "
        f"PF ≥ {summary['promotion_floors']['profit_factor']}",
        "",
        "## Verdict tally",
        f"- **SCALE_CANDIDATE**: {len(summary['scale_candidates'])} {summary['scale_candidates']}",
        f"- **PROMISING_LOW_SAMPLE**: {len(summary['promising_low_sample'])} {summary['promising_low_sample']}",
        f"- **SHADOW_ONLY**: {len(summary['shadow_only'])} {summary['shadow_only']}",
        f"- **REPAIR**: {len(summary['repair'])} {summary['repair']}",
        f"- **FAILS_SPY_BENCHMARK**: {len(summary['fails_spy_benchmark'])} {summary['fails_spy_benchmark']}",
        f"- **KILL**: {len(summary['kill_recommended'])} {summary['kill_recommended']}",
        f"- **INSUFFICIENT_SAMPLE**: {len(summary['insufficient_sample'])} {summary['insufficient_sample']}",
        f"- **ALREADY_KILLED**: {len(summary['already_killed'])} {summary['already_killed']}",
        "",
        "## Per-strategy summary",
        "",
        "| strategy | n | PF | Sortino | IR | ann_ROI | excess_vs_SPY | verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in sorted(per_strategy, key=lambda r: r["strategy"]):
        md_lines.append(
            f"| {row['strategy']} | {row['n']} | {row['profit_factor']} | "
            f"{row['sortino']} | {row['information_ratio']} | "
            f"{row['annualized_return_on_avg_notional']:.2%} | "
            f"{row.get('mean_excess_return_vs_spy', 0):+.4f} | {row['verdict']} |"
        )
    md_lines.append("")
    md_lines.append("## Note on sample-size discipline")
    md_lines.append(
        "Sortino and IR refuse to compute below n=30. PF is reported but should "
        "not drive decisions until n>=75 (per project_decisive_test_protocol.md). "
        "Real interpretation of these results requires post-5/31-reset clean "
        "data accumulating across the surviving fleet."
    )

    (OUT_DIR / "roi_proof_report.md").write_text("\n".join(md_lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", choices=("post_reset", "all"), default="post_reset")
    args = parser.parse_args(argv)

    rows = _load_canonical_fills(args.window)
    grouped = _group_by_strategy(rows)
    spy_bars = bench.load_spy_bars()
    killed = _load_killed_strategies()

    per_strategy: list[dict] = []
    for name, srows in grouped.items():
        per_strategy.append(_summarize_strategy(
            name, srows, spy_bars, killed=(name in killed),
        ))
    per_strategy.sort(key=lambda r: r["strategy"])
    summary = _portfolio_summary(per_strategy)

    ts = datetime.now(timezone.utc).isoformat()
    _write_outputs(per_strategy, args.window, summary, ts)

    print(json.dumps({
        "window": args.window,
        "total_strategies": len(per_strategy),
        "spy_bars_loaded": len(spy_bars),
        "summary": summary,
        "ts": ts,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
