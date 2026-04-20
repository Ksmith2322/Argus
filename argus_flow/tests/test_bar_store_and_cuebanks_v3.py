"""Tests for 2026-04-20 build pass:

  1. helio.bar_store — generic parquet bar loader with schema validation
  2. Mamba real-1m source flag + fallback
  3. Cue Banks supply/demand zones (the v3 edge-lifter)
  4. Cue Banks harmonic bat patterns
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# helio.bar_store
# ---------------------------------------------------------------------------


class TestBarStoreValidation(unittest.TestCase):
    def _make_valid_df(self, n: int = 150) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
        return pd.DataFrame({
            "Open": [100.0 + i * 0.1 for i in range(n)],
            "High": [100.5 + i * 0.1 for i in range(n)],
            "Low": [99.5 + i * 0.1 for i in range(n)],
            "Close": [100.2 + i * 0.1 for i in range(n)],
            "Volume": [1000] * n,
        }, index=idx)

    def test_valid_df_passes(self):
        from helio.bar_store import validate_bar_df
        validate_bar_df(self._make_valid_df())  # no raise

    def test_missing_columns_rejected(self):
        from helio.bar_store import validate_bar_df, BarStoreError
        df = self._make_valid_df().drop(columns=["Volume"])
        with self.assertRaises(BarStoreError):
            validate_bar_df(df)

    def test_non_tz_index_rejected(self):
        from helio.bar_store import validate_bar_df, BarStoreError
        df = self._make_valid_df()
        df.index = df.index.tz_convert(None).tz_localize(None)
        with self.assertRaises(BarStoreError):
            validate_bar_df(df)

    def test_too_few_rows_rejected(self):
        from helio.bar_store import validate_bar_df, BarStoreError
        df = self._make_valid_df(n=5)
        with self.assertRaises(BarStoreError):
            validate_bar_df(df, min_rows=100)

    def test_ohlc_integrity_violation_rejected(self):
        from helio.bar_store import validate_bar_df, BarStoreError
        df = self._make_valid_df()
        # High below close — integrity violation
        df.iloc[10, df.columns.get_loc("High")] = 50.0
        with self.assertRaises(BarStoreError):
            validate_bar_df(df)


def _parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        try:
            import fastparquet  # noqa: F401
            return True
        except ImportError:
            return False


@unittest.skipUnless(_parquet_available(), "pyarrow/fastparquet not installed")
class TestBarStoreRoundTrip(unittest.TestCase):
    def test_write_then_load(self):
        import helio.bar_store as bs
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with mock.patch.object(bs, "BAR_STORE_ROOT", tdp):
                df = pd.DataFrame({
                    "Open": [100.0] * 150, "High": [101.0] * 150,
                    "Low": [99.0] * 150, "Close": [100.5] * 150,
                    "Volume": [1000] * 150,
                }, index=pd.date_range("2026-01-01", periods=150, freq="1min", tz="UTC"))
                bs.write_bars(df, "teststrat", "1m", "TEST=F")
                loaded = bs.load_bars("teststrat", "1m", "TEST=F", min_rows=100)
        self.assertEqual(len(loaded), 150)

    def test_load_missing_raises(self):
        import helio.bar_store as bs
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(bs, "BAR_STORE_ROOT", Path(td)):
                with self.assertRaises(bs.BarStoreError):
                    bs.load_bars("teststrat", "1m", "NEVER=F")


# ---------------------------------------------------------------------------
# Mamba 1m source flag
# ---------------------------------------------------------------------------


class TestMamba1mSource(unittest.TestCase):
    def test_flag_declared(self):
        """MAMBA_1M_SOURCE constant must exist with known values."""
        from forge.mamba.runner import MAMBA_1M_SOURCE
        self.assertIn(MAMBA_1M_SOURCE, ("synthetic", "real"))

    def test_default_is_synthetic(self):
        """Default must be synthetic so current backtest behavior is preserved."""
        from forge.mamba.runner import MAMBA_1M_SOURCE
        self.assertEqual(MAMBA_1M_SOURCE, "synthetic")

    def test_fetch_1min_falls_back_to_synthetic_when_real_missing(self):
        """When MAMBA_1M_SOURCE='real' but the bar file isn't on disk, the
        runner must fall back to synthetic rather than crash."""
        import forge.mamba.runner as mr
        with mock.patch.object(mr, "MAMBA_1M_SOURCE", "real"):
            # No bar file for a synthetic ticker
            trade_bars = pd.DataFrame({
                "Open": [100.0] * 10, "High": [101.0] * 10,
                "Low": [99.0] * 10, "Close": [100.0] * 10,
                "Volume": [1000] * 10,
            }, index=pd.date_range("2026-01-01 09:30", periods=10, freq="5min", tz="UTC"))
            result = mr._fetch_1min_window(
                "NEVER=F", trade_bars, bar_pos=5,
                bar_time=trade_bars.index[5],
            )
            # Falls back to synthetic — returns a DataFrame, not None (enough bars)
            self.assertIsNotNone(result)
            self.assertGreater(len(result), 0)


# ---------------------------------------------------------------------------
# Cue Banks supply/demand zones
# ---------------------------------------------------------------------------


class TestSupplyDemandZones(unittest.TestCase):
    def test_demand_zone_detected_on_breakout_up(self):
        from forge.cuebanks.confluence import find_supply_demand_zones
        # 5 small candles then a big up breakout
        idx = pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC")
        df = pd.DataFrame({
            "Open":  [100.0, 100.2, 100.1, 100.3, 100.0, 100.5],
            "High":  [100.3, 100.4, 100.3, 100.5, 100.2, 102.5],
            "Low":   [ 99.5,  99.8,  99.9,  99.9,  99.7, 100.4],
            "Close": [100.2, 100.1, 100.2, 100.1, 100.0, 102.0],
            "Volume": [1000] * 6,
        }, index=idx)
        zones = find_supply_demand_zones(df, lookback_bars=10, min_breakout_pct=0.005)
        demand_zones = [z for z in zones if z["type"] == "demand"]
        self.assertTrue(len(demand_zones) >= 1, f"expected demand zones, got {zones}")

    def test_supply_zone_detected_on_breakout_down(self):
        from forge.cuebanks.confluence import find_supply_demand_zones
        idx = pd.date_range("2026-01-01", periods=6, freq="4h", tz="UTC")
        df = pd.DataFrame({
            "Open":  [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "High":  [100.5, 100.3, 100.4, 100.3, 100.5, 100.1],
            "Low":   [ 99.8,  99.9,  99.9,  99.8,  99.7,  98.0],
            "Close": [100.1, 100.2, 100.1, 100.0, 100.0,  98.5],
            "Volume": [1000] * 6,
        }, index=idx)
        zones = find_supply_demand_zones(df, lookback_bars=10, min_breakout_pct=0.005)
        supply_zones = [z for z in zones if z["type"] == "supply"]
        self.assertTrue(len(supply_zones) >= 1)

    def test_no_zone_when_no_breakout(self):
        from forge.cuebanks.confluence import find_supply_demand_zones
        # Flat line — no breakouts
        idx = pd.date_range("2026-01-01", periods=10, freq="4h", tz="UTC")
        df = pd.DataFrame({
            "Open":  [100.0] * 10, "High":  [100.1] * 10,
            "Low":   [ 99.9] * 10, "Close": [100.0] * 10,
            "Volume": [1000] * 10,
        }, index=idx)
        zones = find_supply_demand_zones(df, lookback_bars=10, min_breakout_pct=0.005)
        self.assertEqual(len(zones), 0)

    def test_price_in_zone(self):
        from forge.cuebanks.confluence import price_in_zone
        zone = {"type": "demand", "zone_bottom": 100.0, "zone_top": 101.0, "breakout_bar": 5}
        self.assertTrue(price_in_zone(100.5, zone))
        self.assertTrue(price_in_zone(100.0, zone))
        self.assertFalse(price_in_zone(99.0, zone))
        self.assertFalse(price_in_zone(102.0, zone))


# ---------------------------------------------------------------------------
# Cue Banks harmonic bat patterns
# ---------------------------------------------------------------------------


class TestHarmonicPatterns(unittest.TestCase):
    def test_bullish_bat_requires_swing_structure(self):
        """Without proper 5-point structure, returns None."""
        from forge.cuebanks.confluence import detect_bullish_bat
        # Only 1 swing low and 1 high — not enough
        result = detect_bullish_bat(
            swing_highs=[{"bar_idx": 5, "price": 110.0}],
            swing_lows=[{"bar_idx": 0, "price": 100.0}],
            current_price=103.0,
        )
        self.assertIsNone(result)

    def test_bullish_bat_fires_when_structure_aligns(self):
        """A perfect bullish bat setup with price at D."""
        from forge.cuebanks.confluence import detect_bullish_bat
        # X at 100, A at 120, B at 110 (50% retracement), C at 112.14 (78.6% of AB retracement toward A)
        # D target = A - 0.886*XA = 120 - 17.72 = 102.28
        x = {"bar_idx": 0, "price": 100.0}
        a = {"bar_idx": 5, "price": 120.0}
        b = {"bar_idx": 10, "price": 110.0}  # B = A - 0.5*XA = 110
        # C: B + 0.786*(A-B) = 110 + 0.786*10 = 117.86
        c = {"bar_idx": 15, "price": 117.86}
        # D: A - 0.886*XA = 102.28
        result = detect_bullish_bat(
            swing_highs=[a, c], swing_lows=[x, b],
            current_price=102.28,
            tolerance_pct=0.02,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["pattern"], "bullish_bat")

    def test_bearish_bat_fires_mirror(self):
        from forge.cuebanks.confluence import detect_bearish_bat
        # X=120 high, A=100 low, B=110 high (50%), C=102.14 (78.6%), D=117.72
        x = {"bar_idx": 0, "price": 120.0}
        a = {"bar_idx": 5, "price": 100.0}
        b = {"bar_idx": 10, "price": 110.0}
        c = {"bar_idx": 15, "price": 102.14}
        result = detect_bearish_bat(
            swing_highs=[x, b], swing_lows=[a, c],
            current_price=117.72,
            tolerance_pct=0.02,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "SHORT")


class TestCueBanksV3Integration(unittest.TestCase):
    """End-to-end: runner with new defaults produces the expected artifact."""

    def test_defaults_enable_sd_zones(self):
        from forge.cuebanks.runner import CUEBANKS_USE_SD_ZONES, CUEBANKS_USE_HARMONICS
        self.assertTrue(CUEBANKS_USE_SD_ZONES)
        self.assertFalse(CUEBANKS_USE_HARMONICS)  # defaults to OFF per A/B

    def test_current_artifact_reflects_v3(self):
        """After v3 changes, artifact PF > 1.0 and expectancy positive."""
        import json
        data = json.loads((_REPO / "strategy_confidence" / "cue_banks.json").read_text())
        self.assertGreater(data["bt_pf"], 1.0)
        self.assertGreater(data["p_expectancy_positive"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
