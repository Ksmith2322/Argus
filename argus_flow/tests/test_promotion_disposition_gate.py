"""Tests for the disposition-aware promotion gate.

Guards the invariant that a strategy carrying a `kill`, `shelve`, or
`scope_down` disposition in its strategy_confidence artifact CANNOT be
flagged `PROMOTION_ELIGIBLE` regardless of how good the live stats look.
The ruling trumps the bare numeric gates — the operator has reviewed the
full artifact and said do-not-promote.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _write_artifact(dir_path: Path, label: str, disposition_status: str | None) -> Path:
    p = dir_path / f"{label}.json"
    artifact = {
        "schema_version": 1,
        "strategy": label,
        "source": f"test_stub_{label}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_total": 120,
        "n_live": 0,
        "n_paper": 120,
        "bt_pf": 2.0,
        "bt_wr": 0.6,
        "bt_trades": 120,
        "expectancy_usd": 5.0,
        "p_expectancy_positive": 0.99,
        "evidence_bar": "promotion",
    }
    if disposition_status is not None:
        artifact["disposition"] = {
            "status": disposition_status,
            "reason": "test-stub reason meeting min_length constraint",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
    p.write_text(json.dumps(artifact))
    return p


class TestDispositionGate(unittest.TestCase):
    def _evaluate(self, disposition_status: str | None) -> dict:
        """Build a single-strategy report with a stubbed artifact and a
        stubbed live-stats block that would otherwise pass both gates."""
        import helio.promotion_readiness as pr
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_artifact(tdp, "test_strat", disposition_status)
            # Stub stats so gates would otherwise pass
            passing_stats = {"trades": 120, "profit_factor": 2.0, "pnl_usd": 600.0}
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp), \
                 mock.patch.object(pr, "compute_strategy_stats",
                                   return_value=passing_stats), \
                 mock.patch.object(pr, "SHORTLIST", [
                     {"label": "test_strat",
                      "card": None,
                      "artifact_label": "test_strat"}
                 ]):
                report = pr.build_report()
        return report["strategies"][0]

    def test_kill_blocks_promotion(self):
        result = self._evaluate("kill")
        self.assertTrue(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))
        self.assertEqual(result["blocking_disposition"]["status"], "kill")

    def test_shelve_blocks_promotion(self):
        result = self._evaluate("shelve")
        self.assertTrue(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))
        self.assertEqual(result["blocking_disposition"]["status"], "shelve")

    def test_scope_down_blocks_promotion(self):
        result = self._evaluate("scope_down")
        self.assertTrue(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))
        self.assertEqual(result["blocking_disposition"]["status"], "scope_down")

    def test_research_only_does_not_block(self):
        """research_only is informational, not a promotion block.
        Apollo today is research_only — it shouldn't be surfaced as blocked."""
        result = self._evaluate("research_only")
        self.assertFalse(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))
        self.assertIsNone(result["blocking_disposition"])

    def test_promote_candidate_does_not_block(self):
        result = self._evaluate("promote_candidate")
        self.assertFalse(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))

    def test_no_artifact_does_not_block(self):
        """Strategy with no artifact and no disposition — gates run normally."""
        import helio.promotion_readiness as pr
        passing_stats = {"trades": 120, "profit_factor": 2.0, "pnl_usd": 600.0}
        with mock.patch.object(pr, "compute_strategy_stats",
                               return_value=passing_stats), \
             mock.patch.object(pr, "SHORTLIST", [
                 {"label": "other_strat",
                  "card": None,
                  "artifact_label": None}
             ]):
            report = pr.build_report()
        result = report["strategies"][0]
        self.assertIsNone(result["blocking_disposition"])
        self.assertFalse(result["next_action"].startswith("BLOCKED_BY_DISPOSITION"))


class TestAllDispositionsSurface(unittest.TestCase):
    def test_report_includes_all_artifact_dispositions(self):
        """Every artifact with a disposition should appear in the report's
        all_dispositions list, even if the strategy isn't on the shortlist."""
        import helio.promotion_readiness as pr
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_artifact(tdp, "a_kill", "kill")
            _write_artifact(tdp, "b_shelve", "shelve")
            _write_artifact(tdp, "c_no_disp", None)
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp), \
                 mock.patch.object(pr, "SHORTLIST", []):
                report = pr.build_report()
        labels = {d["artifact_label"] for d in report["all_dispositions"]}
        self.assertIn("a_kill", labels)
        self.assertIn("b_shelve", labels)
        self.assertNotIn("c_no_disp", labels)


if __name__ == "__main__":
    unittest.main()
