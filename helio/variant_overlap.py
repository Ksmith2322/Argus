"""Variant pick-overlap diagnostic for the xs_momentum fleet.

The 2026-05-25 Comprehensive Audit's most-pointed finding:

  > "5 of 6 active strategies ARE the xs_momentum engine on different
  > universes; 3 of them currently hold the exact same picks (XLE, XLK,
  > ±EEM). True independent engines: 2."

That's a snapshot — it could be a one-month coincidence or it could be
the steady-state behavior. This module quantifies the answer by
re-running each variant's monthly-rebalance pick history over 20y and
computing:

  - pairwise pick overlap (how often do variants A and B agree?)
  - multi-variant consensus (how many months does >=3 of N pick the same ticker?)
  - "shadow consensus" PnL: what would a sleeve that ONLY trades the
    >=3-vote consensus picks look like?

If the median pairwise overlap is >0.5, the 5-variant fleet is
effectively 1.5-2 independent strategies wearing 5 costumes. If the
consensus sleeve PF is materially higher than the average variant PF
(at lower DD), the consensus rule captures the diversification benefit
the individual variants don't have.

Usage:
    from helio.variant_overlap import analyze_variant_overlap, ConsensusConfig
    cfg = ConsensusConfig(period="10y", min_votes=3)
    result = analyze_variant_overlap(cfg)
    print(result.summary_md())
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from forge.xs_momentum.runner import backtest as xs_backtest
from helio.xs_momentum_universes import CANDIDATE_UNIVERSES
from helio.bootstrap_stats import block_bootstrap_profit_factor


# The 5 distinct-universe xs_momentum variants live in production today
# (after v20). Each has its own universe; the engine + ranking are
# identical, only the universe parameter changes. style_top3 and
# legacy15_regime aren't included here — they share universes with
# existing variants and would double-count if we did.
VARIANTS_FOR_OVERLAP: tuple[tuple[str, Optional[str], Optional[float]], ...] = (
    # (label,                 universe_key in CANDIDATE_UNIVERSES,           top_pick_fraction)
    ("broad_8",               None,                                          None),
    ("sectors_spdr_11",       "sectors_spdr_11",                             None),
    ("style_factors_8",       "style_factors_8",                             None),
    ("legacy_15",             "legacy_sectors_countries_15",                 None),
    ("wide_global_47",        "wide_global_47",                              None),
)


@dataclass(frozen=True)
class ConsensusConfig:
    period: str = "10y"
    min_votes: int = 3
    slippage_bps_rt: float = 10.0


@dataclass
class VariantPickHistory:
    """For one variant: map of YYYY-MM (rebalance date prefix) → set of picked tickers."""
    label: str
    monthly_picks: dict[str, set[str]] = field(default_factory=dict)
    n_trades: int = 0


@dataclass
class HalfStats:
    label: str
    n: int = 0
    pf: float = 0.0
    pf_ci_lower: float = 0.0
    pf_ci_upper: float = 0.0
    avg_ret_pct: float = 0.0
    passes_floor: bool = False


@dataclass
class OverlapAnalysis:
    period: str
    min_votes: int
    variant_histories: dict[str, VariantPickHistory] = field(default_factory=dict)
    months_covered: list[str] = field(default_factory=list)
    pairwise_overlap: dict[tuple[str, str], float] = field(default_factory=dict)
    median_pairwise: float = 0.0
    months_with_consensus_pick: int = 0
    total_months: int = 0
    consensus_picks_by_month: dict[str, list[str]] = field(default_factory=dict)
    top_consensus_tickers: list[tuple[str, int]] = field(default_factory=list)
    consensus_pnl_pcts: list[float] = field(default_factory=list)
    consensus_pf: float = 0.0
    consensus_pf_ci_lower: float = 0.0
    consensus_pf_ci_upper: float = 0.0
    consensus_avg_ret_pct: float = 0.0
    consensus_total_compound: float = 0.0
    # Walk-forward H1/H2 of the consensus sleeve (sorted by month)
    consensus_h1: Optional["HalfStats"] = None
    consensus_h2: Optional["HalfStats"] = None
    pf_floor: float = 1.20

    def summary_md(self) -> str:
        lines = []
        lines.append(f"# Variant Overlap Analysis ({self.period}, min_votes={self.min_votes})")
        lines.append("")
        lines.append(f"- variants: {len(self.variant_histories)}")
        lines.append(f"- months covered: {len(self.months_covered)}")
        lines.append(f"- median pairwise pick overlap: **{self.median_pairwise:.1%}**")
        lines.append(f"- months with >={self.min_votes}-vote consensus pick: "
                     f"{self.months_with_consensus_pick} / {self.total_months}")
        lines.append("")
        lines.append("## Pairwise overlap matrix (Jaccard)")
        labels = list(self.variant_histories.keys())
        lines.append("| | " + " | ".join(labels) + " |")
        lines.append("|---" + "|---" * len(labels) + "|")
        for a in labels:
            row = [a]
            for b in labels:
                if a == b:
                    row.append("1.00")
                else:
                    key = tuple(sorted([a, b]))
                    row.append(f"{self.pairwise_overlap.get(key, 0.0):.2f}")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        if self.top_consensus_tickers:
            lines.append("## Top consensus picks (ticker, # of months in consensus)")
            for t, n in self.top_consensus_tickers[:15]:
                lines.append(f"- {t}: {n}")
            lines.append("")
        lines.append("## Consensus sleeve hypothetical (>=%d-of-%d picks only)" %
                     (self.min_votes, len(self.variant_histories)))
        lines.append(f"- n_trades: {len(self.consensus_pnl_pcts)}")
        lines.append(f"- PF: {self.consensus_pf:.3f}")
        lines.append(f"- bootstrap PF CI: [{self.consensus_pf_ci_lower:.3f}, "
                     f"{self.consensus_pf_ci_upper:.3f}]")
        lines.append(f"- avg net return per trade: {self.consensus_avg_ret_pct:.2f}%")
        lines.append(f"- compounded total: {self.consensus_total_compound:.0f}%")
        floor_pass = (self.consensus_pf_ci_lower >= self.pf_floor)
        lines.append(f"- bootstrap CI lower >= {self.pf_floor:.2f} floor: "
                     f"{'**PASS**' if floor_pass else 'FAIL'}")
        if self.consensus_h1 and self.consensus_h2:
            lines.append("")
            lines.append("## Walk-forward H1/H2 (chronological split of consensus trades)")
            for h in (self.consensus_h1, self.consensus_h2):
                lines.append(f"- {h.label}: n={h.n}, PF={h.pf:.2f}, "
                             f"CI lower={h.pf_ci_lower:.2f}, avg={h.avg_ret_pct:.2f}%, "
                             f"pass floor={h.passes_floor}")
            verdict = ("PASS" if (floor_pass and self.consensus_h1.passes_floor
                                   and self.consensus_h2.passes_floor) else
                        "MARGINAL_PASS" if (floor_pass and
                                            (self.consensus_h1.passes_floor or
                                             self.consensus_h2.passes_floor)) else
                        "FAIL")
            lines.append(f"- **Disciplined-gate verdict: {verdict}**")
        lines.append("")
        return "\n".join(lines)


# ── Per-variant pick history ────────────────────────────────────────

def _backtest_one_variant(label: str, universe_key: Optional[str], top_pick_fraction: Optional[float],
                          period: str) -> VariantPickHistory:
    """Run the xs_momentum backtest with the variant's universe and harvest
    the per-month pick set from the returned trade detail."""
    universe = list(CANDIDATE_UNIVERSES[universe_key]) if universe_key else None
    result = xs_backtest(
        period=period,
        universe_override=universe,
        top_pick_fraction=top_pick_fraction,
    )
    hist = VariantPickHistory(label=label)
    trades = result.get("trades_detail") or []
    hist.n_trades = len(trades)
    # Group trades by entry_date (YYYY-MM prefix) — picks at that rebalance
    for t in trades:
        entry = str(t.get("entry_date", ""))
        if not entry:
            continue
        m_key = entry[:7]  # YYYY-MM
        ticker = t.get("ticker", "")
        if not ticker:
            continue
        hist.monthly_picks.setdefault(m_key, set()).add(ticker)
    return hist


# ── Overlap math ────────────────────────────────────────────────────

def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    if not u:
        return 1.0
    return len(a & b) / len(u)


def analyze_variant_overlap(cfg: ConsensusConfig) -> OverlapAnalysis:
    """Run backtests for each variant, compute pairwise overlap + consensus stats."""
    out = OverlapAnalysis(period=cfg.period, min_votes=cfg.min_votes)

    # Backtest each variant
    for label, universe_key, top_pick_fraction in VARIANTS_FOR_OVERLAP:
        out.variant_histories[label] = _backtest_one_variant(
            label, universe_key, top_pick_fraction, cfg.period,
        )

    # Union of all months any variant traded
    months = set()
    for hist in out.variant_histories.values():
        months.update(hist.monthly_picks.keys())
    out.months_covered = sorted(months)
    out.total_months = len(out.months_covered)

    # Pairwise overlap (Jaccard) — average across months where BOTH variants
    # have non-empty pick sets
    labels = list(out.variant_histories.keys())
    pair_scores: dict[tuple[str, str], list[float]] = {}
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            key = tuple(sorted([a, b]))
            scores = []
            for m in out.months_covered:
                sa = out.variant_histories[a].monthly_picks.get(m, set())
                sb = out.variant_histories[b].monthly_picks.get(m, set())
                if sa or sb:
                    scores.append(_jaccard(sa, sb))
            pair_scores[key] = scores
            if scores:
                out.pairwise_overlap[key] = sum(scores) / len(scores)
            else:
                out.pairwise_overlap[key] = 0.0
    if out.pairwise_overlap:
        vals = sorted(out.pairwise_overlap.values())
        mid = len(vals) // 2
        out.median_pairwise = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    # Consensus picks: for each month, count votes per ticker; keep >= min_votes
    ticker_consensus_count: dict[str, int] = {}
    for m in out.months_covered:
        votes: dict[str, int] = {}
        for hist in out.variant_histories.values():
            for tkr in hist.monthly_picks.get(m, set()):
                votes[tkr] = votes.get(tkr, 0) + 1
        consensus = [t for t, n in votes.items() if n >= cfg.min_votes]
        if consensus:
            out.consensus_picks_by_month[m] = consensus
            for t in consensus:
                ticker_consensus_count[t] = ticker_consensus_count.get(t, 0) + 1
    out.months_with_consensus_pick = len(out.consensus_picks_by_month)
    out.top_consensus_tickers = sorted(
        ticker_consensus_count.items(), key=lambda x: -x[1],
    )

    # Hypothetical consensus-sleeve PnL: for each consensus-month, equal-weight
    # the consensus picks and use the realized next-month return derived from
    # the variants' trades_detail (already-net-of-anything-they-net-of).
    # Simplification: average the per-trade pnl_pct from ANY variant that
    # holds the consensus ticker that month — they're all using next-month
    # close-to-close on the same data, so the per-trade return for ticker X
    # entering at month M is identical across any variant that picked it.
    consensus_pnls: list[float] = []
    # Build a (month, ticker) -> pnl_pct lookup from any variant's trades
    pnl_lookup: dict[tuple[str, str], float] = {}
    for hist in out.variant_histories.values():
        for t in []:
            pass
    # Re-call each variant's backtest to harvest trades with pnl
    for label, universe_key, top_pick_fraction in VARIANTS_FOR_OVERLAP:
        universe = list(CANDIDATE_UNIVERSES[universe_key]) if universe_key else None
        result = xs_backtest(
            period=cfg.period,
            universe_override=universe,
            top_pick_fraction=top_pick_fraction,
        )
        for tr in result.get("trades_detail") or []:
            m = str(tr.get("entry_date", ""))[:7]
            tkr = tr.get("ticker", "")
            if not (m and tkr):
                continue
            if (m, tkr) not in pnl_lookup:
                pnl_lookup[(m, tkr)] = float(tr.get("pnl_pct", 0.0))

    drag = 2.0 * (cfg.slippage_bps_rt / 100.0)  # round-trip slippage
    for m, picks in out.consensus_picks_by_month.items():
        for tkr in picks:
            pnl = pnl_lookup.get((m, tkr))
            if pnl is not None:
                consensus_pnls.append(pnl - drag)

    out.consensus_pnl_pcts = consensus_pnls
    if consensus_pnls:
        wins = sum(p for p in consensus_pnls if p > 0)
        losses = abs(sum(p for p in consensus_pnls if p < 0))
        out.consensus_pf = wins / losses if losses > 0 else float("inf")
        out.consensus_avg_ret_pct = sum(consensus_pnls) / len(consensus_pnls)
        eq = 1.0
        for p in consensus_pnls:
            eq *= (1.0 + p / 100.0)
        out.consensus_total_compound = (eq - 1.0) * 100.0
        boot = block_bootstrap_profit_factor(consensus_pnls, block_size=5, n_resamples=3000)
        out.consensus_pf_ci_lower = boot.ci_lower
        out.consensus_pf_ci_upper = boot.ci_upper

        # Walk-forward H1/H2: chronologically split the consensus trades
        # (in (month, ticker) order) and bootstrap each half.
        chronological = []
        for m, picks in sorted(out.consensus_picks_by_month.items()):
            for tkr in picks:
                pnl = pnl_lookup.get((m, tkr))
                if pnl is not None:
                    chronological.append(pnl - drag)
        if len(chronological) >= 10:
            mid = len(chronological) // 2
            for label, slice_ in (("H1", chronological[:mid]), ("H2", chronological[mid:])):
                w = sum(p for p in slice_ if p > 0)
                l = abs(sum(p for p in slice_ if p < 0))
                pf = w / l if l > 0 else float("inf")
                avg = sum(slice_) / len(slice_) if slice_ else 0.0
                b = block_bootstrap_profit_factor(slice_, block_size=5, n_resamples=2000)
                half = HalfStats(
                    label=label, n=len(slice_), pf=pf,
                    pf_ci_lower=b.ci_lower, pf_ci_upper=b.ci_upper,
                    avg_ret_pct=avg,
                    passes_floor=(b.ci_lower >= out.pf_floor),
                )
                if label == "H1":
                    out.consensus_h1 = half
                else:
                    out.consensus_h2 = half

    return out
