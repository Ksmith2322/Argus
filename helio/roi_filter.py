"""helio/roi_filter.py — phantom + invalid-row quarantine for ROI computations.

Background (2026-05-18 audit): `argus_flow/logs/canonical_fills.jsonl` contains:
  - The 5/5 forge_nq_london_close 417-MNQ phantom (size>>account, sign-questionable)
  - 23 backfill rows with `source: "backfill_from_trade_csv"` (null exit_ts,
    duplicate `ts` with live rows)
  - 99 rows from killed strategies (forge_spy_mean_rev, forge_vix_intraday)
    written AFTER their kill date

Any ROI/promotion math that consumes canonical_fills without filtering these
is producing contaminated numbers. This module is the SINGLE point of filter
truth — every ROI consumer must route through here.

Per Codex audit X4 (evidence_epoch as first-class object): every ROI report
must declare its epoch + exclusion set. This module supplies the exclusion
set; epoch boundary is a separate concern (see project_2026_05_31_clean_reset_plan).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL_FILLS_PATH = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"

# Phantom trades — explicit quarantine list. Each entry uniquely identifies
# a known-bad row. Source: Codex audit 2026-05-18 + manual phantom resolution.
PHANTOM_TRADES: list[dict] = [
    {
        "ts_prefix": "2026-05-05T16:30:35",
        "strategy": "forge_nq_london_close",
        "symbol": "MNQ",
        "size": 417.0,
        "reason": "417 MNQ on $11K anchor (38× leverage); sizing-formula bug pre-5/7 fix; "
                  "broker_anchor_at_fill_usd was null so reconciliation never validated. "
                  "Pnl sign and magnitude both unreliable.",
    },
]

# Killed strategy → effective kill date. Rows from these strategies AFTER
# their kill date are quarantined.
KILLED_STRATEGY_CUTOFFS: dict[str, str] = {
    # Tier 1 explicit kills (each documented in allocation_factors._kill_log)
    "forge_spy_mean_rev": "2026-04-30",
    "forge_multi_orb": "2026-05-07",
    "forge_vix_intraday": "2026-05-12",
    "forge_nq_london_close": "2026-05-13",
    # 2026-05-20 sunset batch — formalized into kill registry on 2026-05-24
    # per operator instruction ("make sure the poor performers are killed").
    # Each strategy was already at allocation=0.0 from 2026-05-20 but lacked
    # the kill-registry entry that triggers the submit_bracket runtime
    # invariant. Project memory:
    # project_2026_05_19_new_edge_candidates + project_2026_05_20_*
    # If any of these are revived in the future, the operator must
    # remove them from this dict (and document the reason).
    "forge_vix_carry":           "2026-05-19",  # backtest -3.07% CAGR / -33% DD / PF 1.01 across 6 variants
    "forge_coint_pairs":         "2026-05-20",  # 0/4 architect candidates; PF 0.91 across 8 pairs
    "forge_aud_asian_breakout":  "2026-05-20",  # no edge in production
    "forge_cuebanks":            "2026-05-20",  # YM YouTube replica, 0 fills post-CBOT fix
    "forge_mamba":               "2026-05-20",  # YM YouTube replica
    "forge_tori":                "2026-05-20",  # YM YouTube replica
    "forge_jpy_pm_short":        "2026-05-20",  # FX, no edge in production
    "forge_wick_gbpusd":         "2026-05-20",
    "forge_vix_revert":          "2026-05-20",
    "forge_fomc_drift":          "2026-05-20",  # resurrection denied 2026-05-24 (CI lower 0.526 @ 7bps post-QE)
    "forge_tom_international":   "2026-05-20",  # event-driven 0-fire pair
    "forge_rebalance":           "2026-05-20",  # annual event archived
    "forge_atlas":                "2026-05-20",  # Greek scanner archived (regime classifier)
    "forge_themis":               "2026-05-20",  # Greek scanner archived
    "forge_gdx_gld":              "2026-05-20",  # shadow strategy, no allocation
    "apollo":                     "2026-05-20",  # Greek scanner; data file used by forge_pead directly
    "hermes":                     "2026-05-20",  # earnings scanner; role absorbed into forge_pead
    "titan":                      "2026-05-20",  # Greek scanner archived
    "argus_gbpusd":               "2026-05-20",  # FX cap-vs-risk conflict, silent
    "argus_usdjpy":               "2026-05-20",
    "argus_cadjpy":               "2026-05-20",
    # 2026-05-23 rigor sprint: failed disciplined gate at REALISTIC slippage
    # (PEAD CI lower 1.025 @ 40bps; NQ overnight CI lower 0.526 @ 7bps).
    # Source: ops/audit/run_slippage_recalibration.py + commit ac45592.
    "forge_pead":                "2026-05-23",
    "forge_nq_overnight":        "2026-05-23",
    # 2026-05-23 concentrate-on-winner reweight: 0.614 correlation with
    # xs_momentum, redundant beta. Could be revived as a benchmark.
    "forge_spy_trend_follower":  "2026-05-23",
    # 2026-05-25: Strategy agent's #1 candidate from
    # docs/AUDIT_2026_05_25_PART2/STRATEGY.md killed by disciplined-gate
    # backtest (helio/overnight_drift + ops/audit/run_overnight_drift_backtest).
    # FAILS at every plausible slippage: PF 0.72-0.81 @ 10bps, 0.90-0.97
    # @ 5bps, 1.02-1.09 @ 2bps — even the best (QQQ @ 2bps) has CI lower
    # 1.01 < 1.20 floor. The overnight premium IS real (~5-7%/yr gross)
    # but slippage drag at 250 trades/yr (25%/yr at 10bps) swamps it.
    # Full results: docs/AUDIT_2026_05_25_PART2/OVERNIGHT_DRIFT_KILL.md
    "forge_overnight_drift_qqq": "2026-05-25",
    # 2026-05-25 (later same evening): Strategy agent's #3 candidate
    # killed by disciplined-gate backtest (helio/credit_spread_regime +
    # ops/audit/run_credit_spread_backtest). FAILS at every parameter
    # combination tested: SMA50/100/200, with/without VIX filter,
    # with/without 5-day confirmation. Best variant (SMA200) hits the
    # agent's predicted point PF=1.53 but H1 PF=2.09 / H2 PF=1.23 split
    # exposes modern-era erosion (CI lower 0.33 in H2). Likely cause:
    # central-bank backstops since 2008 amputate the
    # credit-leads-equity relationship by stepping in before equity
    # reacts. Full results: docs/AUDIT_2026_05_25_PART2/CREDIT_SPREAD_KILL.md
    "forge_credit_spread_regime": "2026-05-25",
    # 2026-05-25 (late evening): Strategy agent's #7 candidate (Halloween
    # effect SPY/SHY rotation) killed by disciplined-gate backtest.
    # H1 (2005-2015) outperformed SPY by +4.4pp; H2 (2015-2026)
    # underperformed by -188pp during the QE-driven bull market.
    # TIME_SPLIT_DIVERGES. The strategy has materially lower max DD
    # (17% vs 56%) but the modern-era CAGR cost (-3.91%/yr) makes it
    # a net loser as a standalone. Useful as a defensive overlay idea
    # but not a standalone strategy. Full results:
    # docs/AUDIT_2026_05_25_PART2/SELL_IN_MAY_KILL.md
    "forge_sell_in_may_modulated": "2026-05-25",
    # 2026-05-25 (very late evening): Strategy agent's #2 candidate
    # (turn-of-quarter SPY) killed at MARGINAL fail. Best variant
    # (30y/5bps) PF=1.71 but CI lower 1.08 vs floor 1.20; H1/H2 both
    # fail. CAGR contribution ~1.7%/yr at full alloc is below noise
    # floor for marginal new strategies. Mechanical overlap with
    # tom_spy's already-deployed window (50% per Q). Full results:
    # docs/AUDIT_2026_05_25_PART2/TURN_OF_QUARTER_KILL.md
    "forge_turn_of_quarter": "2026-05-25",
    # 2026-05-26 (v31 cadence cull): killed for failing the operator's
    # explicit "30 trades/month per strategy, 15-25% per-strategy CAGR"
    # bar. Mathematically these can't be validated in any human-relevant
    # timeframe: 1-2 trades/year means decades to confirm edge persistence.
    # All passed the disciplined backtest gate at honest slippage; killed
    # for *velocity*, not validity. They go to the "considered, validated,
    # parked — revisit when optimizing for diversification, not learning
    # speed" pile. To revive: remove this entry and document why.
    "forge_nov_spy":             "2026-05-26",  # 2 fills/yr (1 round-trip), PF 4.79
    "forge_ewz_breakout":        "2026-05-26",  # ~4 fills/yr, PF 1.73
    "forge_ief_jul_hold":        "2026-05-26",  # 2 fills/yr, PF 12.14
    "forge_gld_jan_hold":        "2026-05-26",  # 2 fills/yr, PF 4.23
    "forge_uso_jun_hold":        "2026-05-26",  # 2 fills/yr, PF 4.07
    "forge_hyg_apr_hold":        "2026-05-26",  # 2 fills/yr, PF 7.41
    # 2026-05-26 v31.5: operator added explicit "each strategy must beat SPY
    # net of taxes" bar = ~13% pre-tax CAGR floor per strategy. tom_spy at
    # 3% backtest CAGR fails this bar by 10pp on its own AND drags fleet-
    # combined CAGR below 13%. Removing it pushes fleet CAGR ~12.89% -> 13.74%
    # (passes mandatory floor). See docs/decisions/
    # 2026_08_31_real_money_promotion_gate.md condition #1.
    "forge_tom_spy":             "2026-05-26",  # 12 fills/yr but CAGR 3% < 13% bar
}

# Single-trade P&L threshold for "obviously phantom" rows that escaped the
# above explicit list. Sub-$10 strategies should never produce a $1K+ trade.
ABS_PNL_PHANTOM_THRESHOLD = 1000.0


@dataclass
class FilterStats:
    n_total: int = 0
    n_kept: int = 0
    n_phantom_explicit: int = 0
    n_phantom_threshold: int = 0
    n_killed_post_cutoff: int = 0
    n_backfill_null_anchor: int = 0
    n_pre_epoch: int = 0
    n_bad_schema: int = 0
    excluded_examples: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"FilterStats(total={self.n_total}, kept={self.n_kept}, "
            f"phantom_explicit={self.n_phantom_explicit}, "
            f"phantom_threshold={self.n_phantom_threshold}, "
            f"killed_post_cutoff={self.n_killed_post_cutoff}, "
            f"backfill_null_anchor={self.n_backfill_null_anchor}, "
            f"pre_epoch={self.n_pre_epoch}, bad_schema={self.n_bad_schema})"
        )


def _is_phantom_explicit(row: dict) -> bool:
    """Check if row matches any explicit phantom in PHANTOM_TRADES."""
    ts = row.get("ts", "")
    strat = row.get("strategy", "")
    sym = row.get("symbol", "")
    size = row.get("size", 0)
    try:
        size = float(size)
    except (TypeError, ValueError):
        size = 0
    for p in PHANTOM_TRADES:
        if (ts.startswith(p["ts_prefix"]) and
                strat == p["strategy"] and
                sym == p["symbol"] and
                abs(size - p["size"]) < 0.5):
            return True
    return False


def _is_killed_post_cutoff(row: dict) -> bool:
    """Check if row is from a killed strategy AFTER its kill cutoff."""
    strat = row.get("strategy", "")
    cutoff = KILLED_STRATEGY_CUTOFFS.get(strat)
    if not cutoff:
        return False
    ts = row.get("ts", "")
    return ts[:10] >= cutoff


def _is_backfill_with_null_anchor(row: dict) -> bool:
    """Backfill rows have source='backfill_from_trade_csv' AND null exit_ts.
    These are duplicates of live rows that lacked broker_anchor metadata.
    """
    return (row.get("source") == "backfill_from_trade_csv" and
            row.get("exit_ts") is None)


def _is_phantom_by_threshold(row: dict) -> bool:
    """Catch phantoms not in the explicit list via absolute P&L magnitude."""
    pnl = row.get("pnl_usd", 0)
    try:
        return abs(float(pnl)) > ABS_PNL_PHANTOM_THRESHOLD
    except (TypeError, ValueError):
        return False


def filter_fills(rows: list[dict], *,
                  epoch_start: str | None = None,
                  apply_phantom_threshold: bool = True,
                  exclude_killed_post_cutoff: bool = True,
                  exclude_backfill_null_anchor: bool = True,
                  track_examples: int = 5) -> tuple[list[dict], FilterStats]:
    """Apply the canonical exclusion set. Returns (kept_rows, stats).

    epoch_start: ISO date. Rows BEFORE this date are excluded as "pre-epoch."
    Use to enforce a clean evidence window (e.g., post-5/31 reset → epoch_start='2026-06-01').

    track_examples: how many excluded rows to keep as examples in stats
    (per-category, for diagnostic logging).
    """
    stats = FilterStats(n_total=len(rows))
    kept = []
    for r in rows:
        # 1. Schema sanity
        if not isinstance(r, dict) or "strategy" not in r or "ts" not in r:
            stats.n_bad_schema += 1
            continue
        # 2. Epoch boundary
        if epoch_start is not None and r.get("ts", "")[:10] < epoch_start:
            stats.n_pre_epoch += 1
            continue
        # 3. Explicit phantom
        if _is_phantom_explicit(r):
            stats.n_phantom_explicit += 1
            if len(stats.excluded_examples) < track_examples:
                stats.excluded_examples.append({"reason": "phantom_explicit", "row": r})
            continue
        # 4. Killed strategy post-cutoff
        if exclude_killed_post_cutoff and _is_killed_post_cutoff(r):
            stats.n_killed_post_cutoff += 1
            continue
        # 5. Backfill with null anchor (likely duplicate of live row)
        if exclude_backfill_null_anchor and _is_backfill_with_null_anchor(r):
            stats.n_backfill_null_anchor += 1
            continue
        # 6. Phantom by threshold (catch-all)
        if apply_phantom_threshold and _is_phantom_by_threshold(r):
            stats.n_phantom_threshold += 1
            if len(stats.excluded_examples) < track_examples:
                stats.excluded_examples.append({"reason": "phantom_threshold", "row": r})
            continue
        # Passed all filters
        kept.append(r)
    stats.n_kept = len(kept)
    return kept, stats


def load_canonical_fills(path: Path = CANONICAL_FILLS_PATH) -> list[dict]:
    """Load canonical_fills.jsonl, tolerating malformed lines."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def filtered_canonical_fills(*, epoch_start: str | None = None,
                              path: Path = CANONICAL_FILLS_PATH,
                              **kwargs) -> tuple[list[dict], FilterStats]:
    """One-shot: load and filter canonical_fills.jsonl. The most common entry
    point for ROI / promotion math."""
    raw = load_canonical_fills(path)
    return filter_fills(raw, epoch_start=epoch_start, **kwargs)


def assert_min_entries(rows: list[dict], min_n: int = 1) -> None:
    """Hard-fail safety check: refuse to compute ROI on zero-entry data.

    Per Codex audit X7: ENTRY/EXIT lifecycle not reconstructible if all rows
    are EXIT. Reports that pass an empty/EXIT-only set should NOT silently
    produce numbers. Caller should pass the ENTRY-side rows here.
    """
    if len(rows) < min_n:
        raise RuntimeError(
            f"ROI computation aborted: only {len(rows)} rows after filtering, "
            f"need >={min_n}. Either canonical_fills lacks ENTRY records, the "
            f"epoch boundary excluded too aggressively, or the data is "
            f"contaminated. Investigate before drawing conclusions."
        )
