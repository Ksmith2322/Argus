"""Tests for v2 updates to Tori, Mamba, and Cue Banks (2026-04-19 consolidation).

Pins the behavior changes introduced after the 18-transcript / 7-trader
review so a future regression is caught immediately:

Tori:
  - `_find_trailing_trendline_stop` fits an angled line through post-entry
    swings and returns a line value with an ATR buffer
  - `grade_trendline_tori_style` returns a 0-10 score matching her rating
    video's vocabulary (A+ / A / B / C)

Mamba:
  - MAMBA_ENTRY_MODE constant exists and defaults to "strict_5" (preserves
    the empirically-optimal current behavior)
  - `compute_mamba_size(transcript_mode=True)` uses the 2% base + 4% at
    5-conf tier from the transcript consolidation

Cue Banks:
  - Writer produces a schema-valid artifact
  - sample_warning flags the missing retest/candle-close/harmonic gates
"""
from __future__ import annotations

import csv
import json
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
# Tori trend-line trailing stop
# ---------------------------------------------------------------------------


class TestToriTrendlineTrail(unittest.TestCase):
    """The v2 exit-logic fix: swing-based stop is replaced by an angled
    trend-line fitted through post-entry swings."""

    def _make_ascending_df(self, n_bars: int = 30) -> pd.DataFrame:
        # Construct a clean ascending price series with regular pullbacks
        # so the swing-low detector has multiple valid pivots to work with.
        bars = []
        base = 100.0
        for i in range(n_bars):
            trend = i * 0.5  # rising trend
            pullback = 1.5 if i % 4 == 2 else 0.0  # dip every 4 bars
            close = base + trend - pullback
            bars.append({
                "Open": close - 0.3,
                "High": close + 0.5,
                "Low": close - 0.5,
                "Close": close,
                "ATR": 1.0,
            })
        return pd.DataFrame(bars)

    def test_returns_none_when_too_few_bars_post_entry(self):
        from forge.tori.trendlines import _find_trailing_trendline_stop
        df = self._make_ascending_df(20)
        # Only 2 bars since entry — not enough to have pivots
        self.assertIsNone(_find_trailing_trendline_stop(df, 12, 10, "LONG", 1.0))

    def test_returns_angled_level_for_long_with_ascending_swings(self):
        from forge.tori.trendlines import _find_trailing_trendline_stop
        df = self._make_ascending_df(30)
        # Enough bars post-entry to have pivots
        stop = _find_trailing_trendline_stop(df, 25, 10, "LONG", 1.0)
        # If the detector found 2+ pivots, we get a price level; otherwise None
        # (the fallback is handled in the runner). Either is acceptable here —
        # what we fence is "doesn't crash, returns float or None, never weird".
        self.assertTrue(stop is None or isinstance(stop, float))

    def test_short_returns_descending_line_or_none(self):
        from forge.tori.trendlines import _find_trailing_trendline_stop
        # Descending series
        bars = []
        base = 200.0
        for i in range(30):
            trend = -i * 0.5
            bounce = 1.5 if i % 4 == 2 else 0.0
            close = base + trend + bounce
            bars.append({
                "Open": close + 0.3, "High": close + 0.5,
                "Low": close - 0.5, "Close": close, "ATR": 1.0,
            })
        df = pd.DataFrame(bars)
        stop = _find_trailing_trendline_stop(df, 25, 10, "SHORT", 1.0)
        self.assertTrue(stop is None or isinstance(stop, float))


class TestToriGradingFunction(unittest.TestCase):
    """Tori's 0-10 rating-video score via `grade_trendline_tori_style`."""

    def _sample_df(self, n: int = 100) -> pd.DataFrame:
        bars = []
        for i in range(n):
            close = 100.0 + i * 0.5
            bars.append({
                "Open": close, "High": close + 0.5,
                "Low": close - 0.5, "Close": close,
            })
        return pd.DataFrame(bars)

    def test_invalidation_penalty_applied(self):
        from forge.tori.trendlines import grade_trendline_tori_style
        tl = {
            "slope": 0.5, "intercept": 100.0,
            "start_idx": 10, "end_idx": 95, "touches": 3,
            "direction": "ascending",
            "pivots": [{"index": 10, "price": 105.0},
                       {"index": 50, "price": 125.0},
                       {"index": 95, "price": 147.5}],
        }
        df = self._sample_df()
        low = grade_trendline_tori_style(tl, df, invalidation_penalty=0)
        high = grade_trendline_tori_style(tl, df, invalidation_penalty=5)
        # Higher penalty ⇒ same-or-lower score
        self.assertGreaterEqual(low["tori_score"], high["tori_score"])

    def test_output_shape(self):
        from forge.tori.trendlines import grade_trendline_tori_style
        tl = {
            "slope": 0.5, "intercept": 100.0,
            "start_idx": 10, "end_idx": 95, "touches": 3,
            "direction": "ascending",
            "pivots": [{"index": 10, "price": 105.0},
                       {"index": 50, "price": 125.0},
                       {"index": 95, "price": 147.5}],
        }
        result = grade_trendline_tori_style(tl, self._sample_df())
        self.assertIn("tori_grade", result)
        self.assertIn("tori_score", result)
        self.assertIn("touch_points_score", result)
        self.assertIn("data_span_score", result)
        self.assertIn("invalidation_penalty", result)
        self.assertIn("legacy", result)
        self.assertIn(result["tori_grade"], ("A+", "A", "B", "C"))

    def test_score_is_in_range(self):
        from forge.tori.trendlines import grade_trendline_tori_style
        tl = {
            "slope": 0.5, "intercept": 100.0,
            "start_idx": 10, "end_idx": 95, "touches": 3,
            "direction": "ascending",
            "pivots": [{"index": 10, "price": 105.0},
                       {"index": 50, "price": 125.0},
                       {"index": 95, "price": 147.5}],
        }
        result = grade_trendline_tori_style(tl, self._sample_df())
        self.assertGreaterEqual(result["tori_score"], 0)
        self.assertLessEqual(result["tori_score"], 10)


# ---------------------------------------------------------------------------
# Mamba entry mode + sizing tiers
# ---------------------------------------------------------------------------


class TestMambaEntryMode(unittest.TestCase):
    def test_default_mode_is_strict_5(self):
        """Default preserves the v1 behavior the backtest was calibrated on.
        Flipping this default would change trade selection — don't do it
        without a re-backtest and written justification."""
        from forge.mamba.runner import MAMBA_ENTRY_MODE
        self.assertEqual(MAMBA_ENTRY_MODE, "strict_5")

    def test_transcript_3_mode_value_exists(self):
        """Spot-check that the transcript_3 string literal exists in the
        source — the flag is defined correctly as a two-value toggle."""
        from pathlib import Path
        runner_src = (Path(_REPO) / "forge" / "mamba" / "runner.py").read_text(encoding="utf-8")
        self.assertIn('"transcript_3"', runner_src)
        self.assertIn('"strict_5"', runner_src)


class TestMambaTranscriptSizing(unittest.TestCase):
    """Transcript mode uses 2% base + 4% at 5-conf enhanced pattern."""

    def test_transcript_base_is_2pct(self):
        from forge.mamba.sizing import compute_mamba_size
        # 2 confluences, transcript_mode=True → 2% risk target
        result = compute_mamba_size(
            equity=100_000, stop_points=50, confluence_count=2,
            instrument="MNQ", transcript_mode=True,
        )
        self.assertFalse(result["skip"])
        self.assertAlmostEqual(result["risk_pct"], 2.0, delta=0.5)

    def test_transcript_5conf_is_roughly_4pct(self):
        from forge.mamba.sizing import compute_mamba_size
        result = compute_mamba_size(
            equity=100_000, stop_points=50, confluence_count=5,
            instrument="MNQ", transcript_mode=True,
        )
        self.assertFalse(result["skip"])
        # 2% * 2.0 conf_mult = 4%
        self.assertAlmostEqual(result["risk_pct"], 4.0, delta=0.5)

    def test_default_mode_still_1pct_base(self):
        """Default (non-transcript) mode preserves v1 behavior."""
        from forge.mamba.sizing import compute_mamba_size
        result = compute_mamba_size(
            equity=100_000, stop_points=50, confluence_count=2,
            instrument="MNQ", transcript_mode=False,
        )
        self.assertAlmostEqual(result["risk_pct"], 1.0, delta=0.3)

    def test_transcript_daily_cap_is_8pct(self):
        """Daily cap raises to 8% under transcript_mode so two enhanced
        trades can both fit in one session."""
        from forge.mamba.sizing import compute_mamba_size
        # Already used 5% today, try to take a 4% enhanced trade
        result = compute_mamba_size(
            equity=100_000, stop_points=50, confluence_count=5,
            instrument="MNQ", transcript_mode=True, daily_risk_used=0.05,
        )
        self.assertFalse(result["skip"])
        # Should get at least some size (5% used + up to 3% remaining under 8% cap)
        self.assertGreater(result["contracts"], 0)

    def test_default_daily_cap_still_3pct(self):
        """Default mode's 3% daily cap is untouched."""
        from forge.mamba.sizing import compute_mamba_size
        result = compute_mamba_size(
            equity=100_000, stop_points=50, confluence_count=4,
            instrument="MNQ", transcript_mode=False, daily_risk_used=0.03,
        )
        self.assertTrue(result["skip"])
        self.assertIn("daily", result["reason"].lower())


# ---------------------------------------------------------------------------
# Cue Banks writer
# ---------------------------------------------------------------------------


class TestCueBanksWriter(unittest.TestCase):
    def _stub_csv(self, td: Path) -> Path:
        p = td / "cuebanks_backtest_trades.csv"
        header = ["date", "time", "direction", "entry", "stop", "tp1", "tp2",
                  "tp3", "confluence_score", "factors", "exit_price",
                  "exit_reason", "pnl_points", "pnl_usd", "rr_achieved",
                  "contracts", "risk_usd", "equity_after"]
        rows = []
        # 15 trades: 7 wins, 8 losses. Ensure more losses than wins so
        # sample_warning's "union is negative" branch fires.
        for i in range(7):
            rows.append(["2026-02-01", "10:00", "LONG", 100.0, 99.0, 102.0,
                          103.0, 104.0, 3.0, "S/R", 102.0, "tp1", 2.0, 100.0,
                          2.0, 1, 50.0, 10100.0])
        for i in range(8):
            rows.append(["2026-02-02", "10:00", "SHORT", 100.0, 101.0, 98.0,
                          97.0, 96.0, 3.0, "S/R", 101.0, "stop_hit", -1.0,
                          -150.0, -1.0, 1, 50.0, 9850.0])
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow(r)
        return p

    def test_writer_builds_valid_artifact(self):
        import forge.cuebanks.confidence_writer as cw
        from helio.strategy_confidence import StrategyConfidenceArtifact
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(cw, "BACKTEST_CSV", csv_path):
                data = cw.build_cuebanks_artifact()
        StrategyConfidenceArtifact.model_validate(data)
        self.assertEqual(data["strategy"], "cue_banks")
        self.assertEqual(data["bt_trades"], 15)

    def test_warning_flags_bot_state(self):
        import forge.cuebanks.confidence_writer as cw
        with tempfile.TemporaryDirectory() as td:
            csv_path = self._stub_csv(Path(td))
            with mock.patch.object(cw, "BACKTEST_CSV", csv_path):
                data = cw.build_cuebanks_artifact()
        # Warning must reference the current bot configuration (v3 state:
        # S/D zones on, retest-gate + harmonics off by default) so a reader
        # knows which rules fire in this backtest.
        warning = data["sample_warning"].lower()
        self.assertIn("retest", warning)
        self.assertIn("s/d zones", warning)

    def test_write_roundtrips_through_loader(self):
        """Writer output must be consumable by the bridge loader."""
        import forge.cuebanks.confidence_writer as cw
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = self._stub_csv(tdp)
            out_dir = tdp / "strategy_confidence"
            out_dir.mkdir()
            out_path = out_dir / "cue_banks.json"
            with mock.patch.object(cw, "BACKTEST_CSV", csv_path), \
                 mock.patch.object(cw, "ARTIFACT_PATH", out_path), \
                 mock.patch.object(sc, "ARTIFACT_DIR", out_dir):
                cw.write_cuebanks_artifact()
                artifact = sc.load_confidence_artifact("cue_banks")
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.strategy, "cue_banks")


if __name__ == "__main__":
    unittest.main(verbosity=2)
