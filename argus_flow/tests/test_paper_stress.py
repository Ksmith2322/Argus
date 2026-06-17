"""Tests for helio.paper_stress — the paper-mode entry-threshold multiplier.

This knob exists to multiply trading activity in paper, surfacing
execution-path bugs faster. It MUST refuse to apply when not on the
paper port or when real-money is enabled — see the safety contract in
helio/paper_stress.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from helio import paper_stress


@pytest.fixture
def paper_env(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7496")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)


@pytest.fixture
def gateway_paper_env(monkeypatch):
    """IB Gateway paper port (4002) — should be treated as paper for
    paper_stress purposes. Added 2026-05-25 alongside the TWS→Gateway
    migration."""
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)


@pytest.fixture
def gateway_live_env(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "4001")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)


def test_paper_stress_recognizes_gateway_paper_port(gateway_paper_env):
    """Gateway paper (4002) must trigger the same paper-stress multiplier
    path that TWS paper (7497) does — otherwise migrating to Gateway
    silently disables the entire paper-stress mechanism."""
    assert paper_stress.apply(3.0, 0.5, strategy="x", knob="y") == 1.5


def test_paper_stress_refuses_gateway_live_port(gateway_live_env, caplog):
    """Gateway live (4001) must NOT be eligible for paper-stress —
    same guarantee TWS live (7496) gets."""
    with caplog.at_level(logging.WARNING):
        assert paper_stress.apply(3.0, 0.5, strategy="x", knob="y") == 3.0
    assert any("IGNORED" in r.message for r in caplog.records)


# ── unit behavior ─────────────────────────────────────────────────────────

def test_multiplier_1_returns_base_unchanged():
    """mult==1.0 is the no-op default; no env check, no log line."""
    assert paper_stress.apply(3.0, 1.0, strategy="x", knob="y") == 3.0


def test_multiplier_below_1_lowers_threshold_on_paper(paper_env):
    """mult=0.5 → threshold halves → strategy fires more often."""
    assert paper_stress.apply(3.0, 0.5, strategy="argus_usdjpy", knob="min_trend_strength") == 1.5


def test_multiplier_above_1_raises_threshold_on_paper(paper_env):
    """mult=2.0 → threshold doubles → strategy fires less often (sanity case)."""
    assert paper_stress.apply(3.0, 2.0, strategy="x", knob="y") == 6.0


def test_REFUSES_on_live_port(live_env, caplog):
    """The single most important safety: never lower a threshold on the live port."""
    with caplog.at_level(logging.WARNING):
        result = paper_stress.apply(3.0, 0.5, strategy="argus_usdjpy", knob="min_trend_strength")
    assert result == 3.0
    assert any("paper_stress IGNORED" in r.message for r in caplog.records)


def test_REFUSES_when_real_money_enabled(paper_env, monkeypatch, caplog):
    """Paper port but real-money flag set — also refuse."""
    monkeypatch.setenv("REAL_MONEY_ENABLED", "1")
    with caplog.at_level(logging.WARNING):
        result = paper_stress.apply(3.0, 0.5, strategy="x", knob="y")
    assert result == 3.0
    assert any("paper_stress IGNORED" in r.message for r in caplog.records)


def test_REFUSES_when_real_money_enabled_true_string(paper_env, monkeypatch):
    monkeypatch.setenv("REAL_MONEY_ENABLED", "true")
    assert paper_stress.apply(3.0, 0.5, strategy="x", knob="y") == 3.0


def test_REFUSES_multiplier_below_min(paper_env, caplog):
    """Below 0.1 = sanity guard against typos like 0.001."""
    with caplog.at_level(logging.WARNING):
        result = paper_stress.apply(3.0, 0.05, strategy="x", knob="y")
    assert result == 3.0
    assert any("outside" in r.message for r in caplog.records)


def test_REFUSES_multiplier_above_max(paper_env):
    """Above 10.0 = sanity guard. Effectively shuts off the strategy
    rather than scaling beyond protocol."""
    assert paper_stress.apply(3.0, 15.0, strategy="x", knob="y") == 3.0


def test_REFUSES_negative_multiplier(paper_env):
    """Negative multiplier would flip thresholds — refuse."""
    assert paper_stress.apply(3.0, -0.5, strategy="x", knob="y") == 3.0


def test_active_mode_logs_at_warning_level(paper_env, caplog):
    """Operators must see PAPER_STRESS_ACTIVE in default logs."""
    with caplog.at_level(logging.WARNING):
        paper_stress.apply(3.0, 0.5, strategy="argus_usdjpy", knob="min_trend_strength")
    assert any("PAPER_STRESS_ACTIVE" in r.message for r in caplog.records)
    assert any("argus_usdjpy" in r.message and "min_trend_strength" in r.message
               for r in caplog.records)


def test_inactive_mode_does_not_log(paper_env, caplog):
    """mult=1.0 should not emit anything — silent default."""
    with caplog.at_level(logging.DEBUG):
        paper_stress.apply(3.0, 1.0, strategy="x", knob="y")
    assert not caplog.records


# ── wiring verification (source-level, matches the test_exit_broker_truth_guard pattern) ──

def _runner_unified_src() -> str:
    return (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")


def _pead_src() -> str:
    return (Path(__file__).resolve().parents[2] / "forge" / "pead" / "runner.py").read_text(encoding="utf-8")


def test_argus_mtf_wired_to_paper_stress():
    """MTF setup block must call paper_stress.apply on both threshold knobs.

    Regression: if someone removes the wiring, the paper_stress_multiplier
    config field stops doing anything — silently."""
    src = _runner_unified_src()
    idx = src.find('config.get("strategy") == "mtf_trend"')
    assert idx >= 0, "MTF setup block not found"
    body = src[idx:idx + 2000]
    assert "from helio.paper_stress import apply" in body, (
        "MTF block no longer imports helio.paper_stress.apply"
    )
    assert "paper_stress_multiplier" in body
    assert 'knob="min_trend_strength"' in body
    assert 'knob="min_confidence"' in body


def test_pead_wired_to_paper_stress():
    """evaluate_pead_signal call must scale all three filters via paper_stress."""
    src = _pead_src()
    idx = src.find("evaluate_pead_signal(")
    assert idx >= 0, "pead signal call not found"
    body = src[idx:idx + 1500]
    assert "from helio.paper_stress import apply" in body or \
           "from helio.paper_stress import apply" in src[max(0, idx - 1500):idx], \
           "pead no longer imports helio.paper_stress.apply"
    assert 'knob="surprise_pct_min"' in body
    assert 'knob="volume_ratio_min"' in body
    assert 'knob="gap_pct_min"' in body
