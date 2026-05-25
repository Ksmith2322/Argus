"""Tests for helio.order_lifecycle (Codex X7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.order_lifecycle import (
    LIFECYCLE_VERDICTS,
    Lifecycle,
    build_lifecycle_table,
    reconcile_all,
)


# ─── Fixture: synthetic canonical_fills.jsonl ────────────────────────

def _write_fills(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "canonical_fills.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _entry(lineage_id, strategy="forge_x", size=100.0, ts="2026-05-26T13:30:00Z"):
    return {
        "ts": ts, "side": "ENTRY", "strategy": strategy,
        "symbol": "SPY", "direction": "long",
        "size": size, "entry_px": 500.0, "entry_ts": ts,
        "lineage_id": lineage_id,
    }


def _exit(lineage_id, strategy="forge_x", size=100.0, pnl=10.0,
          ts="2026-05-26T15:30:00Z"):
    return {
        "ts": ts, "side": "EXIT", "strategy": strategy,
        "symbol": "SPY", "direction": "long",
        "size": size, "exit_px": 501.0, "exit_ts": ts,
        "pnl_usd": pnl, "lineage_id": lineage_id,
    }


# ─── COMPLETE ────────────────────────────────────────────────────────

def test_complete_lifecycle(tmp_path):
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.1"),
        _exit("forge_x.s1.1"),
    ])
    table = build_lifecycle_table(fills_path=path)
    assert len(table) == 1
    lc = table["forge_x.s1.1"]
    assert lc.verdict == "COMPLETE"
    assert lc.realized_pnl == 10.0
    assert lc.total_entry_size == 100.0
    assert lc.total_exit_size == 100.0


# ─── ORPHAN_ENTRY ────────────────────────────────────────────────────

def test_orphan_entry_position_still_open(tmp_path):
    path = _write_fills(tmp_path, [_entry("forge_x.s1.2")])
    table = build_lifecycle_table(fills_path=path)
    lc = table["forge_x.s1.2"]
    assert lc.verdict == "ORPHAN_ENTRY"
    assert "may still be open" in (lc.reasons[0] if lc.reasons else "")


# ─── ORPHAN_EXIT ─────────────────────────────────────────────────────

def test_orphan_exit_is_a_real_bug(tmp_path):
    """An EXIT without a matching ENTRY means we either lost the ENTRY
    fill (data integrity bug) or the broker filled an order we didn't
    intend (real-money disaster). Either way, audit-worthy."""
    path = _write_fills(tmp_path, [_exit("forge_x.s1.3")])
    table = build_lifecycle_table(fills_path=path)
    lc = table["forge_x.s1.3"]
    assert lc.verdict == "ORPHAN_EXIT"


# ─── DUPLICATE_ENTRY ─────────────────────────────────────────────────

def test_duplicate_entry_triggers_alarm(tmp_path):
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.4", ts="2026-05-26T13:30:00Z"),
        _entry("forge_x.s1.4", ts="2026-05-26T13:31:00Z"),
        _exit("forge_x.s1.4"),
    ])
    table = build_lifecycle_table(fills_path=path)
    lc = table["forge_x.s1.4"]
    assert lc.verdict == "DUPLICATE_ENTRY"


# ─── PARTIAL_EXIT ────────────────────────────────────────────────────

def test_partial_exit_when_exit_size_smaller(tmp_path):
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.5", size=100.0),
        _exit("forge_x.s1.5", size=60.0),  # only sold 60 of 100
    ])
    table = build_lifecycle_table(fills_path=path)
    lc = table["forge_x.s1.5"]
    assert lc.verdict == "PARTIAL_EXIT"


def test_partial_exit_summed_across_multiple_exits_recognized_complete(tmp_path):
    """Two partial exits that sum to the entry size = COMPLETE."""
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.6", size=100.0),
        _exit("forge_x.s1.6", size=40.0, ts="2026-05-26T14:00:00Z"),
        _exit("forge_x.s1.6", size=60.0, ts="2026-05-26T15:00:00Z"),
    ])
    table = build_lifecycle_table(fills_path=path)
    lc = table["forge_x.s1.6"]
    assert lc.verdict == "COMPLETE"
    assert lc.total_exit_size == 100.0
    assert lc.realized_pnl == 20.0


# ─── LINEAGE_MISSING (legacy rows) ──────────────────────────────────

def test_legacy_rows_bucket_into_lineage_missing(tmp_path):
    row_no_lineage = _entry("forge_x.s1.7")
    row_no_lineage.pop("lineage_id")
    path = _write_fills(tmp_path, [row_no_lineage])
    table = build_lifecycle_table(fills_path=path)
    assert "__legacy__" in table
    assert table["__legacy__"].verdict == "LINEAGE_MISSING"


def test_legacy_and_lineaged_rows_coexist(tmp_path):
    legacy_row = _entry("ignored")
    legacy_row.pop("lineage_id")
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.8"),
        _exit("forge_x.s1.8"),
        legacy_row,
    ])
    table = build_lifecycle_table(fills_path=path)
    assert table["forge_x.s1.8"].verdict == "COMPLETE"
    assert table["__legacy__"].verdict == "LINEAGE_MISSING"


# ─── Empty / missing files ───────────────────────────────────────────

def test_empty_canonical_fills_yields_empty_table(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert build_lifecycle_table(fills_path=path) == {}


def test_missing_canonical_fills_yields_empty_table(tmp_path):
    path = tmp_path / "nonexistent.jsonl"
    assert build_lifecycle_table(fills_path=path) == {}


# ─── reconcile_all aggregate ─────────────────────────────────────────

def test_reconcile_all_returns_verdict_counts_and_per_strategy(tmp_path):
    path = _write_fills(tmp_path, [
        _entry("forge_a.s1.1", strategy="forge_a"),
        _exit("forge_a.s1.1", strategy="forge_a"),
        _entry("forge_b.s1.1", strategy="forge_b"),  # orphan
    ])
    summary = reconcile_all(fills_path=path)
    assert summary["n_lineages"] == 2
    assert summary["verdict_counts"]["COMPLETE"] == 1
    assert summary["verdict_counts"]["ORPHAN_ENTRY"] == 1
    assert summary["per_strategy"]["forge_a"]["COMPLETE"] == 1
    assert summary["per_strategy"]["forge_b"]["ORPHAN_ENTRY"] == 1


def test_reconcile_orders_anomalies_first(tmp_path):
    """The lifecycles list must come back with most-severe anomalies
    first — operator scanning the report sees real bugs before clean
    rows."""
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.complete"),
        _exit("forge_x.s1.complete"),
        _exit("forge_x.s1.orphanex"),  # orphan exit = highest severity
        _entry("forge_x.s1.orphanen"),  # orphan entry
    ])
    summary = reconcile_all(fills_path=path)
    verdicts = [lc["verdict"] for lc in summary["lifecycles"]]
    assert verdicts[0] == "ORPHAN_EXIT"
    assert verdicts[-1] == "COMPLETE"


# ─── Sanity-pins ────────────────────────────────────────────────────

def test_lifecycle_verdicts_constant_includes_all_states():
    assert "COMPLETE" in LIFECYCLE_VERDICTS
    assert "ORPHAN_ENTRY" in LIFECYCLE_VERDICTS
    assert "ORPHAN_EXIT" in LIFECYCLE_VERDICTS
    assert "DUPLICATE_ENTRY" in LIFECYCLE_VERDICTS
    assert "PARTIAL_EXIT" in LIFECYCLE_VERDICTS
    assert "LINEAGE_MISSING" in LIFECYCLE_VERDICTS


def test_lifecycle_to_dict_is_serializable():
    """JSON-serializable so the dashboard endpoint can return it."""
    lc = Lifecycle(
        lineage_id="forge_x.s.1",
        strategy="forge_x",
        verdict="COMPLETE",
    )
    d = lc.to_dict()
    json.dumps(d)  # must not raise


def test_run_order_lifecycle_cli_returns_exit_2_on_orphan_exit(tmp_path, monkeypatch):
    """Wire-up test: CLI must propagate severity into exit code."""
    path = _write_fills(tmp_path, [_exit("forge_x.s1.1")])
    monkeypatch.setattr(
        "helio.order_lifecycle._canonical_fills_path", lambda: path
    )
    from ops.audit import run_order_lifecycle as r
    rc = r.main(["--json"])
    assert rc == 2


def test_run_order_lifecycle_cli_returns_exit_1_on_orphan_entry(tmp_path, monkeypatch):
    path = _write_fills(tmp_path, [_entry("forge_x.s1.1")])
    monkeypatch.setattr(
        "helio.order_lifecycle._canonical_fills_path", lambda: path
    )
    from ops.audit import run_order_lifecycle as r
    rc = r.main(["--json"])
    assert rc == 1


def test_run_order_lifecycle_cli_returns_exit_0_when_clean(tmp_path, monkeypatch):
    path = _write_fills(tmp_path, [
        _entry("forge_x.s1.1"), _exit("forge_x.s1.1"),
    ])
    monkeypatch.setattr(
        "helio.order_lifecycle._canonical_fills_path", lambda: path
    )
    from ops.audit import run_order_lifecycle as r
    rc = r.main(["--json"])
    assert rc == 0
