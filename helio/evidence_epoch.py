"""Evidence epoch registry (Codex audit 2026-05-18 X4).

Epoch == a named period of evidence accumulation. ROI / promotion / scorecard
consumers MUST identify the epoch they're computing against, so pre-reset
contaminated data cannot silently mix into post-reset promotion math.

Usage:

    from helio.evidence_epoch import current_epoch, get_epoch

    epoch = current_epoch()
    if not epoch.is_clean:
        # caller should refuse to use these stats for promotion decisions
        ...

    # ROI reports stamp their epoch:
    report = {"epoch_id": epoch.id, "computed_at": ..., "metrics": ...}

The registry is `argus_flow/configs/evidence_epoch.json`. The `epoch_reset`
maintenance script advances `current_epoch_id` when archiving pre-reset data.

This module performs no I/O beyond reading the registry JSON."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
EPOCH_REGISTRY_PATH = REPO / "argus_flow" / "configs" / "evidence_epoch.json"


@dataclass(frozen=True)
class EvidenceEpoch:
    """Frozen snapshot of an epoch entry. Comparable by id."""
    id: str
    started_at: datetime
    ends_at: Optional[datetime]
    label: str
    is_clean: bool
    contamination_notes: str
    exclude_strategies: tuple[str, ...]
    phantom_trades: tuple[str, ...]

    def excludes_strategy(self, strategy_label: str) -> bool:
        return strategy_label in self.exclude_strategies

    def contains(self, ts: datetime) -> bool:
        """True if `ts` falls within this epoch's window."""
        if ts < self.started_at:
            return False
        if self.ends_at is not None and ts >= self.ends_at:
            return False
        return True


class EpochRegistryError(RuntimeError):
    """Raised when the registry file is missing/unreadable. Callers that
    cannot proceed without an epoch should let this bubble up (fail closed)."""


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EpochRegistryError(f"invalid datetime: {value!r}") from exc
    # Normalise to tz-aware UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_epoch(row: dict) -> EvidenceEpoch:
    return EvidenceEpoch(
        id=str(row["id"]),
        started_at=_parse_dt(row["started_at"]) or datetime.fromtimestamp(0, tz=timezone.utc),
        ends_at=_parse_dt(row.get("ends_at")),
        label=str(row.get("label", "")),
        is_clean=bool(row.get("is_clean", False)),
        contamination_notes=str(row.get("contamination_notes", "")),
        exclude_strategies=tuple(row.get("exclude_strategies", []) or []),
        phantom_trades=tuple(row.get("phantom_trades", []) or []),
    )


def load_registry(path: Path | str | None = None) -> dict:
    """Load the raw registry JSON. Raises EpochRegistryError on missing/invalid."""
    p = Path(path) if path is not None else EPOCH_REGISTRY_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EpochRegistryError(
            f"evidence_epoch registry missing at {p}. Promotion-grade "
            f"reports require a declared epoch — refusing to proceed."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EpochRegistryError(f"registry unreadable at {p}: {exc}") from exc


def list_epochs(path: Path | str | None = None) -> list[EvidenceEpoch]:
    data = load_registry(path)
    rows = data.get("epochs") or []
    if not isinstance(rows, list):
        raise EpochRegistryError("registry.epochs must be a list")
    return [_row_to_epoch(r) for r in rows]


def get_epoch(epoch_id: str, path: Path | str | None = None) -> EvidenceEpoch:
    """Return the named epoch. Raises EpochRegistryError if not found."""
    for ep in list_epochs(path):
        if ep.id == epoch_id:
            return ep
    raise EpochRegistryError(f"no epoch with id={epoch_id!r}")


def current_epoch(path: Path | str | None = None) -> EvidenceEpoch:
    """Return the epoch currently in effect per `current_epoch_id`.

    Callers that produce ROI / promotion / scorecard reports should stamp
    `epoch.id` onto every output so downstream readers know which window
    the numbers came from."""
    data = load_registry(path)
    cur = data.get("current_epoch_id")
    if not cur:
        raise EpochRegistryError("registry.current_epoch_id is unset")
    return get_epoch(str(cur), path)


def epoch_for_timestamp(
    ts: datetime, path: Path | str | None = None,
) -> Optional[EvidenceEpoch]:
    """Return the epoch whose window contains `ts`, or None if no epoch
    covers it. Useful for reconciling historical rows against the registry."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    for ep in list_epochs(path):
        if ep.contains(ts):
            return ep
    return None


def stamp_report(report: dict, epoch: EvidenceEpoch | None = None) -> dict:
    """Add `epoch_id`, `epoch_label`, `epoch_is_clean` fields to a report
    dict in-place. Returns the same dict for chaining. The 5/31 readiness
    gate inspects these fields; reports without them are treated as
    unstamped and refused."""
    ep = epoch or current_epoch()
    report["epoch_id"] = ep.id
    report["epoch_label"] = ep.label
    report["epoch_is_clean"] = ep.is_clean
    return report
