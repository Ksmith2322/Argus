"""Hardcoded→computed confidence bridge.

Loads and validates per-strategy confidence artifacts from
`strategy_confidence/<label>.json`. The dashboard uses these to replace
hardcoded placeholder numbers when a backtest pipeline has produced a
validated artifact — but canonical_fills (live evidence) still takes
priority.

See `strategy_confidence/README.md` for the schema spec and writing rules.

Design:
  - Pydantic model enforces shape + three invariants (see docstring on
    `StrategyConfidenceArtifact.validate_invariants`).
  - Invalid files are **silently skipped** by the loader. A broken
    artifact must never crash the dashboard or surface a misleading row.
    The `_errors` dict captures validation failures for `/api/health`.
  - No writer logic here — writers live with each strategy and import
    this module to validate before writing.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = _REPO / "strategy_confidence"
SCHEMA_VERSION = 1

# Files prefixed "_" are reserved (README, _example.json, etc.) and never
# loaded as real artifacts, even if well-formed.
_RESERVED_PREFIX = "_"


EvidenceBar = Literal["insufficient", "sanity", "review", "promotion"]


# Kill-or-rework disposition. Surfaces a structured decision alongside the
# statistical evidence so a reader knows whether to promote, park, or scope
# a strategy down — regardless of the raw PF number.
#
# promote_candidate — positive evidence, advance toward paper/live
# paper_only        — positive evidence, hold at paper pending more sample
# research_only     — signal-only, no capital risk
# scope_down        — only a subset works; narrow the universe before promoting
# shelve            — pause further work, evidence insufficient or flawed
# kill              — evidence is negative or misleading, do not promote
DispositionStatus = Literal[
    "promote_candidate", "paper_only", "research_only",
    "scope_down", "shelve", "kill",
]


class Disposition(BaseModel):
    """Structured kill-or-rework decision. Optional — absent means the
    operator hasn't ruled yet and the statistical evidence stands alone."""
    model_config = ConfigDict(extra="forbid")
    status: DispositionStatus
    reason: str = Field(..., min_length=10)
    decided_at: str  # ISO-8601
    next_review_date: str | None = None


class Drawdown(BaseModel):
    """Real peak-to-trough drawdown on the actual trade sequence.

    Distinct from mc_stress.max_drawdown_usd which reports the WORST DD
    across 5000 random shuffles — this is the DD that actually happened
    on the real sequence. Relevant to the funding gate (DD <= 8% against
    anchor capital per project_funding_checklist.md).
    """
    model_config = ConfigDict(extra="forbid")
    max_drawdown_usd: float = Field(..., ge=0.0)
    max_drawdown_pct: float = Field(..., ge=0.0)
    peak_equity_usd: float
    trough_equity_usd: float
    pct_basis: Literal["starting_equity", "peak_relative"]


class CostStress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pf_1x: float | None = None
    pf_2x: float | None = None
    pf_3x: float | None = None


class WalkForward(BaseModel):
    """Sequential fold analysis. Populated by helio.fleet_state._walk_forward_stability.

    Extended 2026-04-19 to carry the richer output — stability_score and
    per-fold detail — without breaking existing consumers (all fields
    optional).
    """
    model_config = ConfigDict(extra="allow")
    folds: int | None = None
    stable_folds: int | None = None
    pf_per_fold: list[float] | None = None
    # New fields from _walk_forward_stability
    n_folds: int | None = None
    fold_size: int | None = None
    positive_folds: int | None = None
    stability_score: float | None = None
    all_folds_positive: bool | None = None


class CostStressExtended(BaseModel):
    """Richer cost-stress output from _cost_stress. Replaces the original
    CostStress incrementally — old CostStress model preserved for backwards
    compat."""
    model_config = ConfigDict(extra="forbid")
    cost_per_trade_usd: float
    pf_1x: float | None = None
    pf_2x: float | None = None
    pf_3x: float | None = None
    total_pnl_1x: float | None = None
    total_pnl_2x: float | None = None
    total_pnl_3x: float | None = None
    survives_2x: bool
    survives_3x: bool


class TopNSensitivity(BaseModel):
    """Output of _top_n_sensitivity — PF as top N winners are removed."""
    model_config = ConfigDict(extra="allow")
    pf_full: float | None = None
    pf_minus_top3: float | None = None
    still_positive_after_top3_removed: bool


class PerGroupProfitability(BaseModel):
    """Output of _per_group_profitability — per-bucket PF for
    per-instrument / per-hour / per-day gates."""
    model_config = ConfigDict(extra="allow")
    group_key: str
    n_groups: int
    positive_groups: int
    all_positive: bool


class HoldoutFreeze(BaseModel):
    """Prospective-holdout framing for a scope_down subset claim.

    Scope_down subsets (e.g., Tori "Dow+LONG only", Apollo "surprise 10-20%")
    are identified by scanning the data for which slice looks best. That's
    in-sample selection and inflates reported PF. Honest testing requires
    freezing the claim now and comparing against trades that land AFTER
    frozen_at. This block captures the frozen hypothesis so later sessions
    can compute an unbiased OOS read.

    Flow:
      1. At freeze time: populate in_sample_claim with the PF/WR/n/expectancy
         observed on the data used to identify the subset.
      2. Runner enforces the subset going forward (e.g., SCOPE_* flag).
      3. After oos_eval_earliest, helio.holdout_eval compares live/paper
         fills since frozen_at against in_sample_claim. Degradation > 30%
         is a red flag; absolute PF < 1.2 on OOS is a kill candidate.
    """
    model_config = ConfigDict(extra="forbid")
    frozen_at: str  # ISO-8601 UTC
    filter_statement: str  # Plain-English rule ("name=='Dow' AND direction=='LONG'")
    in_sample_claim: dict  # {pf, wr, n_trades, expectancy_usd, p_expectancy_positive}
    oos_eval_earliest: str  # ISO-8601 UTC — don't compare before this date
    oos_eval_target_n: int = Field(..., ge=10)  # min OOS trades for meaningful comparison
    degradation_alert_pct: float = Field(0.30, ge=0.0, le=1.0)  # e.g. 0.30 = 30% PF drop
    notes: str | None = None


class MonteCarloStressTest(BaseModel):
    """Output of helio.fleet_state._monte_carlo_shuffle.

    Surfaces in a confidence artifact as `mc_stress` (optional). Reading
    this field tells an operator whether the headline PF is driven by a
    handful of outlier trades or by broad profitability. Critical context
    for any strategy that shows `p_expectancy_positive >= 0.80`.
    """
    model_config = ConfigDict(extra="forbid")
    shuffles: int
    max_drawdown_usd: float
    pct_shuffles_profitable: float = Field(..., ge=0.0, le=1.0)
    ruin_fraction: float = Field(..., ge=0.0, le=1.0)
    top1_pct_of_total_pnl: float
    top3_pct_of_total_pnl: float
    top5_pct_of_total_pnl: float
    ex_top1_total_pnl_usd: float


class StrategyConfidenceArtifact(BaseModel):
    """Validated confidence record for one strategy.

    Invariants (enforced in `validate_invariants`):
      1. `bt_pf` must be a scalar float, not a range string.
      2. `p_expectancy_positive` must be None when n_total < 10.
      3. `p_expectancy_positive` must be None when source contains
         "hardcoded_estimate".

    Violating these rules raises ValidationError and the loader drops the
    artifact — the dashboard row falls back to hardcoded + badge.
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(..., description="must equal 1")
    strategy: str
    source: str
    generated_at: str  # ISO-8601 UTC; kept as string for forward-compat

    n_total: int = Field(..., ge=0)
    n_live: int = Field(..., ge=0)
    n_paper: int = Field(..., ge=0)
    evidence_bar: EvidenceBar

    # Backtest metadata — optional. When present, dashboard row's
    # bt_pf/bt_wr/bt_trades are populated from these values.
    bt_pf: float | None = None
    bt_wr: float | None = None
    bt_trades: int | None = Field(None, ge=0)

    expectancy_usd: float | None = None
    expectancy_r: float | None = None
    p_expectancy_positive: float | None = Field(None, ge=0.0, le=1.0)
    sample_warning: str | None = None

    cost_stress: CostStress | CostStressExtended | None = None
    walk_forward: WalkForward | None = None
    mc_stress: MonteCarloStressTest | None = None
    top_n_sensitivity: TopNSensitivity | None = None
    per_instrument: PerGroupProfitability | None = None
    per_day_of_week: PerGroupProfitability | None = None
    disposition: Disposition | None = None
    drawdown: Drawdown | None = None
    holdout_freeze: HoldoutFreeze | None = None

    @model_validator(mode="after")
    def validate_invariants(self) -> "StrategyConfidenceArtifact":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION}, got {self.schema_version}"
            )
        # Invariant 1: bt_pf is already typed as float | None by pydantic.
        # A string range like "1.1-1.3" fails type-coercion before we get
        # here — nothing to add in code, but documented for clarity.

        # Invariant 2: sample below sanity bar → no scalar confidence
        if self.n_total < 10 and self.p_expectancy_positive is not None:
            raise ValueError(
                f"p_expectancy_positive must be null when n_total < 10 "
                f"(got n_total={self.n_total}, p={self.p_expectancy_positive}); "
                f"bootstrap is not trustworthy below the sanity bar"
            )
        # Invariant 3: manual estimates cannot carry a confidence number
        if (self.source and "hardcoded_estimate" in self.source.lower()
                and self.p_expectancy_positive is not None):
            raise ValueError(
                "p_expectancy_positive must be null when source is a hardcoded_estimate"
            )
        # Consistency: n_live + n_paper == n_total
        if self.n_live + self.n_paper != self.n_total:
            raise ValueError(
                f"n_live + n_paper ({self.n_live}+{self.n_paper}) != n_total ({self.n_total})"
            )
        return self


def _artifact_path(label: str) -> Path:
    return ARTIFACT_DIR / f"{label}.json"


def load_confidence_artifact(label: str) -> StrategyConfidenceArtifact | None:
    """Load and validate one artifact. Returns None if:
      - filename is reserved (prefix `_`)
      - file does not exist
      - JSON is malformed
      - schema validation fails

    Never raises. A broken artifact is the dashboard's "absent" state,
    same as no file at all.
    """
    if label.startswith(_RESERVED_PREFIX):
        return None
    path = _artifact_path(label)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        artifact = StrategyConfidenceArtifact.model_validate(raw)
    except Exception:
        return None
    # Filename must match declared strategy — prevents accidentally
    # copying one strategy's artifact as another's.
    if artifact.strategy != label:
        return None
    return artifact


def audit_artifacts(labels: list[str]) -> dict:
    """Enumerate the state of every expected artifact. Used by /api/health
    to surface bridge health at a glance.

    Returns:
      {
        "total_expected": int,
        "present_valid": int,
        "present_invalid": int,
        "missing": int,
        "valid": [labels],
        "invalid": [{"label": ..., "error": "..."}],
        "absent": [labels],
      }
    """
    valid: list[str] = []
    invalid: list[dict] = []
    absent: list[str] = []
    for label in labels:
        if label.startswith(_RESERVED_PREFIX):
            continue
        path = _artifact_path(label)
        if not path.exists():
            absent.append(label)
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            invalid.append({"label": label, "error": f"parse: {type(e).__name__}: {e}"})
            continue
        try:
            art = StrategyConfidenceArtifact.model_validate(raw)
            if art.strategy != label:
                invalid.append({"label": label,
                                "error": f"filename/strategy mismatch: file={label} strategy={art.strategy}"})
                continue
            valid.append(label)
        except Exception as e:
            invalid.append({"label": label, "error": f"schema: {e}"})
    return {
        "total_expected": len([l for l in labels if not l.startswith(_RESERVED_PREFIX)]),
        "present_valid": len(valid),
        "present_invalid": len(invalid),
        "missing": len(absent),
        "valid": valid,
        "invalid": invalid,
        "absent": absent,
    }
