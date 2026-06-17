"""Promotion panel — packaged 5-layer disciplined OOS gate.

Built 2026-05-22 to crystallize the methodology that emerged from the
backtest factory v1-v5 + bootstrap + period-stability + multi-testing
work. The gate is the same one applied to xs_momentum and spy_trend_follower.

The panel takes a trade-pnl list and returns a structured report with
PASS/FAIL on each layer. It's the single source of truth for "is this
candidate paper-deployable?"

## The five layers

1. **IID bootstrap PF CI** — the baseline 95% CI on profit factor. Pass
   threshold: CI lower bound >= 1.20 (per project_2026_05_20 memo).

2. **Slippage haircut** — same bootstrap with N bps subtracted from each
   pnl_pct (round-trip cost). For ETF strategies, 10 bps is realistic.

3. **Block bootstrap** — Politis-Romano moving-block resampling preserves
   within-block correlation. Momentum trades cluster in regimes; block
   bootstrap is the more honest CI. Pass at the same 1.20 threshold.

4. **Period stability** — H1 vs H2 split-sample. The full-window PF can
   pass while one half is breakeven — that's a regime artifact, not a
   durable edge. BOTH halves must pass for true regime-independence.

5. **Multiple-testing context** — empirical p-value vs the 1.20 floor.
   Bonferroni at N_tests=350 is over-conservative on correlated tests
   but worth knowing. We report the p-value and let the caller interpret.

## Verdict

The headline verdict is "PASS_DISCIPLINED_GATE" only if all five layers
pass with the conservative thresholds. Otherwise "PARTIAL_PASS" (some
layers pass) or "FAIL".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import pandas as pd

from helio.bootstrap_stats import (
    bootstrap_profit_factor,
    block_bootstrap_profit_factor,
)


DEFAULT_PROMOTION_FLOOR = 1.20
DEFAULT_BLOCK_SIZES = (3, 5, 8)
DEFAULT_SLIPPAGE_LEVELS_BPS = (0.0, 10.0)
DEFAULT_N_RESAMPLES = 5000
DEFAULT_CONFIDENCE = 0.95


@dataclass
class LayerResult:
    name: str
    pf_point: float
    ci_lower: float
    ci_upper: float
    n: int
    passes: bool
    note: str = ""


@dataclass
class PromotionPanelReport:
    label: str
    n_trades: int
    promotion_floor: float
    confidence: float
    layers: list[LayerResult] = field(default_factory=list)
    p_value: Optional[float] = None
    n_layers_passed: int = 0
    n_layers_total: int = 0
    headline_verdict: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n_trades": self.n_trades,
            "promotion_floor": self.promotion_floor,
            "confidence": self.confidence,
            "headline_verdict": self.headline_verdict,
            "n_layers_passed": self.n_layers_passed,
            "n_layers_total": self.n_layers_total,
            "p_value_full_block": self.p_value,
            "layers": [
                {
                    "name": l.name, "pf": round(l.pf_point, 4),
                    "ci_lower": round(l.ci_lower, 4), "ci_upper": round(l.ci_upper, 4),
                    "n": l.n, "passes": l.passes, "note": l.note,
                }
                for l in self.layers
            ],
        }


def _apply_slippage(pnls: Sequence[float], slip_bps: float) -> list[float]:
    return [float(p) - slip_bps / 100.0 for p in pnls]


def run_panel(
    pnls: Sequence[float],
    *,
    label: str = "",
    entry_dates: Optional[Sequence] = None,
    promotion_floor: float = DEFAULT_PROMOTION_FLOOR,
    slippage_levels_bps: Sequence[float] = DEFAULT_SLIPPAGE_LEVELS_BPS,
    block_sizes: Sequence[int] = DEFAULT_BLOCK_SIZES,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 42,
    min_n_per_half: int = 5,
) -> PromotionPanelReport:
    """Run the full disciplined gate on a trade-pnl list.

    `pnls` is the gross per-trade percent return list (do NOT pre-subtract
    slippage — the panel will apply each level in `slippage_levels_bps`).

    `entry_dates` is optional; if provided, used to split H1/H2 by chronology
    rather than by position. If None, splits by list-position (assumed
    ordered).
    """
    report = PromotionPanelReport(
        label=label or "(unnamed)",
        n_trades=len(pnls),
        promotion_floor=promotion_floor,
        confidence=confidence,
    )

    if len(pnls) < 10:
        report.headline_verdict = "INSUFFICIENT_N"
        report.layers.append(LayerResult(
            name="precondition_n>=10",
            pf_point=0.0, ci_lower=0.0, ci_upper=0.0,
            n=len(pnls), passes=False,
            note=f"n={len(pnls)} below minimum for bootstrap",
        ))
        report.n_layers_total = 1
        return report

    # Layer 1+2: IID bootstrap at each slippage level
    for slip in slippage_levels_bps:
        pnls_slip = _apply_slippage(pnls, slip)
        bs = bootstrap_profit_factor(
            pnls_slip, n_resamples=n_resamples, confidence=confidence, seed=seed,
        )
        passes = bs.ci_lower >= promotion_floor
        report.layers.append(LayerResult(
            name=f"iid_bootstrap_slip_{slip:.0f}bp",
            pf_point=bs.point, ci_lower=bs.ci_lower, ci_upper=bs.ci_upper,
            n=bs.n_sample, passes=passes,
            note=f"slippage={slip}bp per trade",
        ))

    # Layer 3: Block bootstrap at 10 bp slippage (most realistic)
    real_slip = max(slippage_levels_bps) if slippage_levels_bps else 0.0
    pnls_slip = _apply_slippage(pnls, real_slip)
    for block_size in block_sizes:
        if block_size >= len(pnls_slip):
            continue
        bb = block_bootstrap_profit_factor(
            pnls_slip, block_size=block_size,
            n_resamples=n_resamples, confidence=confidence, seed=seed,
        )
        passes = bb.ci_lower >= promotion_floor
        report.layers.append(LayerResult(
            name=f"block_bootstrap_b{block_size}_slip_{real_slip:.0f}bp",
            pf_point=bb.point, ci_lower=bb.ci_lower, ci_upper=bb.ci_upper,
            n=bb.n_sample, passes=passes,
            note=f"block_size={block_size}, slip={real_slip}bp",
        ))

    # Layer 4: Period stability — H1 vs H2 split-sample at real slippage
    sorted_pnls = list(pnls_slip)
    if entry_dates is not None and len(entry_dates) == len(pnls):
        # Sort by entry_dates
        paired = sorted(
            zip(entry_dates, pnls_slip),
            key=lambda x: pd.Timestamp(x[0]) if x[0] is not None else pd.Timestamp.min,
        )
        sorted_pnls = [p for _, p in paired]
    half = len(sorted_pnls) // 2
    h1 = sorted_pnls[:half]
    h2 = sorted_pnls[half:]
    for h_label, h_pnls in [("H1", h1), ("H2", h2)]:
        if len(h_pnls) < min_n_per_half:
            report.layers.append(LayerResult(
                name=f"period_stability_{h_label.lower()}",
                pf_point=0.0, ci_lower=0.0, ci_upper=0.0,
                n=len(h_pnls), passes=False,
                note=f"n={len(h_pnls)} < min_n_per_half={min_n_per_half}",
            ))
            continue
        bsh = bootstrap_profit_factor(
            h_pnls, n_resamples=max(2000, n_resamples // 2),
            confidence=confidence, seed=seed,
        )
        passes = bsh.ci_lower >= promotion_floor
        report.layers.append(LayerResult(
            name=f"period_stability_{h_label.lower()}_slip_{real_slip:.0f}bp",
            pf_point=bsh.point, ci_lower=bsh.ci_lower, ci_upper=bsh.ci_upper,
            n=bsh.n_sample, passes=passes,
            note=f"chronological half, slip={real_slip}bp",
        ))

    # Layer 5: Multiple-testing context — block bootstrap p-value
    # P[block_bootstrap PF <= promotion_floor]
    p_value = _empirical_p_value(
        pnls_slip,
        threshold=promotion_floor,
        block_size=block_sizes[len(block_sizes) // 2] if block_sizes else 5,
        n_trials=n_resamples * 4,  # finer estimate for p
        seed=seed,
    )
    report.p_value = p_value
    # Treat as passing if p < 0.01 (informative but not the hard gate)
    passes_p = p_value < 0.01
    report.layers.append(LayerResult(
        name="p_value_block_bootstrap",
        pf_point=p_value, ci_lower=0.0, ci_upper=0.0,
        n=len(pnls_slip), passes=passes_p,
        note=f"P[block_bootstrap PF <= {promotion_floor}] = {p_value:.5f}; "
             f"Bonferroni alpha_eff @ N=350 = {0.05/350:.5f}",
    ))

    # Headline verdict
    report.n_layers_passed = sum(1 for l in report.layers if l.passes)
    report.n_layers_total = len(report.layers)
    if report.n_layers_passed == report.n_layers_total:
        report.headline_verdict = "PASS_DISCIPLINED_GATE"
    elif report.n_layers_passed >= report.n_layers_total - 1:
        report.headline_verdict = "MARGINAL_PASS"
    elif report.n_layers_passed >= report.n_layers_total // 2:
        report.headline_verdict = "PARTIAL_PASS"
    else:
        report.headline_verdict = "FAIL"
    return report


def _empirical_p_value(
    pnls: Sequence[float],
    *,
    threshold: float,
    block_size: int,
    n_trials: int,
    seed: int,
) -> float:
    """Compute P[block_bootstrap PF <= threshold] using moving-block
    resampling. Used for multi-testing context."""
    import math
    import random

    from helio.bootstrap_stats import _profit_factor

    if not pnls or block_size < 1:
        return 1.0
    if block_size >= len(pnls):
        block_size = max(1, len(pnls) // 2)
    rng = random.Random(seed)
    n = len(pnls)
    n_blocks = (n + block_size - 1) // block_size
    below = 0
    for _ in range(n_trials):
        resample = []
        for _b in range(n_blocks):
            start = rng.randrange(n - block_size + 1)
            resample.extend(pnls[start:start + block_size])
        resample = resample[:n]
        pf = _profit_factor(resample)
        if math.isfinite(pf) and pf <= threshold:
            below += 1
    return below / n_trials


def render_panel_report(report: PromotionPanelReport) -> str:
    """Pretty-print the panel report for operator review."""
    lines = []
    lines.append(f"=== promotion_panel: {report.label} ===")
    lines.append(f"n_trades={report.n_trades}  promotion_floor={report.promotion_floor}  "
                 f"confidence={report.confidence}")
    lines.append("")
    lines.append(f"{'layer':<40} {'n':>5} {'PF':>7} {'CI_lo':>7} {'CI_hi':>9} {'verdict':>8}")
    lines.append("-" * 85)
    for l in report.layers:
        v = "PASS" if l.passes else "fail"
        lines.append(f"{l.name:<40} {l.n:>5} {l.pf_point:>7.3f} {l.ci_lower:>7.3f} "
                     f"{l.ci_upper:>9.3f} {v:>8}")
    lines.append("")
    if report.p_value is not None:
        lines.append(f"p-value (block bootstrap, P[PF<=floor]): {report.p_value:.5f}")
        lines.append(f"Bonferroni alpha_eff @ N=350: {0.05/350:.5f}")
    lines.append("")
    lines.append(f"HEADLINE: {report.headline_verdict}  "
                 f"({report.n_layers_passed}/{report.n_layers_total} layers passed)")
    return "\n".join(lines)
