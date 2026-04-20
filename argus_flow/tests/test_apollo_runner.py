"""Unit tests for apollo.runner beyond score_pre_earnings_setup.

Apollo's scanner + alerting pipeline has several decision points that
aren't covered by test_apollo_scoring (score math) or
test_apollo_planned_trades (ticket/maturation). This file covers:

  - _dedup_results: only alert on (first appearance OR score delta >= 15)
  - format_discord: threshold gating (score >= 75) + category splits
  - _load_alert_history: handles missing or corrupt file

If dedup breaks, we spam Discord with the same tickers daily — which is
what was happening before this function existed (77 alerts/day, 0 trades).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestDedupResults(unittest.TestCase):
    """_dedup_results: alert only when new OR score moved >= score_change_min."""

    def _fake_history(self, existing: dict) -> mock.MagicMock:
        return mock.MagicMock(return_value=existing)

    def test_first_appearance_is_kept(self):
        import apollo.runner as ar
        with mock.patch.object(ar, "_load_alert_history", return_value={}), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 80},
            ])
        self.assertEqual(len(kept), 1)

    def test_same_score_is_dropped(self):
        """If prev alert at score=80 exists and new score is also 80,
        drop the duplicate."""
        import apollo.runner as ar
        hist = {"MSFT|2026-04-21": {"score": 80, "ts": "x"}}
        with mock.patch.object(ar, "_load_alert_history", return_value=hist), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 80},
            ])
        self.assertEqual(kept, [])

    def test_minor_score_change_is_dropped(self):
        """Score moved by 10 (< 15 default) -> still dropped."""
        import apollo.runner as ar
        hist = {"MSFT|2026-04-21": {"score": 80, "ts": "x"}}
        with mock.patch.object(ar, "_load_alert_history", return_value=hist), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 88},
            ])
        self.assertEqual(kept, [])

    def test_material_score_change_is_kept(self):
        """Score moved by 20 (>= 15 default) -> kept."""
        import apollo.runner as ar
        hist = {"MSFT|2026-04-21": {"score": 80, "ts": "x"}}
        with mock.patch.object(ar, "_load_alert_history", return_value=hist), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 95},
            ])
        self.assertEqual(len(kept), 1)

    def test_score_drop_also_kept_if_material(self):
        """A score collapse (80 -> 60, -20 delta) should also be surfaced."""
        import apollo.runner as ar
        hist = {"MSFT|2026-04-21": {"score": 80, "ts": "x"}}
        with mock.patch.object(ar, "_load_alert_history", return_value=hist), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 60},
            ])
        self.assertEqual(len(kept), 1)

    def test_different_earnings_date_treated_as_new_event(self):
        """Next earnings cycle -> same symbol but different earnings_date ->
        alerted again (Q2 != Q1)."""
        import apollo.runner as ar
        hist = {"MSFT|2026-01-21": {"score": 80, "ts": "x"}}
        with mock.patch.object(ar, "_load_alert_history", return_value=hist), \
             mock.patch("pathlib.Path.write_text"):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 80},
            ])
        self.assertEqual(len(kept), 1)

    def test_history_is_persisted_after_dedup(self):
        """The new history dict gets written to disk so the next run dedups
        against THIS run's alerts too."""
        import apollo.runner as ar
        captured = {}
        def fake_write(content):
            captured["content"] = content
        with tempfile.TemporaryDirectory() as td:
            fake_hist_path = Path(td) / "history.json"
            with mock.patch.object(ar, "_load_alert_history", return_value={}), \
                 mock.patch.object(ar, "_ALERT_HISTORY_PATH", fake_hist_path):
                ar._dedup_results([
                    {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 80},
                ])
            self.assertTrue(fake_hist_path.exists())
            data = json.loads(fake_hist_path.read_text())
            self.assertIn("MSFT|2026-04-21", data)
            self.assertEqual(data["MSFT|2026-04-21"]["score"], 80)

    def test_write_failure_is_tolerated(self):
        """OSError on history write must not crash dedup — kept list still returned."""
        import apollo.runner as ar
        with mock.patch.object(ar, "_load_alert_history", return_value={}), \
             mock.patch("pathlib.Path.write_text", side_effect=OSError("locked")):
            kept = ar._dedup_results([
                {"symbol": "MSFT", "earnings_date": "2026-04-21", "score": 80},
            ])
        self.assertEqual(len(kept), 1)


class TestLoadAlertHistory(unittest.TestCase):
    """_load_alert_history tolerates missing / corrupt files."""

    def test_missing_file_returns_empty_dict(self):
        import apollo.runner as ar
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "does_not_exist.json"
            with mock.patch.object(ar, "_ALERT_HISTORY_PATH", bad):
                self.assertEqual(ar._load_alert_history(), {})

    def test_corrupt_json_returns_empty_dict(self):
        import apollo.runner as ar
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.json"
            p.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(ar, "_ALERT_HISTORY_PATH", p):
                self.assertEqual(ar._load_alert_history(), {})

    def test_valid_json_loaded(self):
        import apollo.runner as ar
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            p.write_text(json.dumps({"k": {"score": 80}}), encoding="utf-8")
            with mock.patch.object(ar, "_ALERT_HISTORY_PATH", p):
                self.assertEqual(ar._load_alert_history(), {"k": {"score": 80}})


class TestFormatDiscord(unittest.TestCase):
    """format_discord builds the alert message, filters below 75, splits
    into imminent / watching / post_er categories."""

    def _result(self, **overrides):
        base = {
            "symbol": "MSFT", "score": 80, "direction": "long",
            "earnings_date": "2026-04-21", "days_until": 3,
            "signals": ["SIGNAL_A", "SIGNAL_B"],
            "bb_pctile": 10, "beat_rate": 0.75,
            "price": 400.0, "ret_5d": 2.0,
        }
        base.update(overrides)
        return base

    def test_high_score_imminent_appears(self):
        import apollo.runner as ar
        with mock.patch.object(ar, "_dedup_results", side_effect=lambda r: r):
            out = ar.format_discord([self._result(score=85, days_until=3)])
        self.assertIn("MSFT", out)
        self.assertIn("EARNINGS THIS WEEK", out)

    def test_below_threshold_filtered_out(self):
        """score < 75 never appears in the message."""
        import apollo.runner as ar
        with mock.patch.object(ar, "_dedup_results", side_effect=lambda r: r):
            out = ar.format_discord([self._result(score=70)])
        self.assertNotIn("MSFT", out)

    def test_dedup_applied_before_categorization(self):
        """If dedup drops a ticker, it must never appear in the formatted output."""
        import apollo.runner as ar
        with mock.patch.object(ar, "_dedup_results", return_value=[]):
            out = ar.format_discord([self._result(score=85)])
        self.assertNotIn("MSFT", out)

    def test_post_er_play_appears_in_actionable(self):
        import apollo.runner as ar
        pe = self._result(score=85, post_er_play=True,
                           trade_plan="LONG MSFT T+3 target $410")
        with mock.patch.object(ar, "_dedup_results", side_effect=lambda r: r):
            out = ar.format_discord([pe])
        self.assertIn("ACTIONABLE", out)
        self.assertIn("POST-ER DRIFT", out)
        self.assertIn("LONG MSFT", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
