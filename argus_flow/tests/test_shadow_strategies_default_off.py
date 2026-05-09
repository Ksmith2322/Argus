"""Verify the shadow-strategy registry stays default-off.

The contract: every entry in ``argus_flow/configs/shadow_strategies.json``
must default to ``global_enabled=false`` AND have empty signing fields,
so a strategy cannot be silently promoted to runtime by accident. Promotion
requires an operator to (a) flip ``global_enabled``, (b) populate
``approver``, and (c) populate ``ledger_entry_id`` — together. Any test
that finds a partially-signed entry should fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.research import mtf_long_support_shadow_eval as shadow

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "argus_flow" / "configs" / "shadow_strategies.json"


def test_registry_file_exists():
    assert REGISTRY.exists(), f"shadow registry missing: {REGISTRY}"


def test_every_shadow_entry_is_default_off_unless_fully_signed():
    raw = json.loads(REGISTRY.read_text())
    entries = raw.get("shadow_strategies") or []
    assert entries, "registry has zero shadow strategies"

    for entry in entries:
        enabled = bool(entry.get("global_enabled", False))
        approver = str(entry.get("approver") or "")
        ledger_id = str(entry.get("ledger_entry_id") or "")
        # If enabled, BOTH signing fields must be present. The reverse
        # (disabled with signing fields populated) is harmless, but we
        # forbid the dangerous half-state of enabled-but-unsigned.
        if enabled:
            assert approver, f"{entry.get('name')} is global_enabled but approver is empty"
            assert ledger_id, f"{entry.get('name')} is global_enabled but ledger_entry_id is empty"


def test_mtf_shadow_loads_disabled_by_default():
    config = shadow.load_config()
    assert config.global_enabled is False
    assert config.approver == ""
    assert config.ledger_entry_id == ""
    assert config.is_promotion_eligible is False


def test_promotion_eligibility_requires_all_three(monkeypatch):
    """Eligibility = global_enabled AND approver AND ledger_entry_id."""
    base = shadow.ShadowConfig(
        name="test",
        global_enabled=True,
        block_reason_filter="",
        applicable_symbols=(),
        min_sample_for_promotion_review=50,
        promotion_pf_threshold=1.30,
        kill_pf_threshold=0.95,
        rolling_window_days=14,
        approver="",
        ledger_entry_id="",
    )
    assert base.is_promotion_eligible is False
    signed_no_ledger = shadow.ShadowConfig(**{**base.__dict__, "approver": "ksmith2322"})
    assert signed_no_ledger.is_promotion_eligible is False
    fully_signed = shadow.ShadowConfig(
        **{**base.__dict__, "approver": "ksmith2322", "ledger_entry_id": "test-1"}
    )
    assert fully_signed.is_promotion_eligible is True


def test_promotion_gate_returns_insufficient_sample_when_n_low():
    config = shadow.load_config()
    summary = {"n": 10, "profit_factor": 5.0}
    decision = shadow.evaluate_promotion_gate(config, summary)
    assert decision["verdict"] == "INSUFFICIENT_SAMPLE"


def test_promotion_gate_blocks_unsigned_even_with_great_pf():
    """A great PF cannot promote a registry entry that is not signed."""
    config = shadow.load_config()  # default: not signed
    summary = {"n": 100, "profit_factor": 3.0}
    decision = shadow.evaluate_promotion_gate(config, summary)
    assert decision["verdict"] == "PROMOTION_BLOCKED_UNSIGNED"


def test_promotion_gate_kill_recommended_on_low_pf():
    config = shadow.load_config()
    summary = {"n": 60, "profit_factor": 0.5}
    decision = shadow.evaluate_promotion_gate(config, summary)
    assert decision["verdict"] == "KILL_RECOMMENDED"


def test_promotion_gate_hold_and_observe_in_middle():
    config = shadow.load_config()
    summary = {"n": 60, "profit_factor": 1.10}
    decision = shadow.evaluate_promotion_gate(config, summary)
    assert decision["verdict"] == "HOLD_AND_OBSERVE"


def test_summary_returns_zero_when_no_ledger(tmp_path):
    summary = shadow.summarize_ledger(tmp_path / "missing.jsonl")
    assert summary["n"] == 0
    assert summary["net_pips"] == 0


def test_summary_aggregates_outcomes(tmp_path):
    p = tmp_path / "ledger.jsonl"
    rows = [
        {"ts": "1", "symbol": "GBPUSD", "direction": "long", "block_reason": "MTF",
         "entry_px": 1.30, "forward_outcome": "win", "pnl_pips": 4.0, "bar_window_n": 12},
        {"ts": "2", "symbol": "GBPUSD", "direction": "long", "block_reason": "MTF",
         "entry_px": 1.30, "forward_outcome": "loss", "pnl_pips": -2.0, "bar_window_n": 12},
        {"ts": "3", "symbol": "GBPUSD", "direction": "long", "block_reason": "MTF",
         "entry_px": 1.30, "forward_outcome": "win", "pnl_pips": 6.0, "bar_window_n": 12},
        # 'open' rows are excluded from win/loss math
        {"ts": "4", "symbol": "GBPUSD", "direction": "long", "block_reason": "MTF",
         "entry_px": 1.30, "forward_outcome": "open", "pnl_pips": 0.0, "bar_window_n": 12},
    ]
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    s = shadow.summarize_ledger(p)
    assert s["n"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["net_pips"] == pytest.approx(8.0)
    # PF = sum_wins / |sum_losses| = 10 / 2 = 5.0
    assert s["profit_factor"] == pytest.approx(5.0)
