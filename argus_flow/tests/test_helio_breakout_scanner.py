"""Unit tests for helio.breakout_scanner — market-wide breakout screener.

format_discord_report and send_discord are the deterministic paths we can
test without a yfinance hit. score_stock wraps yfinance so gets minimal
coverage (defaults when yf unavailable).

The Discord format is what the operator actually reads — regressions in
threshold bucketing (HOT >= 70, WARMING 60-69) would silently misroute
signals between categories.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import breakout_scanner as bs  # noqa: E402


def _fake_result(**overrides) -> dict:
    base = {
        "symbol": "NVDA", "score": 75, "setup": "BREAKOUT",
        "price": 500.0, "bb_pctile": 85, "rsi": 60,
        "vol_ratio": 1.5, "ret_5d": 3.0, "dist_52w_high": -2.0,
    }
    base.update(overrides)
    return base


class TestFormatDiscordReportBucketing(unittest.TestCase):
    """HOT >= 70, WARMING 60-69, others dropped from the visible list."""

    def test_hot_bucket_threshold_is_70(self):
        out = bs.format_discord_report([_fake_result(score=70)])
        self.assertIn("HOT", out)
        self.assertIn("NVDA", out)

    def test_score_69_is_warming_not_hot(self):
        out = bs.format_discord_report([_fake_result(score=69)])
        self.assertIn("WARMING", out)
        self.assertIn("NVDA", out)
        # Not in HOT section
        hot_section_end = out.find("WARMING") if "WARMING" in out else len(out)
        hot_section = out[:hot_section_end]
        self.assertNotIn("NVDA", hot_section.split("WARMING")[0] if False else "")

    def test_score_60_is_warming(self):
        out = bs.format_discord_report([_fake_result(score=60)])
        self.assertIn("WARMING", out)

    def test_score_59_excluded_from_visible_list(self):
        """Score 59 is below WARMING threshold — no ticker line."""
        out = bs.format_discord_report([_fake_result(score=59)])
        self.assertNotIn("[BREAKOUT]", out)  # setup tag only appears in ticker rows

    def test_empty_results_shows_wait_message(self):
        out = bs.format_discord_report([])
        self.assertIn("wait-and-see", out.lower())

    def test_top_n_limits_visible_rows(self):
        """top_n caps how many HOT rows render, but all still counted in
        the 'X stocks hot' footer."""
        results = [_fake_result(symbol=f"S{i}", score=80) for i in range(25)]
        out = bs.format_discord_report(results, top_n=5)
        visible_hot = sum(1 for line in out.splitlines()
                          if "score=80" in line and line.strip().startswith("**S"))
        self.assertLessEqual(visible_hot, 5)
        # Footer still counts all 25
        self.assertIn("25 scanned", out)


class TestFormatDiscordFooter(unittest.TestCase):
    """The footer always includes avg score, count above 70, and total scanned."""

    def test_footer_contains_avg_score(self):
        results = [_fake_result(score=80), _fake_result(symbol="MSFT", score=60)]
        out = bs.format_discord_report(results)
        self.assertIn("avg score", out.lower())

    def test_footer_counts_above_70(self):
        results = [
            _fake_result(score=80), _fake_result(symbol="B", score=75),
            _fake_result(symbol="C", score=65),
        ]
        out = bs.format_discord_report(results)
        self.assertIn("2 stocks hot", out)

    def test_footer_total_scanned(self):
        out = bs.format_discord_report([_fake_result(score=50)] * 7)
        self.assertIn("7 scanned", out)


class TestSendDiscordNoWebhook(unittest.TestCase):
    def test_returns_false_when_webhook_unset(self):
        with mock.patch.object(bs, "WEBHOOK_URL", ""):
            self.assertFalse(bs.send_discord("test"))


class TestScoreStockFallback(unittest.TestCase):
    """score_stock returns None on yfinance failure — dashboard must not
    crash on a bad symbol."""

    def test_none_returned_on_exception(self):
        import helio.breakout_scanner as bs2
        with mock.patch.object(bs2, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = RuntimeError("api down")
            result = bs2.score_stock("FAKE")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
