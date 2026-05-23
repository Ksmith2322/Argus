"""Engine + metrics + walk-forward tests.

Use synthetic OHLC data where outcomes are predictable: e.g. a strictly
rising series should produce 1 LONG that exits on max_hold, not on stop.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from helio.backtest_factory.engine import StrategySpec, run_spec, BacktestResult
from helio.backtest_factory.metrics import compute_metrics
from helio.backtest_factory.walk_forward import walk_forward


def _ohlc(closes: list, *, start="2024-01-01", spread=0.5) -> pd.DataFrame:
    """Synthesize OHLC where high = close+spread, low = close-spread."""
    idx = pd.date_range(start=start, periods=len(closes), freq="1D")
    return pd.DataFrame({
        "Open": closes,
        "High": [c + spread for c in closes],
        "Low": [c - spread for c in closes],
        "Close": closes,
        "Volume": [1_000_000] * len(closes),
    }, index=idx)


# ─── spec wrapper ─────────────────────────────────────────────────────

def test_spec_from_dict_validates_required():
    with pytest.raises(ValueError, match="missing required"):
        StrategySpec.from_dict({"name": "x", "entry": {}})  # no exit


def test_spec_from_dict_roundtrip():
    d = {
        "name": "test", "timeframe": "1d",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 10},
        "exit": {"kind": "hold_period", "bars": 3},
    }
    s = StrategySpec.from_dict(d)
    assert s.name == "test"
    assert s.entry["fast"] == 5
    assert s.exit["bars"] == 3
    back = s.to_dict()
    assert back["name"] == "test"
    assert back["entry"]["fast"] == 5


# ─── engine validation ───────────────────────────────────────────────

def test_engine_rejects_missing_columns():
    df = pd.DataFrame({"Close": [1, 2, 3]}, index=pd.date_range("2024", periods=3))
    spec = {"name": "x", "entry": {"kind": "sma_cross", "fast": 1, "slow": 2},
            "exit": {"kind": "hold_period", "bars": 1}}
    with pytest.raises(ValueError, match="missing required columns"):
        run_spec(spec, df)


def test_engine_rejects_non_datetime_index():
    df = pd.DataFrame({"Open": [1]*40, "High": [1]*40, "Low": [1]*40, "Close": [1]*40},
                      index=range(40))
    spec = {"name": "x", "entry": {"kind": "sma_cross", "fast": 1, "slow": 2},
            "exit": {"kind": "hold_period", "bars": 1}}
    with pytest.raises(ValueError, match="DatetimeIndex"):
        run_spec(spec, df)


# ─── entry / exit smoke ──────────────────────────────────────────────

def test_sma_cross_triggers_at_least_once_on_uptrend():
    # 80 bars of slow flat then upward push → SMA cross will eventually fire
    closes = [100.0] * 50 + [100.0 + 1.5 * i for i in range(50)]
    df = _ohlc(closes)
    spec = {
        "name": "smacross",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 5},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1
    # First trade should be a winner (uptrend, fixed-hold exit)
    assert result.trades[0]["pnl_pct"] > 0


def test_hold_period_exits_at_correct_bar():
    # Flat-then-rising series forces an SMA cross. Enter + exit exactly N bars later.
    closes = [100.0] * 30 + [100.0 + i for i in range(70)]
    df = _ohlc(closes)
    spec = {
        "name": "smacross",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 7},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1
    for t in result.trades:
        assert t["bars_held"] == 7
        assert t["exit_reason"] == "timeout"


def test_atr_stop_triggers_on_pullback():
    # Flat → small upmove (entry) → sharp drop. Target is set wide
    # (target_mult=20) so we know exit is the stop, not the target.
    closes = ([100.0] * 30
              + [100 + 0.3 * i for i in range(10)]    # gentle uptrend → cross fires
              + [103.0, 100, 95, 88, 80, 70, 60])     # sharp drop → stop must hit
    df = _ohlc(closes, spread=0.2)
    spec = {
        "name": "smacross_atr",
        "entry": {"kind": "sma_cross", "fast": 3, "slow": 10},
        "exit": {"kind": "atr_stop_target", "atr_n": 5,
                 "stop_mult": 1.0, "target_mult": 20.0, "max_hold_bars": 100},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1
    assert any(t["exit_reason"] == "stop" for t in result.trades)
    # And the stopped trade must be a loser
    losers = [t for t in result.trades if t["exit_reason"] == "stop"]
    assert all(t["pnl_pct"] < 0 for t in losers)


def test_donchian_breakout_does_not_lookahead():
    # If breakout used current bar's donchian (lookahead), it would never
    # fire (close can never strictly exceed the rolling max that INCLUDES
    # itself). With proper shift(1), it fires the first time close
    # exceeds the prior N-bar high.
    closes = [100.0] * 40 + [105.0, 106.0, 107.0]  # last few break out
    df = _ohlc(closes)
    spec = {
        "name": "donchian20",
        "entry": {"kind": "donchian_breakout", "n": 20},
        "exit": {"kind": "hold_period", "bars": 1},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1


def test_engine_returns_bars_used_and_elapsed():
    closes = [100.0 + i * 0.1 for i in range(200)]
    df = _ohlc(closes)
    spec = {
        "name": "x",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
    }
    result = run_spec(spec, df)
    assert isinstance(result, BacktestResult)
    assert result.bars_used == 200
    assert result.elapsed_ms > 0
    assert "exposure_pct" in result.metrics


# ─── metrics ─────────────────────────────────────────────────────────

def test_metrics_empty_trades_safe():
    m = compute_metrics([])
    assert m["n"] == 0
    assert m["pf"] is None
    assert m["wr"] == 0.0


def test_metrics_pf_basic():
    trades = [
        {"pnl_pct": 2.0, "bars_held": 1, "direction": "LONG", "exit_reason": "target",
         "entry_dt": pd.Timestamp("2024-01-01"), "exit_dt": pd.Timestamp("2024-01-02")},
        {"pnl_pct": -1.0, "bars_held": 1, "direction": "LONG", "exit_reason": "stop",
         "entry_dt": pd.Timestamp("2024-01-03"), "exit_dt": pd.Timestamp("2024-01-04")},
        {"pnl_pct": 3.0, "bars_held": 1, "direction": "LONG", "exit_reason": "target",
         "entry_dt": pd.Timestamp("2024-01-05"), "exit_dt": pd.Timestamp("2024-01-06")},
    ]
    m = compute_metrics(trades)
    assert m["n"] == 3
    assert m["wr"] == pytest.approx(2 / 3, abs=1e-4)  # metrics round to 4dp
    assert m["pf"] == pytest.approx(5.0)  # 5 gross win / 1 gross loss
    assert m["exits"] == {"target": 2, "stop": 1}


def test_metrics_all_winners_pf_is_inf_handled():
    trades = [
        {"pnl_pct": 1.0, "bars_held": 1, "direction": "LONG", "exit_reason": "target",
         "entry_dt": pd.Timestamp("2024-01-01"), "exit_dt": pd.Timestamp("2024-01-02")},
        {"pnl_pct": 2.0, "bars_held": 1, "direction": "LONG", "exit_reason": "target",
         "entry_dt": pd.Timestamp("2024-01-03"), "exit_dt": pd.Timestamp("2024-01-04")},
    ]
    m = compute_metrics(trades)
    # inf PF emits None in the dict (not serializable cleanly)
    assert m["pf"] is None
    assert m["wr"] == 1.0


# ─── walk-forward ────────────────────────────────────────────────────

def test_walk_forward_splits_correctly():
    # Long uptrending series so SMA cross fires many times
    closes = [100.0 + np.sin(i / 5) * 5 + i * 0.05 for i in range(600)]
    df = _ohlc(closes)
    spec = {
        "name": "wf_test",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 5},
    }
    folds, agg = walk_forward(spec, df, n_folds=3, test_frac=0.3)
    assert len(folds) == 3
    assert agg["n_folds"] == 3
    assert agg["oos_pf_min"] is not None
    # Train windows must EXPAND (anchored): later fold's train_end >= earlier fold's train_end
    assert folds[0].train_end <= folds[1].train_end <= folds[2].train_end
    # Test windows must be DISJOINT
    assert folds[0].test_end <= folds[1].test_start
    assert folds[1].test_end <= folds[2].test_start


# ─── new entry kinds (macd_cross, zscore_revert) ─────────────────────

def test_macd_cross_entry_fires_on_uptrend():
    # Flat then rising → MACD histogram eventually crosses up through 0
    closes = [100.0] * 40 + [100.0 + i * 0.4 for i in range(80)]
    df = _ohlc(closes)
    spec = {
        "name": "macd",
        "entry": {"kind": "macd_cross", "fast": 12, "slow": 26, "signal": 9},
        "exit": {"kind": "hold_period", "bars": 5},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1


def test_zscore_revert_entry_fires_on_oversold_recovery():
    # Build flat 100 baseline → ONE big drop bar to 50 (z<<-2) → snap back
    # to 100 (z back above 0). With n=10 the window of 9 hundreds + 1
    # fifty makes z ≈ -3 at the dip and ≈ +0.3 the bar after.
    closes = [100.0] * 30 + [50.0] + [100.0] * 30
    df = _ohlc(closes)
    spec = {
        "name": "zrev",
        "entry": {"kind": "zscore_revert", "n": 10, "lower_thresh": -2.0, "mid_thresh": 0.0},
        "exit": {"kind": "hold_period", "bars": 5},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1


# ─── regime_filter composition ───────────────────────────────────────

def test_regime_filter_blocks_entries_when_false():
    # Use the same SMA cross spec, but with a regime filter that gates
    # entries on close > SMA(200). Since the series has < 200 bars of
    # uptrend, the filter is False throughout → entry count = 0.
    closes = [100.0] * 50 + [100.0 + i * 0.5 for i in range(80)]
    df = _ohlc(closes)
    unfiltered = run_spec({
        "name": "smacross",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
    }, df)
    filtered = run_spec({
        "name": "smacross_gated",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
        "regime_filter": {"kind": "above_sma", "n": 200},
    }, df)
    # Regime filter is always False (not enough bars for SMA(200)) → 0 trades
    assert len(filtered.trades) == 0
    # Unfiltered must have at least one trade as the baseline
    assert len(unfiltered.trades) >= 1


def test_volume_z_above_regime_blocks_when_volume_is_flat():
    # Flat volume → vol_zscore = NaN → False everywhere → no entries
    closes = [100.0] * 30 + [100.0 + i * 0.5 for i in range(60)]
    df = _ohlc(closes)
    # Set ALL volumes constant
    df["Volume"] = 1_000_000
    result = run_spec({
        "name": "vol_gated",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
        "regime_filter": {"kind": "volume_z_above", "n": 20, "threshold": 1.5},
    }, df)
    assert len(result.trades) == 0


def test_volume_z_above_regime_passes_on_volume_spike():
    # Flat baseline, sustained high volume in rising segment → SMA cross
    # fires AND volume z-score gate is True → entry fires.
    closes = [100.0] * 30 + [100.0 + i * 0.5 for i in range(60)]
    df = _ohlc(closes)
    df["Volume"] = 1_000_000  # flat baseline
    # Volume jumps 5x for the entire rising segment (any cross during it passes)
    df.iloc[30:, df.columns.get_loc("Volume")] = 5_000_000
    result = run_spec({
        "name": "vol_gated",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
        "regime_filter": {"kind": "volume_z_above", "n": 20, "threshold": 1.5},
    }, df)
    assert len(result.trades) >= 1


def test_regime_filter_passes_through_when_true():
    # Build flat → flat → rising. Use a SHORT regime SMA(20) so the gate
    # actually engages. Validation: at least one entry must fire, and every
    # entry must occur during the rising (above-SMA20) segment.
    closes = [100.0] * 30 + [100.0] * 30 + [100.0 + i * 0.3 for i in range(60)]
    df = _ohlc(closes)
    filtered = run_spec({
        "name": "smacross_gated",
        "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
        "exit": {"kind": "hold_period", "bars": 3},
        "regime_filter": {"kind": "above_sma", "n": 20},
    }, df)
    assert len(filtered.trades) >= 1
    # Entries must land in the rising segment (last third)
    rising_starts = df.index[60]
    for t in filtered.trades:
        assert t["entry_dt"] >= rising_starts


# ─── hour_of_day entry kind ──────────────────────────────────────────

def _hourly_ohlc(n_bars: int, start: str = "2024-01-02T00:00:00") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n_bars, freq="1h")
    closes = [100.0 + np.sin(i / 5) * 2 + i * 0.01 for i in range(n_bars)]
    return pd.DataFrame({
        "Open": closes, "High": [c + 0.3 for c in closes],
        "Low": [c - 0.3 for c in closes], "Close": closes,
        "Volume": [1_000_000] * n_bars,
    }, index=idx)


def test_hour_of_day_entry_fires_only_on_target_hours():
    """hour_of_day must produce entries only when the bar's UTC hour matches."""
    df = _hourly_ohlc(200, start="2024-01-02T00:00:00")
    spec = {
        "name": "hod_test",
        "entry": {"kind": "hour_of_day", "hours_utc": [13, 14]},
        "exit": {"kind": "hold_period", "bars": 2},
    }
    result = run_spec(spec, df)
    assert len(result.trades) >= 1
    for t in result.trades:
        entry_hour = pd.Timestamp(t["entry_dt"]).hour
        assert entry_hour in (13, 14), f"entry at unexpected hour {entry_hour}"


def test_hour_of_day_handles_tz_aware_index():
    """If df.index is tz-aware (US/Eastern, UTC, etc.), the hour comparison
    is done against UTC after conversion."""
    n = 100
    idx = pd.date_range(start="2024-01-02T08:00:00", periods=n, freq="1h",
                        tz="America/New_York")
    closes = [100.0 + i * 0.01 for i in range(n)]
    df = pd.DataFrame({
        "Open": closes, "High": [c + 0.3 for c in closes],
        "Low": [c - 0.3 for c in closes], "Close": closes,
        "Volume": [1_000_000] * n,
    }, index=idx)
    spec = {
        "name": "hod_tz",
        "entry": {"kind": "hour_of_day", "hours_utc": [13]},  # 13 UTC = 8 ET in winter
        "exit": {"kind": "hold_period", "bars": 1},
    }
    result = run_spec(spec, df)
    # Should have at least one entry — 8am ET converted to 13 UTC (DST aside)
    assert len(result.trades) >= 1


def test_hour_of_day_rejects_missing_hours_field():
    df = _hourly_ohlc(100)
    bad_spec = {
        "name": "bad",
        "entry": {"kind": "hour_of_day"},
        "exit": {"kind": "hold_period", "bars": 1},
    }
    with pytest.raises(ValueError, match="hours_utc"):
        run_spec(bad_spec, df)


def test_walk_forward_rejects_insufficient_bars():
    df = _ohlc([100.0] * 80)
    spec = {"name": "x",
            "entry": {"kind": "sma_cross", "fast": 5, "slow": 20},
            "exit": {"kind": "hold_period", "bars": 5}}
    with pytest.raises(ValueError, match="insufficient bars"):
        walk_forward(spec, df, n_folds=5, test_frac=0.25, min_test_bars=60)
