"""Tests for forge.xs_momentum.runner control-flow.

Focused on the *gates* — month-boundary detection, allocation respect, weekend
skip. The actual order-submission path goes through IBKR and is exercised by
the live --evaluate command, not these unit tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from forge.xs_momentum import runner as xsm


# ─── _is_rebalance_due ───────────────────────────────────────────────────

def test_rebalance_due_when_new_month_and_weekday_and_early():
    """June 1, 2026 is a Monday (weekday 0). Last rebalance was in May → due."""
    state = {"last_rebalance_month": "2026-05"}
    now = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    assert xsm._is_rebalance_due(state, now) is True


def test_not_due_when_same_month_already_rebalanced():
    state = {"last_rebalance_month": "2026-06"}
    now = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    assert xsm._is_rebalance_due(state, now) is False


def test_not_due_when_weekend_even_if_month_new():
    state = {"last_rebalance_month": "2026-05"}
    # 2026-06-06 is a Saturday
    now = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)
    assert xsm._is_rebalance_due(state, now) is False


def test_not_due_when_mid_month_even_if_state_is_fresh():
    """CRITICAL: a fresh state on day 20 must NOT trigger a mid-month
    rebalance. Strategy only rebalances at month boundary."""
    state = {"last_rebalance_month": ""}
    # 2026-06-22 is a Monday but mid-month
    now = datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc)
    assert xsm._is_rebalance_due(state, now) is False


def test_due_within_first_week_window():
    """Month-end holiday could push the first-trading-wake to ~June 3
    or June 4. Still within window."""
    state = {"last_rebalance_month": "2026-05"}
    now = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)  # Monday
    assert xsm._is_rebalance_due(state, now) is True


def test_not_due_on_day_8_even_first_weekday():
    state = {"last_rebalance_month": "2026-05"}
    # If state was never updated and we get all the way to day 8
    now = datetime(2026, 6, 8, 14, 30, tzinfo=timezone.utc)  # Monday
    assert xsm._is_rebalance_due(state, now) is False


# ─── evaluate_once allocation-gate ───────────────────────────────────────

def test_evaluate_blocks_when_allocation_zero(monkeypatch, tmp_path):
    """If allocation_factor is 0, evaluate must refuse to trade (no IBKR
    connect attempted, summary action == 'blocked')."""
    # Force the rebalance-due gate to True
    monkeypatch.setattr(xsm, "_is_rebalance_due", lambda *a, **kw: True)
    # Force alloc = 0
    monkeypatch.setattr(xsm, "get_allocation_factor", lambda label: 0.0)
    # Point state/heartbeat at tmp paths to avoid polluting repo
    monkeypatch.setattr(xsm, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(xsm, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(xsm, "TRADES_PATH", tmp_path / "trades.csv")
    # Sentinel: if connect_with_retry gets called, fail loudly
    def _boom(*a, **kw):
        raise AssertionError("must not connect when alloc=0")
    monkeypatch.setattr(xsm.ibkr, "connect_with_retry", _boom)

    summary = xsm.evaluate_once()
    assert summary["action"] == "blocked"
    assert "alloc_factor" in summary["reason"]


def test_evaluate_blocks_when_allocation_read_fails(monkeypatch, tmp_path):
    """Fail-closed: if allocation_factor read raises, no trade."""
    monkeypatch.setattr(xsm, "_is_rebalance_due", lambda *a, **kw: True)
    def _raise(label):
        raise RuntimeError("config corrupted")
    monkeypatch.setattr(xsm, "get_allocation_factor", _raise)
    monkeypatch.setattr(xsm, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(xsm, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(xsm, "TRADES_PATH", tmp_path / "trades.csv")

    def _boom(*a, **kw):
        raise AssertionError("must not connect when alloc read fails")
    monkeypatch.setattr(xsm.ibkr, "connect_with_retry", _boom)

    summary = xsm.evaluate_once()
    assert summary["action"] == "blocked"
    assert "alloc_read_failed" in summary["reason"]


def test_evaluate_noops_when_not_due(monkeypatch, tmp_path):
    monkeypatch.setattr(xsm, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(xsm, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(xsm, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(xsm, "_is_rebalance_due", lambda *a, **kw: False)

    def _boom(label):
        raise AssertionError("must not read alloc when not due")
    monkeypatch.setattr(xsm, "get_allocation_factor", _boom)

    summary = xsm.evaluate_once()
    assert summary["action"] == "noop"


def test_fetch_history_records_csv_cache_fallback(monkeypatch, tmp_path):
    data_dir = tmp_path / "helio" / "data_yfinance"
    data_dir.mkdir(parents=True)
    (data_dir / "SPY_daily.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-05-20,100,101,99,100,1000\n"
        "2026-05-21,101,102,100,101,1000\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(xsm, "REPO", tmp_path)
    monkeypatch.setattr(xsm.yf, "download", lambda *a, **kw: pd.DataFrame())

    closes = xsm._fetch_history(["SPY"], period="2y")
    assert list(closes.columns) == ["SPY"]
    assert closes.attrs["source_by_ticker"] == {"SPY": "csv_cache"}


def test_data_diagnostics_red_when_bars_are_stale(monkeypatch):
    idx = pd.to_datetime(["2020-01-02"], utc=True)
    closes = pd.DataFrame({"SPY": [100.0]}, index=idx)
    closes.attrs["source_by_ticker"] = {"SPY": "csv_cache"}
    closes.attrs["missing_tickers"] = []
    monkeypatch.setattr(xsm, "_fetch_history", lambda *a, **kw: closes)

    result = xsm.data_diagnostics(period="2y")
    assert result["status"] == "RED"
    assert result["stale_tickers"] == ["SPY"]
