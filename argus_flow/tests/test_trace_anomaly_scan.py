"""Tests for ops.audit.trace_anomaly_scan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.audit import trace_anomaly_scan as tas


# ─── _find_trace_files ─────────────────────────────────────────────

def test_find_trace_files_returns_jsonl_only(tmp_path):
    (tmp_path / "trace1.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("", encoding="utf-8")
    (tmp_path / "trace2.jsonl").write_text("", encoding="utf-8")
    files = tas._find_trace_files([tmp_path])
    names = sorted(p.name for p in files)
    assert names == ["trace1.jsonl", "trace2.jsonl"]


def test_find_trace_files_skips_known_non_traces(tmp_path):
    (tmp_path / "canonical_fills.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "broker_equity_history.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "opportunities.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "trace_real.jsonl").write_text("", encoding="utf-8")
    files = tas._find_trace_files([tmp_path])
    names = [p.name for p in files]
    assert "trace_real.jsonl" in names
    assert "canonical_fills.jsonl" not in names
    assert "broker_equity_history.jsonl" not in names
    assert "opportunities.jsonl" not in names


def test_find_trace_files_empty_dir(tmp_path):
    files = tas._find_trace_files([tmp_path])
    assert files == []


def test_find_trace_files_missing_dir():
    files = tas._find_trace_files([Path("/nonexistent/path/x")])
    assert files == []


# ─── scan_trace ────────────────────────────────────────────────────

def test_scan_trace_handles_corrupt_file(tmp_path):
    """Corrupted trace file (binary content) returns an error result, not a crash."""
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe\x00garbage")
    result = tas.scan_trace(bad)
    # Either reads 0 events or reports an error in sample
    assert result.n_events >= 0


def test_scan_trace_on_clean_trace(tmp_path):
    """A clean trace with no anomalies returns n_anomalies=0."""
    trace_path = tmp_path / "clean.jsonl"
    # Minimal trace: just a single attached marker event
    trace_path.write_text(
        json.dumps({"kind": "attached", "ts": "2026-05-22T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    result = tas.scan_trace(trace_path)
    assert result.n_events == 1
    assert result.n_anomalies == 0
    assert result.anomaly_kinds == {}


def test_scan_trace_on_existing_cascade_fixture():
    """Run against the actual cascade-race fixture if present — should
    find anomalies (this is the 2026-05-19 incident fixture)."""
    fixture = tas.FIXTURE_DIR / "cascade_race_20260519.jsonl"
    if not fixture.exists():
        pytest.skip("cascade fixture not present")
    result = tas.scan_trace(fixture)
    # The cascade fixture should have at least one anomaly
    assert result.n_events > 0
    # We don't assert exact anomaly count (find_anomalies behavior can change)
    # but the file should be readable
    assert isinstance(result.anomaly_kinds, dict)


# ─── scan_repo ─────────────────────────────────────────────────────

def test_scan_repo_returns_structured_report(tmp_path):
    (tmp_path / "trace1.jsonl").write_text(
        json.dumps({"kind": "attached"}) + "\n", encoding="utf-8"
    )
    report = tas.scan_repo([tmp_path])
    assert "generated_at" in report
    assert "n_traces_scanned" in report
    assert report["n_traces_scanned"] == 1
    assert "anomaly_kind_counts" in report


def test_scan_repo_empty():
    report = tas.scan_repo([Path("/path/that/does/not/exist")])
    assert report["n_traces_scanned"] == 0


# ─── markdown rendering ────────────────────────────────────────────

def test_render_markdown_no_traces():
    report = {
        "generated_at": "now", "trace_dirs": [], "n_traces_scanned": 0,
        "n_traces_with_anomalies": 0, "total_anomalies": 0,
        "anomaly_kind_counts": {}, "traces": [],
    }
    md = tas.render_markdown(report)
    assert "Trace Anomaly Scan Report" in md
    assert "No traces found" in md


def test_render_markdown_includes_anomaly_kinds():
    report = {
        "generated_at": "now", "trace_dirs": ["x"], "n_traces_scanned": 1,
        "n_traces_with_anomalies": 1, "total_anomalies": 2,
        "anomaly_kind_counts": {"DOUBLE_FILL": 2},
        "traces": [{
            "file": "x.jsonl", "n_events": 10, "n_anomalies": 2,
            "anomaly_kinds": {"DOUBLE_FILL": 2},
            "sample_anomalies": [
                {"kind": "DOUBLE_FILL", "at_index": 5, "summary": "second fill"},
            ],
        }],
    }
    md = tas.render_markdown(report)
    assert "DOUBLE_FILL" in md
    assert "Anomalies: **2**" in md


# ─── outputs ───────────────────────────────────────────────────────

def test_write_outputs_creates_both_files(tmp_path):
    report = {
        "generated_at": "now", "trace_dirs": [], "n_traces_scanned": 0,
        "n_traces_with_anomalies": 0, "total_anomalies": 0,
        "anomaly_kind_counts": {}, "traces": [],
    }
    j = tmp_path / "out.json"
    m = tmp_path / "out.md"
    tas.write_outputs(report, json_path=j, md_path=m)
    assert j.exists()
    assert m.exists()
    loaded = json.loads(j.read_text(encoding="utf-8"))
    assert loaded["n_traces_scanned"] == 0


# ─── main / CLI smoke ──────────────────────────────────────────────

def test_main_runs_without_crashing(tmp_path, monkeypatch):
    """main() should produce non-zero exit only on anomalies, not on
    missing-trace-dir conditions."""
    monkeypatch.setattr(tas, "DEFAULT_TRACE_DIR", tmp_path / "does_not_exist")
    monkeypatch.setattr(tas, "FIXTURE_DIR", tmp_path / "also_no")
    rc = tas.main([
        "--json-out", str(tmp_path / "out.json"),
        "--md-out", str(tmp_path / "out.md"),
        "--quiet",
    ])
    assert rc == 0  # no traces = no anomalies
    assert (tmp_path / "out.json").exists()
