"""Tests for ops.trace_summary — multi-trace aggregator."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ops import trace_summary


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASCADE_RACE = FIXTURES / "cascade_race_20260519.jsonl"


@pytest.fixture
def populated_dir(tmp_path):
    """Three traces: one violations (cascade), one clean, one empty."""
    # Cascade race fixture has known invariant violations
    shutil.copy(CASCADE_RACE, tmp_path / "violations.jsonl")
    # Synthetic clean trace
    (tmp_path / "clean.jsonl").write_text(
        '{"event": "connected", "data": {}}\n'
        '{"event": "newOrder", "data": {"order_id": 1, "action": "BUY", "symbol": "USD"}}\n'
        '{"event": "orderStatus", "data": {"order_id": 1, "status": "Submitted"}}\n'
        '{"event": "execDetails", "data": {"order_id": 1, "shares": 1000, "price": 1.0, "exec_id": "ex1", "symbol": "USD"}}\n'
        '{"event": "orderStatus", "data": {"order_id": 1, "status": "Filled"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    return tmp_path


# ─── summarize_file ───────────────────────────────────────────────────────

def test_summarize_file_returns_violations_summary():
    s = trace_summary.summarize_file(CASCADE_RACE)
    assert s.status == "VIOLATIONS"
    assert s.invariant_violations > 0
    assert s.event_count == 9
    assert s.top_violation_kind in (
        "ORDER_LIFECYCLE_VALID", "NO_CANCEL_AFTER_FILL",
    )


def test_summarize_file_returns_clean_for_well_formed(populated_dir):
    s = trace_summary.summarize_file(populated_dir / "clean.jsonl")
    assert s.status == "CLEAN"
    assert s.invariant_violations == 0


def test_summarize_file_empty_reports_empty(populated_dir):
    s = trace_summary.summarize_file(populated_dir / "empty.jsonl")
    assert s.status == "EMPTY"
    assert s.event_count == 0


def test_summarize_file_error_path_reports_error(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not valid json\n", encoding="utf-8")
    s = trace_summary.summarize_file(bad)
    assert s.status == "ERROR"
    assert s.error


def test_summarize_file_records_top_anomaly_kind():
    s = trace_summary.summarize_file(CASCADE_RACE)
    # Cascade fixture has at least 1 STATUS_REGRESSION anomaly
    assert s.anomalies >= 1
    assert s.top_anomaly_kind == "STATUS_REGRESSION"


# ─── symbol filter ────────────────────────────────────────────────────────

def test_symbol_filter_keeps_matching_events():
    """Cascade fixture is CAD/JPY. Filter on 'CAD' keeps all relevant events."""
    s = trace_summary.summarize_file(CASCADE_RACE, symbol_filter="CAD")
    # All non-structural events have symbol=CAD; structural events
    # (connected) are kept. Total should equal original 9.
    assert s.event_count == 9


def test_symbol_filter_drops_non_matching_events():
    """Filter on 'EUR' drops all CAD-tagged events but keeps structural."""
    s = trace_summary.summarize_file(CASCADE_RACE, symbol_filter="EUR")
    # Only the structural events (connected = 1) survive
    assert s.event_count == 1
    assert s.invariant_violations == 0


def test_symbol_filter_case_insensitive():
    s_upper = trace_summary.summarize_file(CASCADE_RACE, symbol_filter="CAD")
    s_lower = trace_summary.summarize_file(CASCADE_RACE, symbol_filter="cad")
    assert s_upper.event_count == s_lower.event_count


# ─── summarize_directory ──────────────────────────────────────────────────

def test_directory_summary_finds_all_jsonl(populated_dir):
    summaries = trace_summary.summarize_directory(populated_dir)
    assert len(summaries) == 3


def test_directory_summary_skips_non_jsonl(populated_dir):
    (populated_dir / "ignored.txt").write_text("not a trace", encoding="utf-8")
    summaries = trace_summary.summarize_directory(populated_dir)
    assert len(summaries) == 3  # still 3 — .txt not included


def test_directory_summary_with_days_filter(populated_dir):
    """--days filter keeps files modified within window."""
    import os
    import time
    # Backdate one file by 30 days
    old_file = populated_dir / "clean.jsonl"
    old_mtime = time.time() - 30 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    # --days 7 should drop the backdated file
    summaries = trace_summary.summarize_directory(populated_dir, days=7)
    paths = [Path(s.path).name for s in summaries]
    assert "clean.jsonl" not in paths
    assert "violations.jsonl" in paths


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_cli_exits_1_when_any_trace_has_violations(populated_dir, capsys):
    rc = trace_summary.main(["--dir", str(populated_dir)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "VIOLATIONS" in captured.out
    assert "worst-status: VIOLATIONS" in captured.out


def test_cli_exits_0_when_all_clean(tmp_path, capsys):
    clean = tmp_path / "clean.jsonl"
    clean.write_text(
        '{"event": "newOrder", "data": {"order_id": 1, "action": "BUY"}}\n'
        '{"event": "execDetails", "data": {"order_id": 1, "shares": 100, "exec_id": "ex1"}}\n',
        encoding="utf-8",
    )
    rc = trace_summary.main(["--dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "worst-status: CLEAN" in captured.out


def test_cli_exits_2_on_missing_directory(capsys):
    rc = trace_summary.main(["--dir", "no_such_directory"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_cli_exits_2_when_no_jsonl_in_directory(tmp_path, capsys):
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    rc = trace_summary.main(["--dir", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "no .jsonl traces" in captured.err


def test_cli_json_mode_emits_valid_json(populated_dir, capsys):
    rc = trace_summary.main(["--dir", str(populated_dir), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["directory"] == str(populated_dir)
    assert len(parsed["summaries"]) == 3
    statuses = {s["status"] for s in parsed["summaries"]}
    assert "VIOLATIONS" in statuses
    assert "CLEAN" in statuses
    assert "EMPTY" in statuses
    assert rc == 1  # at least one VIOLATIONS


def test_cli_filter_symbol_flag_works(populated_dir, capsys):
    rc = trace_summary.main([
        "--dir", str(populated_dir), "--json", "--filter-symbol", "EUR",
    ])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    # With symbol filter EUR, cascade fixture's CAD events get dropped,
    # so violations.jsonl now reports CLEAN (only the connected event survives)
    by_name = {Path(s["path"]).name: s for s in parsed["summaries"]}
    assert by_name["violations.jsonl"]["status"] == "CLEAN"
    assert rc == 0  # nothing left with violations
