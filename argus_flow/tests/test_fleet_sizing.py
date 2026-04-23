"""Tests for helio.fleet_sizing — the core sizing module.

Covers: anchor resolution (broker-truth vs fallback), tier evaluation
boundaries, strategy stats (with/without live_cutoff), notional caps,
fleet_max_open_risk derivation. These are the math that drives every
trade's risk budget — most of the system's correctness rides on them.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio import fleet_sizing as fs  # noqa: E402


class TestSizingAnchor(unittest.TestCase):
    def setUp(self):
        fs.invalidate_cache()

    def test_anchor_uses_broker_equity_when_available(self):
        """Happy path: broker reports equity → anchor is that value, no clamp."""
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 50_000}}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path):
                fs.invalidate_cache()
                anchor = fs.get_sizing_anchor_usd()
            self.assertAlmostEqual(anchor, 50_000, places=2)

    def test_anchor_returns_high_broker_equity_unclamped(self):
        """2026-04-23 architecture: no max_anchor_usd clamp. Whatever broker
        reports, we use. If paper account is $1M, we size against $1M. The
        solution is setting the paper balance correctly, not a code-side cap."""
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 1_000_000}}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path):
                fs.invalidate_cache()
                anchor = fs.get_sizing_anchor_usd()
            self.assertAlmostEqual(anchor, 1_000_000, places=2)

    def test_raises_when_broker_missing_and_no_cache(self):
        """No fallback: missing broker report + cold cache → raise, not default."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist.json"
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", missing):
                fs.invalidate_cache()
                with self.assertRaises(fs.BrokerEquityUnavailableError):
                    fs.get_sizing_anchor_usd()

    def test_raises_when_broker_zero_and_no_cache(self):
        """Broker reports 0 (disconnected state) + cold cache → raise, not fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 0}}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path):
                fs.invalidate_cache()
                with self.assertRaises(fs.BrokerEquityUnavailableError):
                    fs.get_sizing_anchor_usd()

    def test_serves_last_known_good_during_brief_outage(self):
        """Broker briefly unavailable after a good read → serve stale cache
        until STALENESS_CEILING_S expires (tolerates TWS hiccups without
        killing sizing)."""
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 12_000}}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path):
                fs.invalidate_cache()
                first = fs.get_sizing_anchor_usd()  # fresh read → cached
                self.assertAlmostEqual(first, 12_000, places=2)
                # Simulate broker going dark — delete report
                ro_path.unlink()
                # Force past 30s read-cache but still within staleness window
                fs._cached_anchor = (first, time.time() - 35)
                stale = fs.get_sizing_anchor_usd()
                self.assertAlmostEqual(stale, 12_000, places=2)

    def test_stale_cache_expires_then_raises(self):
        """After STALENESS_CEILING_S elapses with no fresh read → raise, stop
        serving stale data."""
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 12_000}}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path):
                fs.invalidate_cache()
                fs.get_sizing_anchor_usd()  # prime cache
                ro_path.unlink()  # broker goes dark
                # Force cache older than staleness ceiling
                fs._cached_anchor = (12_000.0, time.time() - (fs._STALENESS_CEILING_S + 10))
                with self.assertRaises(fs.BrokerEquityUnavailableError):
                    fs.get_sizing_anchor_usd()

    def test_anchor_cache_rereads_after_invalidate(self):
        """Cache must re-read on invalidate_cache()."""
        with tempfile.TemporaryDirectory() as tmp:
            ro_path = Path(tmp) / "risk_oversight_report.json"
            ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 10_000}}))
            cfg_path = Path(tmp) / "fleet_sizing.json"
            # No max_anchor_usd: cap disabled so we're testing cache behavior, not clamp
            cfg_path.write_text(json.dumps({"fallback_anchor_usd": 10_000}))
            with mock.patch.object(fs, "_RISK_OVERSIGHT_PATH", ro_path), \
                 mock.patch.object(fs, "_CONFIG_PATH", cfg_path):
                fs.invalidate_cache()
                first = fs.get_sizing_anchor_usd()
                # Update file
                ro_path.write_text(json.dumps({"broker_truth": {"account_equity_usd": 20_000}}))
                fs.invalidate_cache()
                second = fs.get_sizing_anchor_usd()
            self.assertEqual(first, 10_000)
            self.assertEqual(second, 20_000)


class TestTierEvaluation(unittest.TestCase):
    """Tier boundary tests — critical that these gates are off-by-one safe."""

    def setUp(self):
        fs.invalidate_cache()

    def test_unproven_with_zero_trades(self):
        stats = {"trades": 0, "profit_factor": 0, "win_rate": 0, "pnl_usd": 0}
        with mock.patch.object(fs, "compute_strategy_stats", return_value=stats):
            info = fs.get_effective_risk_pct("forge_gld_pm_long")
        self.assertEqual(info["tier_name"], "unproven")
        self.assertAlmostEqual(info["risk_pct"], 0.005, places=4)

    def test_emerging_requires_both_trades_and_pf(self):
        # 10 trades but PF below threshold → stays unproven
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 10, "profit_factor": 0.99, "pnl_usd": 0}):
            self.assertEqual(fs.get_effective_risk_pct("x")["tier_name"], "unproven")
        # 10 trades AND PF >= 1.0 → emerging
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 10, "profit_factor": 1.0, "pnl_usd": 0}):
            info = fs.get_effective_risk_pct("x")
        self.assertEqual(info["tier_name"], "emerging")
        self.assertAlmostEqual(info["risk_pct"], 0.010, places=4)

    def test_boundary_9_trades_stays_unproven(self):
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 9, "profit_factor": 2.0, "pnl_usd": 0}):
            self.assertEqual(fs.get_effective_risk_pct("x")["tier_name"], "unproven")

    def test_boundary_exactly_30_trades_and_pf_1_2_is_validated(self):
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 30, "profit_factor": 1.20, "pnl_usd": 0}):
            info = fs.get_effective_risk_pct("x")
        self.assertEqual(info["tier_name"], "validated")
        self.assertAlmostEqual(info["risk_pct"], 0.015, places=4)

    def test_boundary_60_trades_pf_1_3_is_promoted(self):
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 60, "profit_factor": 1.30, "pnl_usd": 0}):
            info = fs.get_effective_risk_pct("x")
        self.assertEqual(info["tier_name"], "promoted")
        self.assertAlmostEqual(info["risk_pct"], 0.020, places=4)

    def test_boundary_100_trades_pf_1_5_is_exceptional(self):
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 100, "profit_factor": 1.50, "pnl_usd": 0}):
            info = fs.get_effective_risk_pct("x")
        self.assertEqual(info["tier_name"], "exceptional")
        self.assertAlmostEqual(info["risk_pct"], 0.030, places=4)

    def test_tier_demotes_when_pf_decays(self):
        """A validated strategy that decays to PF 0.95 should fall back to unproven."""
        # Ramp up
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 35, "profit_factor": 1.25, "pnl_usd": 50}):
            up = fs.get_effective_risk_pct("x")
        self.assertEqual(up["tier_name"], "validated")
        # Decay: same trade count, PF drops
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 35, "profit_factor": 0.95, "pnl_usd": -20}):
            down = fs.get_effective_risk_pct("x")
        self.assertEqual(down["tier_name"], "unproven")

    def test_risk_pct_never_exceeds_ceiling(self):
        """Ceiling (3%) must clamp even if tier config would go higher."""
        with mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 500, "profit_factor": 5.0, "pnl_usd": 100_000}):
            info = fs.get_effective_risk_pct("x")
        self.assertLessEqual(info["risk_pct"], 0.030 + 1e-9)


class TestComputeRiskUsd(unittest.TestCase):
    def setUp(self):
        fs.invalidate_cache()

    def test_label_based_uses_tier(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000), \
             mock.patch.object(fs, "compute_strategy_stats",
                               return_value={"trades": 0, "profit_factor": 0, "pnl_usd": 0}):
            risk = fs.compute_risk_usd(strategy_label="x")
        self.assertAlmostEqual(risk, 50.0, places=2)  # 0.5% × $10K

    def test_numeric_pct_uses_legacy_path(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            risk = fs.compute_risk_usd(0.02)
        self.assertAlmostEqual(risk, 200.0, places=2)

    def test_numeric_pct_clamps_to_ceiling(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            risk = fs.compute_risk_usd(0.10)  # 10% requested
        self.assertAlmostEqual(risk, 300.0, places=2)  # capped at 3%


class TestNotionalCaps(unittest.TestCase):
    def setUp(self):
        fs.invalidate_cache()

    def test_stock_cap_is_2x_anchor(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            self.assertAlmostEqual(fs.max_notional_usd("stock"), 20_000, places=2)

    def test_fx_cap_is_20x_anchor(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            self.assertAlmostEqual(fs.max_notional_usd("fx"), 200_000, places=2)

    def test_micro_future_cap_is_5x(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            self.assertAlmostEqual(fs.max_notional_usd("micro_future"), 50_000, places=2)

    def test_unknown_asset_class_defaults_to_1x(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=10_000):
            self.assertAlmostEqual(fs.max_notional_usd("some_unknown"), 10_000, places=2)


class TestFleetMaxOpenRisk(unittest.TestCase):
    def setUp(self):
        fs.invalidate_cache()

    def test_derived_from_anchor(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=100_000):
            self.assertAlmostEqual(fs.get_fleet_max_open_risk_usd(), 6000.0, places=2)

    def test_scales_with_anchor(self):
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=50_000):
            small = fs.get_fleet_max_open_risk_usd()
        with mock.patch.object(fs, "get_sizing_anchor_usd", return_value=200_000):
            big = fs.get_fleet_max_open_risk_usd()
        # 4x anchor → 4x cap
        self.assertAlmostEqual(big / small, 4.0, places=2)


class TestComputeStrategyStats(unittest.TestCase):
    """Critical: live_cutoff must exclude back-fill from tier calc."""

    def _write_trades_csv(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            if not rows:
                f.write("")
                return
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_live_cutoff_excludes_backfill(self):
        """gdx_gld has strategy_live_cutoffs in config. Trades before should not count."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Write 3 back-fill rows (old) + 2 post-cutoff rows (new)
            csv_path = repo / "forge" / "logs" / "gdx_gld" / "trades.csv"
            rows = [
                {"exit_date": "2020-01-15", "pnl_usd": "100"},
                {"exit_date": "2021-02-20", "pnl_usd": "200"},
                {"exit_date": "2026-04-02", "pnl_usd": "300"},   # back-fill (before cutoff)
                {"exit_date": "2026-04-18", "pnl_usd": "50"},    # post-cutoff live
                {"exit_date": "2026-04-19", "pnl_usd": "75"},    # post-cutoff live
            ]
            self._write_trades_csv(csv_path, rows)

            # Mock repo path + cutoff config
            fake_cfg = {
                "fallback_anchor_usd": 10000,
                "tiers": [{"name": "unproven", "min_valid_trades": 0, "min_profit_factor": 0, "risk_pct": 0.005}],
                "tier_window_days": 10000,   # big window so date cutoff is the binding constraint
                "strategy_live_cutoffs": {"forge_gdx_gld": "2026-04-17T00:00:00+00:00"},
                "max_risk_pct_per_trade_ceiling": 0.03,
                "drawdown_brake_threshold": 0.5,
            }
            with mock.patch.object(fs, "_REPO", repo), \
                 mock.patch.object(fs, "_load_config", return_value=fake_cfg):
                stats = fs.compute_strategy_stats("forge_gdx_gld")
            # Only the 2 post-cutoff rows should count
            self.assertEqual(stats["trades"], 2)
            self.assertAlmostEqual(stats["pnl_usd"], 125.0, places=2)

    def test_window_days_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            csv_path = repo / "forge" / "logs" / "gld_pm_long" / "trades.csv"
            # Write rows: one 200 days ago, one 3 days ago
            old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
            recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            self._write_trades_csv(csv_path, [
                {"ts": old_date, "pnl_usd": "100"},
                {"ts": recent_date, "pnl_usd": "50"},
            ])
            fake_cfg = {
                "tiers": [{"name": "unproven", "min_valid_trades": 0, "min_profit_factor": 0, "risk_pct": 0.005}],
                "tier_window_days": 30,
                "strategy_live_cutoffs": {},
                "max_risk_pct_per_trade_ceiling": 0.03,
                "drawdown_brake_threshold": 0.5,
            }
            with mock.patch.object(fs, "_REPO", repo), \
                 mock.patch.object(fs, "_load_config", return_value=fake_cfg):
                stats = fs.compute_strategy_stats("forge_gld_pm_long", window_days=30)
            # Only the 3-day-old trade counts
            self.assertEqual(stats["trades"], 1)
            self.assertAlmostEqual(stats["pnl_usd"], 50.0, places=2)

    def test_missing_csv_returns_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fs, "_REPO", Path(tmp)):
                stats = fs.compute_strategy_stats("forge_gld_pm_long")
        self.assertEqual(stats["trades"], 0)
        self.assertEqual(stats["pnl_usd"], 0.0)

    def test_pf_infinite_on_no_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            csv_path = repo / "forge" / "logs" / "gld_pm_long" / "trades.csv"
            today = datetime.now(timezone.utc).isoformat()
            self._write_trades_csv(csv_path, [
                {"ts": today, "pnl_usd": "100"},
                {"ts": today, "pnl_usd": "50"},
            ])
            fake_cfg = {
                "tiers": [{"name": "unproven", "min_valid_trades": 0, "min_profit_factor": 0, "risk_pct": 0.005}],
                "tier_window_days": 365,
                "strategy_live_cutoffs": {},
                "max_risk_pct_per_trade_ceiling": 0.03,
                "drawdown_brake_threshold": 0.5,
            }
            with mock.patch.object(fs, "_REPO", repo), \
                 mock.patch.object(fs, "_load_config", return_value=fake_cfg):
                stats = fs.compute_strategy_stats("forge_gld_pm_long")
        self.assertEqual(stats["trades"], 2)
        # No losses → PF sentinel 999
        self.assertGreater(stats["profit_factor"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
