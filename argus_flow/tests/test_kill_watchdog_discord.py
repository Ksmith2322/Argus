"""Tests for kill_watchdog Discord-message composition.

When kill_watchdog flags a strategy, the operator reads the Discord
message to decide what to do. If the message format breaks, the alert
goes out but the operator can't parse what's failing.

Covers:
  - _notify_discord_if_hits silence when no hits
  - Message contains every flagged strategy's label + triggered rules
  - Discord send failure falls back to discord_failures log
  - Message format is stable (regex-matchable)
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

from helio import kill_watchdog as kw  # noqa: E402


def _report_with(*, kill_candidates=None, drift_warnings=None,
                strategies=None) -> dict:
    return {
        "generated_at": "2026-04-19T12:00:00+00:00",
        "anchor_at_time_usd": 10000,
        "strategies": strategies or [],
        "summary": {
            "kill_candidates": kill_candidates or [],
            "drift_warnings": drift_warnings or [],
            "total_evaluated": len(strategies or []),
        },
    }


class TestNoHitsNoDiscord(unittest.TestCase):
    """If neither kill_candidates nor drift_warnings, no Discord call."""

    def test_all_ok_report_sends_nothing(self):
        report = _report_with()
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        mock_send.assert_not_called()


class TestKillCandidateMessage(unittest.TestCase):
    """Message must contain strategy label + each triggered rule."""

    def test_kill_candidate_strategy_appears_in_message(self):
        report = _report_with(
            kill_candidates=["forge_broken"],
            strategies=[{
                "strategy": "forge_broken",
                "status": "KILL_CANDIDATE",
                "stats": {},
                "triggered_rules": [{
                    "rule": "pf_below_1_on_30plus_trades",
                    "severity": "kill_candidate",
                    "detail": "30 trades, PF=0.75",
                }],
            }],
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        self.assertGreater(mock_send.call_count, 0)
        msg = mock_send.call_args[0][0]
        # Contains the "KILL WATCHDOG ALERT" header
        self.assertIn("KILL WATCHDOG ALERT", msg)
        # Contains the strategy label
        self.assertIn("forge_broken", msg)
        # Contains the rule
        self.assertIn("pf_below_1_on_30plus_trades", msg)
        # Contains the detail
        self.assertIn("PF=0.75", msg)
        # Uses KILL_CANDIDATE tag
        self.assertIn("KILL_CANDIDATE", msg)

    def test_send_discord_called_with_kill_watchdog_system_key(self):
        """Using a distinct system key means kill alerts aren't muted by
        the fleet_monitor's restart-cooldown bucket."""
        report = _report_with(
            kill_candidates=["s"],
            strategies=[{
                "strategy": "s", "status": "KILL_CANDIDATE",
                "stats": {}, "triggered_rules": [{
                    "rule": "x", "severity": "kill_candidate", "detail": "d",
                }],
            }],
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        args, kwargs = mock_send.call_args
        self.assertEqual(kwargs.get("system") or (args[1] if len(args) > 1 else None),
                         "kill_watchdog")

    def test_multiple_strategies_all_appear(self):
        strategies = [{
            "strategy": f"s{i}", "status": "KILL_CANDIDATE",
            "stats": {}, "triggered_rules": [{
                "rule": f"rule{i}", "severity": "kill_candidate", "detail": f"d{i}",
            }],
        } for i in range(3)]
        report = _report_with(
            kill_candidates=[s["strategy"] for s in strategies],
            strategies=strategies,
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        msg = mock_send.call_args[0][0]
        for s in strategies:
            self.assertIn(s["strategy"], msg)
            self.assertIn(s["triggered_rules"][0]["rule"], msg)


class TestDriftWarningMessage(unittest.TestCase):
    def test_drift_warning_appears(self):
        report = _report_with(
            drift_warnings=["forge_drifty"],
            strategies=[{
                "strategy": "forge_drifty", "status": "DRIFT_WARNING",
                "stats": {}, "triggered_rules": [{
                    "rule": "signal_drift_severe_3_consecutive_days",
                    "severity": "drift_warning",
                    "detail": "3 severe runs",
                }],
            }],
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        msg = mock_send.call_args[0][0]
        self.assertIn("DRIFT_WARNING", msg)
        self.assertIn("forge_drifty", msg)


class TestDiscordFailureFallsBackToLog(unittest.TestCase):
    """If send_discord raises, we log to discord_failures.jsonl instead
    of propagating."""

    def test_send_exception_written_to_failures_log(self):
        report = _report_with(
            kill_candidates=["s"],
            strategies=[{
                "strategy": "s", "status": "KILL_CANDIDATE",
                "stats": {}, "triggered_rules": [{
                    "rule": "x", "severity": "kill_candidate", "detail": "d",
                }],
            }],
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "failures.jsonl"
            with mock.patch.object(kw, "DISCORD_FAILURES_LOG", log_path), \
                 mock.patch("helio.fleet_monitor.send_discord",
                            side_effect=RuntimeError("webhook down")):
                # Must not raise
                kw._notify_discord_if_hits(report)
            self.assertTrue(log_path.exists())
            rows = [json.loads(l) for l in log_path.read_text().splitlines() if l]
            self.assertEqual(len(rows), 1)
            self.assertIn("kill_watchdog_alert_failed", rows[0]["reason"])
            self.assertIn("RuntimeError", rows[0]["reason"])

    def test_message_prefix_in_failure_log(self):
        report = _report_with(
            kill_candidates=["s"],
            strategies=[{
                "strategy": "s", "status": "KILL_CANDIDATE",
                "stats": {}, "triggered_rules": [{
                    "rule": "test_rule", "severity": "kill_candidate",
                    "detail": "test detail",
                }],
            }],
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "failures.jsonl"
            with mock.patch.object(kw, "DISCORD_FAILURES_LOG", log_path), \
                 mock.patch("helio.fleet_monitor.send_discord",
                            side_effect=Exception("x")):
                kw._notify_discord_if_hits(report)
            row = json.loads(log_path.read_text().splitlines()[0])
        # The message prefix contains the alert header
        self.assertIn("KILL WATCHDOG ALERT", row["msg_prefix"])


class TestMessageShapeStability(unittest.TestCase):
    """The message format is what the operator reads. Regressions in
    bullet style / section headers change what they scan for."""

    def test_each_strategy_uses_bullet_format(self):
        """- **{strategy}** [{status}]  — the bullet pattern is stable."""
        report = _report_with(
            kill_candidates=["s"],
            strategies=[{
                "strategy": "s", "status": "KILL_CANDIDATE", "stats": {},
                "triggered_rules": [{"rule": "r", "severity": "kill_candidate",
                                      "detail": "d"}],
            }],
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        msg = mock_send.call_args[0][0]
        # Format is bullet + bold label + bracketed status
        self.assertIn("- **s**", msg)
        self.assertIn("[KILL_CANDIDATE]", msg)

    def test_rules_use_indented_bullets(self):
        report = _report_with(
            kill_candidates=["s"],
            strategies=[{
                "strategy": "s", "status": "KILL_CANDIDATE", "stats": {},
                "triggered_rules": [
                    {"rule": "ruleA", "severity": "x", "detail": "detailA"},
                    {"rule": "ruleB", "severity": "x", "detail": "detailB"},
                ],
            }],
        )
        with mock.patch("helio.fleet_monitor.send_discord") as mock_send:
            kw._notify_discord_if_hits(report)
        msg = mock_send.call_args[0][0]
        # Both rules appear in the message
        self.assertIn("ruleA", msg)
        self.assertIn("ruleB", msg)
        self.assertIn("detailA", msg)
        self.assertIn("detailB", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
