"""Tests for /api/strategy_performance — the dashboard strategy table.

Fences the contract after 2026-04-19: rows that have canonical_fills data
must pull confidence from the shared bootstrap in helio.fleet_state
(confidence_source = "computed"). Rows without canonical_fills must be
tagged confidence_source = "hardcoded" so the UI can badge them. The
fleet_confidence header must NEVER show an unexplained round number — it
either reports a computed fraction or flags insufficient_sample.
"""
from __future__ import annotations

import json
import sys
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


class TestStrategyPerformanceEndpoint(unittest.TestCase):
    def test_returns_200_and_strategies_list(self):
        c = _client()
        r = c.get("/api/strategy_performance")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("strategies", data)
        self.assertIsInstance(data["strategies"], list)
        self.assertGreater(len(data["strategies"]), 0)

    def test_every_strategy_has_confidence_source(self):
        c = _client()
        data = c.get("/api/strategy_performance").json()
        for s in data["strategies"]:
            self.assertIn("confidence_source", s,
                f"{s.get('system')} missing confidence_source — dashboard can't render badge")
            self.assertIn(s["confidence_source"], ("computed", "hardcoded"),
                f"{s.get('system')} has invalid confidence_source: {s['confidence_source']}")

    def test_argus_is_computed_with_detail_block(self):
        c = _client()
        data = c.get("/api/strategy_performance").json()
        argus = next(s for s in data["strategies"] if s["system"] == "Argus")
        self.assertEqual(argus["confidence_source"], "computed")
        # confidence_detail must carry the fields the UI relies on
        detail = argus.get("confidence_detail") or {}
        self.assertIn("n_total", detail)
        self.assertIn("n_live", detail)
        self.assertIn("n_paper", detail)
        self.assertIn("evidence_bar", detail)

    def test_gdx_gld_is_computed(self):
        c = _client()
        data = c.get("/api/strategy_performance").json()
        gdx = next(s for s in data["strategies"] if s["system"] == "GDX/GLD")
        self.assertEqual(gdx["confidence_source"], "computed")
        # Backfill-only: the confidence block must carry the warning that
        # lets the UI surface the ⚠ icon.
        detail = gdx.get("confidence_detail") or {}
        if detail.get("n_paper", 0) > 0 and detail.get("n_live", 0) == 0:
            self.assertIsNotNone(detail.get("sample_warning"),
                "backfill-only strategy must have sample_warning")

    def test_strategies_without_data_are_hardcoded(self):
        """Rows with neither canonical fills nor a strategy_confidence
        artifact must be tagged hardcoded so the UI badges them."""
        # Stub the artifact dir empty so this test is independent of
        # which writers have landed.
        import helio.strategy_confidence as sc
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(sc, "ARTIFACT_DIR", Path(td)):
                c = _client()
                data = c.get("/api/strategy_performance").json()
        hardcoded_systems = {s["system"] for s in data["strategies"]
                             if s["confidence_source"] == "hardcoded"}
        for must_hardcoded in ("Titan", "Ares", "Hermes", "Apollo", "Mamba",
                                "Cue Banks", "Tori", "VIX Revert",
                                "Index Rebal", "Sector Rot", "Themis"):
            self.assertIn(must_hardcoded, hardcoded_systems,
                f"{must_hardcoded} should be hardcoded when no artifact + no canonical")

    def test_fleet_confidence_has_source_and_detail(self):
        c = _client()
        data = c.get("/api/strategy_performance").json()
        # Source must be one of the three honest states — never a bare
        # manual number without a label.
        self.assertIn(data.get("fleet_confidence_source"),
                      ("computed", "hardcoded", "insufficient_sample"))
        self.assertIn("fleet_confidence_detail", data)

    def test_fleet_confidence_is_none_when_no_sample(self):
        """If nothing is at the sanity bar, the header must not fabricate a
        number — it must return None and tag the source insufficient_sample
        so the UI renders '—' with the warning badge."""
        # Both data sources must be empty to truly have no sample: stub
        # canonical fills AND the artifact directory.
        import helio.fleet_state as fs
        import helio.strategy_confidence as sc
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(fs, "_canonical_fills_by_strategy", return_value={}), \
             mock.patch.object(sc, "ARTIFACT_DIR", Path(td)):
            c = _client()
            data = c.get("/api/strategy_performance").json()
        self.assertIsNone(data["fleet_confidence"])
        self.assertEqual(data["fleet_confidence_source"], "insufficient_sample")

    def test_computed_confidence_percentage_matches_bootstrap(self):
        """Integration: when we feed 15 all-winning canonical fills for
        forge_gdx_gld, the endpoint's GDX/GLD row must report confidence=100
        (P(exp>0)=1.0 for all-wins) with source=computed."""
        import helio.fleet_state as fs
        from helio.domain import Fill
        stub = {
            "forge_gdx_gld": [Fill(strategy="forge_gdx_gld", pnl_usd=5.0,
                                    source=None,
                                    ts="2026-04-19T10:00:00+00:00")] * 15,
        }
        with mock.patch.object(fs, "_canonical_fills_by_strategy", return_value=stub):
            c = _client()
            data = c.get("/api/strategy_performance").json()
        gdx = next(s for s in data["strategies"] if s["system"] == "GDX/GLD")
        self.assertEqual(gdx["confidence_source"], "computed")
        self.assertEqual(gdx["confidence"], 100)  # all wins → P=1.0 → 100%
        self.assertEqual(gdx["confidence_detail"]["n_live"], 15)
        self.assertEqual(gdx["confidence_detail"]["n_paper"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
