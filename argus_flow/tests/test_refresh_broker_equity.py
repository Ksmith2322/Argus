"""Tests for argus_flow.ops.refresh_broker_equity.

Verifies fail-open semantics (broker unreachable -> exit 0, cache untouched)
and the happy-path patch shape.
"""
from __future__ import annotations

import json
from pathlib import Path

from argus_flow.ops import refresh_broker_equity as rbe


def test_main_exits_0_when_broker_unreachable(monkeypatch):
    """fetch_net_liquidation_usd returning None must not error out the
    refresh_managed_truth chain."""
    monkeypatch.setattr(rbe, "fetch_net_liquidation_usd", lambda *a, **kw: None)
    assert rbe.main(["--quiet"]) == 0


def test_main_exits_0_when_equity_is_zero_or_negative(monkeypatch):
    """Non-positive equity should be refused (don't patch with garbage),
    but the chain still succeeds (fail-open)."""
    monkeypatch.setattr(rbe, "fetch_net_liquidation_usd", lambda *a, **kw: 0.0)
    assert rbe.main(["--quiet"]) == 0
    monkeypatch.setattr(rbe, "fetch_net_liquidation_usd", lambda *a, **kw: -1.0)
    assert rbe.main(["--quiet"]) == 0


def test_patch_report_writes_equity_and_marks_source(tmp_path):
    """patch_report must update account_equity_usd, preserve other
    broker_truth fields, prepend 'ibkr_direct' to sources, and add
    last_ibkr_refresh timestamp."""
    p = tmp_path / "risk_oversight_report.json"
    initial = {
        "timestamp": "2026-05-26T00:00:00Z",
        "broker_truth": {
            "account_equity_usd": 0.0,
            "fleet_open_risk_usd": 123.45,
            "sources": ["missing", "state"],
        },
        "other_field": "preserved",
    }
    p.write_text(json.dumps(initial), encoding="utf-8")

    assert rbe.patch_report(249_669.53, path=p) is True

    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["broker_truth"]["account_equity_usd"] == 249669.53
    assert after["broker_truth"]["fleet_open_risk_usd"] == 123.45  # preserved
    assert after["broker_truth"]["sources"][0] == "ibkr_direct"
    assert "missing" in after["broker_truth"]["sources"]  # original sources kept
    assert "last_ibkr_refresh" in after["broker_truth"]
    assert after["other_field"] == "preserved"
    assert after["timestamp"] == "2026-05-26T00:00:00Z"


def test_patch_report_returns_false_when_file_missing(tmp_path):
    p = tmp_path / "never_created.json"
    assert rbe.patch_report(100_000.0, path=p) is False


def test_patch_report_returns_false_when_file_corrupt(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not json", encoding="utf-8")
    assert rbe.patch_report(100_000.0, path=p) is False


def test_patch_report_creates_broker_truth_dict_if_missing(tmp_path):
    """If the file has no broker_truth key at all, patch should create it."""
    p = tmp_path / "no_bt.json"
    p.write_text(json.dumps({"other": "field"}), encoding="utf-8")
    assert rbe.patch_report(50_000.0, path=p) is True
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["broker_truth"]["account_equity_usd"] == 50000.0
    assert after["broker_truth"]["sources"] == ["ibkr_direct"]


def test_patch_report_does_not_duplicate_ibkr_direct_source(tmp_path):
    """If sources already contains 'ibkr_direct', don't add it again."""
    p = tmp_path / "already_marked.json"
    p.write_text(json.dumps({
        "broker_truth": {"account_equity_usd": 0, "sources": ["ibkr_direct", "old"]}
    }), encoding="utf-8")
    assert rbe.patch_report(60_000.0, path=p) is True
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["broker_truth"]["sources"].count("ibkr_direct") == 1
