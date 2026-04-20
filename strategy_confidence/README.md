# strategy_confidence/

Signed confidence artifacts for strategies that do **not** have a live
canonical_fills stream. The dashboard's `/api/strategy_performance` prefers
these over its hardcoded placeholders, so shipping a file here replaces a
red `HARDCODED` badge with a green `CALC` badge.

## Priority order (sources of truth, highest first)

1. **Canonical fills** (`canonical_fills.jsonl`, per-strategy label).
   Computed in `helio.fleet_state._confidence_summary`. This is what
   Argus and GDX/GLD use today.
2. **Artifact in this directory** (`<label>.json`). Validated by
   `helio.strategy_confidence.load_confidence_artifact`. Picked up when
   a strategy has no canonical fills but does have a backtest pipeline
   output.
3. **Hardcoded literals** in `ops/dashboard.py`. Last-resort placeholder
   that renders with the `HARDCODED` badge.

Writing an artifact **does not** override canonical-fills confidence —
live evidence always wins. This is deliberate: a backtest number must
never outrank actual live trades.

## Schema (v1)

```json
{
  "schema_version": 1,
  "strategy": "mamba",
  "source": "backtest_walk_forward_20260418",
  "generated_at": "2026-04-18T23:30:00+00:00",
  "bt_pf": 0.68,
  "bt_wr": 0.16,
  "bt_trades": 128,
  "n_total": 128,
  "n_live": 0,
  "n_paper": 128,
  "expectancy_usd": -4.12,
  "expectancy_r": -0.08,
  "p_expectancy_positive": 0.08,
  "evidence_bar": "promotion",
  "sample_warning": "128 backfill, 0 live — historical only",
  "cost_stress": {"pf_1x": 0.68, "pf_2x": 0.41, "pf_3x": 0.19},
  "walk_forward": {"folds": 4, "stable_folds": 1, "pf_per_fold": [0.9, 0.4, 0.7, 0.5]}
}
```

### Required fields
- `schema_version` — must equal `1`
- `strategy` — label, must match the filename (minus `.json`)
- `source` — pipeline that wrote the artifact (name + date or git sha)
- `generated_at` — ISO-8601 UTC
- `n_total`, `n_live`, `n_paper` — integer sample sizes
- `evidence_bar` — one of `"insufficient" | "sanity" | "review" | "promotion"`

### Optional fields
- `bt_pf`, `bt_wr`, `bt_trades` — backtest metadata; when present, dashboard
  row columns are populated from these values instead of the hardcoded ones.
- `expectancy_usd`, `expectancy_r`
- `p_expectancy_positive` — bootstrap over the backtest pnls. See invariant below.
- `sample_warning` — short human-readable caveat. Surfaces as a ⚠ tooltip.
- `cost_stress`, `walk_forward` — richer provenance for later features.

### Invariants (enforced by the loader; a violating file is ignored)

1. **`bt_pf` must be a scalar float**, not a string range. If there's genuine
   uncertainty, use `walk_forward.pf_per_fold` or a CI field.
2. **`p_expectancy_positive` must be `null` when `n_total < 10`**. The 10/30/60
   evidence bars (see `project_strategy_gameplan_20260419.md`) say a sample
   under 10 is too small for the bootstrap to give a trustworthy number — no
   hasty writer may fake confidence with a scalar below that threshold.
3. **`p_expectancy_positive` must be `null` when `source` contains
   `"hardcoded_estimate"`**. This closes the lazy-writer loophole: you can
   declare a source as manual if you want, but you can't attach a confidence
   number to a manual estimate.

## Who writes these?

Per-strategy pipelines (planned: Tori first, then Mamba, then the rest).
Each writer lives with its strategy code (e.g. `forge/tori/confidence_writer.py`).
The writer must validate against this schema before writing and must not
overwrite an existing file with a newer `generated_at` unless the new
artifact is strictly more complete.
