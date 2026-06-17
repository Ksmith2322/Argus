"""Contract tests for helio.domain — the shared vocabulary.

These tests enforce that the dataclasses in helio/domain.py can round-trip
with the actual on-disk canonical_fills.jsonl rows (both live-written and
backfilled-from-CSV shapes). If a test fails, either:

  1. The domain dataclass is missing a field that real data has, OR
  2. The canonical writer changed shape in a way that breaks the contract.

Either way, fix before proceeding with Phase 2 migrations.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio.domain import Fill, Signal, Trade  # noqa: E402


class TestFillFromLiveRow(unittest.TestCase):
    """A live-written fill has numeric prices and a broker anchor."""

    def test_parses_live_shape(self):
        live = {
            "ts": "2026-04-17T13:30:00+00:00",
            "strategy": "forge_gld_pm_long",
            "symbol": "GLD",
            "direction": "long",
            "side": "EXIT",
            "entry_ts": "2026-04-16T19:30:00+00:00",
            "exit_ts": "2026-04-17T13:30:00+00:00",
            "entry_px": 440.16,
            "exit_px": 441.98,
            "size": 109,
            "risk_usd": 100.0,
            "pnl_usd": 199.15,
            "exit_reason": "target",
            "broker_anchor_at_fill_usd": 1029900.0,
        }
        f = Fill.from_canonical_row(live)
        self.assertEqual(f.strategy, "forge_gld_pm_long")
        self.assertEqual(f.symbol, "GLD")
        self.assertEqual(f.direction, "long")
        self.assertEqual(f.side, "EXIT")
        self.assertAlmostEqual(f.entry_px, 440.16)
        self.assertAlmostEqual(f.pnl_usd, 199.15)
        self.assertEqual(f.broker_anchor_at_fill_usd, 1029900.0)


class TestFillFromBackfillRow(unittest.TestCase):
    """Backfilled rows have string-typed prices and a `source` marker."""

    def test_parses_backfill_shape_with_string_prices(self):
        backfill = {
            "ts": "2026-04-19T01:33:15.223992+00:00",
            "strategy": "argus_usdjpy",
            "symbol": "",
            "direction": "long",
            "side": "EXIT",
            "entry_ts": "2026-03-31T01:16:00.461972+00:00",
            "exit_ts": None,
            "entry_px": "159.73600",  # STRING, not float
            "exit_px": "159.84550",
            "size": None,
            "risk_usd": None,
            "pnl_usd": None,
            "exit_reason": "timeout",
            "broker_anchor_at_fill_usd": None,
            "source": "backfill_from_trade_csv",
        }
        f = Fill.from_canonical_row(backfill)
        self.assertEqual(f.source, "backfill_from_trade_csv")
        # String price must coerce to float
        self.assertAlmostEqual(f.entry_px, 159.736, places=3)
        self.assertAlmostEqual(f.exit_px, 159.8455, places=4)
        # Null fields stay None
        self.assertIsNone(f.size)
        self.assertIsNone(f.pnl_usd)


class TestFillRoundTripWithRealFile(unittest.TestCase):
    """Every row in argus_flow/logs/canonical_fills.jsonl must parse AND
    round-trip without losing required fields. Catches any on-disk data
    shape the dataclass hasn't accommodated."""

    def test_every_canonical_fill_roundtrips(self):
        path = _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
        if not path.exists():
            self.skipTest("canonical_fills.jsonl does not exist yet")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) == 0:
            # Right after an epoch_reset the file is intentionally empty.
            # No rows to exercise; nothing to assert here.
            self.skipTest("canonical_fills.jsonl is empty (post-reset baseline)")
        parsed = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            original = json.loads(line)
            fill = Fill.from_canonical_row(original)
            rt = fill.to_canonical_row()
            # Mandatory identity fields must survive round-trip
            self.assertEqual(rt["strategy"], original["strategy"])
            self.assertEqual(rt.get("entry_ts"), original.get("entry_ts"))
            self.assertEqual(rt.get("exit_ts"), original.get("exit_ts"))
            self.assertEqual(rt["side"], original.get("side", ""))
            parsed += 1
        # Ensure we actually exercised the file (catches an empty-file bug)
        self.assertGreater(parsed, 0)


class TestTradeFromCsvRow(unittest.TestCase):
    """Trade.from_csv_row must handle the shapes across strategy CSVs."""

    def test_parses_gld_pm_long_shape(self):
        row = {
            "ts": "2026-04-16T19:30:00+00:00",
            "direction": "long",
            "entry_px": "440.16",
            "exit_px": "441.98",
            "pnl_pts": "1.82",
            "exit_reason": "target",
            "duration_min": "1080.0",
            "pnl_usd": "199.15",
            "position_size": "109",
            "risk_usd": "100.0",
            "experiment_valid": "true",
            "config_hash": "abc123",
            "atr_entry": "0.8203",
            "stop_px": "438.78",
            "target_px": "441.80",
        }
        t = Trade.from_csv_row(row, strategy_label="forge_gld_pm_long")
        self.assertEqual(t.strategy, "forge_gld_pm_long")
        self.assertEqual(t.direction, "long")
        self.assertAlmostEqual(t.pnl_usd, 199.15)
        self.assertEqual(t.position_size, 109)
        self.assertTrue(t.experiment_valid)
        self.assertEqual(t.exit_reason, "target")

    def test_parses_gdx_gld_shape_with_alternate_field_names(self):
        """gdx_gld uses gdx_entry/gdx_exit/shares instead of entry_px/exit_px/position_size."""
        row = {
            "entry_date": "2026-04-17",
            "direction": "long_spread",
            "gdx_entry": "38.15",
            "gdx_exit": "38.82",
            "shares": "50",
            "pnl_usd": "33.50",
        }
        t = Trade.from_csv_row(row, strategy_label="forge_gdx_gld")
        self.assertEqual(t.ts, "2026-04-17")  # fell through to entry_date
        self.assertAlmostEqual(t.entry_px, 38.15)
        self.assertAlmostEqual(t.exit_px, 38.82)
        self.assertEqual(t.position_size, 50)

    def test_experiment_valid_false_is_preserved(self):
        row = {"ts": "x", "experiment_valid": "false"}
        t = Trade.from_csv_row(row)
        self.assertFalse(t.experiment_valid)


class TestFrozenImmutability(unittest.TestCase):
    """Domain objects must be frozen — no accidental in-place mutation."""

    def test_fill_is_frozen(self):
        f = Fill(strategy="x")
        with self.assertRaises(Exception):
            f.strategy = "y"  # type: ignore[misc]

    def test_trade_is_frozen(self):
        t = Trade(strategy="x")
        with self.assertRaises(Exception):
            t.strategy = "y"  # type: ignore[misc]

    def test_signal_is_frozen(self):
        s = Signal(strategy="x")
        with self.assertRaises(Exception):
            s.strategy = "y"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main(verbosity=2)
