"""Cross-process write smoke test for canonical_fills.jsonl.

Each strategy runner is its own OS process. The intra-process threading
lock I added doesn't serialise across processes. This test fires up
multiple Python subprocesses simultaneously, each writing N fills, and
verifies:

  1. No malformed JSONL lines (partial writes / interleaving)
  2. Total row count == expected (no lost rows)
  3. Every line has exactly one trailing newline

If this test fails, we need OS-level file locking (msvcrt.locking on
Windows). Under typical real workloads (4 runners, low concurrency,
~1 fill per minute at most), the race is narrow enough that append-mode
is usually atomic per-line below 4KB. But this test quantifies the real
behavior rather than hoping.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


_WRITER_SCRIPT = r"""
import json, os, sys
from pathlib import Path

# Set target via env
target = Path(os.environ['CANON_TARGET'])
strategy = os.environ['CANON_STRATEGY']
count = int(os.environ['CANON_COUNT'])

# Import canonical_fills with patched CANONICAL_FILLS_PATH
sys.path.insert(0, os.environ['REPO_ROOT'])
import helio.canonical_fills as cf
cf.CANONICAL_FILLS_PATH = target

from helio.domain import Fill
for i in range(count):
    cf.write_fill_typed(Fill(
        strategy=strategy, symbol="X", direction="long", side="EXIT",
        entry_ts="t1", exit_ts="t2",
        entry_px=100.0, exit_px=101.0,
        size=10.0, risk_usd=5.0, pnl_usd=1.0,
        exit_reason="target",
    ))
"""


class TestCrossProcessWrites(unittest.TestCase):
    """Spawn N subprocesses, each writing M fills concurrently."""

    def test_four_processes_each_writing_25_fills(self):
        """4 subprocesses × 25 fills = 100 total rows expected."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "canonical_fills.jsonl"
            script = Path(td) / "writer.py"
            script.write_text(_WRITER_SCRIPT, encoding="utf-8")

            procs = []
            for i in range(4):
                env = os.environ.copy()
                env["CANON_TARGET"] = str(target)
                env["CANON_STRATEGY"] = f"proc_{i}"
                env["CANON_COUNT"] = "25"
                env["REPO_ROOT"] = str(_REPO)
                procs.append(subprocess.Popen(
                    [sys.executable, str(script)],
                    env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                ))
            for p in procs:
                _, stderr = p.communicate(timeout=60)
                if p.returncode != 0:
                    self.fail(f"writer subprocess failed: {stderr.decode()}")

            # Now verify the result
            self.assertTrue(target.exists())
            raw = target.read_text(encoding="utf-8")
            lines = raw.splitlines()

        # Total: 4 × 25 = 100 lines expected
        self.assertEqual(len(lines), 100,
            f"expected 100 rows from 4 concurrent processes × 25 writes, "
            f"got {len(lines)}")

        # Every line parses as JSON
        bad = []
        for i, line in enumerate(lines):
            try:
                r = json.loads(line)
                if not r.get("strategy"):
                    bad.append((i, "missing strategy field"))
            except json.JSONDecodeError as e:
                bad.append((i, str(e)))
        self.assertEqual(bad, [],
            f"{len(bad)} malformed lines from cross-process writes")

        # Every strategy has exactly 25 rows (no process lost any)
        counts = {f"proc_{i}": 0 for i in range(4)}
        for line in lines:
            r = json.loads(line)
            counts[r["strategy"]] = counts.get(r["strategy"], 0) + 1
        for strat, n in counts.items():
            self.assertEqual(n, 25,
                f"{strat} has {n} rows (expected 25) — cross-process race")

    def test_raw_ends_with_newline(self):
        """After cross-process writes, the file must end with \\n so
        readers don't truncate the last row."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "canonical_fills.jsonl"
            script = Path(td) / "writer.py"
            script.write_text(_WRITER_SCRIPT, encoding="utf-8")

            env = os.environ.copy()
            env.update({
                "CANON_TARGET": str(target),
                "CANON_STRATEGY": "single",
                "CANON_COUNT": "5",
                "REPO_ROOT": str(_REPO),
            })
            subprocess.run([sys.executable, str(script)], env=env,
                            check=True, timeout=30,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
            raw = target.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"),
            "file does not end with newline — last row may truncate on reread")


if __name__ == "__main__":
    unittest.main(verbosity=2)
