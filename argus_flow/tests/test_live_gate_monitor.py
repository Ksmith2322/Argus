"""Tests for helio.live_gate_monitor — the operational layer that compares
rolling live PF to the disciplined-gate baseline."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from helio.live_gate_monitor import (
    GateMonitorStatus,
    _classify,
    _compute_pf,
    evaluate_cohort,
    evaluate_strategy,
    load_baseline,
    write_report,
)


# ─── _compute_pf ─────────────────────────────────────────────────────

def test_compute_pf_basic():
    assert _compute_pf([2.0, -1.0, 3.0, -1.0]) == pytest.approx(5.0 / 2.0)


def test_compute_pf_no_losses_returns_inf():
    import math
    assert math.isinf(_compute_pf([1.0, 2.0, 3.0]))


def test_compute_pf_empty():
    assert _compute_pf([]) is None


# ─── _classify ────────────────────────────────────────────────────────

def _thresh():
    return {"warning_pct_of_ci_lower": 0.85, "fail_pct_of_ci_lower": 0.70,
            "min_n_for_status": 20}


def test_classify_pass_gate():
    # live_pf above 0.85 * ci_lower → PASS
    verdict, pct = _classify(1.6, 1.86, 30, thresholds=_thresh())
    assert verdict == "PASS_GATE"
    assert pct == pytest.approx(1.6 / 1.86, abs=1e-4)


def test_classify_warning():
    # live_pf in [0.70, 0.85] * ci_lower → WARNING
    verdict, pct = _classify(1.4, 1.86, 30, thresholds=_thresh())
    assert verdict == "WARNING"


def test_classify_fail():
    # live_pf below 0.70 * ci_lower → FAIL
    verdict, pct = _classify(1.0, 1.86, 30, thresholds=_thresh())
    assert verdict == "FAIL"


def test_classify_insufficient_n():
    verdict, pct = _classify(1.8, 1.86, 5, thresholds=_thresh())
    assert verdict == "INSUFFICIENT_N"
    assert pct is None


def test_classify_pf_none_yields_insufficient_n():
    verdict, pct = _classify(None, 1.86, 30, thresholds=_thresh())
    assert verdict == "INSUFFICIENT_N"


# ─── load_baseline ───────────────────────────────────────────────────

def test_load_baseline_returns_dict():
    """The committed baseline must be parseable + name our active 5 strategies."""
    baseline = load_baseline()
    assert "strategies" in baseline
    assert "alert_thresholds" in baseline
    for needed in ("forge_xs_momentum", "forge_spy_trend_follower",
                   "forge_gld_pm_long", "forge_nq_overnight", "forge_pead"):
        assert needed in baseline["strategies"], f"missing baseline for {needed}"


def test_baseline_records_ci_lower_for_each_strategy():
    baseline = load_baseline()
    for strategy, spec in baseline["strategies"].items():
        ci = spec.get("ci_95_lower")
        assert ci is not None, f"{strategy} missing ci_95_lower"
        assert ci > 0, f"{strategy} ci_95_lower must be positive, got {ci}"


# ─── evaluate_strategy with synthetic trades ─────────────────────────

def test_evaluate_strategy_no_baseline_returns_no_baseline(tmp_path):
    fake_baseline = {"alert_thresholds": _thresh(), "strategies": {}}
    st = evaluate_strategy("forge_nonexistent", baseline=fake_baseline)
    assert st.verdict == "NO_BASELINE"


def test_evaluate_strategy_synthetic_passing(monkeypatch, tmp_path):
    """Construct a synthetic trades.csv that should land PASS_GATE."""
    # Patch the strategy log dir to tmp
    from helio import live_gate_monitor as m
    monkeypatch.setitem(m.STRATEGY_LOG_DIRS, "forge_test", tmp_path)
    # Build a trades.csv: 30 trades, mostly winners → PF ~ high
    rows = []
    for i in range(30):
        rows.append({"ts": f"2026-05-{(i % 28) + 1:02d}T12:00:00+00:00",
                     "pnl_pct": 2.0 if i % 3 != 0 else -0.5})
    pd.DataFrame(rows).to_csv(tmp_path / "trades.csv", index=False)
    baseline = {
        "alert_thresholds": _thresh(),
        "strategies": {"forge_test": {
            "ci_95_lower": 1.86, "point_pf": 3.0, "n_backtest": 100,
            "config": "test"
        }},
    }
    st = evaluate_strategy("forge_test", baseline=baseline)
    assert st.verdict == "PASS_GATE", f"got {st.verdict}: {st.notes}"
    assert st.n_live_trades == 30
    assert st.live_pf_30trades > 1.86 * 0.85


def test_evaluate_strategy_synthetic_failing(monkeypatch, tmp_path):
    from helio import live_gate_monitor as m
    monkeypatch.setitem(m.STRATEGY_LOG_DIRS, "forge_test", tmp_path)
    # All losers → PF very low
    rows = [{"ts": f"2026-05-{i+1:02d}T12:00:00+00:00",
             "pnl_pct": -1.0 if i % 4 != 0 else 0.2} for i in range(28)]
    pd.DataFrame(rows).to_csv(tmp_path / "trades.csv", index=False)
    baseline = {
        "alert_thresholds": _thresh(),
        "strategies": {"forge_test": {
            "ci_95_lower": 1.86, "point_pf": 3.0, "n_backtest": 100,
            "config": "test"
        }},
    }
    st = evaluate_strategy("forge_test", baseline=baseline)
    assert st.verdict == "FAIL"


def test_evaluate_strategy_handles_pnl_pct_of_fleet(monkeypatch, tmp_path):
    """gld_pm_long-style schema uses pnl_pct_of_fleet instead of pnl_pct."""
    from helio import live_gate_monitor as m
    monkeypatch.setitem(m.STRATEGY_LOG_DIRS, "forge_test", tmp_path)
    rows = [{"ts": f"2026-05-{i+1:02d}T12:00:00+00:00",
             "pnl_pct_of_fleet": 1.5 if i % 3 != 0 else -0.5} for i in range(25)]
    pd.DataFrame(rows).to_csv(tmp_path / "trades.csv", index=False)
    baseline = {
        "alert_thresholds": _thresh(),
        "strategies": {"forge_test": {
            "ci_95_lower": 1.0, "point_pf": 1.5, "n_backtest": 25,
            "config": "test"
        }},
    }
    st = evaluate_strategy("forge_test", baseline=baseline)
    # Should NOT crash; should produce a real verdict
    assert st.verdict in ("PASS_GATE", "WARNING", "FAIL")
    assert st.n_live_trades == 25


def test_evaluate_strategy_no_trades_file(monkeypatch, tmp_path):
    """If no trades.csv exists and no archive available, status is INSUFFICIENT_N."""
    from helio import live_gate_monitor as m
    monkeypatch.setitem(m.STRATEGY_LOG_DIRS, "forge_empty", tmp_path)
    monkeypatch.setattr(m, "ARCHIVE_ROOT", tmp_path / "nonexistent")
    baseline = {
        "alert_thresholds": _thresh(),
        "strategies": {"forge_empty": {
            "ci_95_lower": 1.86, "point_pf": 3.0, "n_backtest": 100,
            "config": "test"
        }},
    }
    st = evaluate_strategy("forge_empty", baseline=baseline)
    assert st.verdict == "INSUFFICIENT_N"
    assert "no trades file" in st.notes.lower()


# ─── evaluate_cohort + write_report ─────────────────────────────────

def test_evaluate_cohort_against_committed_baseline():
    """Run against the real committed baseline. Output must be JSON-serializable
    and include every strategy in the baseline."""
    report = evaluate_cohort()
    assert "generated_at" in report
    assert "strategies" in report
    assert "verdict_counts" in report
    # 5 strategies in baseline → 5 statuses
    assert len(report["strategies"]) == 5
    # JSON-serializable
    import json
    serialized = json.dumps(report, default=str)
    assert "forge_xs_momentum" in serialized


def test_write_report_creates_file(tmp_path):
    fake_report = {"generated_at": "now", "strategies": []}
    path = tmp_path / "out.json"
    write_report(fake_report, path)
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["generated_at"] == "now"


# ─── GateMonitorStatus dataclass ─────────────────────────────────────

def test_gate_monitor_status_to_dict():
    st = GateMonitorStatus(strategy="x", verdict="PASS_GATE",
                            baseline_ci_lower=1.86, n_live_trades=30)
    d = st.to_dict()
    assert d["strategy"] == "x"
    assert d["verdict"] == "PASS_GATE"
    assert d["baseline_ci_lower"] == 1.86
