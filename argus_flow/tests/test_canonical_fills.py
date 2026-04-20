"""Unit tests for helio.canonical_fills — the writer, reader, and
backfill-from-CSV paths.

canonical_fills.jsonl is the cross-strategy source of truth for closed
trades. Existing tests (test_canonical_rotation_readiness) cover the
reader-glob path for future rotated archives. This file fills the other
gap: writer behavior and the backfill dedup logic that stops us from
double-counting the same trade across repeated backfill runs.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestWriteFill(unittest.TestCase):
    """write_fill must never raise and must append a valid JSONL row."""

    def test_writes_single_row(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                cf.write_fill(
                    strategy="forge_gld_pm_long",
                    symbol="GLD",
                    direction="long",
                    side="EXIT",
                    entry_ts="2026-04-16T19:30:00+00:00",
                    exit_ts="2026-04-17T13:30:00+00:00",
                    entry_px=440.16,
                    exit_px=441.98,
                    size=109,
                    risk_usd=100.0,
                    pnl_usd=199.15,
                    exit_reason="target",
                )
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["strategy"], "forge_gld_pm_long")
        self.assertEqual(r["symbol"], "GLD")
        self.assertEqual(r["pnl_usd"], 199.15)
        # ts is injected at write time
        self.assertIn("ts", r)

    def test_appends_not_overwrites(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                cf.write_fill(strategy="A", symbol="X", direction="long", side="EXIT")
                cf.write_fill(strategy="B", symbol="Y", direction="short", side="EXIT")
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["strategy"], "A")
        self.assertEqual(rows[1]["strategy"], "B")

    def test_never_raises_even_if_path_unwritable(self):
        """Runtime invariant: a broken fills log must not crash a live trade.
        write_fill swallows all exceptions by design."""
        import helio.canonical_fills as cf
        # Point at a path that cannot be created (contains a file as a parent dir)
        with tempfile.TemporaryDirectory() as td:
            bad_parent = Path(td) / "not_a_dir"
            bad_parent.write_text("i am a file", encoding="utf-8")
            bad_path = bad_parent / "inside" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", bad_path):
                # Must not raise
                cf.write_fill(strategy="X", symbol="Y", direction="long", side="EXIT")

    def test_includes_broker_anchor_in_row(self):
        """Each live fill captures the anchor that was in force at fill time —
        required for reproducing the sizing decision in audits."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_current_anchor", return_value=9876.54):
                cf.write_fill(strategy="X", symbol="Y", direction="long", side="EXIT")
            row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertAlmostEqual(row["broker_anchor_at_fill_usd"], 9876.54, places=2)

    def test_extra_field_preserved(self):
        """Optional 'extra' blob must be included when passed."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                cf.write_fill(
                    strategy="X", symbol="Y", direction="long", side="EXIT",
                    extra={"signal_id": "abc123", "atr_entry": 0.82},
                )
            row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["extra"]["signal_id"], "abc123")
        self.assertAlmostEqual(row["extra"]["atr_entry"], 0.82)


class TestBackfillDedup(unittest.TestCase):
    """Backfill is idempotent: running twice must not duplicate rows."""

    def _make_trades_csv(self, path: Path, rows: list[dict], header: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_second_backfill_appends_zero_rows(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Stage a fake repo layout
            csv_path = tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv"
            self._make_trades_csv(csv_path,
                rows=[{"ts": "2026-04-16T19:30:00", "direction": "long",
                       "entry_px": "440.16", "exit_px": "441.98", "pnl_usd": "199.15",
                       "position_size": "109", "risk_usd": "100.0"}],
                header=["ts", "direction", "entry_px", "exit_px", "pnl_usd",
                        "position_size", "risk_usd"])
            out = tdp / "argus_flow" / "logs" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_REPO", tdp):
                first = cf.backfill_from_trade_csvs()
                second = cf.backfill_from_trade_csvs()
        self.assertGreaterEqual(first, 1)
        self.assertEqual(second, 0, "dedup failed — second run appended rows")

    def test_backfill_keeps_experiment_valid_filter_for_argus(self):
        """Argus pairs filter out rows where experiment_valid != 'true'. This
        is the filter-semantics source that reconciliation's DRIFT currently
        flags — ensure the gating is still active."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = tdp / "argus_flow" / "logs" / "usdjpy" / "trades.csv"
            self._make_trades_csv(csv_path,
                rows=[
                    {"ts": "2026-04-01", "entry_ts": "2026-04-01", "exit_ts": "2026-04-02",
                     "experiment_valid": "true", "pnl_usd": "10", "direction": "long",
                     "entry_px": "159.7", "exit_px": "159.9"},
                    {"ts": "2026-04-03", "entry_ts": "2026-04-03", "exit_ts": "2026-04-04",
                     "experiment_valid": "false", "pnl_usd": "-5", "direction": "long",
                     "entry_px": "159.8", "exit_px": "159.75"},
                ],
                header=["ts", "entry_ts", "exit_ts", "experiment_valid", "pnl_usd",
                        "direction", "entry_px", "exit_px"])
            out = tdp / "argus_flow" / "logs" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_REPO", tdp):
                appended = cf.backfill_from_trade_csvs()
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
        # Only one row (the valid one) made it through
        self.assertEqual(appended, 1)
        self.assertEqual(rows[0]["strategy"], "argus_usdjpy")
        self.assertAlmostEqual(float(rows[0]["pnl_usd"] or 0), 10.0)

    def test_forge_strategies_do_NOT_filter_on_experiment_valid(self):
        """Forge strategies ship without experiment_valid gating."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Forge path: valid_only is False in SPECS
            csv_path = tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv"
            self._make_trades_csv(csv_path,
                rows=[
                    {"ts": "2026-04-01", "experiment_valid": "false",
                     "pnl_usd": "50", "direction": "long",
                     "entry_px": "440", "exit_px": "441"},
                ],
                header=["ts", "experiment_valid", "pnl_usd", "direction",
                        "entry_px", "exit_px"])
            out = tdp / "argus_flow" / "logs" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_REPO", tdp):
                appended = cf.backfill_from_trade_csvs()
        self.assertEqual(appended, 1,
            "forge backfill should NOT filter on experiment_valid")

    def test_missing_csv_is_tolerated(self):
        """A strategy whose CSV doesn't exist yet is silently skipped."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # No CSVs anywhere
            out = tdp / "argus_flow" / "logs" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_REPO", tdp):
                appended = cf.backfill_from_trade_csvs()
        self.assertEqual(appended, 0)

    def test_backfill_marks_source_field(self):
        """Rows from backfill must carry source='backfill_from_trade_csv' so
        dashboards can distinguish them from live writes."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            csv_path = tdp / "forge" / "logs" / "gld_pm_long" / "trades.csv"
            self._make_trades_csv(csv_path,
                rows=[{"ts": "2026-04-01", "entry_px": "440", "pnl_usd": "10",
                       "direction": "long", "exit_px": "441"}],
                header=["ts", "entry_px", "pnl_usd", "direction", "exit_px"])
            out = tdp / "argus_flow" / "logs" / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch.object(cf, "_REPO", tdp):
                cf.backfill_from_trade_csvs()
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(rows[0]["source"], "backfill_from_trade_csv")
        # Backfilled rows have no live broker anchor
        self.assertIsNone(rows[0]["broker_anchor_at_fill_usd"])


class TestWriteFillTyped(unittest.TestCase):
    """write_fill_typed — the Fill-object twin of write_fill."""

    def test_writes_fill_object_to_disk(self):
        import helio.canonical_fills as cf
        from helio.domain import Fill
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            f = Fill(
                strategy="forge_gld_pm_long",
                symbol="GLD",
                direction="long",
                side="EXIT",
                entry_ts="2026-04-16T19:30:00+00:00",
                exit_ts="2026-04-17T13:30:00+00:00",
                entry_px=440.16,
                exit_px=441.98,
                size=109,
                risk_usd=100.0,
                pnl_usd=199.15,
                exit_reason="target",
            )
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                cf.write_fill_typed(f)
            rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["strategy"], "forge_gld_pm_long")
        self.assertAlmostEqual(r["pnl_usd"], 199.15)
        self.assertEqual(r["exit_reason"], "target")
        self.assertIn("ts", r)

    def test_roundtrips_via_read_fills_with_fill(self):
        """Write a Fill, read it back, parse through Fill.from_canonical_row —
        every field that was set on write must survive."""
        import helio.canonical_fills as cf
        from helio.domain import Fill
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            original = Fill(
                strategy="X", symbol="Y", direction="long", side="EXIT",
                entry_ts="2026-04-01", exit_ts="2026-04-02",
                entry_px=100.0, exit_px=101.5,
                size=50, risk_usd=25.0, pnl_usd=75.0, exit_reason="target",
            )
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                cf.write_fill_typed(original)
                rows = cf.read_fills()
        roundtripped = Fill.from_canonical_row(rows[0])
        # identity fields survive
        self.assertEqual(roundtripped.strategy, original.strategy)
        self.assertEqual(roundtripped.symbol, original.symbol)
        self.assertEqual(roundtripped.direction, original.direction)
        self.assertEqual(roundtripped.side, original.side)
        self.assertEqual(roundtripped.entry_ts, original.entry_ts)
        self.assertAlmostEqual(roundtripped.pnl_usd, original.pnl_usd)

    def test_typed_and_kwargs_produce_identical_shape(self):
        """write_fill and write_fill_typed must produce the same on-disk shape
        so consumers don't care which path was used."""
        import helio.canonical_fills as cf
        from helio.domain import Fill
        with tempfile.TemporaryDirectory() as td:
            out1 = Path(td) / "kwargs.jsonl"
            out2 = Path(td) / "typed.jsonl"

            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out1), \
                 mock.patch.object(cf, "_current_anchor", return_value=9999.0):
                cf.write_fill(
                    strategy="X", symbol="Y", direction="long", side="EXIT",
                    entry_ts="t1", exit_ts="t2",
                    entry_px=100.0, exit_px=101.0,
                    size=10, risk_usd=5.0, pnl_usd=10.0, exit_reason="target",
                )
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out2), \
                 mock.patch.object(cf, "_current_anchor", return_value=9999.0):
                cf.write_fill_typed(Fill(
                    strategy="X", symbol="Y", direction="long", side="EXIT",
                    entry_ts="t1", exit_ts="t2",
                    entry_px=100.0, exit_px=101.0,
                    size=10.0, risk_usd=5.0, pnl_usd=10.0, exit_reason="target",
                ))

            r1 = json.loads(out1.read_text(encoding="utf-8").splitlines()[0])
            r2 = json.loads(out2.read_text(encoding="utf-8").splitlines()[0])
        # Compare all fields except ts (timestamp differs by microseconds)
        r1.pop("ts")
        r2.pop("ts")
        self.assertEqual(r1, r2, "kwargs and typed writers diverged in shape")

    def test_never_raises_on_bad_fill(self):
        """write_fill_typed is a live-trade safe path: never break trading."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                # Pass something completely broken — must not raise
                cf.write_fill_typed(None)
                cf.write_fill_typed("not a fill")


class TestRotateIfNeeded(unittest.TestCase):
    """Size-triggered rotation: when canonical_fills.jsonl crosses the
    threshold, rename to canonical_fills_YYYYMM.jsonl and start fresh."""

    def test_no_rotation_when_under_threshold(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            out.write_text('{"strategy":"x"}\n', encoding="utf-8")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                result = cf.rotate_if_needed()
        self.assertIsNone(result)

    def test_rotation_when_over_threshold(self):
        """Write a file bigger than the threshold, confirm rotation archives it."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            big_content = "x" * (6 * 1024 * 1024)  # 6 MB > 5 MB threshold
            out.write_text(big_content, encoding="utf-8")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                archive = cf.rotate_if_needed()
            self.assertIsNotNone(archive)
            self.assertRegex(archive.name, r"^canonical_fills_\d{6}\.jsonl$")
            # Original path now fresh/empty
            self.assertTrue(out.exists())
            self.assertEqual(out.stat().st_size, 0)
            # Archive retains the old content
            self.assertGreater(archive.stat().st_size, 5 * 1024 * 1024)

    def test_rotation_preserves_existing_archive_when_stamp_collides(self):
        """If canonical_fills_YYYYMM.jsonl already exists (month-boundary
        edge), skip rotation rather than overwriting."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            out.write_text("x" * (6 * 1024 * 1024), encoding="utf-8")
            stamp = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y%m")
            collision = Path(td) / f"canonical_fills_{stamp}.jsonl"
            collision.write_text("existing archive content", encoding="utf-8")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                result = cf.rotate_if_needed()
            # Rotation skipped — existing archive untouched
            self.assertIsNone(result)
            self.assertEqual(collision.read_text(encoding="utf-8"),
                             "existing archive content")

    def test_rotation_never_raises(self):
        """A rotation failure must not propagate — runtime safety invariant."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            out.write_text("x" * (6 * 1024 * 1024), encoding="utf-8")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out), \
                 mock.patch("pathlib.Path.rename", side_effect=OSError("locked")):
                # Must not raise
                result = cf.rotate_if_needed()
        self.assertIsNone(result)

    def test_missing_file_returns_none(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                result = cf.rotate_if_needed()
        self.assertIsNone(result)

    def test_readers_see_both_current_and_rotated_archive(self):
        """After rotation + a fresh write, read_fills returns rows from
        the archive AND the new current file."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            # Start with a small file, manually rename to simulate past rotation
            arch = Path(td) / "canonical_fills_202512.jsonl"
            arch.write_text(json.dumps({"strategy": "old_s", "ts": "2025-12-01"}) + "\n",
                            encoding="utf-8")
            out.write_text(json.dumps({"strategy": "new_s", "ts": "2026-04-01"}) + "\n",
                           encoding="utf-8")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                from helio.canonical_fills import read_fills
                rows = read_fills()
        strategies = sorted(r["strategy"] for r in rows)
        self.assertEqual(strategies, ["new_s", "old_s"])


class TestReadFillsFiltering(unittest.TestCase):
    """Basic read-path behaviors beyond rotation (covered elsewhere)."""

    def test_strategy_filter_excludes_other_strategies(self):
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with open(out, "w", encoding="utf-8") as f:
                f.write(json.dumps({"strategy": "keep", "pnl_usd": 10}) + "\n")
                f.write(json.dumps({"strategy": "skip", "pnl_usd": 20}) + "\n")
                f.write(json.dumps({"strategy": "keep", "pnl_usd": 30}) + "\n")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                keep = cf.read_fills(strategy="keep")
        self.assertEqual(len(keep), 2)
        for r in keep:
            self.assertEqual(r["strategy"], "keep")

    def test_malformed_json_lines_are_skipped(self):
        """Corrupt lines must not crash the reader."""
        import helio.canonical_fills as cf
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with open(out, "w", encoding="utf-8") as f:
                f.write(json.dumps({"strategy": "A"}) + "\n")
                f.write("this is not JSON\n")
                f.write(json.dumps({"strategy": "B"}) + "\n")
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                rows = cf.read_fills()
        strategies = sorted(r["strategy"] for r in rows)
        self.assertEqual(strategies, ["A", "B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
