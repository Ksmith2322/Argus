"""Tests for ops/stress_injector.py — the paper-only synthetic signal injector.

The injector submits real paper orders on demand. The safety locks are
the only thing standing between this and a live-account incident if
someone ever flips a port or env var by accident, so the locks get the
most coverage."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from ops import stress_injector
from ops.stress_injector import Event, TEST_INSTRUMENTS, STRESS_CLIENT_ID


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("IBKR_PORT", "REAL_MONEY_ENABLED", "STRESS_INJECT_OK"):
        monkeypatch.delenv(k, raising=False)


# ─── safety locks ─────────────────────────────────────────────────────────

def test_locks_refuse_when_nothing_set(clean_env):
    with pytest.raises(RuntimeError) as exc:
        stress_injector._enforce_safety_locks()
    assert "STRESS_INJECT_OK" in str(exc.value)
    assert "IBKR_PORT" in str(exc.value)


def test_locks_refuse_when_ibkr_port_wrong(clean_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7496")  # live port
    monkeypatch.setenv("STRESS_INJECT_OK", "1")
    with pytest.raises(RuntimeError) as exc:
        stress_injector._enforce_safety_locks()
    assert "IBKR_PORT" in str(exc.value)
    assert "7496" in str(exc.value)


def test_locks_refuse_when_real_money_env_set(clean_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("STRESS_INJECT_OK", "1")
    monkeypatch.setenv("REAL_MONEY_ENABLED", "1")
    with pytest.raises(RuntimeError) as exc:
        stress_injector._enforce_safety_locks()
    assert "REAL_MONEY_ENABLED" in str(exc.value)


def test_locks_refuse_when_real_money_env_true_string(clean_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("STRESS_INJECT_OK", "1")
    monkeypatch.setenv("REAL_MONEY_ENABLED", "true")
    with pytest.raises(RuntimeError):
        stress_injector._enforce_safety_locks()


def test_locks_refuse_when_helio_real_money_module_enabled(clean_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("STRESS_INJECT_OK", "1")
    from helio import real_money
    monkeypatch.setattr(real_money, "REAL_MONEY_ENABLED", True)
    with pytest.raises(RuntimeError) as exc:
        stress_injector._enforce_safety_locks()
    assert "helio.real_money" in str(exc.value)


def test_locks_pass_when_all_four_gates_pass(clean_env, monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("STRESS_INJECT_OK", "1")
    from helio import real_money
    monkeypatch.setattr(real_money, "REAL_MONEY_ENABLED", False)
    # No raise = pass
    stress_injector._enforce_safety_locks()


# ─── no overlap with survivor roster ──────────────────────────────────────

def test_test_instruments_do_not_overlap_argus_fx_survivors():
    """TEST_INSTRUMENTS exists so stress fires don't pollute real strategy
    attribution. USDJPY/GBPUSD/CADJPY are the argus FX pairs — must NEVER
    be in TEST_INSTRUMENTS."""
    forbidden = {"USDJPY", "GBPUSD", "CADJPY"}
    assert not (forbidden & set(TEST_INSTRUMENTS.keys())), (
        f"TEST_INSTRUMENTS overlaps argus survivor roster: "
        f"{forbidden & set(TEST_INSTRUMENTS.keys())}"
    )


def test_test_instruments_have_required_fields():
    for sym, spec in TEST_INSTRUMENTS.items():
        for required in ("base", "quote", "lot", "pip"):
            assert required in spec, f"{sym} missing {required}"
        assert spec["pip"] > 0
        assert spec["lot"] > 0


# ─── client-id isolation ──────────────────────────────────────────────────

def test_stress_client_id_in_dedicated_range():
    """250-299 is the stress harness range — must not collide with
    production allocation (1 argus_flow + 12/51/53 argus pairs +
    60/70/80/90 Greek + 101-117 forge + 186 flatten_eod)."""
    assert 250 <= STRESS_CLIENT_ID < 300, (
        f"STRESS_CLIENT_ID={STRESS_CLIENT_ID} outside reserved 250-299 range"
    )
    production_ids = {1, 12, 51, 53, 60, 70, 80, 90, 186} | set(range(101, 118))
    assert STRESS_CLIENT_ID not in production_ids


# ─── event log ────────────────────────────────────────────────────────────

def test_event_roundtrips_through_json():
    e = Event(ts="2026-05-20T12:00:00Z", cycle_id="abc123",
              kind="ENTRY_SUBMIT", payload={"mid": 1.0850})
    j = json.dumps(asdict(e), default=str)
    parsed = json.loads(j)
    assert parsed["cycle_id"] == "abc123"
    assert parsed["payload"]["mid"] == 1.0850


def test_log_path_under_argus_flow_logs():
    """Logs go to argus_flow/logs/stress_injector.jsonl — NOT to
    canonical_fills.jsonl (which would pollute strategy attribution)."""
    p = Path(stress_injector.LOG_PATH)
    assert p.name == "stress_injector.jsonl"
    assert p.parent.name == "logs"
    assert p.parent.parent.name == "argus_flow"


# ─── chaos modes ──────────────────────────────────────────────────────────

def test_chaos_modes_includes_known_races():
    """The four chaos modes target failure classes we've already hit live:
    none, cancel_during_fill (5/19 cascade), double_submit (duplicate
    order handling), disconnect_after_submit (network blip recovery)."""
    from ops.stress_injector import CHAOS_MODES
    assert "none" in CHAOS_MODES
    assert "cancel_during_fill" in CHAOS_MODES
    assert "double_submit" in CHAOS_MODES
    assert "disconnect_after_submit" in CHAOS_MODES


def test_run_one_cycle_rejects_unknown_chaos_mode():
    with pytest.raises(ValueError) as exc:
        stress_injector.run_one_cycle("EURUSD", 1000, chaos="delete_account")
    assert "unknown chaos mode" in str(exc.value)


def test_log_event_writes_jsonl_line(tmp_path, monkeypatch):
    monkeypatch.setattr(stress_injector, "LOG_PATH", tmp_path / "stress.jsonl")
    e = Event(ts="2026-05-20T12:00:00Z", cycle_id="abc", kind="TEST", payload={"x": 1})
    stress_injector._log_event(e)
    lines = (tmp_path / "stress.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["kind"] == "TEST"
    assert parsed["payload"]["x"] == 1
