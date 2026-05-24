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
    "forge_spy_mean_rev": "2026-04-30",
    "forge_multi_orb": "2026-05-07",
    "forge_vix_intraday": "2026-05-12",
    "forge_nq_london_close": "2026-05-13",
    # 2026-05-23 rigor sprint: both failed the disciplined gate at
    # REALISTIC slippage (PEAD CI lower 1.025 @ 40bps; NQ overnight
    # CI lower 0.526 @ 7bps). Source:
    # ops/audit/run_slippage_recalibration.py + commit ac45592.
    "forge_pead": "2026-05-23",
    "forge_nq_overnight": "2026-05-23",
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
