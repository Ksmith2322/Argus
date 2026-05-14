"""Tests for close_position_market reqExecutions-fallback recovery.

Background (2026-05-13 audit): close_position_market's `_wait_for_fill`
can return filled=False even when the broker actually filled — TWS race
where the synchronous wait window expires while the fill is in flight,
or the order is reported as Cancelled but a separate fill goes through.
Without a fallback, fomc_drift / tom_international silently lose trades
(exit_px=None gates the trade record append, but state["open_trade"] is
already cleared). The fix queries reqExecutions when the wait fails and
recovers the actual fill price from the broker's record.
"""
from __future__ import annotations

import time
import types
from datetime import datetime, timezone
from unittest import mock

from helio import ibkr_execution as ie


def _make_execution(side: str, price: float, ts: datetime) -> types.SimpleNamespace:
    """Mock ib_insync Fill: has .execution (with .side, .price, .orderId) and .time."""
    return types.SimpleNamespace(
        execution=types.SimpleNamespace(side=side, price=price, orderId=42),
        time=ts,
    )


def test_recover_close_picks_latest_opposite_side_fill():
    """Long position closed → look for SLD fills, return the latest."""
    now = datetime.now(timezone.utc)
    older = now.replace(year=now.year - 1)  # an entry fill from way earlier
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = [
        _make_execution("BOT", 100.0, older),  # entry fill — wrong side, ignored
        _make_execution("SLD", 101.5, now),    # the close we want
    ]
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")

    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="long"
    )

    assert result is not None
    assert result.filled is True
    assert result.fill_price == 101.5


def test_recover_close_returns_none_when_no_opposite_fill():
    """If reqExecutions has only entry-side fills, return None."""
    now = datetime.now(timezone.utc)
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = [
        _make_execution("BOT", 100.0, now),  # entry only, no close
    ]
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")

    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="long"
    )

    assert result is None


def test_recover_close_returns_none_when_reqexecutions_empty():
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = []
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")

    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="long"
    )

    assert result is None


def test_recover_close_returns_none_when_reqexecutions_raises():
    """Defensive: a transient TWS error shouldn't bubble up as an exception
    — the runner should see None and continue with the original failure."""
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.side_effect = Exception("TWS disconnected mid-call")
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")

    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="long"
    )

    assert result is None


def test_recover_close_for_short_position_picks_bot_side():
    """Short position closed → look for BOT (cover-buy) fills."""
    now = datetime.now(timezone.utc)
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = [
        _make_execution("SLD", 100.0, now.replace(year=now.year - 1)),  # entry
        _make_execution("BOT", 99.2, now),                              # close
    ]
    contract = types.SimpleNamespace(symbol="MYM", secType="FUT")

    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="short"
    )

    assert result is not None
    assert result.fill_price == 99.2


def test_recover_close_after_ts_filter_excludes_stale_fills():
    """after_ts cutoff excludes fills from before order submission — avoids
    confusing a yesterday's close with today's close on the same symbol."""
    now = datetime.now(timezone.utc)
    older = now.replace(year=now.year - 1)
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = [
        _make_execution("SLD", 100.0, older),  # before our submit_ts cutoff
    ]
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")

    cutoff = time.time()  # NOW — older fill is well before
    result = ie._recover_close_fill_from_executions(
        fake_ib, contract, entry_direction="long", after_ts=cutoff,
    )

    assert result is None


def test_close_position_market_returns_recovered_fill_when_wait_times_out():
    """End-to-end: _wait_for_fill returns unfilled, but reqExecutions has the
    real fill — close_position_market should return the recovered FillResult."""
    now = datetime.now(timezone.utc)
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = [
        _make_execution("SLD", 101.5, now),
    ]
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")
    fake_trade = mock.MagicMock()
    fake_ib.placeOrder.return_value = fake_trade

    with mock.patch.object(ie, "is_market_open", return_value=True), \
         mock.patch.object(ie, "is_flatten_active", return_value=(False, "")), \
         mock.patch.object(ie, "cancel_order_by_id"), \
         mock.patch.object(ie, "_wait_for_fill", return_value=ie.FillResult(
             filled=False, reject_reason="Cancelled",
         )):
        result = ie.close_position_market(
            fake_ib, contract, direction="long", size=21.0,
        )

    assert result.filled is True
    assert result.fill_price == 101.5


def test_close_position_market_returns_unfilled_when_no_recovery_available():
    """If wait fails AND reqExecutions has no matching fill, return the
    original unfilled FillResult — preserves reject_reason for the caller."""
    fake_ib = mock.MagicMock()
    fake_ib.reqExecutions.return_value = []
    contract = types.SimpleNamespace(symbol="GLD", secType="STK")
    fake_trade = mock.MagicMock()
    fake_ib.placeOrder.return_value = fake_trade

    with mock.patch.object(ie, "is_market_open", return_value=True), \
         mock.patch.object(ie, "is_flatten_active", return_value=(False, "")), \
         mock.patch.object(ie, "cancel_order_by_id"), \
         mock.patch.object(ie, "_wait_for_fill", return_value=ie.FillResult(
             filled=False, reject_reason="Cancelled",
         )):
        result = ie.close_position_market(
            fake_ib, contract, direction="long", size=21.0,
        )

    assert result.filled is False
    assert result.reject_reason == "Cancelled"
