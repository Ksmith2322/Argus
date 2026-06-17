"""Lineage ID stamping on canonical_fills (Codex audit 2026-05-18 X3+X7).

A lineage id (`<strategy>.<session_id>.<entry_order_id>`) lets the fleet
ledger attribute fills by *intent*, not symbol. The 5/12 UVXY orphan was
hard to attribute because broker truth said "long 283 UVXY" — but which
strategy intended that position? Intent-based ownership is the X3/X7
foundation; this test set verifies the format + that the Fill model
round-trips lineage_id correctly."""
from __future__ import annotations

from pathlib import Path

from helio.domain import Fill, make_lineage_id


# ── make_lineage_id format ─────────────────────────────────────────────────

def test_make_lineage_id_format():
    lid = make_lineage_id(
        strategy="forge_gld_pm_long",
        session_id="c102p12345",
        entry_order_id="987654",
    )
    assert lid == "forge_gld_pm_long.c102p12345.987654"


def test_make_lineage_id_session_missing_uses_dash():
    lid = make_lineage_id(strategy="argus_gbpusd", session_id=None, entry_order_id="42")
    assert lid == "argus_gbpusd.-.42"


def test_make_lineage_id_order_missing_uses_dash():
    """Paper-only signals before a broker order id exists still produce a
    valid key (lineage_id can be filled in later when the order materializes
    via reconciliation; the placeholder marks intent without a broker anchor)."""
    lid = make_lineage_id(strategy="argus_usdjpy", session_id="c12p999", entry_order_id=None)
    assert lid == "argus_usdjpy.c12p999.-"


def test_make_lineage_id_truncates_long_components():
    """Defensive: don't let a runaway order id explode the lineage column."""
    huge = "x" * 200
    lid = make_lineage_id("s", session_id=huge, entry_order_id=huge)
    parts = lid.split(".")
    assert len(parts[1]) <= 24
    assert len(parts[2]) <= 24


# ── Fill round-trip ────────────────────────────────────────────────────────

def test_fill_round_trip_preserves_lineage_id():
    f = Fill(
        strategy="forge_vix_intraday",
        symbol="UVXY",
        direction="long",
        side="ENTRY",
        entry_px=37.50,
        size=10,
        lineage_id="forge_vix_intraday.c103p5555.123456",
    )
    row = f.to_canonical_row()
    assert row["lineage_id"] == "forge_vix_intraday.c103p5555.123456"
    f2 = Fill.from_canonical_row(row)
    assert f2.lineage_id == f.lineage_id


def test_fill_omits_lineage_id_when_none_for_dedup_stability():
    """Older rows (pre-lineage) don't have the field. The writer omits it
    when None so the dedup-key computation in
    helio.canonical_fills._backfill_dedup_key stays stable with historical
    backfill rows that also lack the field."""
    f = Fill(strategy="x", symbol="Y", direction="long", side="EXIT")
    row = f.to_canonical_row()
    assert "lineage_id" not in row, (
        "Fill.to_canonical_row() must drop lineage_id when None; older "
        "canonical_fills rows lack the field and adding it on serialize "
        "would silently inflate dedup-key shape."
    )


def test_fill_from_row_missing_lineage_id_gets_none():
    """Reading an older row (no lineage_id field) must yield None, not
    raise / not produce a placeholder."""
    row = {
        "strategy": "x", "symbol": "Y", "direction": "long",
        "side": "EXIT", "pnl_usd": 1.0,
    }
    f = Fill.from_canonical_row(row)
    assert f.lineage_id is None


# ── Writer integration (source-level) ──────────────────────────────────────
# Verify that the live ENTRY-write callsites stamp lineage_id. Catches the
# regression "someone removed the lineage stamp."

REPO = Path(__file__).resolve().parents[2]


def test_submit_bracket_entry_write_stamps_lineage_id():
    src = (REPO / "helio" / "ibkr_execution.py").read_text(encoding="utf-8")
    # Look for the ENTRY-side dual-write region.
    idx = src.find("ENTRY FILLED")
    assert idx >= 0, "ENTRY-side dual-write region missing in submit_bracket"
    tail = src[idx:idx + 2500]
    assert "make_lineage_id" in tail, (
        "submit_bracket ENTRY-side dual-write is no longer stamping a "
        "lineage_id. Re-add the make_lineage_id() call so canonical_fills "
        "rows can be attributed by intent (Codex X3)."
    )
    assert "lineage_id=lineage" in tail or "lineage_id=" in tail, (
        "submit_bracket ENTRY Fill no longer passes lineage_id."
    )


def test_runner_unified_entry_write_stamps_lineage_id():
    src = (REPO / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    # The ENTRY-side dual-write is right after the "ENTRY FILLED" log line in _on_fill.
    idx = src.find("ENTRY FILLED")
    assert idx >= 0, "_on_fill ENTRY-side dual-write region missing"
    tail = src[idx:idx + 2500]
    assert "make_lineage_id" in tail, (
        "runner_unified ENTRY-side dual-write no longer stamps a lineage_id. "
        "Re-add the make_lineage_id() call so canonical_fills attribution "
        "stays intent-based."
    )
