"""Tests for helio.strategy_scorecard + ops.strategy_scorecard CLI +
the helio.ibkr_execution.connect() recorder wiring."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from helio.strategy_scorecard import (
    StrategyScore, score_fleet, score_strategy,
)
from ops import strategy_scorecard as cli_mod


# ─── score_strategy ──────────────────────────────────────────────────────

def test_green_when_nothing_to_flag(tmp_path):
    """No artifacts, allocation 0 — no red flags, all clear."""
    s = score_strategy("forge_x")
    assert s.status == "GREEN"


def test_red_when_invariant_violations_present(tmp_path):
    """Trace file with an invariant violation forces RED."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    cascade = fixtures / "cascade_race_20260519.jsonl"
    s = score_strategy("forge_x", trace_path=cascade)
    assert s.status == "RED"
    assert "invariant" in s.reason.lower()
    assert s.metrics["invariant_violations"] > 0


def test_red_when_allocated_and_silent_and_heartbeat_stale(tmp_path):
    """Strategy has allocation > 0, no fills, no fresh heartbeat → RED."""
    # No fills file, no heartbeat — silent strategy
    s = score_strategy("forge_x", allocation=0.5)
    assert s.status == "RED"
    assert "no fills" in s.reason.lower() or "dead" in s.reason.lower()


def test_red_when_heartbeat_stale_even_with_fills(tmp_path):
    fills = tmp_path / "canonical_fills.jsonl"
    today = datetime.now(timezone.utc).date().isoformat()
    fills.write_text(
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T15:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    hb = tmp_path / "heartbeat.json"
    old = (datetime.now(timezone.utc) - timedelta(minutes=300)).isoformat()
    hb.write_text(json.dumps({"last_update": old}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5,
                       fills_path=fills, heartbeat_path=hb)
    assert s.status == "RED"
    assert "stale" in s.reason.lower()


def test_green_with_fresh_heartbeat_and_balanced_fills(tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    fills = tmp_path / "canonical_fills.jsonl"
    fills.write_text("\n".join([
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T10:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "EXIT",  "ts": today + "T15:00:00+00:00"}),
    ]) + "\n", encoding="utf-8")
    hb = tmp_path / "heartbeat.json"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    hb.write_text(json.dumps({"last_update": fresh}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5,
                       fills_path=fills, heartbeat_path=hb)
    assert s.status == "GREEN"


def test_yellow_when_position_imbalance(tmp_path):
    """Threshold is |entries - exits| > 1 — single-position overnight
    strategies (gld_pm_long etc.) report GREEN, but 3 entries with 1
    exit signals real drift."""
    today = datetime.now(timezone.utc).date().isoformat()
    fills = tmp_path / "canonical_fills.jsonl"
    fills.write_text("\n".join([
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T10:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T11:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T12:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "EXIT",  "ts": today + "T15:00:00+00:00"}),
    ]) + "\n", encoding="utf-8")
    hb = tmp_path / "heartbeat.json"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    hb.write_text(json.dumps({"last_update": fresh}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5,
                       fills_path=fills, heartbeat_path=hb)
    assert s.status == "YELLOW"
    assert "imbalance" in s.reason.lower()


def test_yellow_when_allocated_but_zero_fills_with_fresh_heartbeat(tmp_path):
    """Low-frequency strategy: alive but quiet today. YELLOW for visibility."""
    hb = tmp_path / "heartbeat.json"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    hb.write_text(json.dumps({"last_update": fresh}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5, heartbeat_path=hb)
    assert s.status == "YELLOW"
    assert "no fills" in s.reason.lower()


def test_fills_filtered_by_strategy_label(tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    fills = tmp_path / "canonical_fills.jsonl"
    fills.write_text("\n".join([
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": today + "T10:00:00+00:00"}),
        json.dumps({"strategy": "forge_y", "side": "ENTRY", "ts": today + "T10:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "EXIT",  "ts": today + "T15:00:00+00:00"}),
    ]) + "\n", encoding="utf-8")
    hb = tmp_path / "heartbeat.json"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    hb.write_text(json.dumps({"last_update": fresh}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5,
                       fills_path=fills, heartbeat_path=hb)
    assert s.metrics["fills_today"] == 2  # only forge_x rows


def test_fills_filtered_by_date(tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    fills = tmp_path / "canonical_fills.jsonl"
    fills.write_text("\n".join([
        json.dumps({"strategy": "forge_x", "side": "ENTRY", "ts": yesterday + "T10:00:00+00:00"}),
        json.dumps({"strategy": "forge_x", "side": "EXIT",  "ts": today + "T15:00:00+00:00"}),
    ]) + "\n", encoding="utf-8")
    hb = tmp_path / "heartbeat.json"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    hb.write_text(json.dumps({"last_update": fresh}), encoding="utf-8")
    s = score_strategy("forge_x", allocation=0.5,
                       fills_path=fills, heartbeat_path=hb)
    assert s.metrics["fills_today"] == 1  # only today's row


# ─── score_fleet ──────────────────────────────────────────────────────────

def test_score_fleet_returns_one_score_per_strategy(tmp_path):
    fills = tmp_path / "canonical_fills.jsonl"
    fills.write_text("", encoding="utf-8")
    scores = score_fleet(
        [("strat_a", 0.5), ("strat_b", 1.0)],
        fills_path=fills,
    )
    assert len(scores) == 2
    assert {s.strategy for s in scores} == {"strat_a", "strat_b"}


def test_score_fleet_finds_per_strategy_trace_by_convention(tmp_path):
    """If trace_dir contains <strategy>_<today>.jsonl, it's picked up."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    today = datetime.now(timezone.utc).date().isoformat()
    # Copy the cascade fixture as today's trace for strat_a
    cascade = fixtures / "cascade_race_20260519.jsonl"
    (trace_dir / f"strat_a_{today}.jsonl").write_bytes(cascade.read_bytes())

    scores = score_fleet(
        [("strat_a", 0.5), ("strat_b", 0.5)],
        trace_dir=trace_dir,
    )
    by_name = {s.strategy: s for s in scores}
    assert by_name["strat_a"].status == "RED"
    assert by_name["strat_a"].metrics["invariant_violations"] > 0
    # strat_b has no trace — falls through to silent-strategy check
    assert by_name["strat_b"].status == "RED"


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_cli_single_strategy_mode_runs(capsys):
    rc = cli_mod.main(["--strategy", "forge_x", "--allocation", "0"])
    captured = capsys.readouterr()
    assert "forge_x" in captured.out
    # No allocation, no artifacts → GREEN, exit 0
    assert rc == 0


def test_cli_single_strategy_json_emits_valid_json(capsys):
    rc = cli_mod.main(["--strategy", "forge_x", "--allocation", "0", "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "scores" in parsed
    assert len(parsed["scores"]) == 1
    assert parsed["scores"][0]["strategy"] == "forge_x"


def test_cli_exit_2_when_no_allocation_file(monkeypatch, capsys):
    """If allocation_factors.json is missing, return 2."""
    monkeypatch.setattr(cli_mod, "ALLOCATIONS_PATH",
                        Path("definitely_does_not_exist.json"))
    rc = cli_mod.main([])
    assert rc == 2


def test_cli_load_allocations_filters_to_positive(monkeypatch, tmp_path):
    """Strategies with allocation 0 are excluded; positive are kept."""
    alloc = tmp_path / "allocation_factors.json"
    alloc.write_text(json.dumps({
        "factors": {
            "live_one": 0.5,
            "live_two": 1.0,
            "dead_one": 0.0,
        }
    }), encoding="utf-8")
    monkeypatch.setattr(cli_mod, "ALLOCATIONS_PATH", alloc)
    loaded = cli_mod._load_allocations()
    names = [s for s, _ in loaded]
    assert "live_one" in names
    assert "live_two" in names
    assert "dead_one" not in names


# ─── recorder wiring on helio.ibkr_execution.connect() ───────────────────

def test_attach_via_env_returns_none_when_env_unset(monkeypatch):
    from helio.event_recorder import attach_via_env

    class _Slot:
        def __init__(self): self.handlers = []
        def __iadd__(self, h): self.handlers.append(h); return self
        def __isub__(self, h):
            if h in self.handlers: self.handlers.remove(h)
            return self

    ib = SimpleNamespace(
        orderStatusEvent=_Slot(), execDetailsEvent=_Slot(),
        errorEvent=_Slot(), positionEvent=_Slot(),
        newOrderEvent=_Slot(), disconnectedEvent=_Slot(),
        connectedEvent=_Slot(),
    )
    monkeypatch.delenv("GOLDEN_TRACE_PATH", raising=False)
    assert attach_via_env(ib) is None


def test_attach_via_env_attaches_when_env_set(monkeypatch, tmp_path):
    from helio.event_recorder import attach_via_env

    class _Slot:
        def __init__(self): self.handlers = []
        def __iadd__(self, h): self.handlers.append(h); return self
        def __isub__(self, h):
            if h in self.handlers: self.handlers.remove(h)
            return self

    ib = SimpleNamespace(
        orderStatusEvent=_Slot(), execDetailsEvent=_Slot(),
        errorEvent=_Slot(), positionEvent=_Slot(),
        newOrderEvent=_Slot(), disconnectedEvent=_Slot(),
        connectedEvent=_Slot(),
    )
    trace_path = tmp_path / "t.jsonl"
    monkeypatch.setenv("GOLDEN_TRACE_PATH", str(trace_path))
    rec = attach_via_env(ib)
    assert rec is not None
    assert trace_path.exists()
    rec.close()


def test_ibkr_execution_connect_calls_attach_via_env_source_level():
    """Source-level: connect() must call attach_via_env after connecting."""
    src = (Path(__file__).resolve().parents[2] / "helio" / "ibkr_execution.py").read_text(encoding="utf-8")
    idx = src.find("def connect(")
    assert idx >= 0
    # Function body up to next def
    end = src.find("\ndef ", idx + 10)
    body = src[idx:end]
    assert "from helio.event_recorder import attach_via_env" in body
    assert "attach_via_env(ib)" in body
