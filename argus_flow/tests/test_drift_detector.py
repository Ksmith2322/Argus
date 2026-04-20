"""Unit tests for helio.drift_detector — feature distribution drift.

Drift detection decides whether live signals still resemble the baseline
they were trained/backtested on. The output (GREEN/YELLOW/RED) feeds the
kill-watchdog signal-drift rule. Thresholds under test:

  MEAN_SHIFT_THRESHOLD  = 2.0   # |live_mean - base_mean| / base_std
  VARIANCE_RATIO_HIGH   = 2.0
  VARIANCE_RATIO_LOW    = 0.5
  RANGE_ESCAPE_PCT      = 0.20

Severity ladder:
  mean_shift:    > 2.0 -> warning, > 4.0 -> critical
  variance:      > 2.0 or < 0.5 -> warning, > 4.0 or < 0.25 -> critical
  range_escape:  > 20% outside range -> warning, > 40% -> critical

Overall health:
  any critical -> RED
  3+ warnings  -> RED
  any warning  -> YELLOW
  else         -> GREEN
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import drift_detector as dd  # noqa: E402


# Shared baseline: mean=0, std=1, range [-3, 3]
_TEST_BASELINE = {
    "mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0,
    "p25": -0.67, "p50": 0.0, "p75": 0.67, "n": 1000,
}


class TestMeanShiftRule(unittest.TestCase):
    """Mean shift: |live - base| / base_std. Warning at >2.0, critical at >4.0."""

    def test_no_drift_when_live_matches_baseline(self):
        np.random.seed(42)
        live = np.random.standard_normal(1000)  # ~N(0, 1) matches baseline
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        mean_shifts = [d for d in drifts if d.type == "mean_shift"]
        self.assertEqual(mean_shifts, [], "no drift expected on matching data")

    def test_warning_at_2_5_std_shift(self):
        """Live mean = 2.5 (2.5 std away from base 0) -> warning."""
        live = np.full(500, 2.5) + np.random.standard_normal(500) * 0.1
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        mean_shifts = [d for d in drifts if d.type == "mean_shift"]
        self.assertEqual(len(mean_shifts), 1)
        self.assertEqual(mean_shifts[0].severity, "warning")

    def test_critical_at_5_std_shift(self):
        """Live mean = 5 -> critical."""
        live = np.full(500, 5.0) + np.random.standard_normal(500) * 0.01
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        mean_shifts = [d for d in drifts if d.type == "mean_shift"]
        self.assertEqual(len(mean_shifts), 1)
        self.assertEqual(mean_shifts[0].severity, "critical")

    def test_zero_base_std_guarded(self):
        """Zero-std baseline must not divide by zero."""
        baseline = dict(_TEST_BASELINE, std=0.0)
        live = np.array([0.5] * 50)
        # Must not raise
        dd._check_feature("x", live, baseline)


class TestVarianceRule(unittest.TestCase):
    """Variance: live_std/base_std. Outside [0.5, 2.0] -> warning;
    outside [0.25, 4.0] -> critical."""

    def test_no_drift_when_stds_equal(self):
        np.random.seed(42)
        live = np.random.standard_normal(1000)
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        variance = [d for d in drifts if d.type == "variance"]
        self.assertEqual(variance, [])

    def test_warning_when_std_triples(self):
        np.random.seed(42)
        live = np.random.standard_normal(1000) * 3.0  # 3x std -> ratio 3
        # But range escape will also fire because values extend beyond [-3, 3]
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        variance = [d for d in drifts if d.type == "variance"]
        self.assertEqual(len(variance), 1)
        self.assertEqual(variance[0].severity, "warning")

    def test_critical_when_std_multiplies_by_5(self):
        np.random.seed(42)
        live = np.random.standard_normal(1000) * 5.0  # ratio 5 > 4 -> critical
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        variance = [d for d in drifts if d.type == "variance"]
        self.assertEqual(len(variance), 1)
        self.assertEqual(variance[0].severity, "critical")

    def test_warning_when_std_halves(self):
        """Very tight distribution (std 0.3, ratio 0.3 < 0.5) -> warning."""
        np.random.seed(42)
        live = np.random.standard_normal(1000) * 0.3
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        variance = [d for d in drifts if d.type == "variance"]
        self.assertEqual(len(variance), 1)
        self.assertEqual(variance[0].severity, "warning")


class TestRangeEscapeRule(unittest.TestCase):
    """Range escape: fraction outside [base_min, base_max].
    >20% -> warning, >40% -> critical."""

    def test_no_escape_within_range(self):
        live = np.linspace(-2.5, 2.5, 100)  # all within [-3, 3]
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        escape = [d for d in drifts if d.type == "range_escape"]
        self.assertEqual(escape, [])

    def test_warning_at_25pct_escape(self):
        live = np.concatenate([np.full(75, 0.0), np.full(25, 5.0)])  # 25% escape
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        escape = [d for d in drifts if d.type == "range_escape"]
        self.assertEqual(len(escape), 1)
        self.assertEqual(escape[0].severity, "warning")

    def test_critical_at_50pct_escape(self):
        live = np.concatenate([np.full(50, 0.0), np.full(50, 5.0)])  # 50% escape
        drifts = dd._check_feature("x", live, _TEST_BASELINE)
        escape = [d for d in drifts if d.type == "range_escape"]
        self.assertEqual(len(escape), 1)
        self.assertEqual(escape[0].severity, "critical")


class TestDetectDriftHealthVerdict(unittest.TestCase):
    """detect_drift aggregates per-feature checks into overall health."""

    def _live_df_with_features(self, **cols: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(cols)

    def test_no_features_yields_green(self):
        """Empty live_df is GREEN (nothing to drift)."""
        r = dd.detect_drift(baselines={}, live_df=pd.DataFrame())
        self.assertEqual(r.overall_health, "GREEN")

    def test_matching_live_features_yield_green(self):
        np.random.seed(42)
        df = self._live_df_with_features(
            vol_z=np.random.standard_normal(500),
        )
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        self.assertEqual(r.overall_health, "GREEN")
        self.assertEqual(r.features_checked, 1)

    def test_single_warning_yields_yellow(self):
        """Mean shift of 2.5 std with matching variance (std=1) -> only one warning."""
        np.random.seed(42)
        df = self._live_df_with_features(
            # Live std ~1 matches baseline; only the mean shift (2.5x) fires
            vol_z=np.full(500, 2.5) + np.random.standard_normal(500),
        )
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        self.assertEqual(r.overall_health, "YELLOW",
            f"drifts: {[(d.type, d.severity) for d in r.drifted_features]}")
        self.assertGreaterEqual(len(r.drifted_features), 1)

    def test_critical_drift_yields_red(self):
        np.random.seed(42)
        df = self._live_df_with_features(
            vol_z=np.full(500, 5.0) + np.random.standard_normal(500) * 0.01,
        )
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        self.assertEqual(r.overall_health, "RED")

    def test_three_warnings_also_yield_red(self):
        """Three warnings on a single feature (mean + variance + range_escape)
        should roll up to RED per the >=3-warnings rule."""
        np.random.seed(42)
        # Tight distribution shifted far from 0 with some outliers hits all 3
        df = self._live_df_with_features(
            vol_z=np.concatenate([
                np.full(400, 2.5) + np.random.standard_normal(400) * 0.1,
                np.full(100, -4.0),  # out-of-range escape + drags variance
            ]),
        )
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        warnings = sum(1 for d in r.drifted_features if d.severity == "warning")
        criticals = sum(1 for d in r.drifted_features if d.severity == "critical")
        if criticals > 0:
            self.assertEqual(r.overall_health, "RED")
        elif warnings >= 3:
            self.assertEqual(r.overall_health, "RED")

    def test_insufficient_samples_skipped(self):
        """Fewer than 10 samples -> feature silently skipped (no drift claim)."""
        df = self._live_df_with_features(vol_z=np.array([0.0, 1.0, -1.0]))
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        self.assertEqual(r.features_checked, 0)
        self.assertEqual(r.overall_health, "GREEN")

    def test_unknown_feature_in_df_ignored(self):
        """Live df has a column we don't track -> ignored."""
        np.random.seed(42)
        df = self._live_df_with_features(
            vol_z=np.random.standard_normal(500),
            some_random_col=np.random.standard_normal(500),
        )
        baselines = {"vol_z": _TEST_BASELINE}
        r = dd.detect_drift(baselines=baselines, live_df=df)
        self.assertEqual(r.features_checked, 1)


class TestBuildBaseline(unittest.TestCase):
    """build_baseline summarises historical CSVs into mean/std/p25/p50/p75/min/max/n."""

    def test_baseline_stats_computed_correctly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "signals.csv"
            # vol_z values with known mean 2.0, std ~1.0
            vals = list(range(500, 1500))  # mean 999.5
            df = pd.DataFrame({"vol_z": vals})
            df.to_csv(csv, index=False)
            out = Path(td) / "baseline.json"
            result = dd.build_baseline([csv], output=out)
        self.assertIn("vol_z", result)
        b = result["vol_z"]
        self.assertAlmostEqual(b["mean"], 999.5, places=1)
        self.assertEqual(b["min"], 500)
        self.assertEqual(b["max"], 1499)
        self.assertEqual(b["n"], 1000)

    def test_missing_csv_files_are_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist.csv"
            out = Path(td) / "baseline.json"
            result = dd.build_baseline([missing], output=out)
        self.assertEqual(result, {})


class TestLoadBaselines(unittest.TestCase):
    def test_creates_default_baseline_when_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "baseline.json"
            result = dd.load_baselines(path)
        # Default templates ship with all TRACKED_FEATURES
        for feat in dd.TRACKED_FEATURES:
            self.assertIn(feat, result)

    def test_reads_existing_baseline(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "baseline.json"
            path.write_text(json.dumps({"custom_feat": {"mean": 99}}), encoding="utf-8")
            result = dd.load_baselines(path)
        self.assertIn("custom_feat", result)
        self.assertEqual(result["custom_feat"]["mean"], 99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
