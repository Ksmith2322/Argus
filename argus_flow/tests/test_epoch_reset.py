"""Tests for the 5/31 epoch reset script.

The reset is high-stakes: a bug in archive logic could lose evidence,
and a bug in counter resets could corrupt strategy state. These tests
build a synthetic repo layout in tmp_path and verify the script does
exactly what it claims.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.maintenance import epoch_reset as er


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Set up a minimal repo skeleton in tmp_path and rebind the module's
    REPO/LOGS constants. Returns the tmp_path root."""
    (tmp_path / "argus_flow" / "logs").mkdir(parents=True)
    (tmp_path / "forge" / "logs" / "vix_intraday").mkdir(parents=True)
    (tmp_path / "forge" / "logs" / "gld_pm_long").mkdir(parents=True)

    # Files that will be archived
    (tmp_path / "argus_flow" / "logs" / "canonical_fills.jsonl").write_text(
        '{"ts":"2026-04-23","strategy":"x","pnl_usd":10}\n', encoding="utf-8"
    )
    (tmp_path / "forge" / "logs" / "vix_intraday" / "trades.csv").write_text(
        "ts,direction,pnl_usd\n2026-04-23,long,10\n", encoding="utf-8"
    )
    (tmp_path / "forge" / "logs" / "gld_pm_long" / "trades.csv").write_text(
        "ts,direction,pnl_usd\n2026-04-23,long,5\n", encoding="utf-8"
    )

    # Argus state files with non-zero counters that should reset
    (tmp_path / "argus_flow" / "logs" / "cadjpy").mkdir()
    (tmp_path / "argus_flow" / "logs" / "cadjpy" / "state.json").write_text(
        json.dumps({"position": "FLAT", "trade_count": 6, "trade_serial": 6,
                    "next_trade_num": 7, "config_hash": "abc123"}),
        encoding="utf-8",
    )
    (tmp_path / "argus_flow" / "logs" / "gbpusd").mkdir()
    (tmp_path / "argus_flow" / "logs" / "gbpusd" / "state.json").write_text(
        json.dumps({"position": "FLAT", "trade_count": 37, "trade_serial": 37,
                    "next_trade_num": 38, "config_hash": "def456"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(er, "REPO", tmp_path)
    monkeypatch.setattr(er, "LOGS", tmp_path / "argus_flow" / "logs")
    return tmp_path


# ---------------------------------------------------------------------------
# plan_reset (no I/O)
# ---------------------------------------------------------------------------

def test_plan_reset_lists_archive_moves(fake_repo):
    plan = er.plan_reset("TEST")
    archive_srcs = [m["src"] for m in plan["archive_moves"]]
    # Cross-platform: archive_srcs may use '/' or '\' depending on OS;
    # normalize before assertion.
    norm = lambda p: p.replace("\\", "/")
    assert "argus_flow/logs/canonical_fills.jsonl" in [norm(s) for s in archive_srcs]
    assert any(norm(s) == "forge/logs/vix_intraday/trades.csv" for s in archive_srcs)


def test_plan_reset_includes_counter_resets(fake_repo):
    plan = er.plan_reset("TEST")
    assert plan["n_counter_resets"] == 2  # cadjpy + gbpusd present (no usdjpy)


def test_plan_reset_blocks_when_halt_flag_present(fake_repo):
    (fake_repo / "argus_flow" / "logs" / "HALT.flag").write_text("halted", encoding="utf-8")
    plan = er.plan_reset("TEST")
    assert plan["ready_to_execute"] is False
    assert any(p.endswith("HALT.flag") for p in plan["blockers_present"])


def test_plan_reset_ready_when_flags_absent(fake_repo):
    plan = er.plan_reset("TEST")
    assert plan["ready_to_execute"] is True
    assert plan["blockers_present"] == []


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_plan_reset_is_idempotent_after_archive(fake_repo):
    """After execute_reset moves files, a re-plan should show 0 archive
    moves remaining (idempotent)."""
    er.execute_reset("TEST")
    plan = er.plan_reset("TEST")
    assert plan["n_archive_moves"] == 0


# ---------------------------------------------------------------------------
# execute_reset
# ---------------------------------------------------------------------------

def test_execute_reset_moves_files_to_archive(fake_repo):
    result = er.execute_reset("TEST")
    assert result["status"] == "executed"
    archived = fake_repo / "argus_flow" / "logs" / "_archive" / "pre_reset_TEST"
    # Original files should be gone, archive copies present
    assert not (fake_repo / "argus_flow" / "logs" / "canonical_fills.jsonl").exists() \
        or (fake_repo / "argus_flow" / "logs" / "canonical_fills.jsonl").read_text() == ""
    assert (archived / "argus_flow" / "logs" / "canonical_fills.jsonl").exists()
    assert (archived / "forge" / "logs" / "vix_intraday" / "trades.csv").exists()


def test_execute_reset_zeroes_argus_counters(fake_repo):
    er.execute_reset("TEST")
    after = json.loads(
        (fake_repo / "argus_flow" / "logs" / "cadjpy" / "state.json").read_text()
    )
    assert after["trade_count"] == 0
    assert after["trade_serial"] == 0
    assert after["next_trade_num"] == 0
    # Position and config_hash preserved
    assert after["position"] == "FLAT"
    assert after["config_hash"] == "abc123"


def test_execute_reset_writes_manifest(fake_repo):
    er.execute_reset("TEST")
    manifest = (fake_repo / "argus_flow" / "logs" / "_archive"
                / "pre_reset_TEST" / "_manifest.jsonl")
    assert manifest.exists()
    lines = [l for l in manifest.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    payload = json.loads(lines[0])
    assert payload["target"] == "TEST"
    assert isinstance(payload["events"], list)


def test_execute_reset_refuses_when_halt_flag_present(fake_repo):
    (fake_repo / "argus_flow" / "logs" / "HALT.flag").write_text("halted", encoding="utf-8")
    result = er.execute_reset("TEST")
    assert result["status"] == "blocked"
    # Original files NOT moved
    assert (fake_repo / "argus_flow" / "logs" / "canonical_fills.jsonl").exists()


def test_execute_reset_recreates_canonical_fills_empty(fake_repo):
    er.execute_reset("TEST")
    # canonical_fills.jsonl is restored as an empty file (template rewrite)
    cf = fake_repo / "argus_flow" / "logs" / "canonical_fills.jsonl"
    assert cf.exists()
    assert cf.read_text() == ""


def test_execute_reset_idempotent_second_run_safe(fake_repo):
    r1 = er.execute_reset("TEST")
    r2 = er.execute_reset("TEST")
    # Second run shouldn't crash, just no-ops on already-archived files
    assert r1["status"] == "executed"
    assert r2["status"] == "executed"


# ---------------------------------------------------------------------------
# Counter reset preserves non-counter fields
# ---------------------------------------------------------------------------

def test_counter_reset_preserves_non_counter_fields(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "position": "LONG", "trade_count": 5, "trade_serial": 5,
        "next_trade_num": 6, "config_hash": "abc",
        "deployment_stage": "paper", "entry_price": 1.5,
    }), encoding="utf-8")
    er._reset_counter_in_state(state_path)
    after = json.loads(state_path.read_text())
    assert after["trade_count"] == 0
    assert after["position"] == "LONG"  # not touched
    assert after["entry_price"] == 1.5
    assert after["deployment_stage"] == "paper"
    assert after["config_hash"] == "abc"
