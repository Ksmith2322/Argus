"""Tests for helio.runner_lock — single-instance PID lock guard.

Background: 2026-05-24 rogue gld_pm_long runner using system Python
ran in parallel with venv runner, spamming NOTIONAL_CAP alerts. The
lock guard prevents that recurring.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from helio.runner_lock import (
    RunnerAlreadyRunning,
    acquire_runner_lock,
    check_runner_lock,
    _lock_path,
    _pid_is_alive,
    _read_lock,
)


@pytest.fixture
def patched_repo(monkeypatch, tmp_path):
    """Redirect _REPO so the lock files land in tmp."""
    monkeypatch.setattr("helio.runner_lock._REPO", tmp_path)
    return tmp_path


# ─── PID liveness ────────────────────────────────────────────────────

def test_pid_zero_or_negative_is_not_alive():
    assert _pid_is_alive(0) is False
    assert _pid_is_alive(-1) is False


def test_our_own_pid_is_alive():
    assert _pid_is_alive(os.getpid()) is True


def test_obviously_dead_pid_is_not_alive():
    # 999999999 is past the conventional max PID on all our platforms.
    assert _pid_is_alive(999_999_999) is False


# ─── Lock path conventions ──────────────────────────────────────────

def test_lock_path_strips_forge_prefix(patched_repo):
    path = _lock_path("forge_gld_pm_long")
    assert path == patched_repo / "forge" / "logs" / "gld_pm_long" / "runner.lock"


def test_lock_path_for_unprefixed_strategy(patched_repo):
    path = _lock_path("xs_momentum")
    # No "forge_" prefix → strategy name used as-is
    assert path.name == "runner.lock"
    assert "xs_momentum" in str(path)


# ─── Happy path ──────────────────────────────────────────────────────

def test_acquire_writes_lock_and_releases_on_exit(patched_repo):
    path = _lock_path("forge_test")
    assert not path.exists()
    with acquire_runner_lock("forge_test") as lp:
        assert lp == path
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["python_executable"] == sys.executable
        assert "started_at" in data
    # Lock should be released
    assert not path.exists()


def test_lock_records_python_executable_for_diagnostics(patched_repo):
    """Operator must be able to see which Python launched the runner
    (the 2026-05-24 incident was a system-Python vs venv-Python
    collision)."""
    with acquire_runner_lock("forge_test"):
        info = check_runner_lock("forge_test")
        assert info["python_executable"] == sys.executable
        assert info["alive"] is True


# ─── Refusal when live lock exists ──────────────────────────────────

def test_acquire_refuses_when_live_lock_exists(patched_repo):
    """Simulate a live other-process lock by writing our own PID."""
    path = _lock_path("forge_test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": "2026-05-25T01:30:00Z",
        "python_executable": "C:\\some\\other\\python.exe",
        "argv": ["-m", "forge.test.runner"],
    }), encoding="utf-8")
    with pytest.raises(RunnerAlreadyRunning) as exc_info:
        with acquire_runner_lock("forge_test"):
            pytest.fail("should have refused")
    assert "already running" in str(exc_info.value).lower()
    # Original lock still present
    assert path.exists()


def test_acquire_replaces_stale_lock(patched_repo):
    """When the prior lock's PID is dead, acquire succeeds + overwrites."""
    path = _lock_path("forge_test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": 999_999_999,  # definitely dead
        "started_at": "2026-05-25T01:30:00Z",
        "python_executable": "C:\\some\\other\\python.exe",
        "argv": ["old"],
    }), encoding="utf-8")
    with acquire_runner_lock("forge_test"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["argv"] != ["old"]
    # Cleanly released after context exit
    assert not path.exists()


def test_acquire_replaces_corrupt_lock(patched_repo):
    """If a previous run wrote a partial lock file, treat as stale."""
    path = _lock_path("forge_test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with acquire_runner_lock("forge_test"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()


# ─── Release safety ──────────────────────────────────────────────────

def test_release_does_not_delete_someone_elses_lock(patched_repo):
    """If between yield and finally another process steals the lock,
    don't evict them on our cleanup."""
    path = _lock_path("forge_test")
    other_pid = os.getpid() + 1  # bogus but != ours
    try:
        with acquire_runner_lock("forge_test"):
            # Simulate another process stealing the lock
            path.write_text(json.dumps({
                "pid": other_pid,
                "started_at": "stolen",
                "python_executable": "other",
                "argv": [],
            }), encoding="utf-8")
    except Exception:
        pass
    # The "stolen" lock should remain — we MUST NOT have deleted it
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == other_pid


def test_release_handles_already_deleted_lock_gracefully(patched_repo):
    """If something external removed the lock during the with block,
    the cleanup should not raise."""
    path = _lock_path("forge_test")
    with acquire_runner_lock("forge_test"):
        path.unlink()  # external nuke
    # Should not have raised


# ─── check_runner_lock (inspection only) ─────────────────────────────

def test_check_runner_lock_returns_none_when_no_lock(patched_repo):
    assert check_runner_lock("forge_test") is None


def test_check_runner_lock_reports_alive_status(patched_repo):
    with acquire_runner_lock("forge_test"):
        info = check_runner_lock("forge_test")
        assert info["alive"] is True
        assert info["pid"] == os.getpid()
        assert "lock_path" in info


def test_check_runner_lock_reports_dead_status_for_stale_lock(patched_repo):
    path = _lock_path("forge_test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": 999_999_999,
        "started_at": "old",
        "python_executable": "old",
        "argv": [],
    }), encoding="utf-8")
    info = check_runner_lock("forge_test")
    assert info["alive"] is False
    assert info["pid"] == 999_999_999
