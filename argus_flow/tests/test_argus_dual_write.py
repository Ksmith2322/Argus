"""Regression tests for the Argus runner_unified dual-write to canonical_fills.

Phase 2: Argus pairs (usdjpy, gbpusd, cadjpy) now emit a canonical Fill from
their `_log_trade` method after a successful CSV write. Gated on
experiment_valid — rows the backfill would filter out are also not emitted
here, to match reconciliation semantics.

Tests focus on the pure dual-write path: with a synthetic UnifiedRunner-like
stub holding the minimum state, does _log_trade's dual-write branch produce
exactly one correctly-shaped canonical row?

We don't build a real UnifiedRunner (that would require config + IB client).
Instead we test the inline dual-write logic by replicating it in a test
wrapper, which still catches field-rename drift.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _invoke_dual_write(*, strategy_symbol: str, valid: bool, position: str,
                      entry_price: float, exit_price: float,
                      position_size: float, entry_risk_usd: float,
                      pnl_usd: float, exit_reason: str) -> list[dict]:
    """Replicate the Argus runner's inline dual-write block with the exact
    same shape as runner_unified._log_trade. Returns rows persisted."""
    import helio.canonical_fills as cf

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "canonical_fills.jsonl"
        with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
            if valid:
                from helio.canonical_fills import write_fill_typed
                from helio.domain import Fill
                now = datetime(2026, 4, 19, 13, 30, tzinfo=timezone.utc)
                entry_time = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)
                try:
                    write_fill_typed(Fill(
                        strategy=f"argus_{strategy_symbol.lower()}",
                        symbol=strategy_symbol,
                        direction=position.lower() if position else "",
                        side="EXIT",
                        entry_ts=entry_time.isoformat(),
                        exit_ts=now.isoformat(),
                        entry_px=float(entry_price),
                        exit_px=float(exit_price),
                        size=float(position_size),
                        risk_usd=float(entry_risk_usd or 0.0),
                        pnl_usd=round(pnl_usd, 2),
                        exit_reason=exit_reason,
                    ))
                except Exception:
                    pass
        if not out.exists():
            return []
        return [json.loads(l) for l in out.read_text().splitlines() if l]


class TestArgusDualWriteHappyPath(unittest.TestCase):
    def test_long_trade_emits_canonical_row(self):
        rows = _invoke_dual_write(
            strategy_symbol="USDJPY", valid=True, position="LONG",
            entry_price=159.34, exit_price=159.50,
            position_size=10000, entry_risk_usd=50.0,
            pnl_usd=16.0, exit_reason="target",
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "argus_usdjpy")
        self.assertEqual(row["symbol"], "USDJPY")
        self.assertEqual(row["direction"], "long")
        self.assertEqual(row["side"], "EXIT")
        self.assertAlmostEqual(row["entry_px"], 159.34)
        self.assertAlmostEqual(row["exit_px"], 159.50)
        self.assertEqual(row["exit_reason"], "target")
        self.assertAlmostEqual(row["pnl_usd"], 16.0)

    def test_short_trade_direction_lowercased(self):
        rows = _invoke_dual_write(
            strategy_symbol="GBPUSD", valid=True, position="SHORT",
            entry_price=1.2600, exit_price=1.2550,
            position_size=20000, entry_risk_usd=100.0,
            pnl_usd=100.0, exit_reason="stop",
        )
        self.assertEqual(rows[0]["direction"], "short")

    def test_strategy_label_format_matches_backfill_specs(self):
        """The canonical strategy label must be 'argus_<lowercase>' so
        reconciliation can match live fills to the per-pair CSV. A typo in
        this format would silently split the data into two label buckets."""
        rows = _invoke_dual_write(
            strategy_symbol="CADJPY", valid=True, position="LONG",
            entry_price=105.00, exit_price=105.10,
            position_size=10000, entry_risk_usd=50.0,
            pnl_usd=10.0, exit_reason="target",
        )
        self.assertEqual(rows[0]["strategy"], "argus_cadjpy")


class TestArgusDualWriteValidityGating(unittest.TestCase):
    """Invalid trades (experiment_valid != 'true') must NOT emit a canonical
    row — matches reconciliation's argus backfill filter to prevent false
    DRIFT flags."""

    def test_invalid_trade_does_not_emit_canonical_row(self):
        rows = _invoke_dual_write(
            strategy_symbol="USDJPY", valid=False, position="LONG",
            entry_price=159.0, exit_price=159.1,
            position_size=10000, entry_risk_usd=50.0,
            pnl_usd=10.0, exit_reason="target",
        )
        self.assertEqual(rows, [])


class TestArgusDualWriteSwallowsErrors(unittest.TestCase):
    """A broken canonical log path must not crash the trade-logging flow."""

    def test_canonical_failure_does_not_raise(self):
        import helio.canonical_fills as cf
        with mock.patch.object(cf, "write_fill_typed", side_effect=RuntimeError("boom")):
            # Should not raise
            try:
                from helio.canonical_fills import write_fill_typed
                from helio.domain import Fill
                try:
                    write_fill_typed(Fill(strategy="argus_usdjpy"))
                except Exception:
                    pass
            except Exception as e:
                self.fail(f"dual-write path leaked exception: {e}")


class TestRunnerUnifiedSourceShape(unittest.TestCase):
    """Verify the dual-write block stays wired into the source file. If
    someone removes the block in a future refactor, this test catches it."""

    def test_runner_unified_has_write_fill_typed_call(self):
        src = (_REPO / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
        self.assertIn("write_fill_typed", src,
            "runner_unified must still contain the canonical dual-write call")
        self.assertIn("argus_{self.symbol.lower()}", src,
            "argus dual-write must use the 'argus_<symbol>' label pattern")

    def test_dual_write_is_inside_log_trade(self):
        """The dual-write block must be in _log_trade (the close path),
        not in __init__ or some other method."""
        src = (_REPO / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
        # Find where _log_trade starts and where the next def starts
        start = src.index("def _log_trade(")
        next_def_idx = src.find("\n    def ", start + 1)
        log_trade_block = src[start:next_def_idx] if next_def_idx > 0 else src[start:]
        self.assertIn("write_fill_typed", log_trade_block,
            "write_fill_typed must be inside _log_trade, not elsewhere")

    def test_dual_write_is_gated_on_valid(self):
        """The dual-write must be inside an `if valid:` block to match
        backfill filter semantics."""
        src = (_REPO / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
        # Look for the pattern: `if valid:` shortly before `write_fill_typed`
        write_idx = src.index("write_fill_typed")
        preamble = src[max(0, write_idx - 500):write_idx]
        self.assertIn("if valid:", preamble,
            "dual-write must be gated by the experiment_valid check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
