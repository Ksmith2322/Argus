"""Integration test for the ROI proof orchestrator.

Builds a synthetic canonical_fills stream + SPY price file in a tmp
directory and verifies the verdict matrix maps the right strategies to
the right labels. Avoids depending on whatever's currently in
production logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio import roi_proof_core as core
from helio import spy_benchmark as bench
from ops.audit import run_strategy_roi_proof as orch


def _ts(y: int, m: int, d: int, h: int = 0) -> str:
    return datetime(y, m, d, h, tzinfo=timezone.utc).isoformat()


def _fill(strategy: str, entry_ts: str, exit_ts: str, pnl: float, size: float, entry_px: float) -> dict:
    return {
        "ts": exit_ts, "strategy": strategy, "side": "EXIT",
        "entry_ts": entry_ts, "exit_ts": exit_ts,
        "pnl_usd": pnl, "size": size, "entry_px": entry_px,
        "symbol": "SYNTH", "direction": "long",
    }


# ---------------------------------------------------------------------------
# Verdict matrix
# ---------------------------------------------------------------------------

def test_verdict_already_killed_short_circuits():
    stats = {"n": 200, "profit_factor": 2.0, "sortino": 1.5, "information_ratio": 0.8}
    v, _ = orch._verdict(stats, killed=True)
    assert v == "ALREADY_KILLED"


def test_verdict_insufficient_sample_below_continuation_n():
    stats = {"n": 5, "profit_factor": 5.0}
    v, reason = orch._verdict(stats, killed=False)
    assert v == "INSUFFICIENT_SAMPLE"
    assert "continuation gate" in reason


def test_verdict_kill_when_pf_low_and_friction_negative():
    stats = {
        "n": 100, "profit_factor": 0.6, "friction_adj_expectancy": -2.0,
        "sortino": -0.3, "information_ratio": -0.4, "concentration_score": 0.2,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "KILL"


def test_verdict_fails_spy_when_ir_strongly_negative():
    stats = {
        "n": 100, "profit_factor": 1.10, "friction_adj_expectancy": 0.5,
        "sortino": 0.2, "information_ratio": -0.8, "spy_coverage_pct": 0.9,
        "concentration_score": 0.2,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "FAILS_SPY_BENCHMARK"


def test_verdict_repair_sizing_when_concentration_high():
    stats = {
        "n": 100, "profit_factor": 1.5, "friction_adj_expectancy": 1.0,
        "sortino": 0.9, "information_ratio": 0.6,
        "concentration_score": 0.7,  # 70% from one trade
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "REPAIR_SIZING"


def test_verdict_promising_low_sample_when_n_below_serious():
    stats = {
        "n": 50, "profit_factor": 1.5, "friction_adj_expectancy": 1.0,
        "sortino": 0.9, "information_ratio": 0.6, "concentration_score": 0.3,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "PROMISING_LOW_SAMPLE"


def test_verdict_scale_candidate_when_all_floors_met():
    stats = {
        "n": 200, "profit_factor": 1.6, "friction_adj_expectancy": 1.5,
        "sortino": 1.0, "information_ratio": 0.7, "concentration_score": 0.2,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "SCALE_CANDIDATE"


def test_verdict_repair_exit_when_pf_marginal():
    stats = {
        "n": 200, "profit_factor": 1.05, "friction_adj_expectancy": 0.3,
        "sortino": 0.4, "information_ratio": 0.1, "concentration_score": 0.2,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "REPAIR_EXIT"


def test_verdict_shadow_only_when_pf_positive_but_ratios_under_floor():
    stats = {
        "n": 200, "profit_factor": 1.20, "friction_adj_expectancy": 0.4,
        "sortino": 0.5, "information_ratio": 0.2, "concentration_score": 0.2,
    }
    v, _ = orch._verdict(stats, killed=False)
    assert v == "SHADOW_ONLY"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def test_canonical_fills_loader_filters_post_reset(tmp_path, monkeypatch):
    """Trades before POST_RESET (2026-04-23) must be excluded under
    --window post_reset. They must be included under --window all."""
    p = tmp_path / "canonical_fills.jsonl"
    rows = [
        _fill("s1", _ts(2026, 4, 1), _ts(2026, 4, 2), 10, 100, 1.0),     # pre-reset
        _fill("s1", _ts(2026, 5, 1), _ts(2026, 5, 2), 20, 100, 1.0),     # post-reset
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(orch, "CANONICAL_FILLS", p)
    post = orch._load_canonical_fills("post_reset")
    all_rows = orch._load_canonical_fills("all")
    assert len(post) == 1
    assert len(all_rows) == 2


def test_killed_strategies_loader(tmp_path, monkeypatch):
    p = tmp_path / "allocation_factors.json"
    p.write_text(json.dumps({
        "factors": {"forge_dead": 0.0, "forge_live": 0.5, "forge_full": 1.0}
    }), encoding="utf-8")
    monkeypatch.setattr(orch, "ALLOCATION_FACTORS", p)
    killed = orch._load_killed_strategies()
    assert killed == {"forge_dead"}


# ---------------------------------------------------------------------------
# End-to-end on synthetic data
# ---------------------------------------------------------------------------

def test_summarize_strategy_with_synthetic_winning_strategy(monkeypatch):
    """200 trades, +$5 expectancy, modest variance: should summarize as
    SCALE_CANDIDATE if SPY coverage allows the IR/Sortino to clear."""
    rows = []
    base_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(200):
        entry = base_ts + timedelta(hours=i)
        exit_t = entry + timedelta(hours=1)
        # Alternating pnl: 7 wins of $10 then 3 losses of -$3 (PF ≈ 7.78)
        pnl = 10.0 if (i % 10 < 7) else -3.0
        rows.append(_fill("forge_synth", entry.isoformat(), exit_t.isoformat(),
                          pnl=pnl, size=10, entry_px=100.0))

    # SPY bars covering the same window, modest 0.0001 per hour drift
    spy_bars = []
    for i in range(250):
        spy_bars.append(bench.SpyBar(
            ts=base_ts + timedelta(hours=i),
            close=600.0 * (1.0001 ** i),
        ))

    out = orch._summarize_strategy("forge_synth", rows, spy_bars, killed=False)
    assert out["n"] == 200
    # PF should be roughly (7*10) / (3*3) = 70/9 ≈ 7.78
    assert out["profit_factor"] != "inf"
    assert out["spy_coverage_pct"] > 0.5
    # Verdict should be one of the positive labels with this strong PF
    assert out["verdict"] in ("SCALE_CANDIDATE", "PROMISING_LOW_SAMPLE", "SHADOW_ONLY")


def test_summarize_strategy_marks_insufficient_when_low_n():
    rows = [_fill("forge_thin", _ts(2026, 5, 1), _ts(2026, 5, 2), 10, 1, 100)]
    out = orch._summarize_strategy("forge_thin", rows, spy_bars=[], killed=False)
    assert out["verdict"] == "INSUFFICIENT_SAMPLE"


def test_portfolio_summary_buckets_correctly():
    per_strat = [
        {"strategy": "a", "verdict": "SCALE_CANDIDATE"},
        {"strategy": "b", "verdict": "INSUFFICIENT_SAMPLE"},
        {"strategy": "c", "verdict": "KILL"},
        {"strategy": "d", "verdict": "REPAIR_EXIT"},
        {"strategy": "e", "verdict": "ALREADY_KILLED"},
    ]
    s = orch._portfolio_summary(per_strat)
    assert s["scale_candidates"] == ["a"]
    assert s["insufficient_sample"] == ["b"]
    assert s["kill_recommended"] == ["c"]
    assert s["repair"] == ["d"]
    assert s["already_killed"] == ["e"]
