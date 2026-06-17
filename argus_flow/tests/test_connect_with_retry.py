"""Tests for helio.ibkr_execution.connect_with_retry.

Background (2026-05-18): TWS restart leaves client_id slots "in use" for
1-5 minutes (IBKR Error 326). Wake-and-sleep runners with 1-4hr cycles
would go silent until next cycle. This helper retries with backoff to
cover the typical slot-clear window.
"""
from __future__ import annotations

from unittest import mock

import pytest

from helio import ibkr_execution as ie


def test_succeeds_on_first_attempt():
    """Happy path: connect succeeds first try, no retries needed."""
    fake_ib = mock.MagicMock()
    with mock.patch.object(ie, "connect", return_value=fake_ib) as mock_connect:
        result = ie.connect_with_retry(client_id=999, max_attempts=3, backoff_s=0.01)
    assert result is fake_ib
    assert mock_connect.call_count == 1


def test_succeeds_on_third_attempt_after_transient_failures():
    """Two transient failures, third attempt succeeds. Common pattern
    right after TWS restart — slots clear within 1-3 minutes."""
    fake_ib = mock.MagicMock()
    side_effects = [
        ie.IBKRExecutionError("connect failed: TimeoutError(): attempt 1"),
        ie.IBKRExecutionError("connect failed: ConnectionRefusedError: attempt 2"),
        fake_ib,
    ]
    with mock.patch.object(ie, "connect", side_effect=side_effects) as mock_connect:
        result = ie.connect_with_retry(client_id=999, max_attempts=5, backoff_s=0.01)
    assert result is fake_ib
    assert mock_connect.call_count == 3


def test_raises_after_all_attempts_exhausted():
    """All attempts fail → IBKRExecutionError with full history."""
    err = ie.IBKRExecutionError("connect failed: persistent error")
    with mock.patch.object(ie, "connect", side_effect=err) as mock_connect:
        with pytest.raises(ie.IBKRExecutionError) as exc_info:
            ie.connect_with_retry(client_id=999, max_attempts=3, backoff_s=0.01)
    # All 3 attempts were made
    assert mock_connect.call_count == 3
    # Error message contains attempt history
    assert "exhausted 3 attempts" in str(exc_info.value)
    assert "client_id=999" in str(exc_info.value)


def test_max_attempts_one_behaves_like_single_attempt():
    """max_attempts=1 is equivalent to the bare connect() — no retries."""
    err = ie.IBKRExecutionError("connect failed")
    with mock.patch.object(ie, "connect", side_effect=err) as mock_connect:
        with pytest.raises(ie.IBKRExecutionError):
            ie.connect_with_retry(client_id=100, max_attempts=1, backoff_s=0.01)
    assert mock_connect.call_count == 1


def test_backoff_time_is_actually_waited(monkeypatch):
    """Verify the helper sleeps between attempts (covers the TWS-slot-clear window)."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(ie.time, "sleep", lambda s: sleep_calls.append(s))
    fake_ib = mock.MagicMock()
    side_effects = [
        ie.IBKRExecutionError("first fail"),
        ie.IBKRExecutionError("second fail"),
        fake_ib,
    ]
    with mock.patch.object(ie, "connect", side_effect=side_effects):
        ie.connect_with_retry(client_id=999, max_attempts=5, backoff_s=2.5)
    # Two failures → two sleeps between attempts
    assert sleep_calls == [2.5, 2.5]


def test_passes_timeout_to_underlying_connect():
    """The timeout kwarg is threaded through to the underlying connect()."""
    fake_ib = mock.MagicMock()
    with mock.patch.object(ie, "connect", return_value=fake_ib) as mock_connect:
        ie.connect_with_retry(client_id=42, timeout=30)
    mock_connect.assert_called_once_with(42, timeout=30)
