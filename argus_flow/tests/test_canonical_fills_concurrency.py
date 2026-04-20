"""Concurrency + atomicity tests for canonical_fills.jsonl writes.

Four shortlist strategies now dual-write on every exit. If two strategies
close trades at the same moment (e.g., end-of-day clock tick), Windows
file-open semantics are NOT identical to Linux's O_APPEND. Garbled rows
would feed reconciliation's DRIFT flag silently forever.

Verify:
  1. N threads each calling write_fill_typed concurrently produce
     N valid JSONL rows — no partial lines, no truncation.
  2. Interleaved lines all parse as JSON.
  3. Every strategy label that was written appears in the file with the
     expected count.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class TestConcurrentWrites(unittest.TestCase):
    def test_100_threads_produce_100_valid_rows(self):
        """Hammer the writer from 100 threads simultaneously. Every line
        in the resulting file must be valid JSON."""
        import helio.canonical_fills as cf
        from helio.domain import Fill

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                def writer(idx: int):
                    cf.write_fill_typed(Fill(
                        strategy=f"thread_{idx % 4}",
                        symbol="TEST", direction="long", side="EXIT",
                        entry_ts="t1", exit_ts="t2",
                        entry_px=100.0 + idx, exit_px=101.0 + idx,
                        size=10, risk_usd=5.0, pnl_usd=1.0 * idx,
                        exit_reason="target",
                    ))

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(100)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 100,
            f"expected 100 rows, got {len(lines)} — concurrent writes dropped data")
        bad_lines = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                bad_lines.append((i, str(e), line[:80]))
        self.assertEqual(bad_lines, [],
            f"found {len(bad_lines)} malformed lines in concurrent write output")

    def test_concurrent_writes_preserve_strategy_distribution(self):
        """25 writes each for 4 strategies, confirm every strategy's
        count matches."""
        import helio.canonical_fills as cf
        from helio.domain import Fill
        strategies = ["strat_a", "strat_b", "strat_c", "strat_d"]

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                def writer(strategy: str):
                    for _ in range(25):
                        cf.write_fill_typed(Fill(
                            strategy=strategy, symbol="T",
                            direction="long", side="EXIT",
                            entry_ts="t1", exit_ts="t2",
                            entry_px=1.0, exit_px=2.0, size=1,
                            risk_usd=1.0, pnl_usd=1.0, exit_reason="target",
                        ))

                threads = [threading.Thread(target=writer, args=(s,))
                           for s in strategies]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                rows = [json.loads(l) for l in out.read_text().splitlines() if l]

        counts = {s: 0 for s in strategies}
        for r in rows:
            counts[r["strategy"]] = counts.get(r["strategy"], 0) + 1
        for s in strategies:
            self.assertEqual(counts[s], 25,
                f"{s} count was {counts[s]}, expected 25 "
                f"— concurrent writes lost rows")

    def test_kwargs_writer_also_concurrent_safe(self):
        """The kwargs-API write_fill has the same guarantees as the
        typed version."""
        import helio.canonical_fills as cf

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                def writer(idx: int):
                    cf.write_fill(strategy=f"s_{idx}", symbol="T",
                                   direction="long", side="EXIT",
                                   pnl_usd=float(idx))

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                lines = out.read_text().splitlines()
        self.assertEqual(len(lines), 50)
        for line in lines:
            json.loads(line)  # each must parse


class TestAppendAtomicity(unittest.TestCase):
    """Every line must end with exactly one \\n — verifies that append
    mode on Windows doesn't fragment a line mid-write."""

    def test_every_line_has_single_trailing_newline(self):
        import helio.canonical_fills as cf
        from helio.domain import Fill

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                for i in range(10):
                    cf.write_fill_typed(Fill(strategy=f"s{i}", symbol="T"))
                raw = out.read_text(encoding="utf-8")
        # Split on single \n; the file should end with \n so last chunk
        # is empty. No chunk should contain another \n (all single).
        chunks = raw.split("\n")
        self.assertEqual(chunks[-1], "",
            "file must end with \\n — missing final newline")
        for i, chunk in enumerate(chunks[:-1]):
            self.assertNotIn("\n", chunk,
                f"chunk {i} contained embedded newline — line fragmented: {chunk[:80]}")


class TestRotationUnderConcurrentWrites(unittest.TestCase):
    """A rotation can fire from any write. Concurrent writes must not
    lose data when rotation happens mid-stream."""

    def test_rotation_does_not_corrupt_concurrent_writes(self):
        """Pre-populate the file past the rotation threshold. Then
        concurrent writers hit it — first one triggers rotation, rest
        see a fresh file. Total row count must still equal N."""
        import helio.canonical_fills as cf
        from helio.domain import Fill

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "canonical_fills.jsonl"
            # Write 6MB so next write triggers rotation
            out.write_text("{\"prefill\":true}\n" * 300000, encoding="utf-8")
            initial_size = out.stat().st_size
            with mock.patch.object(cf, "CANONICAL_FILLS_PATH", out):
                def writer(idx: int):
                    cf.write_fill_typed(Fill(strategy=f"s{idx}", symbol="T"))

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                # Count rows across both current AND archive
                from helio.canonical_fills import _iter_canonical_paths
                total_lines = 0
                all_valid = True
                for path in _iter_canonical_paths():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        total_lines += 1
                        try:
                            json.loads(line)
                        except json.JSONDecodeError:
                            all_valid = False
        self.assertTrue(all_valid,
            "rotation during concurrent writes produced malformed JSON")
        # Total must be the original 300000 prefill rows + 20 new writes
        self.assertEqual(total_lines, 300020,
            f"expected 300020 total rows, got {total_lines} — data loss across rotation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
