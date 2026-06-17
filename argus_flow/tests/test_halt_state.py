from __future__ import annotations

import json

from helio.halt_state import get_halt_state


def test_halt_state_clear_when_all_sources_clear(tmp_path):
    state = get_halt_state(
        halt_flag_path=tmp_path / "HALT.flag",
        flatten_flag_path=tmp_path / "FLATTEN_EOD.flag",
        broker_drift_state_path=tmp_path / "broker_drift_state.json",
    )
    assert state.halted is False
    assert state.sources == ()


def test_halt_state_reads_halt_flag(tmp_path):
    halt = tmp_path / "HALT.flag"
    halt.write_text("manual halt", encoding="utf-8")
    state = get_halt_state(
        halt_flag_path=halt,
        flatten_flag_path=tmp_path / "FLATTEN_EOD.flag",
        broker_drift_state_path=tmp_path / "broker_drift_state.json",
    )
    assert state.halted is True
    assert "HALT.flag" in state.sources
    assert "manual halt" in state.reason


def test_halt_state_fails_closed_on_broker_drift_trip(tmp_path):
    drift = tmp_path / "broker_drift_state.json"
    drift.write_text(json.dumps({"tripped": True, "divergence_pct": 1.7, "sustained_minutes": 65}), encoding="utf-8")
    state = get_halt_state(
        halt_flag_path=tmp_path / "HALT.flag",
        flatten_flag_path=tmp_path / "FLATTEN_EOD.flag",
        broker_drift_state_path=drift,
    )
    assert state.halted is True
    assert "broker_drift_state.json" in state.sources
    assert "1.7" in state.reason
