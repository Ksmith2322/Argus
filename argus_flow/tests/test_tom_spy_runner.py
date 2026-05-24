"""Tests for forge.tom_spy.runner — control-flow + calendar logic.

The TOM strategy is calendar-based, so the central correctness check is:
do is_tom_entry_day / is_tom_exit_day return the expected dates? IBKR
submission paths are tested via mocking like xs_momentum/test_xs_momentum_runner.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from forge.tom_spy import runner as tom_runner


# ─── is_tom_entry_day / is_tom_exit_day ──────────────────────────────

def test_entry_day_may_2026():
    """May 2026 weekdays = May 1, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15,
    18, 19, 20, 21, 22, 25, 26, 27, 28, 29. That's 21 weekdays.
    PARAMS['entry_offset']=4 ⇒ days[-5] = May 25 (Mon). This is the
    canonical-TOM convention: enter at close of day -5 so the position
    is held through the LAST 4 DAYS of the month + first 3 of next."""
    assert tom_runner.is_tom_entry_day(date(2026, 5, 25)) is True
    # Adjacent days should NOT be entry days
    assert tom_runner.is_tom_entry_day(date(2026, 5, 22)) is False  # Fri prev week
    assert tom_runner.is_tom_entry_day(date(2026, 5, 26)) is False  # day -4
    assert tom_runner.is_tom_entry_day(date(2026, 5, 27)) is False  # day -3


def test_exit_day_june_2026():
    """June 2026 weekdays start: June 1, 2, 3 (Mon/Tue/Wed). 3rd weekday
    is June 3."""
    assert tom_runner.is_tom_exit_day(date(2026, 6, 3)) is True
    assert tom_runner.is_tom_exit_day(date(2026, 6, 2)) is False
    assert tom_runner.is_tom_exit_day(date(2026, 6, 4)) is False


def test_neither_in_middle_of_month():
    """May 15, 2026 is neither entry nor exit."""
    assert tom_runner.is_tom_entry_day(date(2026, 5, 15)) is False
    assert tom_runner.is_tom_exit_day(date(2026, 5, 15)) is False


def test_weekend_is_never_entry_or_exit():
    """Saturday/Sunday aren't in the weekday list, so they can never
    match the Nth-to-last or Mth weekday."""
    assert tom_runner.is_tom_entry_day(date(2026, 5, 30)) is False  # Sat
    assert tom_runner.is_tom_entry_day(date(2026, 5, 31)) is False  # Sun
    assert tom_runner.is_tom_exit_day(date(2026, 5, 30)) is False
    assert tom_runner.is_tom_exit_day(date(2026, 5, 31)) is False


def test_short_month_february_2024_entry():
    """Feb 2024 has 21 weekdays. days[-5] = Feb 23 (Fri) per the day-5
    entry convention (canonical-TOM: hold last 4 days + first 3)."""
    assert tom_runner.is_tom_entry_day(date(2024, 2, 23)) is True


def test_january_2025_exit_day():
    """Jan 2025 weekdays start Wed Jan 1 (let's check)… Actually Jan 1
    2025 is a Wed. So weekday list begins Jan 1, 2, 3 (Wed-Fri). 3rd
    weekday is Jan 3."""
    assert tom_runner.is_tom_exit_day(date(2025, 1, 3)) is True


# ─── evaluate_once control flow ──────────────────────────────────────

def test_evaluate_noops_on_non_action_day(monkeypatch, tmp_path):
    """When today is neither entry nor exit, action='noop' and IBKR is
    never contacted."""
    monkeypatch.setattr(tom_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tom_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(tom_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(tom_runner, "is_tom_entry_day", lambda d: False)
    monkeypatch.setattr(tom_runner, "is_tom_exit_day", lambda d: False)

    def _boom(*a, **kw):
        raise AssertionError("must not contact IBKR on non-action day")
    monkeypatch.setattr(tom_runner.ibkr, "connect_with_retry", _boom)

    summary = tom_runner.evaluate_once()
    assert summary["action"] == "noop"


def test_evaluate_blocks_when_allocation_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(tom_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tom_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(tom_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(tom_runner, "is_tom_entry_day", lambda d: True)
    monkeypatch.setattr(tom_runner, "is_tom_exit_day", lambda d: False)
    monkeypatch.setattr(tom_runner, "get_allocation_factor", lambda label: 0.0)

    def _boom(*a, **kw):
        raise AssertionError("must not connect IBKR when alloc=0")
    monkeypatch.setattr(tom_runner.ibkr, "connect_with_retry", _boom)

    summary = tom_runner.evaluate_once()
    assert summary["action"] == "blocked"
    assert "alloc_factor" in summary["reason"]


def test_evaluate_skips_entry_when_position_already_open(monkeypatch, tmp_path):
    """If state.open_trade is set, don't double up on entry day."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"open_trade": {"entry_ts": "2026-05-26T20:00:00Z", '
                            '"entry_px": 500.0, "qty": 10}, "trade_count": 1, '
                            '"session_id": "abc"}', encoding="utf-8")
    monkeypatch.setattr(tom_runner, "STATE_PATH", state_path)
    monkeypatch.setattr(tom_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(tom_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(tom_runner, "is_tom_entry_day", lambda d: True)
    monkeypatch.setattr(tom_runner, "is_tom_exit_day", lambda d: False)

    def _boom(*a, **kw):
        raise AssertionError("must not contact IBKR or read alloc when position open")
    monkeypatch.setattr(tom_runner.ibkr, "connect_with_retry", _boom)
    monkeypatch.setattr(tom_runner, "get_allocation_factor", _boom)

    summary = tom_runner.evaluate_once()
    assert "skip_entry" in summary["action"]


def test_evaluate_skips_exit_when_no_position(monkeypatch, tmp_path):
    monkeypatch.setattr(tom_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tom_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(tom_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(tom_runner, "is_tom_entry_day", lambda d: False)
    monkeypatch.setattr(tom_runner, "is_tom_exit_day", lambda d: True)

    def _boom(*a, **kw):
        raise AssertionError("must not contact IBKR when no position to exit")
    monkeypatch.setattr(tom_runner.ibkr, "connect_with_retry", _boom)

    summary = tom_runner.evaluate_once()
    assert "skip_exit" in summary["action"]


def test_signal_only_mode_does_not_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(tom_runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tom_runner, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(tom_runner, "TRADES_PATH", tmp_path / "trades.csv")
    monkeypatch.setattr(tom_runner, "is_tom_entry_day", lambda d: True)
    monkeypatch.setattr(tom_runner, "is_tom_exit_day", lambda d: False)
    monkeypatch.setattr(tom_runner, "get_allocation_factor", lambda label: 0.3)
    monkeypatch.setattr(tom_runner, "_SIGNAL_ONLY_MODE", True)

    def _boom(*a, **kw):
        raise AssertionError("must not connect IBKR in signal-only mode")
    monkeypatch.setattr(tom_runner.ibkr, "connect_with_retry", _boom)

    summary = tom_runner.evaluate_once()
    assert summary["action"] == "signal_only_entry"
