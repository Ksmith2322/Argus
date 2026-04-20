"""fleet_state.json aggregator — Phase 1 additive read model.

Per RESTRUCTURE_GUIDE_20260419.md §6, "truth scatter" is one of the top four
problems: the dashboard reads 10+ JSON files to answer "is strategy X live
and healthy". This module consolidates the answer into one file.

Phase 1 rules (kept minimal on purpose):
  - This is a READ MODEL. Underlying reports (fleet_status.json,
    kill_watchdog_report.json, reconciliation_report.json, promotion_readiness)
    remain the authoritative sources. fleet_state.json is regenerated from
    them on demand.
  - Nothing in the system depends on fleet_state.json existing or being
    fresh. If something goes wrong here, the pre-existing endpoints still
    work.
  - The shape is intentionally simple — one nested dict per strategy, plus a
    fleet-wide block. Phase 2 will expand it only if consumers need more.

Usage:
    python -m helio.fleet_state           # rebuild + print summary
    python -m helio.fleet_state --json    # rebuild + dump the full state

Wired into:
    ops/run_cohort_report.ps1 (after individual report steps)
    /api/fleet_state endpoint (Phase 2 — not added yet)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

LOGS = _REPO / "argus_flow" / "logs"
OUT_PATH = LOGS / "fleet_state.json"
HISTORY_PATH = LOGS / "fleet_state_history.jsonl"

_FLEET_STATUS = LOGS / "fleet_status.json"
_KILL_WATCHDOG = LOGS / "kill_watchdog_report.json"
_RECON = LOGS / "reconciliation_report.json"
_RISK_OVERSIGHT = LOGS / "risk_oversight_report.json"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _index_by_strategy(items: list, key: str = "strategy") -> dict[str, dict]:
    """Turn [{strategy: X, ...}, ...] into {X: {...}}."""
    return {item.get(key): item for item in items if isinstance(item, dict) and item.get(key)}


def _safe_get(d: dict | None, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _canonical_fills_by_strategy() -> dict[str, list]:
    """Load every canonical fill as a helio.domain.Fill, bucketed by strategy.
    Fourth production consumer of Fill (after reconciliation, morning_brief,
    /api/canonical_fills). Fully typed access means a shape drift in
    canonical_fills.jsonl fails fast here instead of feeding nonsense through
    the dashboard."""
    try:
        from helio.canonical_fills import read_fills
        from helio.domain import Fill
    except ImportError:
        return {}
    raw = read_fills()
    by_strat: dict[str, list] = {}
    for r in raw:
        f = Fill.from_canonical_row(r)
        if f.strategy:
            by_strat.setdefault(f.strategy, []).append(f)
    return by_strat


# Evidence bars from project_strategy_gameplan_20260419: 10 = sanity,
# 30 = review, 60 = promotion. Below 10, the bootstrap distribution is
# degenerate enough to give misleadingly confident numbers, so
# p_expectancy_positive is withheld.
_EVIDENCE_SANITY = 10
_EVIDENCE_REVIEW = 30
_EVIDENCE_PROMOTION = 60
_BOOTSTRAP_RESAMPLES = 1000
_BOOTSTRAP_SEED = 42  # deterministic — tests pin exact output


def _evidence_bar(n: int) -> str:
    if n < _EVIDENCE_SANITY:
        return "insufficient"
    if n < _EVIDENCE_REVIEW:
        return "sanity"
    if n < _EVIDENCE_PROMOTION:
        return "review"
    return "promotion"


def _bootstrap_p_positive(pnls: list[float]) -> float | None:
    """P(resampled mean > 0) via 1000-sample bootstrap. None under _EVIDENCE_SANITY.

    Deterministic: seeded RNG so the same fills always produce the same
    number. This is a "90% confidence in positive expectancy" estimator, not
    a significance test — don't read it as a p-value.
    """
    n = len(pnls)
    if n < _EVIDENCE_SANITY:
        return None
    rng = random.Random(_BOOTSTRAP_SEED)
    positive = 0
    for _ in range(_BOOTSTRAP_RESAMPLES):
        total = 0.0
        for _ in range(n):
            total += pnls[rng.randrange(n)]
        if total > 0:
            positive += 1
    return positive / _BOOTSTRAP_RESAMPLES


_MC_SHUFFLES = 5000


def _monte_carlo_shuffle(pnls: list[float]) -> dict | None:
    """Monte Carlo trade-order shuffle — discovery test #8 from
    project_discovery_tests_20260419.md. Shuffles the sequence of actual PnL
    outcomes 5000 times and measures:

      - max_drawdown_usd: worst peak-to-trough equity excursion across all
        shuffles (USD). Higher = the strategy tolerates bad sequencing less.
      - ruin_fraction: fraction of shuffles where cumulative PnL ever dips
        below -abs(total_pnl) (a rough "would I have quit?" threshold).
      - top_k_concentration: how much of total PnL comes from the best K
        trades. If 1-3 trades carry 80%+ of PnL, the strategy is outlier-
        dependent and a real losing streak at the start would wipe the edge.
      - pct_shuffles_profitable: fraction of shuffles where final cumulative
        PnL > 0. Comparable to _bootstrap_p_positive but via path rather
        than resample.

    Returns None under _EVIDENCE_SANITY (same rule as bootstrap).
    Deterministic via _BOOTSTRAP_SEED for test stability.
    """
    n = len(pnls)
    if n < _EVIDENCE_SANITY:
        return None

    total_pnl = sum(pnls)
    # Outlier concentration — top 1, top 3, top 5
    sorted_descending = sorted(pnls, reverse=True)
    if total_pnl != 0:
        top1_pct = sorted_descending[0] / total_pnl if len(sorted_descending) >= 1 else 0.0
        top3_pct = sum(sorted_descending[:3]) / total_pnl if len(sorted_descending) >= 3 else 0.0
        top5_pct = sum(sorted_descending[:5]) / total_pnl if len(sorted_descending) >= 5 else 0.0
    else:
        top1_pct = top3_pct = top5_pct = 0.0

    # Ex-top-1 sanity: if we drop the single best trade, does the strategy
    # still have positive expectancy? A single-trade-dependent strategy is
    # fragile even if the headline number looks good.
    if len(pnls) > 1:
        ex_top1 = sum(pnls) - sorted_descending[0]
    else:
        ex_top1 = 0.0

    rng = random.Random(_BOOTSTRAP_SEED)
    max_dd = 0.0
    profitable_shuffles = 0
    ruin_threshold = -abs(total_pnl) if total_pnl != 0 else -1.0
    ruin_shuffles = 0

    work = list(pnls)
    for _ in range(_MC_SHUFFLES):
        rng.shuffle(work)
        cum = 0.0
        peak = 0.0
        hit_ruin = False
        for p in work:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
            if cum < ruin_threshold:
                hit_ruin = True
        if cum > 0:
            profitable_shuffles += 1
        if hit_ruin:
            ruin_shuffles += 1

    return {
        "shuffles": _MC_SHUFFLES,
        "max_drawdown_usd": round(max_dd, 2),
        "pct_shuffles_profitable": round(profitable_shuffles / _MC_SHUFFLES, 3),
        "ruin_fraction": round(ruin_shuffles / _MC_SHUFFLES, 3),
        "top1_pct_of_total_pnl": round(top1_pct, 3),
        "top3_pct_of_total_pnl": round(top3_pct, 3),
        "top5_pct_of_total_pnl": round(top5_pct, 3),
        "ex_top1_total_pnl_usd": round(ex_top1, 2),
    }


def _walk_forward_stability(pnls: list[float], n_folds: int = 4) -> dict | None:
    """Split the trade sequence into N sequential, equal-sized folds and
    measure PF per fold. Answers: does the edge hold across time, or is
    it concentrated in one sub-window?

    Returns None under the sanity bar or when n_folds > n/2 (folds too
    small to be meaningful).
    """
    n = len(pnls)
    if n < _EVIDENCE_SANITY or n < n_folds * 5:
        return None

    fold_size = n // n_folds
    fold_results = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else n
        fold = pnls[start:end]
        gross_w = sum(p for p in fold if p > 0)
        gross_l = abs(sum(p for p in fold if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        total = sum(fold)
        fold_results.append({
            "fold_index": i,
            "n_trades": len(fold),
            "total_pnl_usd": round(total, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
        })

    positive_folds = sum(1 for f in fold_results if f["total_pnl_usd"] > 0)
    # Stability score: fraction of folds with positive total PnL
    stability = positive_folds / n_folds

    return {
        "n_folds": n_folds,
        "fold_size": fold_size,
        "fold_details": fold_results,
        "positive_folds": positive_folds,
        "stability_score": round(stability, 3),
        "all_folds_positive": positive_folds == n_folds,
    }


def _cost_stress(pnls: list[float], cost_per_trade_usd: float = 5.0) -> dict | None:
    """Stress-test strategy expectancy at 1x, 2x, 3x typical costs (commission +
    slippage). If PF survives 2x, the strategy has cost headroom. If it dies
    at 2x, it's fragile to fill-quality degradation in live markets.

    The default $5/trade is a conservative futures day-trade estimate
    (commission + 1 tick slippage per side on micro contracts).
    """
    n = len(pnls)
    if n < _EVIDENCE_SANITY:
        return None

    def _pf_at(multiplier: float) -> tuple[float | None, float]:
        per_trade_cost = cost_per_trade_usd * multiplier
        adjusted = [p - per_trade_cost for p in pnls]
        gross_w = sum(p for p in adjusted if p > 0)
        gross_l = abs(sum(p for p in adjusted if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        return (round(pf, 2) if pf != float("inf") else None, round(sum(adjusted), 2))

    pf_1x, total_1x = _pf_at(1.0)
    pf_2x, total_2x = _pf_at(2.0)
    pf_3x, total_3x = _pf_at(3.0)

    return {
        "cost_per_trade_usd": cost_per_trade_usd,
        "pf_1x": pf_1x,
        "pf_2x": pf_2x,
        "pf_3x": pf_3x,
        "total_pnl_1x": total_1x,
        "total_pnl_2x": total_2x,
        "total_pnl_3x": total_3x,
        "survives_2x": pf_2x is not None and pf_2x > 1.0,
        "survives_3x": pf_3x is not None and pf_3x > 1.0,
    }


def _top_n_sensitivity(pnls: list[float], max_n: int = 5) -> dict | None:
    """Iteratively remove the top-N winning trades and measure surviving PF.
    A strategy whose edge disappears after removing the top 1-3 trades is
    outlier-dependent and fragile. A strategy whose edge degrades gradually
    has broad profitability.

    Mirrors the spirit of MC stress's top_k_concentration but gives PF at
    each step so you can see the fragility curve.
    """
    n = len(pnls)
    if n < _EVIDENCE_SANITY or max_n >= n - 5:
        return None

    sorted_desc = sorted(pnls, reverse=True)
    sensitivity = []
    for k in range(0, max_n + 1):
        if k >= n:
            break
        remaining = sorted_desc[k:]
        gross_w = sum(p for p in remaining if p > 0)
        gross_l = abs(sum(p for p in remaining if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        sensitivity.append({
            "top_removed": k,
            "n_trades": len(remaining),
            "total_pnl_usd": round(sum(remaining), 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
        })

    # Edge-robustness flags
    pf_full = sensitivity[0]["profit_factor"]
    pf_minus_3 = sensitivity[min(3, len(sensitivity) - 1)]["profit_factor"]
    still_positive_minus_3 = (pf_minus_3 is not None and pf_minus_3 > 1.0)

    return {
        "sensitivity": sensitivity,
        "pf_full": pf_full,
        "pf_minus_top3": pf_minus_3,
        "still_positive_after_top3_removed": still_positive_minus_3,
    }


def _per_group_profitability(
    rows: list[dict],
    group_key: str,
    pnl_key: str = "pnl_usd",
) -> dict | None:
    """Bucket trades by a categorical group (e.g., instrument, day-of-week,
    entry-hour) and measure PF + WR per bucket. Surfaces "this strategy works
    on X but not Y" — the key question behind per-symbol promotion gates."""
    if not rows:
        return None

    from collections import defaultdict
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        key = str(r.get(group_key, "_unknown"))
        try:
            pnl = float(r.get(pnl_key, 0))
        except (TypeError, ValueError):
            continue
        groups[key].append(pnl)

    bucket_stats = []
    positive_buckets = 0
    for k, pnls in sorted(groups.items()):
        wins = sum(1 for p in pnls if p > 0)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        total = sum(pnls)
        if total > 0:
            positive_buckets += 1
        bucket_stats.append({
            "group": k,
            "n_trades": len(pnls),
            "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
            "total_pnl_usd": round(total, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
        })

    return {
        "group_key": group_key,
        "n_groups": len(bucket_stats),
        "positive_groups": positive_buckets,
        "all_positive": positive_buckets == len(bucket_stats),
        "buckets": bucket_stats,
    }


_CORRELATION_MIN_OVERLAP = 5


def _portfolio_correlation(
    strategy_trades: dict[str, list[dict]],
    date_key: str = "date",
    pnl_key: str = "pnl_usd",
) -> dict | None:
    """Pairwise Pearson correlation of daily PnL across strategies.

    Input:
        {strategy_name: [{date: "YYYY-MM-DD" or ISO, pnl_usd: float}, ...]}

    For each pair, only days where BOTH strategies have a trade are included
    in the correlation — a zero-fill would artificially pull correlation to
    zero and hide real diversification failure. Min overlap of
    `_CORRELATION_MIN_OVERLAP` is required; below that, correlation is
    reported as None (insufficient sample).

    Returns:
        {
          "strategies": [...], "n_strategies": N,
          "matrix": {s1: {s2: corr_or_None}},
          "overlap": {s1: {s2: n_common_days}},
          "warnings": [str]  # flags high-corr pairs (|r| >= 0.7 with >=10 overlap)
        }
    """
    if not strategy_trades:
        return None

    daily: dict[str, dict[str, float]] = {}
    for name, rows in strategy_trades.items():
        bucket: dict[str, float] = {}
        for r in rows:
            d = r.get(date_key)
            if not d:
                continue
            d = str(d)[:10]
            try:
                p = float(r.get(pnl_key, 0) or 0)
            except (TypeError, ValueError):
                continue
            bucket[d] = bucket.get(d, 0.0) + p
        daily[name] = bucket

    names = sorted(daily.keys())
    matrix: dict[str, dict[str, float | None]] = {n: {} for n in names}
    overlap: dict[str, dict[str, int]] = {n: {} for n in names}
    warnings: list[str] = []

    for i, a in enumerate(names):
        for b in names[i:]:
            if a == b:
                matrix[a][b] = 1.0
                overlap[a][b] = len(daily[a])
                continue
            common = sorted(set(daily[a].keys()) & set(daily[b].keys()))
            n = len(common)
            overlap[a][b] = n
            overlap[b][a] = n
            if n < _CORRELATION_MIN_OVERLAP:
                matrix[a][b] = None
                matrix[b][a] = None
                continue
            xs = [daily[a][d] for d in common]
            ys = [daily[b][d] for d in common]
            mx = sum(xs) / n
            my = sum(ys) / n
            num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
            dx = sum((v - mx) ** 2 for v in xs) ** 0.5
            dy = sum((v - my) ** 2 for v in ys) ** 0.5
            if dx == 0 or dy == 0:
                matrix[a][b] = None
                matrix[b][a] = None
                continue
            r = num / (dx * dy)
            r = round(r, 4)
            matrix[a][b] = r
            matrix[b][a] = r
            if n >= 10 and abs(r) >= 0.7:
                sign = "+" if r > 0 else "-"
                warnings.append(
                    f"{a} <-> {b}: r={r} ({sign}) over {n} shared days "
                    f"— diversification weak"
                )

    return {
        "strategies": names,
        "n_strategies": len(names),
        "matrix": matrix,
        "overlap": overlap,
        "min_overlap": _CORRELATION_MIN_OVERLAP,
        "warnings": warnings,
    }


def _confidence_summary(fills: list) -> dict:
    """Per-strategy confidence block: sample sizing + expectancy + bootstrapped
    P(expectancy>0). Target from project_testing_framework_20260419 is
    P(exp>0) >= 0.90, not win rate."""
    pnls = [f.pnl_usd for f in fills if f.pnl_usd is not None]
    n_total = len(pnls)
    n_live = sum(
        1 for f in fills
        if f.pnl_usd is not None and getattr(f, "source", None) != "backfill_from_trade_csv"
    )
    n_paper = n_total - n_live

    expectancy_usd = (sum(pnls) / n_total) if n_total else None
    r_vals = [
        f.pnl_usd / f.risk_usd
        for f in fills
        if f.pnl_usd is not None and f.risk_usd not in (None, 0)
    ]
    expectancy_r = (sum(r_vals) / len(r_vals)) if r_vals else None

    p_pos = _bootstrap_p_positive(pnls)
    bar = _evidence_bar(n_total)

    warning = None
    if n_total == 0:
        warning = "no trades recorded"
    elif n_total < _EVIDENCE_SANITY:
        warning = f"only {n_total} trades — bootstrap confidence withheld"
    elif n_live < 5 and n_paper > 0:
        warning = f"{n_live} live + {n_paper} backfill — heavily weighted by historical"

    return {
        "n_total": n_total,
        "n_live": n_live,
        "n_paper": n_paper,
        "expectancy_usd": round(expectancy_usd, 4) if expectancy_usd is not None else None,
        "expectancy_r": round(expectancy_r, 4) if expectancy_r is not None else None,
        "p_expectancy_positive": round(p_pos, 3) if p_pos is not None else None,
        "evidence_bar": bar,
        "sample_warning": warning,
    }


def _fills_summary(fills: list) -> dict:
    """Compact stats for the fleet_state per-strategy block."""
    n = len(fills)
    if n == 0:
        return {"count": 0, "pnl_usd_total": 0.0, "wins": 0, "losses": 0,
                "last_fill_ts": None, "backfill_count": 0, "live_count": 0}
    pnls = [f.pnl_usd for f in fills if f.pnl_usd is not None]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    # Most-recent ts wins (read_fills returns newest-first per file)
    last_ts = next((f.ts for f in fills if f.ts), None)
    backfill_count = sum(1 for f in fills if f.source == "backfill_from_trade_csv")
    return {
        "count": n,
        "pnl_usd_total": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "last_fill_ts": last_ts,
        "backfill_count": backfill_count,
        "live_count": n - backfill_count,
    }


def build_fleet_state() -> dict:
    """Aggregate all per-strategy and fleet-wide signals into one dict.

    Safe to call with any combination of underlying files missing — each
    missing source degrades to None in its slot, not an exception.
    """
    fleet_status = _load_json(_FLEET_STATUS) or {}
    kill_report = _load_json(_KILL_WATCHDOG) or {}
    recon_report = _load_json(_RECON) or {}
    risk_oversight = _load_json(_RISK_OVERSIGHT) or {}
    fills_by_strat = _canonical_fills_by_strategy()

    # Promotion readiness — compute on demand so we always have current stats.
    try:
        from helio.promotion_readiness import build_report as _build_prom
        prom_report = _build_prom()
    except Exception as e:
        prom_report = {"error": f"promotion_readiness failed: {e}", "strategies": []}

    kill_by_strat = _index_by_strategy(kill_report.get("strategies", []))
    recon_by_strat = _index_by_strategy(recon_report.get("strategies", []))
    prom_by_strat = _index_by_strategy(prom_report.get("strategies", []),
                                        key="strategy")

    systems = fleet_status.get("systems", {}) or {}

    # Build the per-strategy dict. Keys = union across all sources so nothing
    # gets silently dropped if one report has a label another doesn't.
    all_labels: set[str] = set()
    all_labels.update(kill_by_strat.keys())
    all_labels.update(recon_by_strat.keys())
    all_labels.update(prom_by_strat.keys())
    all_labels.update(fills_by_strat.keys())
    # fleet_status groups by family (argus/forge/...); include family-level keys
    # as pass-through so the dashboard can still show them until Phase 2 aligns
    # the naming.

    strategies: dict[str, dict] = {}
    for label in sorted(all_labels):
        kill = kill_by_strat.get(label, {})
        recon = recon_by_strat.get(label, {})
        prom = prom_by_strat.get(label, {})

        strategies[label] = {
            "performance": {
                "trades": _safe_get(kill, "stats", "trades"),
                "wins": _safe_get(kill, "stats", "wins"),
                "losses": _safe_get(kill, "stats", "losses"),
                "profit_factor": _safe_get(kill, "stats", "profit_factor"),
                "win_rate": _safe_get(kill, "stats", "win_rate"),
                "pnl_usd": _safe_get(kill, "stats", "pnl_usd"),
                "peak_pnl_usd": _safe_get(kill, "stats", "peak_pnl_usd"),
                "current_drawdown_usd": _safe_get(kill, "stats", "current_drawdown_usd"),
            },
            "gates": {
                "review_gate_passed": _safe_get(prom, "review_gate_passed"),
                "canonical_gate_passed": _safe_get(prom, "canonical_gate_passed"),
                "next_action": _safe_get(prom, "next_action"),
                "blockers": _safe_get(prom, "blockers", default=[]),
            },
            "watchdog": {
                "status": kill.get("status"),
                "triggered_rules": kill.get("triggered_rules") or [],
            },
            "reconciliation": {
                "status": recon.get("status"),
                "csv_row_count": recon.get("csv_row_count"),
                "canonical_row_count": recon.get("canonical_row_count"),
                "pnl_drift_usd": recon.get("pnl_drift_usd"),
                "drift_flags": recon.get("drift_flags") or [],
            },
            "canonical_fills": _fills_summary(fills_by_strat.get(label, [])),
            "confidence": _confidence_summary(fills_by_strat.get(label, [])),
            "is_healthy": _compute_is_healthy(kill, recon, prom),
        }

    fleet_block = {
        "broker_equity_usd": _safe_get(risk_oversight, "broker_truth", "account_equity_usd"),
        "broker_connected": _derive_broker_connected(risk_oversight, fleet_status),
        "broker_runners": _derive_broker_runners(fleet_status),
        "fleet_drawdown_pips": _safe_get(risk_oversight, "fleet_drawdown_pips"),
        "risk_level": _safe_get(risk_oversight, "risk_level"),
        "correlation_exposure": _safe_get(risk_oversight, "correlation_exposure"),
        "pause_entries": _safe_get(fleet_status, "control_files", "pause_entries", default=[]),
        "kill_summary": kill_report.get("summary") or {},
        "reconciliation_summary": recon_report.get("summary") or {},
        "promotion_summary": prom_report.get("summary") or {},
        "confidence_rollup": _confidence_rollup(strategies),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_of_truth_status": "READ_MODEL_ONLY",
        "_note": "Read model over fleet_status/kill_watchdog/reconciliation/promotion_readiness. Individual reports remain authoritative.",
        "sources": {
            "fleet_status":        _FLEET_STATUS.name if _FLEET_STATUS.exists() else None,
            "kill_watchdog":       _KILL_WATCHDOG.name if _KILL_WATCHDOG.exists() else None,
            "reconciliation":      _RECON.name if _RECON.exists() else None,
            "risk_oversight":      _RISK_OVERSIGHT.name if _RISK_OVERSIGHT.exists() else None,
            "promotion_readiness": "computed_in_memory",
        },
        "strategies": strategies,
        "fleet": fleet_block,
    }


def _derive_broker_runners(fleet_status: dict) -> dict[str, bool]:
    """Flatten per-runner broker_connected flags out of fleet_status.

    fleet_status shape: {systems: {argus: {runtime: {usdjpy: {broker_connected: true}, ...}}}}
    Returns: {"argus.usdjpy": True, "argus.gbpusd": True, ...}
    """
    out: dict[str, bool] = {}
    systems = (fleet_status or {}).get("systems") or {}
    if not isinstance(systems, dict):
        return out
    for sys_name, sys_blk in systems.items():
        if not isinstance(sys_blk, dict):
            continue
        runtime = sys_blk.get("runtime") or {}
        if not isinstance(runtime, dict):
            continue
        for runner_name, runner_blk in runtime.items():
            if isinstance(runner_blk, dict) and "broker_connected" in runner_blk:
                out[f"{sys_name}.{runner_name}"] = bool(runner_blk["broker_connected"])
    return out


def _derive_broker_connected(risk_oversight: dict, fleet_status: dict) -> bool | None:
    """Fleet-level broker_connected: prefer an explicit risk_oversight
    broker_truth.connected (future schema), else roll up per-runner
    broker_connected from fleet_status. True iff at least one runner
    reports a connection; False iff any runner is known and all are False;
    None iff no runner information is available."""
    explicit = _safe_get(risk_oversight, "broker_truth", "connected")
    if explicit is not None:
        return bool(explicit)
    runners = _derive_broker_runners(fleet_status)
    if not runners:
        return None
    return any(runners.values())


def _confidence_rollup(strategies: dict) -> dict:
    """Fleet-level confidence scoreboard aggregate."""
    rollup = {
        "total_strategies": len(strategies),
        "strategies_with_positive_expectancy": 0,
        "strategies_at_sanity_bar": 0,
        "strategies_at_review_bar": 0,
        "strategies_at_promotion_bar": 0,
        "strategies_p_positive_gte_50pct": 0,
        "strategies_p_positive_gte_90pct": 0,
        "leaders_by_p_positive": [],  # top strategies by P(exp>0), only if above sanity bar
    }
    leaders = []
    for label, block in strategies.items():
        c = block.get("confidence") or {}
        exp = c.get("expectancy_usd")
        if exp is not None and exp > 0:
            rollup["strategies_with_positive_expectancy"] += 1
        bar = c.get("evidence_bar")
        if bar in ("sanity", "review", "promotion"):
            rollup["strategies_at_sanity_bar"] += 1
        if bar in ("review", "promotion"):
            rollup["strategies_at_review_bar"] += 1
        if bar == "promotion":
            rollup["strategies_at_promotion_bar"] += 1
        p = c.get("p_expectancy_positive")
        if p is not None:
            if p >= 0.50:
                rollup["strategies_p_positive_gte_50pct"] += 1
            if p >= 0.90:
                rollup["strategies_p_positive_gte_90pct"] += 1
            leaders.append((p, label, c.get("n_total")))
    leaders.sort(reverse=True)
    rollup["leaders_by_p_positive"] = [
        {"strategy": lbl, "p_expectancy_positive": round(p, 3), "n_total": n}
        for p, lbl, n in leaders[:5]
    ]
    return rollup


def _compute_is_healthy(kill: dict, recon: dict, prom: dict) -> bool:
    """Strategy is healthy iff: watchdog OK, reconciliation OK or empty,
    and the kill-candidate/drift signals are absent."""
    if kill.get("status") == "KILL_CANDIDATE":
        return False
    if kill.get("triggered_rules"):
        return False
    if recon.get("status") == "DRIFT":
        return False
    return True


def _history_row(state: dict) -> dict:
    """Compact snapshot suitable for an append-only history log. The full
    state is already persisted in fleet_state.json; history keeps just the
    fleet-level rollup + a small per-strategy block so we can chart
    progression over time without loading every historical snapshot."""
    strategies_compact = {}
    for label, block in state.get("strategies", {}).items():
        perf = block.get("performance") or {}
        gates = block.get("gates") or {}
        cf = block.get("canonical_fills") or {}
        conf = block.get("confidence") or {}
        strategies_compact[label] = {
            "trades": perf.get("trades"),
            "profit_factor": perf.get("profit_factor"),
            "pnl_usd": perf.get("pnl_usd"),
            "next_action": gates.get("next_action"),
            "is_healthy": block.get("is_healthy"),
            "canonical_fills_count": cf.get("count"),
            "expectancy_usd": conf.get("expectancy_usd"),
            "p_expectancy_positive": conf.get("p_expectancy_positive"),
            "evidence_bar": conf.get("evidence_bar"),
        }
    fleet = state.get("fleet") or {}
    return {
        "ts": state.get("generated_at"),
        "broker_equity_usd": fleet.get("broker_equity_usd"),
        "broker_connected": fleet.get("broker_connected"),
        "risk_level": fleet.get("risk_level"),
        "kill_candidates": (fleet.get("kill_summary") or {}).get("kill_candidates", []),
        "drift_warnings": (fleet.get("kill_summary") or {}).get("drift_warnings", []),
        "drift_strategies": (fleet.get("reconciliation_summary") or {}).get("drift_strategies", []),
        "strategies": strategies_compact,
    }


def append_history(state: dict) -> Path:
    """Append a compact row to fleet_state_history.jsonl. Never raises —
    a broken history file must not break the snapshot write path."""
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(_history_row(state), default=str) + "\n")
    except Exception:
        pass
    return HISTORY_PATH


def write_fleet_state(state: dict | None = None, *, append_to_history: bool = True) -> Path:
    """Build (if not passed) and write fleet_state.json. Also appends a
    compact row to fleet_state_history.jsonl unless append_to_history=False."""
    if state is None:
        state = build_fleet_state()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    if append_to_history:
        append_history(state)
    return OUT_PATH


def _print_summary(state: dict) -> None:
    print(f"Fleet state  —  generated {state['generated_at']}")
    print(f"Source of truth: {state['source_of_truth_status']}")
    print()
    fleet = state.get("fleet", {})
    print(f"Broker equity:     ${fleet.get('broker_equity_usd')}")
    print(f"Broker connected:  {fleet.get('broker_connected')}")
    print(f"Pause entries:     {fleet.get('pause_entries')}")
    print(f"Risk level:        {fleet.get('risk_level')}")
    print()
    print(f"{'Strategy':28s} {'Trades':>7s} {'PF':>6s} {'PnL$':>10s} "
          f"{'P(exp>0)':>9s} {'Bar':>13s} {'Health':>7s}  Action")
    print("-" * 110)
    for label, block in state.get("strategies", {}).items():
        perf = block["performance"]
        gates = block["gates"]
        conf = block.get("confidence") or {}
        health = "OK" if block["is_healthy"] else "WARN"
        pf = perf.get("profit_factor")
        pnl = perf.get("pnl_usd")
        p_pos = conf.get("p_expectancy_positive")
        p_str = f"{p_pos:>9.2f}" if p_pos is not None else f"{'--':>9s}"
        bar = conf.get("evidence_bar") or "--"
        print(f"  {label:26s} {perf.get('trades') or 0:>7d} "
              f"{(pf if pf is not None else 0):>6.2f} "
              f"${(pnl if pnl is not None else 0):>9.2f} "
              f"{p_str} {bar:>13s} "
              f"{health:>7s}  {gates.get('next_action') or '--'}")
    print()
    rollup = (state.get("fleet") or {}).get("confidence_rollup") or {}
    print(f"Confidence rollup: "
          f"{rollup.get('strategies_at_sanity_bar', 0)} sanity, "
          f"{rollup.get('strategies_at_review_bar', 0)} review, "
          f"{rollup.get('strategies_at_promotion_bar', 0)} promotion; "
          f"{rollup.get('strategies_p_positive_gte_90pct', 0)} with P(exp>0) >= 0.90")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Dump full state to stdout")
    args = ap.parse_args()

    state = build_fleet_state()
    out = write_fleet_state(state)

    if args.json:
        print(json.dumps(state, indent=2, default=str))
    else:
        _print_summary(state)
        print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
