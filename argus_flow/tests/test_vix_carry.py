"""Tests for forge.vix_carry + helio.vix_term_structure.

These cover the pure-function decision logic AND a smoke test of the
runner's evaluate-once path with a synthetic data frame. No network
I/O — yfinance is mocked."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pandas as pd
import pytest

from helio.vix_term_structure import (
    compute_term_structure,
    evaluate_carry_signal,
    realized_vol_annualized,
)


# ── compute_term_structure ─────────────────────────────────────────────────

def test_contango_ratio_above_one():
    ts = compute_term_structure(vix_close=15.0, vix3m_close=18.0)
    assert ts.in_contango
    assert not ts.in_backwardation
    assert abs(ts.contango_ratio - 1.2) < 1e-6


def test_backwardation_below_one():
    ts = compute_term_structure(vix_close=30.0, vix3m_close=24.0)
    assert ts.in_backwardation
    assert not ts.in_contango


def test_zero_vix_raises():
    with pytest.raises(ValueError):
        compute_term_structure(vix_close=0.0, vix3m_close=18.0)


# ── realized_vol_annualized ────────────────────────────────────────────────

def test_realized_vol_flat_series_is_zero():
    assert realized_vol_annualized([100.0, 100.0, 100.0, 100.0, 100.0]) == 0.0


def test_realized_vol_increases_with_volatility():
    calm = [100.0, 100.5, 100.0, 100.5, 100.0]
    spicy = [100.0, 105.0, 95.0, 103.0, 97.0]
    assert realized_vol_annualized(spicy) > realized_vol_annualized(calm) * 5


def test_realized_vol_handles_short_input():
    assert realized_vol_annualized([100.0]) == 0.0
    assert realized_vol_annualized([]) == 0.0


# ── evaluate_carry_signal ─────────────────────────────────────────────────

def test_enter_when_contango_calm_vix_low_realized_low():
    ts = compute_term_structure(vix_close=15.0, vix3m_close=18.0)  # ratio=1.2
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=10.0, has_open_position=False,
    )
    assert sig.action == "ENTER_LONG_SVXY"
    assert "contango" in sig.reason


def test_wait_when_contango_insufficient():
    ts = compute_term_structure(vix_close=15.0, vix3m_close=15.5)  # ratio≈1.033
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=10.0, has_open_position=False,
    )
    assert sig.action == "WAIT"
    assert "contango_insufficient" in sig.reason


def test_wait_when_vix_too_high():
    ts = compute_term_structure(vix_close=22.0, vix3m_close=27.0)  # ratio in contango
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=10.0, has_open_position=False,
    )
    assert sig.action == "WAIT"
    assert "vix_too_high" in sig.reason


def test_wait_when_realized_vol_too_high():
    ts = compute_term_structure(vix_close=15.0, vix3m_close=18.0)
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=20.0, has_open_position=False,
    )
    assert sig.action == "WAIT"
    assert "realized_vol_too_high" in sig.reason


def test_hold_in_contango_when_open():
    ts = compute_term_structure(vix_close=15.0, vix3m_close=18.0)
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=10.0, has_open_position=True,
    )
    assert sig.action == "HOLD"


def test_exit_when_backwardation():
    ts = compute_term_structure(vix_close=22.0, vix3m_close=20.0)  # ratio < 1.0
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=18.0, has_open_position=True,
    )
    assert sig.action == "EXIT"
    assert "contango_collapsed" in sig.reason


def test_exit_when_vix_spike():
    # Term structure can be in mild contango but VIX spike still triggers exit.
    ts = compute_term_structure(vix_close=26.0, vix3m_close=27.5)  # ratio ≈ 1.057
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=18.0, has_open_position=True,
    )
    assert sig.action == "EXIT"
    assert "vix_spike" in sig.reason


def test_no_entry_when_already_open():
    """The decision-logic path for `has_open_position=True` produces
    HOLD/EXIT — it should never produce ENTER_LONG_SVXY, even if all
    entry criteria are simultaneously met."""
    ts = compute_term_structure(vix_close=14.0, vix3m_close=17.0)
    sig = evaluate_carry_signal(
        ts, spy_realized_vol_pct=8.0, has_open_position=True,
    )
    assert sig.action in {"HOLD", "EXIT"}


# ── Runner smoke (no network) ───────────────────────────────────────────────

def _fake_history_df() -> pd.DataFrame:
    """Build a synthetic 30-day frame: persistent contango, calm regime."""
    dates = pd.date_range("2026-04-19", periods=30, freq="D", tz="UTC")
    return pd.DataFrame({
        "vix": [15.0] * 30,
        "vix3m": [18.0] * 30,  # ratio 1.20
        "spy": [500.0 + i * 0.5 for i in range(30)],  # steady uptrend, low vol
        "svxy": [30.0 + i * 0.1 for i in range(30)],
    }, index=dates)


def test_runner_evaluate_once_opens_position_in_contango(tmp_path, monkeypatch):
    from forge.vix_carry import runner

    # Redirect logs to tmp
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(runner, "TRADES_PATH", tmp_path / "trades.csv")

    # Mock data fetcher
    monkeypatch.setattr(runner, "_fetch_history", lambda period="60d": _fake_history_df())
    # Mock max_notional_usd to a known value so position sizing is deterministic
    monkeypatch.setattr(runner, "max_notional_usd",
                        lambda asset_class, strategy_label=None: 9_000.0)

    state = runner._load_state()
    result = runner._evaluate_once(state, ib=None)
    assert result["action"] == "ENTER_LONG_SVXY"
    assert state["open_trade"] is not None
    assert state["open_trade"]["position_size"] > 0
    # 9000 cap * 0.5 fraction / ~33 SVXY = ~136 shares
    assert state["open_trade"]["position_size"] >= 130
    assert state["open_trade"]["stop_px"] < state["open_trade"]["entry_px"]


def test_runner_evaluate_once_exits_on_backwardation(tmp_path, monkeypatch):
    from forge.vix_carry import runner

    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(runner, "max_notional_usd",
                        lambda asset_class, strategy_label=None: 9_000.0)

    # Pre-open a position
    state = runner._load_state()
    state["open_trade"] = {
        "entry_ts": "2026-04-19T00:00:00+00:00",
        "entry_px": 30.0,
        "position_size": 100,
        "stop_px": 27.6,
        "execution_venue": "signal_only",
        "entry_contango": 1.2,
        "entry_vix": 15.0,
    }
    state["trade_count"] = 1

    # Force backwardation today
    def _backwardation_df(period="60d"):
        df = _fake_history_df()
        df.iloc[-1, df.columns.get_loc("vix")] = 22.0
        df.iloc[-1, df.columns.get_loc("vix3m")] = 20.0  # ratio < 1
        return df

    monkeypatch.setattr(runner, "_fetch_history", _backwardation_df)

    result = runner._evaluate_once(state, ib=None)
    assert result["action"] == "EXIT"
    assert state["open_trade"] is None


def test_runner_evaluate_hard_stop_overrides_signal(tmp_path, monkeypatch):
    """If SVXY price has dropped past -8% stop, exit even if the
    contango/VIX picture says HOLD."""
    from forge.vix_carry import runner

    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(runner, "max_notional_usd",
                        lambda asset_class, strategy_label=None: 9_000.0)

    state = runner._load_state()
    state["open_trade"] = {
        "entry_ts": "2026-04-19T00:00:00+00:00",
        "entry_px": 30.0,
        "position_size": 100,
        "stop_px": 27.6,  # -8% from 30
        "execution_venue": "signal_only",
        "entry_contango": 1.2,
        "entry_vix": 15.0,
    }

    def _stopped_df(period="60d"):
        df = _fake_history_df()
        df.iloc[-1, df.columns.get_loc("svxy")] = 27.0  # under the stop
        return df

    monkeypatch.setattr(runner, "_fetch_history", _stopped_df)

    result = runner._evaluate_once(state, ib=None)
    assert result["action"] == "EXIT"
    assert result["reason"] == "hard_stop_8pct"
    assert state["open_trade"] is None
