from __future__ import annotations

from datetime import datetime, timezone, timedelta

from ops.audit.run_blocked_opportunity_forward_score import resolve


def test_resolve_long_target():
    start = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    bars = [
        {"ts": start + timedelta(minutes=1), "open": 1.1000, "high": 1.1010, "low": 1.0998, "close": 1.1005},
        {"ts": start + timedelta(minutes=2), "open": 1.1005, "high": 1.1035, "low": 1.1004, "close": 1.1030},
    ]
    row = {"ts": start.isoformat(), "direction": "long", "price": "1.1000", "symbol": "GBPUSD"}
    result = resolve(row, bars)
    assert result["status"] == "RESOLVED"
    assert result["exit_reason"] == "target"
    assert result["pnl_pips"] == 30.0


def test_resolve_short_stop():
    start = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    bars = [
        {"ts": start + timedelta(minutes=1), "open": 155.00, "high": 155.31, "low": 154.95, "close": 155.20},
    ]
    row = {"ts": start.isoformat(), "direction": "short", "price": "155.00", "symbol": "USDJPY"}
    result = resolve(row, bars)
    assert result["status"] == "RESOLVED"
    assert result["exit_reason"] == "stop"
    assert result["pnl_pips"] == -30.0
