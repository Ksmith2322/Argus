"""Tests for Discord-webhook failure paths.

During monitor mode Discord is the primary alert channel. If Discord goes
down for a week, we need to know the failures dropped into
`discord_failures.jsonl` — NOT that they silently muted the cooldown bucket
and we miss the next legitimate alert.

Covers `helio.fleet_monitor.send_discord`:
  - no webhook URL configured -> returns False, logs reason
  - HTTP non-2xx -> logs status, returns False, does NOT advance cooldown
  - Connection timeout / exception -> logs exception type, returns False
  - Cooldown bypass flag works
  - 2xx success advances the per-system cooldown (NOT the shared one)
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import fleet_monitor as fm  # noqa: E402


class TestSendDiscordWithoutWebhook(unittest.TestCase):
    def test_no_webhook_returns_false_and_logs_failure(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "WEBHOOK_URL", ""), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", {}):
                result = fm.send_discord("test message", system="test_sys",
                                          bypass_cooldown=True)
            self.assertFalse(result)
            # Failure recorded
            self.assertTrue(log.exists())
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "no_webhook_url")


class TestSendDiscordHttpFailures(unittest.TestCase):
    """HTTP non-2xx responses must log + return False + NOT advance cooldown."""

    def _fake_response(self, status_code: int) -> mock.MagicMock:
        r = mock.MagicMock()
        r.status_code = status_code
        return r

    def test_500_logs_status_and_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", {}), \
                 mock.patch("requests.post", return_value=self._fake_response(500)):
                result = fm.send_discord("x", system="s", bypass_cooldown=True)
            self.assertFalse(result)
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            self.assertEqual(rows[0]["reason"], "http_500")
            self.assertEqual(rows[0]["status"], 500)

    def test_non_2xx_does_not_advance_cooldown(self):
        """Critical: if the first send fails, the cooldown bucket must
        still be clear so the NEXT legitimate alert goes out."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            last_alert: dict = {}
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", last_alert), \
                 mock.patch("requests.post", return_value=self._fake_response(503)):
                fm.send_discord("x", system="test_sys")
            self.assertNotIn("test_sys", last_alert,
                "failed send advanced cooldown — next alert would be muted")

    def test_2xx_success_advances_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            last_alert: dict = {}
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", last_alert), \
                 mock.patch("requests.post", return_value=self._fake_response(204)):
                result = fm.send_discord("x", system="test_sys")
            self.assertTrue(result)
            self.assertIn("test_sys", last_alert)


class TestSendDiscordExceptions(unittest.TestCase):
    def test_timeout_logs_exception_type(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", {}), \
                 mock.patch("requests.post", side_effect=TimeoutError("slow")):
                result = fm.send_discord("x", system="s", bypass_cooldown=True)
            self.assertFalse(result)
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            self.assertIn("TimeoutError", rows[0]["reason"])

    def test_connection_error_logs(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", {}), \
                 mock.patch("requests.post",
                            side_effect=ConnectionError("no route")):
                fm.send_discord("x", system="s", bypass_cooldown=True)
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            self.assertIn("ConnectionError", rows[0]["reason"])

    def test_send_never_raises_even_on_library_failure(self):
        """Paranoia: if requests.post raises something unexpected (e.g.,
        SSL error), send_discord must not propagate — the monitor cycle
        cannot crash on alert sends."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", {}), \
                 mock.patch("requests.post", side_effect=RuntimeError("boom")):
                try:
                    fm.send_discord("x", system="s", bypass_cooldown=True)
                except Exception as e:
                    self.fail(f"send_discord leaked exception: {e}")


class TestCooldownPerKey(unittest.TestCase):
    """Regression guard for the shared-bucket bug: per-system cooldown
    must NOT mute alerts from other systems."""

    def test_cooldown_does_not_cross_systems(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            last_alert: dict = {}
            # System A was alerted 1 minute ago
            last_alert["sys_a"] = time.time() - 60

            with mock.patch.object(fm, "WEBHOOK_URL", "https://discord.test/webhook"), \
                 mock.patch.object(fm, "DISCORD_FAILURES_LOG", log), \
                 mock.patch.object(fm, "_last_alert", last_alert), \
                 mock.patch.object(fm, "ALERT_COOLDOWN_S", 1800), \
                 mock.patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 204
                # Second alert to a DIFFERENT system should still fire
                result = fm.send_discord("x", system="sys_b")
            self.assertTrue(result,
                "sys_b was muted by sys_a cooldown — per-key isolation broken")


class TestDiscordFailureLogShape(unittest.TestCase):
    """Every row in discord_failures.jsonl has a stable shape so a future
    'monitor Discord health' tool can grep it."""

    def test_failure_row_has_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "DISCORD_FAILURES_LOG", log):
                fm._log_discord_failure("test_reason", 500, "short message")
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            for k in ("ts", "reason", "status", "msg_prefix"):
                self.assertIn(k, rows[0])

    def test_msg_prefix_truncated_to_120_chars(self):
        """Long messages must not blow up the failure log."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "failures.jsonl"
            with mock.patch.object(fm, "DISCORD_FAILURES_LOG", log):
                fm._log_discord_failure("test", None, "x" * 500)
            rows = [json.loads(l) for l in log.read_text().splitlines() if l]
            self.assertLessEqual(len(rows[0]["msg_prefix"]), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
