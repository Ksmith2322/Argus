"""Unit tests for helio.strategy_common — shared runner helpers.

These are the small functions every runner re-implements. Tests pin
deterministic hashing, git-sha fallback, atomic-save semantics, and
tolerant json reads.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import strategy_common as sc  # noqa: E402


class TestConfigHash(unittest.TestCase):
    """config_hash must be deterministic, order-independent, 16-hex."""

    def test_deterministic(self):
        self.assertEqual(sc.config_hash({"a": 1, "b": 2}),
                         sc.config_hash({"a": 1, "b": 2}))

    def test_insertion_order_independent(self):
        """{'a':1,'b':2} and {'b':2,'a':1} must hash the same."""
        h1 = sc.config_hash({"a": 1, "b": 2})
        h2 = sc.config_hash({"b": 2, "a": 1})
        self.assertEqual(h1, h2)

    def test_different_values_hash_differently(self):
        self.assertNotEqual(sc.config_hash({"x": 1}), sc.config_hash({"x": 2}))

    def test_returns_16_hex_chars(self):
        result = sc.config_hash({"a": 1})
        self.assertEqual(len(result), 16)
        self.assertRegex(result, r"^[0-9a-f]{16}$")

    def test_empty_dict_still_hashes(self):
        self.assertRegex(sc.config_hash({}), r"^[0-9a-f]{16}$")

    def test_nested_dicts_supported(self):
        h = sc.config_hash({"outer": {"inner": [1, 2, 3]}})
        self.assertEqual(len(h), 16)

    def test_matches_existing_runner_implementations(self):
        """Each runner's local _config_hash must produce the same output
        as the shared helper. This is the migration-safety guarantee:
        future runners can call sc.config_hash() and produce byte-
        identical trade stamps to their legacy implementations."""
        # Replicate the exact pattern used in forge/gld_pm_long/runner.py
        params = {"version": "v1", "symbol": "GLD", "atr_period": 14}
        import hashlib
        local = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(sc.config_hash(params), local)


class TestGitSha(unittest.TestCase):
    def test_returns_string(self):
        result = sc.git_sha()
        self.assertIsInstance(result, str)
        # Either a short hash or the 'unknown' sentinel
        self.assertTrue(len(result) > 0)

    def test_subprocess_failure_yields_unknown(self):
        with mock.patch("subprocess.check_output",
                        side_effect=OSError("git not found")):
            self.assertEqual(sc.git_sha(), "unknown")

    def test_never_raises(self):
        """The git-sha stamp must never break a trade write path."""
        with mock.patch("subprocess.check_output",
                        side_effect=Exception("anything")):
            try:
                sc.git_sha()
            except Exception as e:
                self.fail(f"git_sha leaked exception: {e}")


class TestAtomicSaveJson(unittest.TestCase):
    def test_writes_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            sc.atomic_save_json(p, {"a": 1, "b": "x"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")),
                             {"a": 1, "b": "x"})

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nested" / "dir" / "state.json"
            sc.atomic_save_json(p, {"x": 1})
            self.assertTrue(p.exists())

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text('{"old": true}', encoding="utf-8")
            sc.atomic_save_json(p, {"new": True})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")),
                             {"new": True})

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            sc.atomic_save_json(p, {"x": 1})
            # .tmp suffix file should be gone after successful save
            tmp = p.with_suffix(p.suffix + ".tmp")
            self.assertFalse(tmp.exists())


class TestLoadJson(unittest.TestCase):
    def test_valid_json_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(sc.load_json(p), {"ok": True})

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.json"
            self.assertEqual(sc.load_json(p, default={}), {})

    def test_corrupt_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(sc.load_json(p))

    def test_explicit_default_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "corrupt.json"
            p.write_text("}]", encoding="utf-8")
            self.assertEqual(sc.load_json(p, default={"fallback": 1}),
                             {"fallback": 1})


class TestEnsureParentDir(unittest.TestCase):
    def test_creates_parent_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a" / "b" / "file.txt"
            sc.ensure_parent_dir(p)
            self.assertTrue(p.parent.exists())

    def test_no_op_if_exists(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            # parent already exists
            result = sc.ensure_parent_dir(p)
            self.assertEqual(result, p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
