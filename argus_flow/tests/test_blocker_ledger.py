from __future__ import annotations

from datetime import datetime, timedelta, timezone

from helio.blocker_ledger import read_blockers, record_blocker, summarize_blockers


def test_blocker_ledger_records_and_summarizes(tmp_path):
    path = tmp_path / "blocker_ledger.csv"
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()

    record_blocker(
        "forge_xs_momentum",
        "CAP_EXCEEDED",
        symbol="GLD",
        stage="entry",
        action="BUY",
        qty=3,
        notional_usd=612.345,
        reason="OVERLAP:GLD_ACTIVE_SLEEVES",
        context={"cap_x": 0.4},
        path=path,
    )
    record_blocker(
        "forge_xs_momentum",
        "SIZE_ZERO",
        symbol="TLT",
        stage="entry",
        action="BUY",
        reason="computed quantity < 1",
        ts=old,
        path=path,
    )

    rows = read_blockers(path=path)
    assert len(rows) == 2
    assert rows[0]["strategy"] == "forge_xs_momentum"
    assert rows[0]["code"] == "CAP_EXCEEDED"
    assert rows[0]["notional_usd"] == "612.35"

    summary = summarize_blockers(days=30, path=path)
    assert summary["total"] == 1
    assert summary["by_code"] == {"CAP_EXCEEDED": 1}
    assert summary["by_strategy"] == {"forge_xs_momentum": {"CAP_EXCEEDED": 1}}
