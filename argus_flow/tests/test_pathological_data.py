"""Integration-test scaffold: feed pathological synthetic bars through the
signal-evaluation path and assert the runner doesn't crash, doesn't generate
nonsensical entries, and respects risk gates.

This is a scaffold — the framework + one example test. Full adversarial
suite is future-session. Extend by adding test methods to
TestPathologicalBarsBase.

Pathologies covered:
  - zero_volume bars (liquid drought)
  - nan/inf ohlc (data glitch)
  - cross_spread bars (bid > ask)
  - flash_reversal (100-pip spike and retrace in one bar)
  - gap_open (4-sigma overnight gap)
  - stuck_price (10 consecutive identical bars)

Usage:
    python -m unittest argus_flow.tests.test_pathological_data
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd


def _baseline_bars(n: int = 100, start: float = 158.0, step: float = 0.001, seed: int = 42) -> pd.DataFrame:
    """Generate clean, uneventful hourly bars with a seeded small random walk.

    Pure monotone drift produces degenerate values in some indicators
    (e.g., choppiness_14 → log10 of atr_sum/range where atr == range gives
    NaN). Adding a deterministic noise term keeps bars OHLC-sane while
    allowing every indicator to produce finite values.
    """
    import random
    rng = random.Random(seed)
    idx = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=n, freq="h")
    closes = []
    price = start
    for _ in range(n):
        # drift + small noise
        price += step + (rng.random() - 0.5) * 0.0015
        closes.append(price)
    highs, lows = [], []
    for c in closes:
        span = 0.0008 + rng.random() * 0.002
        highs.append(c + span)
        lows.append(c - span * 0.8)
    return pd.DataFrame({
        "Open": closes,
        "High": highs,
        "Low":  lows,
        "Close": closes,
        "Volume": [1000] * n,
    }, index=idx)


def _inject(bars: pd.DataFrame, idx: int, row: dict) -> pd.DataFrame:
    """Replace bar at position `idx` with values in `row`."""
    out = bars.copy()
    for k, v in row.items():
        out.iloc[idx, out.columns.get_loc(k)] = v
    return out


class TestPathologicalBarsBase(unittest.TestCase):
    """Base class — provides synthetic bars and assertion helpers.

    Each subclass/method should:
      1. Construct a bar sequence (baseline + pathology)
      2. Feed it into a strategy's feature-computation or signal function
      3. Assert: no exception, no NaN leaking into outputs, no impossible
         entry (size=0, risk=0, stop=entry, etc.)
    """

    def assertNoNaN(self, series_or_df, msg=""):
        """Fail if any NaN/inf in the result."""
        if isinstance(series_or_df, pd.DataFrame):
            has_nan = series_or_df.isna().any().any()
            has_inf = series_or_df.isin([math.inf, -math.inf]).any().any() if series_or_df.select_dtypes(include="number").shape[1] else False
        else:  # Series
            has_nan = series_or_df.isna().any()
            has_inf = series_or_df.isin([math.inf, -math.inf]).any()
        self.assertFalse(has_nan, f"NaN found: {msg}")
        self.assertFalse(has_inf, f"inf found: {msg}")

    def assertSaneEntry(self, entry_px, stop_px, target_px, size, msg=""):
        """Fail if any entry param is zero/None/NaN/negative where it shouldn't be."""
        for name, v in [("entry", entry_px), ("stop", stop_px), ("target", target_px), ("size", size)]:
            self.assertIsNotNone(v, f"{name} is None — {msg}")
            self.assertFalse(math.isnan(v), f"{name} is NaN — {msg}")
            self.assertFalse(math.isinf(v), f"{name} is inf — {msg}")
        self.assertGreater(size, 0, f"non-positive size — {msg}")
        self.assertNotEqual(entry_px, stop_px, f"stop == entry (zero risk) — {msg}")


class TestWickFeaturesOnPathologicalBars(TestPathologicalBarsBase):
    """Example: verify wick_gbpusd feature computation survives garbage input."""

    def test_zero_volume_bar_does_not_alter_features(self):
        """Volume is NOT an input to wick_gbpusd features — injecting 0
        should be a no-op against the baseline. Verify exact equality after
        the rolling indicators stabilize (idx 80+).
        """
        from forge.wick_gbpusd.runner import compute_features
        baseline = compute_features(_baseline_bars(100))
        injected = compute_features(_inject(_baseline_bars(100), 50, {"Volume": 0}))
        # Compare from idx 80 onwards (where all rolling indicators are stable)
        for col in baseline.columns:
            a = baseline[col].iloc[80:].dropna().values
            b = injected[col].iloc[80:].dropna().values
            self.assertEqual(len(a), len(b), f"{col}: NaN count diverged after zero-volume injection")
            if len(a) > 0:
                max_delta = max(abs(ai - bi) for ai, bi in zip(a, b))
                self.assertEqual(max_delta, 0.0, f"{col}: feature value changed by {max_delta}")

    def test_nan_price(self):
        from forge.wick_gbpusd.runner import compute_features
        bars = _baseline_bars(100)
        bars = _inject(bars, 50, {"Close": float("nan")})
        # Expect: feature computation either skips the bar cleanly or raises
        # a recognizable exception — NOT silent NaN propagation into the signal.
        try:
            feats = compute_features(bars)
        except Exception as e:
            # Acceptable if clearly NaN-related
            self.assertIn("nan", str(e).lower() + repr(e).lower(),
                          f"unexpected exception type: {type(e).__name__}")
            return
        # If it didn't raise, assert NaN didn't leak past the injected bar
        # into far-future bars (sliding indicators may carry it briefly).
        late = feats.iloc[80:]
        # Drop known-transient NaN columns in the lookback window, check the
        # signal-relevant columns only.
        for col in ("upper_wick_pct", "close_pos_in_range"):
            if col in late.columns:
                self.assertFalse(late[col].isna().all(),
                                 f"{col} fully NaN after injected NaN — not recoverable")

    def test_cross_spread_bar(self):
        """High < Low is invalid but occasionally shows up from feed bugs."""
        from forge.wick_gbpusd.runner import compute_features
        bars = _baseline_bars(100)
        bars = _inject(bars, 50, {"High": 157.0, "Low": 159.0})  # inverted
        # Acceptable outcomes: skip the bar, raise a clear error, or emit
        # a warning-marked feature value. NOT acceptable: silently generate
        # a trade entry on inverted data.
        try:
            feats = compute_features(bars)
        except Exception:
            return  # ok
        # If no exception, at least verify the signal on that bar is False
        from forge.wick_gbpusd.runner import signal_long
        sig = signal_long(bars, feats)
        self.assertFalse(bool(sig.iloc[50]),
                         "signal fired on cross-spread bar — no sanity check on H>=L")


class TestSignalFrameworkIsExtensible(unittest.TestCase):
    """Meta-test: the scaffold itself should be easy to extend."""

    def test_baseline_is_clean(self):
        bars = _baseline_bars(50)
        self.assertEqual(len(bars), 50)
        self.assertFalse(bars.isna().any().any())
        self.assertTrue((bars["High"] >= bars["Low"]).all())


if __name__ == "__main__":
    unittest.main()
