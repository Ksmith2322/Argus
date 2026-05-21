"""Tests for ops.preflight_paper_stress — verifies each check fires
correctly when its precondition is met or violated."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import preflight_paper_stress as pp


@pytest.fixture
def good_env(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    monkeypatch.delenv("GOLDEN_TRACE_PATH", raising=False)
    from helio import real_money
    monkeypatch.setattr(real_money, "REAL_MONEY_ENABLED", False)


# ─── env checks ───────────────────────────────────────────────────────────

def test_env_check_passes_with_paper_port(good_env):
    report = pp.PreflightReport()
    pp._check_env(report)
    failures = [c for c in report.checks if not c.passed]
    assert not failures, [c.name for c in failures]


def test_env_check_fails_when_port_wrong(good_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7496")
    report = pp.PreflightReport()
    pp._check_env(report)
    port_check = next(c for c in report.checks if "IBKR_PORT" in c.name)
    assert not port_check.passed


def test_env_check_fails_when_real_money_set(good_env, monkeypatch):
    monkeypatch.setenv("REAL_MONEY_ENABLED", "1")
    report = pp.PreflightReport()
    pp._check_env(report)
    rm_check = next(c for c in report.checks if "REAL_MONEY_ENABLED env" in c.name)
    assert not rm_check.passed


def test_env_check_fails_when_helio_module_enabled(good_env, monkeypatch):
    from helio import real_money
    monkeypatch.setattr(real_money, "REAL_MONEY_ENABLED", True)
    report = pp.PreflightReport()
    pp._check_env(report)
    mod_check = next(c for c in report.checks if "helio.real_money" in c.name)
    assert not mod_check.passed


# ─── imports ──────────────────────────────────────────────────────────────

def test_all_phase_modules_importable():
    report = pp.PreflightReport()
    pp._check_imports(report)
    failures = [c for c in report.checks if not c.passed]
    assert not failures, [f"{c.name}: {c.detail}" for c in failures]


# ─── configs ──────────────────────────────────────────────────────────────

def test_config_check_reports_each_argus_fx_config():
    report = pp.PreflightReport()
    pp._check_configs(report)
    # Three checks expected for the three FX configs
    config_checks = [c for c in report.checks if c.name.startswith("config ")]
    assert len(config_checks) == 3
    # All should pass (default mult or set value, just reports detail)
    failures = [c for c in config_checks if not c.passed]
    assert not failures, [f"{c.name}: {c.detail}" for c in failures]


def test_config_check_reports_multiplier_in_detail():
    report = pp.PreflightReport()
    pp._check_configs(report)
    for c in (c for c in report.checks if c.name.startswith("config ")):
        assert "paper_stress_multiplier" in c.detail


# ─── trace path ───────────────────────────────────────────────────────────

def test_trace_path_check_passes_when_unset(good_env):
    report = pp.PreflightReport()
    pp._check_trace_path(report)
    check = next(c for c in report.checks if "GOLDEN_TRACE_PATH" in c.name)
    assert check.passed
    assert "not set" in check.detail


def test_trace_path_check_passes_for_writable_dir(good_env, monkeypatch, tmp_path):
    monkeypatch.setenv("GOLDEN_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    report = pp.PreflightReport()
    pp._check_trace_path(report)
    check = next(c for c in report.checks if "writable" in c.name)
    assert check.passed


def test_trace_path_check_fails_for_unwritable_path(good_env, monkeypatch):
    # NUL on Windows / /dev/null/x on Unix — paths that can't be a dir
    bad = "Z:\\nonexistent_drive\\xx\\trace.jsonl"
    monkeypatch.setenv("GOLDEN_TRACE_PATH", bad)
    report = pp.PreflightReport()
    pp._check_trace_path(report)
    check = next(c for c in report.checks if "writable" in c.name)
    assert not check.passed


# ─── harness smoke ────────────────────────────────────────────────────────

def test_harness_smoke_completes_successfully():
    report = pp.PreflightReport()
    pp._check_harness_smoke(report)
    check = next(c for c in report.checks if "harness end-to-end" in c.name)
    assert check.passed, check.detail


# ─── full orchestration ──────────────────────────────────────────────────

def test_run_preflight_returns_report_with_checks(good_env):
    report = pp.run_preflight()
    assert len(report.checks) > 5  # we have many checks
    # In a clean test env all should pass
    failures = [c for c in report.checks if not c.passed]
    assert not failures, [f"{c.name}: {c.detail}" for c in failures]


def test_cli_exits_0_when_all_pass(good_env, capsys):
    rc = pp.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "READY" in captured.out


def test_cli_exits_1_when_env_wrong(monkeypatch, capsys):
    monkeypatch.setenv("IBKR_PORT", "7496")
    monkeypatch.setenv("REAL_MONEY_ENABLED", "")
    rc = pp.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "NOT READY" in captured.out


def test_cli_json_mode_emits_valid_json(good_env, capsys):
    rc = pp.main(["--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "all_passed" in parsed
    assert "checks" in parsed
    assert isinstance(parsed["checks"], list)
    assert parsed["all_passed"] is True
    assert rc == 0


def test_cli_json_mode_reports_failure_when_env_wrong(monkeypatch, capsys):
    monkeypatch.setenv("IBKR_PORT", "7496")
    rc = pp.main(["--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["all_passed"] is False
    assert rc == 1
