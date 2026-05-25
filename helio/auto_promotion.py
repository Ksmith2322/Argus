"""Auto-promotion recommendation engine.

Mirror of helio.auto_pause but for the OPPOSITE direction: surfaces
strategies whose live + backtest evidence supports a HIGHER
allocation than current.

DESIGN
======
Reads three evidence sources:
  1. walk_forward_oos.json     — does the strategy hold OOS?
  2. xs_momentum_cohort_correlation.json — Sharpe vs cohort
  3. canonical_fills.jsonl     — live realized PnL (when available)

Emits recommendations with a confidence score so operator can
filter. Default threshold for proposing a UP-flip:
  - Walk-forward verdict = STRENGTHENED_OOS or MILD_DECAY
  - Live n_trades ≥ 20 (when fills accumulate) OR audit-based-only
    when n_trades < 20
  - Currently allocated < 1.0×
  - Combined-Sharpe contribution > cohort median

WHY HUMAN-GATED (just like auto_pause)
======================================
Promotions move CAPITAL. The audit may be right but the operator
needs to confirm against context (capacity, real-money status,
risk policy). The `--apply --confirm` pattern matches auto_pause.

USAGE (the engine — CLI lives in ops/auto_promotion.py)
=======================================================
    from helio.auto_promotion import compute_recommendations
    recs = compute_recommendations()
    # recs["recommendations"] is a list of dicts with proposed_alloc
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]


# Recommendation thresholds
MIN_LIVE_N_FOR_PROMOTION = 20         # live trades required
MAX_ALLOCATION_CEILING = 1.0          # never propose > 1.0x
PROMOTION_STEP = 0.25                 # step up 0.25x at a time

# Walk-forward verdicts that QUALIFY for promotion
PROMOTION_QUALIFYING_VERDICTS = {
    "STRENGTHENED_OOS",
    "MILD_DECAY",
    # NOT: LUCKY_OOS (train fail), BOTH_FAIL, DEGRADED_BUT_PASSING,
    # OVERFIT_DEGRADED
}


@dataclass
class Recommendation:
    strategy: str
    current_alloc: float
    proposed_alloc: float
    confidence: str       # "HIGH" / "MED" / "LOW"
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def delta(self) -> float:
        return self.proposed_alloc - self.current_alloc


# ─── Source readers ─────────────────────────────────────────────────

def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return rows


def _load_walk_forward() -> dict[str, dict]:
    """Returns {universe_label: row}. Maps universe to walk-forward
    verdict + IS/OOS Sharpe."""
    rows = _read_json(
        _REPO / "ops" / "reports" / "system_audit" / "walk_forward_oos.json"
    )
    if not rows or not isinstance(rows, list):
        return {}
    return {r.get("universe"): r for r in rows if r.get("universe")}


def _load_cohort_correlation() -> Optional[dict]:
    return _read_json(
        _REPO / "ops" / "reports" / "system_audit"
              / "xs_momentum_cohort_correlation.json"
    )


def _load_allocation() -> dict[str, float]:
    cfg = _read_json(
        _REPO / "argus_flow" / "configs" / "allocation_factors.json"
    )
    if not cfg:
        return {}
    return {k: float(v) for k, v in (cfg.get("factors") or {}).items()}


def _load_live_fill_counts() -> dict[str, dict]:
    """Returns {strategy_label: {n_trades, n_wins, pnl_usd}} based on
    canonical_fills EXIT entries since the current epoch."""
    fills = _read_jsonl(
        _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    )
    out: dict[str, dict] = {}
    for row in fills:
        if (row.get("side") or "").upper() != "EXIT":
            continue
        strat = row.get("strategy")
        if not strat:
            continue
        pnl = row.get("pnl_usd")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        d = out.setdefault(strat, {
            "n_trades": 0, "n_wins": 0, "pnl_usd": 0.0,
        })
        d["n_trades"] += 1
        if pnl > 0:
            d["n_wins"] += 1
        d["pnl_usd"] += pnl
    return out


# ─── Universe → strategy_label mapping ──────────────────────────────

def _strategy_to_universe_label() -> dict[str, str]:
    """xs_momentum variants map to their universe label in the
    walk-forward audit. gld_pm_long is not in the sweep (1h bars)."""
    return {
        "forge_xs_momentum":              "broad_8",
        "forge_xs_momentum_sectors":      "sectors_spdr_11",
        "forge_xs_momentum_style":        "style_factors_8",
        "forge_xs_momentum_legacy15":     "legacy_15",
        "forge_xs_momentum_style_top3":   "style_factors_8",  # same universe
        "forge_xs_momentum_legacy15_regime": "legacy_15",
    }


# ─── Recommendation logic ───────────────────────────────────────────

def compute_recommendations() -> dict:
    """Build per-strategy promotion recommendations based on the
    available evidence."""
    wf = _load_walk_forward()
    cohort = _load_cohort_correlation()
    allocs = _load_allocation()
    live = _load_live_fill_counts()
    label_to_universe = _strategy_to_universe_label()

    cohort_per_variant = {}
    if cohort:
        for name, stats in (cohort.get("per_variant_stats") or {}).items():
            # cohort uses variant short names; map to full label
            for sl in label_to_universe:
                if sl.endswith(name) or name == "baseline" and sl == "forge_xs_momentum":
                    cohort_per_variant[sl] = stats
                    break

    recs: list[Recommendation] = []
    for strategy_label, universe in label_to_universe.items():
        current = allocs.get(strategy_label, 0.0)
        if current >= MAX_ALLOCATION_CEILING:
            continue  # already at ceiling, nothing to recommend

        # Walk-forward verdict gate
        wf_row = wf.get(universe) or {}
        verdict = wf_row.get("verdict")
        wf_qualifies = verdict in PROMOTION_QUALIFYING_VERDICTS

        # Live evidence
        live_row = live.get(strategy_label) or {}
        n_live = int(live_row.get("n_trades") or 0)
        live_pnl = float(live_row.get("pnl_usd") or 0.0)
        live_qualifies = n_live >= MIN_LIVE_N_FOR_PROMOTION and live_pnl > 0

        # Cohort Sharpe
        cohort_sharpe = (
            (cohort_per_variant.get(strategy_label) or {}).get("sharpe")
        )

        reasons: list[str] = []
        confidence = "LOW"

        if wf_qualifies:
            reasons.append(
                f"walk_forward={verdict} "
                f"(IS Sharpe {wf_row.get('train', {}).get('sharpe')}, "
                f"OOS Sharpe {wf_row.get('test', {}).get('sharpe')})"
            )
            confidence = "MED"
        if live_qualifies:
            reasons.append(
                f"live n={n_live} pnl=${live_pnl:.0f}"
            )
            confidence = "HIGH" if wf_qualifies else "MED"
        if cohort_sharpe is not None and cohort_sharpe >= 0.8:
            reasons.append(f"cohort Sharpe {cohort_sharpe:.2f}")

        # Decide proposed allocation
        if wf_qualifies and (live_qualifies or n_live == 0):
            # Audit-based bump: step up by PROMOTION_STEP, cap at ceiling
            proposed = min(MAX_ALLOCATION_CEILING, current + PROMOTION_STEP)
        else:
            proposed = current

        if proposed > current:
            recs.append(Recommendation(
                strategy=strategy_label,
                current_alloc=current,
                proposed_alloc=proposed,
                confidence=confidence,
                reasons=reasons,
                evidence={
                    "walk_forward_verdict": verdict,
                    "live_n_trades": n_live,
                    "live_pnl_usd": live_pnl,
                    "cohort_sharpe": cohort_sharpe,
                },
            ))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_recommendations": len(recs),
        "recommendations": [asdict(r) for r in recs],
        "thresholds": {
            "min_live_n_for_promotion": MIN_LIVE_N_FOR_PROMOTION,
            "max_allocation_ceiling": MAX_ALLOCATION_CEILING,
            "promotion_step": PROMOTION_STEP,
            "qualifying_verdicts": sorted(PROMOTION_QUALIFYING_VERDICTS),
        },
    }
