"""Shared domain objects — Trade, Signal, Fill.

Phase 1 of the restructure (per RESTRUCTURE_GUIDE_20260419.md) introduces a
typed vocabulary for the core trading concepts. Nothing in live code is
required to use these yet; they exist so that:

  1. Dashboard readers, reports, and tests can import one definition instead
     of re-inventing the dict shape.
  2. We can write round-trip tests that prove this shape round-trips with the
     existing canonical_fills.jsonl rows.
  3. Phase 2 migrations have a fixed target to migrate toward.

Design choices
--------------
  - Frozen dataclasses, not pydantic models: the existing canonical_fills
    tolerates mixed numeric/string prices ("159.73600" vs 159.736). Pydantic
    would want strict validation; we accept the looser contract for now.
  - Optional everywhere — the existing data has None for almost every field
    on at least one row. Strictness comes in Phase 2 when we coerce on write.
  - from_canonical_row / to_canonical_row are the canonical serializers. They
    mirror the shape written by helio/canonical_fills.py:write_fill().
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

Direction = Literal["long", "short", ""]
Side = Literal["ENTRY", "EXIT", ""]


def make_lineage_id(
    strategy: str,
    session_id: str | None,
    entry_order_id: str | None,
) -> str:
    """Compose a stable lineage identifier for an entry intent.

    Format: ``<strategy>.<session_id>.<entry_order_id>``. The session_id
    isolates within-process intents (a runner restart produces a new
    session and therefore a new lineage namespace); the entry_order_id
    discriminates concurrent intents within one session.

    Either part may be empty; the helper emits ``"-"`` placeholders so
    the format stays parseable. Callers that don't have a session yet
    (e.g., paper-only signals before a broker order id exists) can still
    produce a useful key via ``strategy + "" + ""`` = ``strategy.-.-``.
    """
    return f"{strategy}.{(session_id or '-')[:24]}.{(entry_order_id or '-')[:24]}"


def _coerce_float(v: Any) -> Optional[float]:
    """Tolerate 'price is a string like "159.736"' rows from historical CSVs."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Fill:
    """One closed-trade record in canonical_fills.jsonl.

    A Fill is the atomic reconciliation unit: each Fill must round-trip
    exactly with its source row in canonical_fills.jsonl so reconciliation
    can diff canonical against per-strategy trades.csv without field drift.
    """
    ts: Optional[str] = None                      # ISO datetime when logged
    strategy: str = ""
    symbol: str = ""
    direction: str = ""                           # "long" | "short" | ""
    side: str = ""                                # "ENTRY" | "EXIT"
    entry_ts: Optional[str] = None
    exit_ts: Optional[str] = None
    entry_px: Optional[float] = None
    exit_px: Optional[float] = None
    size: Optional[float] = None
    risk_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    exit_reason: Optional[str] = None
    broker_anchor_at_fill_usd: Optional[float] = None
    source: Optional[str] = None                  # "backfill_from_trade_csv" | None (live)
    # Codex audit 2026-05-18 X3+X7 prerequisite: a stable lineage identifier
    # so each broker fill can be attributed to a specific strategy intent,
    # not just a symbol. Format: "<strategy>.<session_id>.<entry_order_id>".
    # ENTRY rows compute lineage_id from their own order_id; matching EXIT
    # rows copy the ENTRY lineage_id. Enables intent-based reconciliation
    # (which strategy *intended* this position) rather than symbol-based
    # (which strategy *trades* this symbol).
    lineage_id: Optional[str] = None
    extra: Optional[dict] = None

    @classmethod
    def from_canonical_row(cls, row: dict) -> "Fill":
        """Parse one dict-shaped row from canonical_fills.jsonl.
        Tolerates string-typed prices from historical CSV backfills."""
        return cls(
            ts=row.get("ts"),
            strategy=row.get("strategy", ""),
            symbol=row.get("symbol", "") or "",
            direction=row.get("direction", "") or "",
            side=row.get("side", "") or "",
            entry_ts=row.get("entry_ts"),
            exit_ts=row.get("exit_ts"),
            entry_px=_coerce_float(row.get("entry_px")),
            exit_px=_coerce_float(row.get("exit_px")),
            size=_coerce_float(row.get("size")),
            risk_usd=_coerce_float(row.get("risk_usd")),
            pnl_usd=_coerce_float(row.get("pnl_usd")),
            exit_reason=row.get("exit_reason"),
            broker_anchor_at_fill_usd=_coerce_float(row.get("broker_anchor_at_fill_usd")),
            source=row.get("source"),
            lineage_id=row.get("lineage_id"),
            extra=row.get("extra"),
        )

    def to_canonical_row(self) -> dict:
        """Serialize back to the canonical_fills.jsonl shape.
        Drops None-valued 'extra', 'source', and 'lineage_id' for symmetry
        with write_fill() (older rows lack the field; writer omits it when
        unset to preserve dedup-key stability)."""
        d = asdict(self)
        if d.get("extra") is None:
            d.pop("extra", None)
        if d.get("source") is None:
            d.pop("source", None)
        if d.get("lineage_id") is None:
            d.pop("lineage_id", None)
        return d


@dataclass(frozen=True)
class Trade:
    """Per-strategy trade record — what lives in each strategy's trades.csv.

    This is a richer shape than Fill because strategies capture execution
    metadata (config_hash, session_id, atr_entry, stop_px, etc.) that isn't
    meaningful at the cross-strategy canonical layer.

    Phase 1 does not enforce anything — it's a shape contract. Phase 2
    migrates strategy runners to construct Trade and serialize via
    to_csv_row() instead of duplicating the dict literal.
    """
    ts: Optional[str] = None
    strategy: str = ""                            # label: "forge_gld_pm_long" etc.
    direction: str = ""
    entry_px: Optional[float] = None
    exit_px: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct_of_fleet: Optional[float] = None
    position_size: Optional[float] = None
    risk_usd: Optional[float] = None
    risk_pct_of_fleet: Optional[float] = None
    sizing_policy: Optional[str] = None
    entry_regime: Optional[str] = None
    experiment_valid: Optional[bool] = None
    invalid_reason: Optional[str] = None
    config_hash: Optional[str] = None
    session_id: Optional[str] = None
    git_sha: Optional[str] = None
    atr_entry: Optional[float] = None
    stop_px: Optional[float] = None
    target_px: Optional[float] = None
    exit_reason: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_csv_row(cls, row: dict, strategy_label: str = "") -> "Trade":
        """Parse one row from a per-strategy trades.csv. The strategy label
        comes from the file path, not the row itself (CSVs don't carry it)."""
        ev = row.get("experiment_valid")
        valid_bool = None
        if ev is not None and ev != "":
            valid_bool = str(ev).lower() == "true"
        return cls(
            ts=row.get("ts") or row.get("entry_ts") or row.get("entry_date"),
            strategy=strategy_label,
            direction=row.get("direction", "") or "",
            entry_px=_coerce_float(row.get("entry_px") or row.get("gdx_entry")),
            exit_px=_coerce_float(row.get("exit_px") or row.get("gdx_exit")),
            pnl_usd=_coerce_float(row.get("pnl_usd")),
            pnl_pct_of_fleet=_coerce_float(row.get("pnl_pct_of_fleet")),
            position_size=_coerce_float(row.get("position_size") or row.get("shares")),
            risk_usd=_coerce_float(row.get("risk_usd")),
            risk_pct_of_fleet=_coerce_float(row.get("risk_pct_of_fleet")),
            sizing_policy=row.get("sizing_policy"),
            entry_regime=row.get("entry_regime"),
            experiment_valid=valid_bool,
            invalid_reason=row.get("invalid_reason"),
            config_hash=row.get("config_hash"),
            session_id=row.get("session_id"),
            git_sha=row.get("git_sha"),
            atr_entry=_coerce_float(row.get("atr_entry")),
            stop_px=_coerce_float(row.get("stop_px")),
            target_px=_coerce_float(row.get("target_px")),
            exit_reason=row.get("exit_reason"),
        )


@dataclass(frozen=True)
class Signal:
    """A strategy's evaluation snapshot at one timestamp — what goes into
    signals.csv. Emitted every eval cycle whether or not a trade fired."""
    ts: Optional[str] = None
    strategy: str = ""
    action: str = ""                              # "ENTRY_LONG" | "ENTRY_SHORT" | "NO_TRIGGER" | ...
    features: dict = field(default_factory=dict)  # strategy-specific indicator values
    config_hash: Optional[str] = None
