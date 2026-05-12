"""Tests for helio.capital_ladder.

The ladder is a capital safety system. These tests focus on fail-closed
behavior, exact stage boundaries, and the requirement that missing evidence
blocks promotion.
"""
from __future__ import annotations

from helio import capital_ladder as cl


def _config():
    return {
        "version": "test",
        "total_capital_reference_usd": 100_000,
        "stages": [
            {"name": "RESEARCH_FREEZE", "approved_capital_usd": 0, "criteria": {}},
            {
                "name": "LIVE_10K",
                "approved_capital_usd": 10_000,
                "criteria": {
                    "min_qualified_strategies": 1,
                    "min_clusters": 1,
                    "min_real_fills_per_strategy": 50,
                    "min_live_days_per_strategy": 30,
                    "min_strategy_live_ir": 0.7,
                    "max_paper_live_ir_delta": 0.3,
                    "min_spy_coverage_pct": 0.85,
                    "max_strategy_drawdown_pct": 6.0,
                    "min_capacity_test_usd": 10_000,
                    "min_clean_ops_days": 30,
                    "max_silent_failures_30d": 0,
                    "max_phantom_fills_30d": 0,
                    "max_tws_cascades_30d": 0,
                    "min_broker_reconcile_passed_days": 14,
                    "require_kill_switch_tested": True,
                },
            },
            {
                "name": "LIVE_25K",
                "approved_capital_usd": 25_000,
                "criteria": {
                    "min_qualified_strategies": 2,
                    "min_clusters": 2,
                    "min_real_fills_per_strategy": 50,
                    "min_live_days_per_strategy": 45,
                    "min_strategy_live_ir": 0.8,
                    "max_paper_live_ir_delta": 0.3,
                    "min_spy_coverage_pct": 0.85,
                    "max_strategy_drawdown_pct": 7.5,
                    "min_capacity_test_usd": 25_000,
                    "min_fleet_live_ir": 0.8,
                    "max_fleet_drawdown_pct": 8.0,
                    "max_worst_single_day_pct": 3.0,
                    "min_clean_ops_days": 30,
                    "max_silent_failures_30d": 0,
                    "max_phantom_fills_30d": 0,
                    "max_tws_cascades_30d": 0,
                    "min_broker_reconcile_passed_days": 21,
                    "require_kill_switch_tested": True,
                    "max_pairwise_corr_abs": 0.65,
                    "min_effective_independent_bets": 1.5,
                },
            },
        ],
    }


def _strategy(name: str, cluster: str, fills: int = 50, days: int = 45, ir: float = 0.9, capacity: int = 25_000):
    return {
        "strategy": name,
        "cluster": cluster,
        "real_fill_trades": fills,
        "live_days": days,
        "live_ir": ir,
        "paper_live_ir_delta_abs": 0.2,
        "spy_coverage_pct": 0.9,
        "max_drawdown_pct": 4.0,
        "capacity_test_usd": capacity,
    }


def _evidence(strategies):
    return {
        "strategies": {s["strategy"]: s for s in strategies},
        "ops": {
            "clean_ops_days": 45,
            "silent_failures_30d": 0,
            "phantom_fills_30d": 0,
            "tws_cascades_30d": 0,
            "broker_reconcile_passed_days": 30,
            "kill_switch_tested": True,
        },
        "portfolio": {
            "fleet_live_ir": 0.9,
            "fleet_max_drawdown_pct": 5.0,
            "worst_single_day_pct": 2.0,
        },
        "diversification": {
            "max_pairwise_corr_abs": 0.4,
            "effective_independent_bets": 1.8,
        },
    }


def test_missing_evidence_stays_research_freeze():
    report = cl.evaluate_ladder({"strategies": {}, "ops": {}, "portfolio": {}}, _config())
    assert report["current_stage"] == "RESEARCH_FREEZE"
    assert report["approved_capital_usd"] == 0
    assert report["next_stage"]["name"] == "LIVE_10K"
    assert "qualified_strategies 0/1" in report["next_stage"]["blockers"][0]


def test_live_10k_passes_at_exact_boundaries():
    s = _strategy("s1", "METALS", fills=50, days=30, ir=0.7, capacity=10_000)
    report = cl.evaluate_ladder(_evidence([s]), _config())
    assert report["current_stage"] == "LIVE_10K"
    assert report["approved_capital_usd"] == 10_000


def test_live_10k_fails_when_paper_live_delta_missing():
    s = _strategy("s1", "METALS", fills=50, days=30, ir=0.7, capacity=10_000)
    s.pop("paper_live_ir_delta_abs")
    report = cl.evaluate_ladder(_evidence([s]), _config())
    assert report["current_stage"] == "RESEARCH_FREEZE"
    assert any("paper_live_ir_delta_abs" in b for b in report["next_stage"]["blockers"])


def test_live_25k_requires_second_cluster():
    a = _strategy("s1", "METALS")
    b = _strategy("s2", "METALS")
    report = cl.evaluate_ladder(_evidence([a, b]), _config())
    assert report["current_stage"] == "LIVE_10K"
    assert report["next_stage"]["name"] == "LIVE_25K"
    assert any("qualified_clusters 1/2" in b for b in report["next_stage"]["blockers"])


def test_live_25k_passes_with_two_clusters_and_portfolio_evidence():
    a = _strategy("s1", "METALS")
    b = _strategy("s2", "FX_USD")
    report = cl.evaluate_ladder(_evidence([a, b]), _config())
    assert report["current_stage"] == "LIVE_25K"
    assert report["approved_capital_usd"] == 25_000


def test_smoke_stage_accepts_low_sample_ir_when_flag_set():
    cfg = _config()
    cfg["stages"].append({
        "name": "LIVE_SMOKE_5K",
        "approved_capital_usd": 5000,
        "criteria": {
            "min_qualified_strategies": 1,
            "min_clusters": 1,
            "min_real_fills_per_strategy": 10,
            "min_live_days_per_strategy": 14,
            "min_strategy_live_ir": 0.3,
            "allow_low_sample_ir": True,
            "max_paper_live_ir_delta": 0.5,
            "min_spy_coverage_pct": 0.8,
            "max_strategy_drawdown_pct": 5.0,
            "min_capacity_test_usd": 5_000,
            "min_clean_ops_days": 14,
            "max_silent_failures_30d": 0,
            "max_phantom_fills_30d": 0,
            "max_tws_cascades_30d": 0,
            "min_broker_reconcile_passed_days": 7,
            "require_kill_switch_tested": True,
        },
    })

    s = _strategy("s1", "METALS", fills=20, days=14, ir=0.0, capacity=5_000)
    s["live_ir"] = None  # strict IR missing
    s["live_ir_low_sample"] = 0.6  # low-sample IR present
    s["max_drawdown_pct"] = 4.0

    evidence = _evidence([s])
    evidence["ops"]["broker_reconcile_passed_days"] = 7
    evidence["ops"]["clean_ops_days"] = 14

    report = cl.evaluate_ladder(evidence, cfg)
    assert report["current_stage"] == "LIVE_SMOKE_5K"
    assert report["approved_capital_usd"] == 5_000


def test_strict_stage_ignores_low_sample_ir_even_when_present():
    cfg = _config()
    s = _strategy("s1", "METALS", fills=50, days=30, ir=0.0, capacity=10_000)
    s["live_ir"] = None
    s["live_ir_low_sample"] = 0.9  # would pass a low-sample check, but...
    report = cl.evaluate_ladder(_evidence([s]), cfg)
    assert report["current_stage"] == "RESEARCH_FREEZE"  # LIVE_10K does not opt in


def test_ops_failure_blocks_promotion_even_with_good_strategies():
    e = _evidence([_strategy("s1", "METALS")])
    e["ops"]["phantom_fills_30d"] = 1
    report = cl.evaluate_ladder(e, _config())
    assert report["current_stage"] == "RESEARCH_FREEZE"
    assert any("ops.phantom_fills_30d" in b for b in report["next_stage"]["blockers"])
