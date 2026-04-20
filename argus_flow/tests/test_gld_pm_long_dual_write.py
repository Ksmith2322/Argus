"""Regression test for the forge/gld_pm_long dual-write to canonical_fills.

Phase 2 proof: gld_pm_long's _close() now writes a Fill to canonical_fills
in addition to its per-strategy trades.csv. This test makes sure:

  1. A successful close emits one canonical_fills row with the right shape.
  2. If canonical_fills.write_fill_typed errors out, trading continues
     (the try/except swallow is preserved).
  3. The row appears AS THE TRADE CLOSES — not on the nightly backfill.
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


def _stub_state() -> dict:
    return {
        "open_trade": {
            "entry_ts": "2026-04-18T19:00:00+00:00",
            "entry_px": 440.16,
            "target_px": 441.80,
            "stop_px": 438.78,
            "atr_entry": 0.82,
            "position_size": 109,
            "risk_usd": 100.0,
            "session_id": "sess",
            "config_hash": "hash123",
            "git_sha": "sha",
            "signal_hour_utc": 19,
        },
        "trade_count": 5,
        "runtime_start": time.time(),
        "session_id": "sess",
    }


class TestDualWriteHappyPath(unittest.TestCase):
    def test_close_emits_canonical_row(self):
        import helio.canonical_fills as cf
        from forge.gld_pm_long import runner as r
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(r, "_append_trade"):  # isolate canonical effect
                r._close(_stub_state(), "2026-04-19T13:00:00+00:00", 441.98, "target")
            lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1,
            "_close should emit exactly one canonical_fills row per trade")
        row = json.loads(lines[0])
        self.assertEqual(row["strategy"], "forge_gld_pm_long")
        self.assertEqual(row["symbol"], "GLD")
        self.assertEqual(row["direction"], "long")
        self.assertEqual(row["side"], "EXIT")
        self.assertAlmostEqual(row["entry_px"], 440.16)
        self.assertAlmostEqual(row["exit_px"], 441.98)
        # pnl = (441.98 - 440.16) * 109 = 1.82 * 109 = 198.38
        self.assertAlmostEqual(row["pnl_usd"], 198.38, places=2)
        self.assertEqual(row["exit_reason"], "target")

    def test_entry_ts_carried_through_unchanged(self):
        """Downstream reconciliation matches on (strategy, entry_ts, exit_ts) —
        both timestamps must survive the dual-write path intact."""
        import helio.canonical_fills as cf
        from forge.gld_pm_long import runner as r
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            state = _stub_state()
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(r, "_append_trade"):
                r._close(state, "2026-04-19T13:00:00+00:00", 441.98, "target")
            row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["entry_ts"], "2026-04-18T19:00:00+00:00")
        self.assertEqual(row["exit_ts"], "2026-04-19T13:00:00+00:00")


class TestDualWriteFailuresDoNotBreakTrading(unittest.TestCase):
    """If the canonical write fails for any reason, _close must still
    complete normally and clear open_trade. Paper trading must not break
    because of a broken log path."""

    def test_write_fill_exception_does_not_raise(self):
        from forge.gld_pm_long import runner as r
        state = _stub_state()
        with mock.patch.object(r, "_append_trade"), \
             mock.patch("helio.canonical_fills.write_fill_typed",
                        side_effect=RuntimeError("log path broken")):
            # Must not raise
            r._close(state, "2026-04-19T13:00:00+00:00", 441.98, "target")
        # Open trade was closed out despite the canonical write failure
        self.assertIsNone(state["open_trade"])
        self.assertEqual(state["trade_count"], 6)

    def test_canonical_fills_import_failure_does_not_raise(self):
        """If helio.canonical_fills is unimportable (not possible today but
        guard against a future breakage), trading still closes."""
        from forge.gld_pm_long import runner as r
        state = _stub_state()
        with mock.patch.object(r, "_append_trade"), \
             mock.patch.dict("sys.modules", {"helio.canonical_fills": None}):
            # Importing None raises — confirm _close still completes
            try:
                r._close(state, "2026-04-19T13:00:00+00:00", 441.98, "target")
            except Exception as e:
                self.fail(f"_close raised on canonical import failure: {e}")
        self.assertIsNone(state["open_trade"])


class TestDualWriteDoesNotDouble_Count(unittest.TestCase):
    """Each call to _close emits ONE canonical row. Back-to-back closes
    must not interfere."""

    def test_two_closes_produce_two_rows(self):
        import helio.canonical_fills as cf
        from forge.gld_pm_long import runner as r
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(r, "_append_trade"):
                s1 = _stub_state()
                r._close(s1, "2026-04-19T13:00:00+00:00", 441.98, "target")
                s2 = _stub_state()
                s2["trade_count"] = 6
                r._close(s2, "2026-04-20T13:00:00+00:00", 439.00, "stop")
            lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        rows = [json.loads(l) for l in lines]
        self.assertEqual(rows[0]["exit_reason"], "target")
        self.assertEqual(rows[1]["exit_reason"], "stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
