"""Tests for helio.replay_harness — the bug-catching diff logic.

The replay harness's value is in the DIFF math, not the replay
itself (the latter is straightforward bar-walking). These tests
pin every diff outcome the audit can produce.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helio.replay_harness import (
    DiffMismatch,
    FillRow,
    ReplayEvent,
    _ts_to_dt,
    diff_against_canonical_fills,
    read_canonical_fills,
)


# ─── Timestamp parser ───────────────────────────────────────────────

def test_ts_to_dt_parses_iso_with_offset():
    dt = _ts_to_dt("2026-05-25T13:30:00+00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 25


def test_ts_to_dt_parses_iso_with_z():
    dt = _ts_to_dt("2026-05-25T13:30:00Z")
    assert dt is not None


def test_ts_to_dt_falls_back_to_date_only():
    dt = _ts_to_dt("2026-05-25")
    assert dt is not None
    assert dt.year == 2026


def test_ts_to_dt_returns_none_for_empty():
    assert _ts_to_dt("") is None


def test_ts_to_dt_returns_none_for_garbage():
    assert _ts_to_dt("not a date") is None


# ─── Canonical-fills reader ─────────────────────────────────────────

def _write_fills(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "canonical_fills.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_read_canonical_fills_handles_missing_file(tmp_path):
    assert read_canonical_fills(fills_path=tmp_path / "nope.jsonl") == []


def test_read_canonical_fills_filters_by_strategy(tmp_path):
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "A",
         "symbol": "SPY", "side": "ENTRY"},
        {"ts": "2026-05-25T14:00:00Z", "strategy": "B",
         "symbol": "QQQ", "side": "ENTRY"},
    ])
    rows = read_canonical_fills(strategy="A", fills_path=p)
    assert len(rows) == 1
    assert rows[0].ticker == "SPY"


# ─── Diff: matched pairs ───────────────────────────────────────────

def test_diff_no_mismatches_when_replay_matches_ledger(tmp_path):
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "X",
         "symbol": "SPY", "side": "ENTRY"},
    ])
    replay = [ReplayEvent(
        ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    assert result["n_mismatches"] == 0
    assert result["n_matched"] == 1


def test_diff_matches_within_time_tolerance(tmp_path):
    """Replay says SPY at 13:30, ledger says SPY at 14:00 (30 min off)
    -> matched because default tolerance is 24h."""
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T14:00:00Z", "strategy": "X",
         "symbol": "SPY", "side": "ENTRY"},
    ])
    replay = [ReplayEvent(
        ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    assert result["n_mismatches"] == 0


# ─── Diff: REPLAY_ONLY ──────────────────────────────────────────────

def test_diff_flags_replay_only_when_ledger_empty(tmp_path):
    p = _write_fills(tmp_path, [])
    replay = [ReplayEvent(
        ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    assert result["mismatch_counts"]["REPLAY_ONLY"] == 1
    assert result["n_matched"] == 0


def test_diff_replay_only_when_no_matching_fill(tmp_path):
    """Strategy says ENTRY SPY, ledger has ENTRY QQQ instead."""
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "X",
         "symbol": "QQQ", "side": "ENTRY"},
    ])
    replay = [ReplayEvent(
        ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    # SPY replay has no SPY in ledger -> REPLAY_ONLY
    # QQQ in ledger has no QQQ in replay -> LEDGER_ONLY
    assert result["mismatch_counts"]["REPLAY_ONLY"] == 1
    assert result["mismatch_counts"]["LEDGER_ONLY"] == 1


# ─── Diff: LEDGER_ONLY ──────────────────────────────────────────────

def test_diff_flags_ledger_only_when_replay_empty(tmp_path):
    """Worst-case bug: ledger has fills the strategy doesn't predict."""
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "X",
         "symbol": "SPY", "side": "ENTRY"},
    ])
    result = diff_against_canonical_fills(
        replay=[], strategy="X", fills_path=p,
    )
    assert result["mismatch_counts"]["LEDGER_ONLY"] == 1


# ─── Diff: TIME_DIVERGENT ───────────────────────────────────────────

def test_diff_flags_time_divergent(tmp_path):
    """Same ticker + side, but timestamps differ by >24h tolerance."""
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "X",
         "symbol": "SPY", "side": "ENTRY"},
    ])
    replay = [ReplayEvent(
        ts="2026-05-30T13:30:00Z",  # 5 days later
        side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    assert "TIME_DIVERGENT" in result["mismatch_counts"]


# ─── Diff: per-strategy filter ──────────────────────────────────────

def test_diff_only_compares_to_specified_strategy(tmp_path):
    """If we filter by strategy A, fills from strategy B don't pollute
    the diff."""
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "A",
         "symbol": "SPY", "side": "ENTRY"},
        {"ts": "2026-05-25T14:00:00Z", "strategy": "B",
         "symbol": "QQQ", "side": "ENTRY"},
    ])
    replay = [ReplayEvent(
        ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY",
    )]
    result = diff_against_canonical_fills(
        replay, strategy="A", fills_path=p,
    )
    assert result["n_mismatches"] == 0


# ─── Multiple events: ordered output ────────────────────────────────

def test_diff_handles_entry_exit_pair(tmp_path):
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "X",
         "symbol": "SPY", "side": "ENTRY"},
        {"ts": "2026-05-25T15:30:00Z", "strategy": "X",
         "symbol": "SPY", "side": "EXIT"},
    ])
    replay = [
        ReplayEvent(ts="2026-05-25T13:30:00Z", side="ENTRY", ticker="SPY"),
        ReplayEvent(ts="2026-05-25T15:30:00Z", side="EXIT", ticker="SPY"),
    ]
    result = diff_against_canonical_fills(
        replay, strategy="X", fills_path=p,
    )
    assert result["n_matched"] == 2
    assert result["n_mismatches"] == 0


# ─── CLI exit codes ─────────────────────────────────────────────────

def test_cli_returns_2_when_ledger_only(monkeypatch, tmp_path):
    """Real bug signal: fills exist that strategy doesn't predict."""
    # Build a synthetic ledger with one fill
    p = _write_fills(tmp_path, [
        {"ts": "2026-05-25T13:30:00Z", "strategy": "forge_gld_pm_long",
         "symbol": "GLD", "side": "ENTRY"},
    ])
    monkeypatch.setattr(
        "helio.replay_harness._canonical_fills_path", lambda: p
    )
    # Empty replay
    monkeypatch.setattr(
        "ops.audit.run_replay_audit._run_one",
        lambda strategy_label, period: {
            "strategy": strategy_label,
            "family": "gld_pm_long",
            "n_replay_events": 0,
            "n_ledger_fills": 1,
            "n_matched": 0,
            "n_mismatches": 1,
            "mismatch_counts": {"LEDGER_ONLY": 1},
            "mismatches": [],
        },
    )
    from ops.audit import run_replay_audit
    rc = run_replay_audit.main([
        "--strategy", "forge_gld_pm_long", "--json",
    ])
    assert rc == 2


def test_cli_returns_1_when_replay_only(monkeypatch, tmp_path):
    """Replay says fire but no ledger fill — review, not bug."""
    p = _write_fills(tmp_path, [])
    monkeypatch.setattr(
        "helio.replay_harness._canonical_fills_path", lambda: p
    )
    monkeypatch.setattr(
        "ops.audit.run_replay_audit._run_one",
        lambda strategy_label, period: {
            "strategy": strategy_label,
            "family": "gld_pm_long",
            "n_replay_events": 5,
            "n_ledger_fills": 0,
            "n_matched": 0,
            "n_mismatches": 5,
            "mismatch_counts": {"REPLAY_ONLY": 5},
            "mismatches": [],
        },
    )
    from ops.audit import run_replay_audit
    rc = run_replay_audit.main([
        "--strategy", "forge_gld_pm_long", "--json",
    ])
    assert rc == 1


def test_cli_returns_0_when_clean(monkeypatch, tmp_path):
    p = _write_fills(tmp_path, [])
    monkeypatch.setattr(
        "helio.replay_harness._canonical_fills_path", lambda: p
    )
    monkeypatch.setattr(
        "ops.audit.run_replay_audit._run_one",
        lambda strategy_label, period: {
            "strategy": strategy_label,
            "family": "gld_pm_long",
            "n_replay_events": 0,
            "n_ledger_fills": 0,
            "n_matched": 0,
            "n_mismatches": 0,
            "mismatch_counts": {},
            "mismatches": [],
        },
    )
    from ops.audit import run_replay_audit
    rc = run_replay_audit.main([
        "--strategy", "forge_gld_pm_long", "--json",
    ])
    assert rc == 0


# ─── Sanity pins ────────────────────────────────────────────────────

def test_replay_event_is_frozen_dataclass():
    e = ReplayEvent(ts="t", side="ENTRY", ticker="SPY")
    with pytest.raises(Exception):
        e.ticker = "QQQ"  # type: ignore


def test_fill_row_is_frozen_dataclass():
    f = FillRow(ts="t", side="ENTRY", ticker="SPY")
    with pytest.raises(Exception):
        f.ticker = "QQQ"  # type: ignore


def test_diff_mismatch_has_all_kinds():
    """Pin the mismatch-kind vocabulary so the dashboard/CLI can rely
    on the same names."""
    kinds = {"REPLAY_ONLY", "LEDGER_ONLY", "TICKER_DIVERGENT",
             "TIME_DIVERGENT"}
    # Just verify the dataclass accepts each
    for k in kinds:
        m = DiffMismatch(kind=k, detail="x")
        assert m.kind == k
