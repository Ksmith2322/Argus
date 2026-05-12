from __future__ import annotations

from helio.domain import Fill
from helio.canonical_fills import _backfill_dedup_key
from helio.reconciliation import reconcile_strategy
from ops.canonical_reconcile import _dedup_rows, _duplicate_stats


def test_reconciliation_prefers_live_fill_over_backfill_twin():
    spec = ("forge_gld_pm_long", "unused.csv", "ts", False, "GLD", False)
    fills = [
        Fill(
            ts="2026-05-02T04:00:00+00:00",
            strategy="forge_gld_pm_long",
            symbol="GLD",
            side="EXIT",
            entry_ts="2026-05-01 18:30:00+00:00",
            exit_ts=None,
            pnl_usd=10.0,
            source="backfill_from_trade_csv",
        ),
        Fill(
            ts="2026-05-01T20:00:00+00:00",
            strategy="forge_gld_pm_long",
            symbol="GLD",
            side="EXIT",
            entry_ts="2026-05-01 18:30:00+00:00",
            exit_ts="2026-05-01T19:45:00+00:00",
            pnl_usd=10.0,
        ),
    ]

    report = reconcile_strategy(spec, fills)

    assert report["canonical_row_count"] == 1
    assert report["canonical_pnl_sum_usd"] == 10.0
    assert report["status"] == "DRIFT"  # no CSV staged in this unit test


def test_reconciliation_normalizes_nq_symbol_alias():
    spec = ("forge_nq_overnight", "unused.csv", "ts", False, "MNQ", False)
    fills = [
        Fill(
            ts="2026-05-02T04:00:00+00:00",
            strategy="forge_nq_overnight",
            symbol="NQ",
            side="EXIT",
            entry_ts="2026-05-01 20:00:00+00:00",
            pnl_usd=20.0,
            source="backfill_from_trade_csv",
        ),
        Fill(
            ts="2026-05-01T22:00:00+00:00",
            strategy="forge_nq_overnight",
            symbol="MNQ",
            side="EXIT",
            entry_ts="2026-05-01 20:00:00+00:00",
            exit_ts="2026-05-01 22:00:00+00:00",
            pnl_usd=20.0,
        ),
    ]

    report = reconcile_strategy(spec, fills)

    assert report["canonical_row_count"] == 1
    assert report["canonical_pnl_sum_usd"] == 20.0


def test_backfill_dedup_key_matches_live_rows_without_csv_exit_ts():
    live_key = _backfill_dedup_key(
        "forge_gld_pm_long",
        "2026-05-01 18:30:00+00:00",
        "2026-05-01T19:45:27+00:00",
        "GLD",
    )
    csv_backfill_key = _backfill_dedup_key(
        "forge_gld_pm_long",
        "2026-05-01 18:30:00+00:00",
        None,
        "GLD",
    )

    assert live_key == csv_backfill_key


def test_backfill_dedup_key_uses_argus_exit_timestamp_and_symbol_alias():
    live_key = _backfill_dedup_key(
        "argus_usdjpy",
        "2026-04-23T15:50:01+00:00",
        "2026-04-23T15:56:32+00:00",
        "USDJPY",
    )
    csv_backfill_key = _backfill_dedup_key(
        "argus_usdjpy",
        "2026-04-23 15:56:32+00:00",
        None,
        "USDJPY",
    )
    nq_key = _backfill_dedup_key("forge_nq_overnight", "2026-05-01 20:00:00+00:00", None, "NQ")
    mnq_key = _backfill_dedup_key("forge_nq_overnight", "2026-05-01 20:00:00+00:00", None, "MNQ")

    assert live_key == csv_backfill_key
    assert nq_key == mnq_key


def test_count_reconcile_keeps_distinct_same_minute_trades():
    rows = [
        {
            "strategy": "forge_multi_orb",
            "symbol": "QQQ",
            "entry_ts": "2026-05-07 15:50:00+00:00",
            "direction": "long",
            "entry_px": "100",
            "exit_px": "101",
            "pnl_usd": "12.18",
        },
        {
            "strategy": "forge_multi_orb",
            "symbol": "QQQ",
            "entry_ts": "2026-05-07 15:50:00+00:00",
            "direction": "long",
            "entry_px": "100",
            "exit_px": "99",
            "pnl_usd": "-6.58",
        },
    ]

    assert len(_dedup_rows("forge_multi_orb", rows, None)) == 2
    assert _duplicate_stats("forge_multi_orb", rows, None)["duplicate_same_source"] == 0


def test_count_reconcile_treats_backfill_twin_as_shadowed_not_duplicate():
    rows = [
        {
            "strategy": "forge_gld_pm_long",
            "symbol": "GLD",
            "entry_ts": "2026-05-01 18:30:00+00:00",
            "exit_ts": "2026-05-01T19:45:27+00:00",
            "direction": "long",
            "entry_px": "424.5",
            "exit_px": "423.32",
            "pnl_usd": "-25.96",
        },
        {
            "strategy": "forge_gld_pm_long",
            "symbol": "GLD",
            "entry_ts": "2026-05-01 18:30:00+00:00",
            "exit_ts": None,
            "direction": "long",
            "entry_px": "424.5",
            "exit_px": "423.32",
            "pnl_usd": "-25.96",
            "source": "backfill_from_trade_csv",
        },
    ]

    stats = _duplicate_stats("forge_gld_pm_long", rows, "GLD")

    assert len(_dedup_rows("forge_gld_pm_long", rows, "GLD")) == 1
    assert stats["duplicate_same_source"] == 0
    assert stats["shadowed_backfills"] == 1
