"""Per-lineage order lifecycle audit (Codex X7).

Reconstructs the full life of each strategy intent from the canonical
fill ledger:

    ENTRY fill (size, side, ts) -> EXIT fill (size, side, ts, pnl)

grouped by ``lineage_id`` (``<strategy>.<session_id>.<entry_order_id>``).
Surfaces:

    COMPLETE       — 1 ENTRY + 1 EXIT, side opposite, sizes match
    ORPHAN_ENTRY   — ENTRY with no matching EXIT (still open OR lost)
    ORPHAN_EXIT    — EXIT with no matching ENTRY (broker fill leaked
                     in without a prior intent — INVESTIGATE)
    DUPLICATE_ENTRY — same lineage_id has >1 ENTRY rows
    PARTIAL_EXIT   — ENTRY size != sum of EXIT sizes
    LINEAGE_MISSING — fill has no lineage_id (legacy rows, can't
                     reconcile)

The lineage_id-stamping was wired into canonical_fills at commit
1d25487 (helio/ibkr_execution + argus_flow runner_unified). Rows
prior to that show up under ``LINEAGE_MISSING`` and are operationally
fine for ROI math — they just can't be reconciled at the lifecycle
level.

USAGE
=====
    from helio.order_lifecycle import (
        build_lifecycle_table, reconcile_all,
    )
    table = build_lifecycle_table()       # dict[lineage_id, Lifecycle]
    summary = reconcile_all()             # aggregate verdict counts

Each Lifecycle is a frozen dataclass; ``to_dict()`` for JSON
serialization.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from helio.domain import Fill


_REPO = Path(__file__).resolve().parents[1]


LIFECYCLE_VERDICTS = (
    "COMPLETE",
    "ORPHAN_ENTRY",
    "ORPHAN_EXIT",
    "DUPLICATE_ENTRY",
    "PARTIAL_EXIT",
    "LINEAGE_MISSING",
)


@dataclass(frozen=True)
class Lifecycle:
    """One intent's full lifecycle."""
    lineage_id: str
    strategy: str
    verdict: str
    entries: tuple[Fill, ...] = field(default_factory=tuple)
    exits: tuple[Fill, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_entry_size(self) -> float:
        return sum((f.size or 0.0) for f in self.entries)

    @property
    def total_exit_size(self) -> float:
        return sum((f.size or 0.0) for f in self.exits)

    @property
    def realized_pnl(self) -> float:
        return sum((f.pnl_usd or 0.0) for f in self.exits)

    def to_dict(self) -> dict:
        return {
            "lineage_id": self.lineage_id,
            "strategy": self.strategy,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "n_entries": len(self.entries),
            "n_exits": len(self.exits),
            "total_entry_size": self.total_entry_size,
            "total_exit_size": self.total_exit_size,
            "realized_pnl_usd": round(self.realized_pnl, 2),
            "first_entry_ts": self.entries[0].ts if self.entries else None,
            "last_exit_ts": self.exits[-1].ts if self.exits else None,
        }


def _canonical_fills_path() -> Path:
    return _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"


def _iter_fills_from_path(path: Path) -> Iterable[Fill]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield Fill.from_canonical_row(row)


def _group_by_lineage(
    fills: Iterable[Fill],
) -> tuple[dict[str, list[Fill]], list[Fill]]:
    """Returns (by_lineage, missing) — fills with no lineage_id go
    into the second bucket."""
    by_lineage: dict[str, list[Fill]] = {}
    missing: list[Fill] = []
    for f in fills:
        if not f.lineage_id:
            missing.append(f)
            continue
        by_lineage.setdefault(f.lineage_id, []).append(f)
    return by_lineage, missing


def _classify_lineage(lineage_id: str, fills: list[Fill]) -> Lifecycle:
    """Apply the verdict rules to one lineage's fill list."""
    entries = [f for f in fills if (f.side or "").upper() == "ENTRY"]
    exits = [f for f in fills if (f.side or "").upper() == "EXIT"]
    # Sort by ts (best-effort: ts may be None for some legacy rows)
    entries.sort(key=lambda f: f.ts or "")
    exits.sort(key=lambda f: f.ts or "")

    # Strategy is taken from the first ENTRY (most authoritative); fall
    # back to first EXIT if no ENTRY present.
    strategy = (entries[0].strategy if entries
                else (exits[0].strategy if exits else ""))

    reasons: list[str] = []
    if not entries and exits:
        verdict = "ORPHAN_EXIT"
        reasons.append(f"{len(exits)} exit(s) with no matching entry")
    elif entries and not exits:
        verdict = "ORPHAN_ENTRY"
        reasons.append(f"{len(entries)} entry(s) with no matching exit "
                       "(position may still be open)")
    elif len(entries) > 1:
        verdict = "DUPLICATE_ENTRY"
        reasons.append(f"{len(entries)} entry rows under one lineage")
    else:
        # 1 ENTRY + ≥1 EXIT
        entry_size = abs(entries[0].size or 0.0)
        exit_size = sum(abs(f.size or 0.0) for f in exits)
        if entry_size > 0 and abs(entry_size - exit_size) > 1e-6:
            verdict = "PARTIAL_EXIT"
            reasons.append(
                f"entry size {entry_size} != sum of exit sizes {exit_size}"
            )
        else:
            verdict = "COMPLETE"

    return Lifecycle(
        lineage_id=lineage_id,
        strategy=strategy,
        verdict=verdict,
        entries=tuple(entries),
        exits=tuple(exits),
        reasons=tuple(reasons),
    )


def build_lifecycle_table(
    fills_path: Path | None = None,
) -> dict[str, Lifecycle]:
    """Reconstruct one Lifecycle per lineage_id from the canonical
    fill ledger. Fills without a lineage_id are reported under the
    synthetic key ``__legacy__`` with verdict ``LINEAGE_MISSING``."""
    path = fills_path or _canonical_fills_path()
    fills = list(_iter_fills_from_path(path))
    by_lineage, missing = _group_by_lineage(fills)
    table: dict[str, Lifecycle] = {}
    for lineage_id, lineage_fills in by_lineage.items():
        table[lineage_id] = _classify_lineage(lineage_id, lineage_fills)
    if missing:
        strategy = missing[0].strategy or "?"
        table["__legacy__"] = Lifecycle(
            lineage_id="__legacy__",
            strategy=strategy,
            verdict="LINEAGE_MISSING",
            entries=tuple(f for f in missing if (f.side or "").upper() == "ENTRY"),
            exits=tuple(f for f in missing if (f.side or "").upper() == "EXIT"),
            reasons=(
                f"{len(missing)} fill(s) pre-date the lineage_id "
                "feature (commit 1d25487); cannot reconcile",
            ),
        )
    return table


def reconcile_all(fills_path: Path | None = None) -> dict:
    """Build the lifecycle table and produce an aggregate summary:
        - verdict_counts
        - per_strategy counts
        - lifecycles (full list, ordered by verdict severity)
    """
    table = build_lifecycle_table(fills_path=fills_path)
    verdict_counts: dict[str, int] = {v: 0 for v in LIFECYCLE_VERDICTS}
    per_strategy: dict[str, dict[str, int]] = {}
    for lc in table.values():
        verdict_counts[lc.verdict] = verdict_counts.get(lc.verdict, 0) + 1
        bucket = per_strategy.setdefault(
            lc.strategy or "?",
            {v: 0 for v in LIFECYCLE_VERDICTS},
        )
        bucket[lc.verdict] = bucket.get(lc.verdict, 0) + 1

    severity_order = {
        "ORPHAN_EXIT": 0, "DUPLICATE_ENTRY": 1, "PARTIAL_EXIT": 2,
        "ORPHAN_ENTRY": 3, "LINEAGE_MISSING": 4, "COMPLETE": 5,
    }
    lifecycles = sorted(
        (lc.to_dict() for lc in table.values()),
        key=lambda d: (severity_order.get(d["verdict"], 99),
                       d.get("lineage_id", "")),
    )
    return {
        "n_lineages": len(table),
        "verdict_counts": verdict_counts,
        "per_strategy": per_strategy,
        "lifecycles": lifecycles,
    }
