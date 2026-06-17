"""Tests for helio.roi_filter — phantom + killed + backfill exclusion."""
from __future__ import annotations

import pytest

from helio import roi_filter as rf


def test_phantom_explicit_excluded():
    rows = [
        {"ts": "2026-05-05T16:30:35.000+00:00", "strategy": "forge_nq_london_close",
         "symbol": "MNQ", "size": 417, "pnl_usd": 8324.36},
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 0
    assert stats.n_phantom_explicit == 1


def test_phantom_threshold_excluded():
    rows = [
        {"ts": "2026-05-10T12:00:00+00:00", "strategy": "forge_unknown",
         "symbol": "FOO", "size": 1, "pnl_usd": 5000.0},
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 0
    assert stats.n_phantom_threshold == 1


def test_killed_strategy_post_cutoff_excluded():
    rows = [
        {"ts": "2026-05-13T10:00:00+00:00", "strategy": "forge_spy_mean_rev",
         "symbol": "SPY", "size": 10, "pnl_usd": -5.0},  # post-kill-date
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 0
    assert stats.n_killed_post_cutoff == 1


def test_killed_strategy_pre_cutoff_kept():
    rows = [
        {"ts": "2026-04-25T10:00:00+00:00", "strategy": "forge_spy_mean_rev",
         "symbol": "SPY", "size": 10, "pnl_usd": -5.0},  # pre-kill-date
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 1


def test_backfill_null_anchor_excluded():
    rows = [
        {"ts": "2026-04-28T02:55:33+00:00", "strategy": "forge_jpy_pm_short",
         "symbol": "CADJPY", "size": 59075, "pnl_usd": -38.8,
         "source": "backfill_from_trade_csv", "exit_ts": None},
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 0
    assert stats.n_backfill_null_anchor == 1


def test_epoch_boundary_excludes_pre_epoch():
    rows = [
        {"ts": "2026-04-25T10:00:00+00:00", "strategy": "forge_nq_overnight",
         "symbol": "SPY", "size": 1, "pnl_usd": 50.0},
        {"ts": "2026-06-02T10:00:00+00:00", "strategy": "forge_xs_momentum",
         "symbol": "SPY", "size": 1, "pnl_usd": 75.0},
    ]
    kept, stats = rf.filter_fills(rows, epoch_start="2026-06-01")
    assert len(kept) == 1
    assert kept[0]["pnl_usd"] == 75.0
    assert stats.n_pre_epoch == 1


def test_normal_row_kept():
    rows = [
        {"ts": "2026-05-15T10:00:00+00:00", "strategy": "forge_nq_overnight",
         "symbol": "MNQ", "size": 1, "pnl_usd": 50.0},
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 1
    assert stats.n_total == 1
    assert stats.n_kept == 1


def test_bad_schema_excluded():
    rows = [
        "not a dict",
        {"missing_ts": True},
        {"ts": "2026-05-15T10:00:00+00:00"},  # missing strategy
    ]
    kept, stats = rf.filter_fills(rows)
    assert len(kept) == 0
    assert stats.n_bad_schema == 3


def test_assert_min_entries_raises_on_zero():
    with pytest.raises(RuntimeError, match="ROI computation aborted"):
        rf.assert_min_entries([], min_n=1)


def test_assert_min_entries_passes_with_enough():
    rf.assert_min_entries([{"ts": "x"}, {"ts": "y"}], min_n=2)


def test_filter_stats_str_includes_all_counts():
    stats = rf.FilterStats(n_total=10, n_kept=8, n_phantom_explicit=1, n_phantom_threshold=1)
    s = str(stats)
    assert "total=10" in s
    assert "kept=8" in s
    assert "phantom_explicit=1" in s
