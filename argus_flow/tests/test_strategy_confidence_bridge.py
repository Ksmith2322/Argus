"""Tests for the hardcoded→computed confidence bridge.

Three layers:
  1. Schema enforcement — invariants in StrategyConfidenceArtifact
  2. Loader — returns None for anything broken, never raises
  3. Dashboard integration — a valid artifact upgrades a row from
     hardcoded to computed; canonical_fills still wins over artifacts.
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


def _client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi.testclient not available")
    from ops.dashboard import app
    return TestClient(app)


def _valid_artifact_dict(label: str = "tori", **overrides) -> dict:
    base = {
        "schema_version": 1,
        "strategy": label,
        "source": "backtest_v1_20260418",
        "generated_at": "2026-04-18T23:30:00+00:00",
        "bt_pf": 0.61,
        "bt_wr": 0.29,
        "bt_trades": 65,
        "n_total": 65,
        "n_live": 0,
        "n_paper": 65,
        "evidence_bar": "promotion",
        "p_expectancy_positive": 0.2,
        "sample_warning": "65 backfill, 0 live",
    }
    base.update(overrides)
    return base


class TestSchemaInvariants(unittest.TestCase):
    def test_valid_artifact_parses(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        a = StrategyConfidenceArtifact.model_validate(_valid_artifact_dict())
        self.assertEqual(a.strategy, "tori")
        self.assertAlmostEqual(a.bt_pf, 0.61)

    def test_schema_version_must_be_one(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(schema_version=2)
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)

    def test_bt_pf_rejects_string_range(self):
        """Invariant 1: bt_pf must be a float, not '1.1-1.3'."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(bt_pf="1.1-1.3")
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)

    def test_sample_under_sanity_bar_must_null_p_positive(self):
        """Invariant 2: n_total<10 → p_expectancy_positive=None required."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(n_total=5, n_live=0, n_paper=5,
                                 p_expectancy_positive=0.75)
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)

    def test_small_sample_with_null_p_positive_is_ok(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(n_total=5, n_live=0, n_paper=5,
                                 p_expectancy_positive=None,
                                 evidence_bar="insufficient")
        # Should parse without error
        StrategyConfidenceArtifact.model_validate(d)

    def test_hardcoded_estimate_source_forbids_p_positive(self):
        """Invariant 3: source with 'hardcoded_estimate' cannot carry a
        confidence number — closes the lazy-writer loophole."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(source="hardcoded_estimate_by_me",
                                 p_expectancy_positive=0.8)
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)

    def test_n_live_plus_n_paper_must_equal_n_total(self):
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(n_total=100, n_live=40, n_paper=30)
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)

    def test_extra_fields_rejected(self):
        """extra='forbid' catches accidental typos so writers don't think
        they set a field that nothing reads."""
        from helio.strategy_confidence import StrategyConfidenceArtifact
        d = _valid_artifact_dict(profit_factor=1.5)  # typo of bt_pf
        with self.assertRaises(Exception):
            StrategyConfidenceArtifact.model_validate(d)


class TestLoaderSafety(unittest.TestCase):
    """Loader must return None for every broken state, never raise."""

    def test_missing_file_returns_none(self):
        from helio.strategy_confidence import load_confidence_artifact
        self.assertIsNone(load_confidence_artifact("does_not_exist_xyz"))

    def test_reserved_prefix_returns_none(self):
        """The _example.json file must never load as a real artifact."""
        from helio.strategy_confidence import load_confidence_artifact
        self.assertIsNone(load_confidence_artifact("_example"))

    def test_malformed_json_returns_none(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "broken.json").write_text("{this is not json", encoding="utf-8")
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                self.assertIsNone(sc.load_confidence_artifact("broken"))

    def test_schema_failure_returns_none(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bad = _valid_artifact_dict(strategy="bad", bt_pf="not-a-float")
            (tdp / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                self.assertIsNone(sc.load_confidence_artifact("bad"))

    def test_filename_strategy_mismatch_returns_none(self):
        """File named tori.json but contents say strategy=mamba → reject.
        Prevents accidental copy-paste bugs."""
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            d = _valid_artifact_dict(strategy="mamba")
            (tdp / "tori.json").write_text(json.dumps(d), encoding="utf-8")
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                self.assertIsNone(sc.load_confidence_artifact("tori"))

    def test_valid_file_loads(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "tori.json").write_text(
                json.dumps(_valid_artifact_dict("tori")), encoding="utf-8"
            )
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                art = sc.load_confidence_artifact("tori")
        self.assertIsNotNone(art)
        self.assertEqual(art.strategy, "tori")


class TestAuditArtifacts(unittest.TestCase):
    def test_audit_reports_all_three_states(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Valid
            (tdp / "tori.json").write_text(
                json.dumps(_valid_artifact_dict("tori")), encoding="utf-8"
            )
            # Invalid schema
            (tdp / "mamba.json").write_text(
                json.dumps(_valid_artifact_dict("mamba", bt_pf="not-a-number")),
                encoding="utf-8",
            )
            # Missing: titan (no file)
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                audit = sc.audit_artifacts(["tori", "mamba", "titan"])
        self.assertEqual(audit["present_valid"], 1)
        self.assertEqual(audit["present_invalid"], 1)
        self.assertEqual(audit["missing"], 1)
        self.assertIn("tori", audit["valid"])
        self.assertIn("titan", audit["absent"])
        self.assertTrue(any(x["label"] == "mamba" for x in audit["invalid"]))

    def test_reserved_labels_are_not_expected(self):
        from helio.strategy_confidence import audit_artifacts
        audit = audit_artifacts(["_example", "tori"])
        # _example must not be counted toward total_expected
        self.assertEqual(audit["total_expected"], 1)


class TestDashboardIntegration(unittest.TestCase):
    """A valid artifact flips a row from hardcoded to computed in
    /api/strategy_performance. Canonical fills still win over artifacts."""

    def test_artifact_upgrades_hardcoded_to_computed(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "tori.json").write_text(
                json.dumps(_valid_artifact_dict("tori",
                                                  bt_pf=1.23,
                                                  bt_trades=42,
                                                  bt_wr=0.55,
                                                  p_expectancy_positive=0.62)),
                encoding="utf-8",
            )
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                c = _client()
                data = c.get("/api/strategy_performance").json()
        tori = next(s for s in data["strategies"] if s["system"] == "Tori")
        self.assertEqual(tori["confidence_source"], "computed")
        self.assertEqual(tori["confidence"], 62)
        self.assertEqual(tori["backtest_pf"], "1.23")
        self.assertEqual(tori["backtest_trades"], 42)
        detail = tori["confidence_detail"]
        self.assertEqual(detail["artifact_source"], "backtest_v1_20260418")
        self.assertIsNotNone(detail.get("sample_warning"))

    def test_no_artifact_leaves_hardcoded(self):
        """With an empty artifact dir, Tori stays hardcoded."""
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(sc, "ARTIFACT_DIR", Path(td)):
                c = _client()
                data = c.get("/api/strategy_performance").json()
        tori = next(s for s in data["strategies"] if s["system"] == "Tori")
        self.assertEqual(tori["confidence_source"], "hardcoded")

    def test_canonical_fills_beat_artifact(self):
        """GDX/GLD has 87 canonical fills; an artifact in the directory
        must not override that — live evidence wins."""
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # An artifact claiming p=0.30 for gdx_gld
            (tdp / "gdx_gld.json").write_text(
                json.dumps(_valid_artifact_dict("gdx_gld",
                                                  p_expectancy_positive=0.30)),
                encoding="utf-8",
            )
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                c = _client()
                data = c.get("/api/strategy_performance").json()
        gdx = next(s for s in data["strategies"] if s["system"] == "GDX/GLD")
        # The label map doesn't include GDX/GLD, so artifact never consulted.
        # Canonical fills give p≈0.97 (87 winning backfill rows), not 0.30.
        self.assertEqual(gdx["confidence_source"], "computed")
        self.assertNotEqual(gdx["confidence"], 30)

    def test_invalid_artifact_does_not_crash_endpoint(self):
        """A malformed artifact must not crash /api/strategy_performance."""
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "tori.json").write_text("{not json", encoding="utf-8")
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                c = _client()
                r = c.get("/api/strategy_performance")
        self.assertEqual(r.status_code, 200)


class TestHealthSurface(unittest.TestCase):
    def test_health_reports_strategy_confidence_surface(self):
        c = _client()
        data = c.get("/api/health").json()
        surfaces = {s["surface"]: s for s in data["surfaces"]}
        self.assertIn("strategy_confidence", surfaces)
        sc_surf = surfaces["strategy_confidence"]
        self.assertIn(sc_surf["status"], ("OK", "EMPTY", "INVALID", "BROKEN"))
        self.assertIn("total_expected", sc_surf)

    def test_health_degrades_when_artifact_invalid(self):
        import helio.strategy_confidence as sc
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "tori.json").write_text("{not json", encoding="utf-8")
            with mock.patch.object(sc, "ARTIFACT_DIR", tdp):
                c = _client()
                data = c.get("/api/health").json()
        self.assertEqual(data["overall"], "DEGRADED")
        sc_surf = next(s for s in data["surfaces"] if s["surface"] == "strategy_confidence")
        self.assertEqual(sc_surf["status"], "INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
