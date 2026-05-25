"""Single-instance PID lock for forge runners.

Background (2026-05-24): a rogue gld_pm_long runner using the system
Python (not the venv) ran in parallel with the official venv-based
runner for an indeterminate period. The two processes stomped on each
other's state file and the rogue, on stale code/state, spammed Discord
with NOTIONAL_CAP warnings using cached pre-reset anchor values.

This module provides a cross-process PID lock so a second invocation
of a runner detects the first and refuses to start, regardless of
which Python launched it.

USAGE
=====
    from helio.runner_lock import acquire_runner_lock

    with acquire_runner_lock("forge_gld_pm_long"):
        # main loop
        ...

If a lock already exists with a LIVE pid, ``acquire_runner_lock`` raises
``RunnerAlreadyRunning`` so the second process exits non-zero. Stale
locks (PID does not exist any more) are silently replaced.

Lock file layout:
    <repo>/forge/logs/<short>/runner.lock
    {
        "pid": 12345,
        "started_at": "2026-05-25T01:30:00Z",
        "python_executable": "C:\\Argus\\.venv\\Scripts\\python.exe",
        "argv": ["-m", "forge.gld_pm_long.runner", "--loop"]
    }

A different Python (system vs venv) is logged so the operator can
diagnose at a glance which Python launched the rogue.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_REPO = Path(__file__).resolve().parents[1]


class RunnerAlreadyRunning(RuntimeError):
    """Raised by acquire_runner_lock when another live process already
    holds the lock for the same strategy."""


def _lock_path(strategy_label: str) -> Path:
    """Build the lock path. ``forge_gld_pm_long`` -> ``forge/logs/gld_pm_long/runner.lock``."""
    short = strategy_label.replace("forge_", "", 1)
    return _REPO / "forge" / "logs" / short / "runner.lock"


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform check for whether ``pid`` is a live process."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # Use ctypes to query process state without a process handle leak.
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                still = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                # STILL_ACTIVE = 259
                return bool(still) and exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        else:
            # POSIX: kill(pid, 0) succeeds if process exists + we have permission.
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _read_lock(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt lock file. Treat as stale so the caller overwrites.
        return None


def _write_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python_executable": sys.executable,
        "argv": list(sys.argv),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def check_runner_lock(strategy_label: str) -> dict | None:
    """Inspect-only: return current lock state for ``strategy_label`` or
    None if no lock exists.

    Returned dict adds an ``alive`` boolean derived from the lock's pid.
    Useful for diagnostics (e.g. dashboard panel) without altering state.
    """
    path = _lock_path(strategy_label)
    data = _read_lock(path)
    if data is None:
        return None
    pid = int(data.get("pid", 0))
    data = dict(data)
    data["alive"] = _pid_is_alive(pid)
    data["lock_path"] = str(path)
    return data


@contextmanager
def acquire_runner_lock(strategy_label: str) -> Iterator[Path]:
    """Acquire a single-instance lock for ``strategy_label`` for the
    lifetime of the ``with`` block.

    Raises ``RunnerAlreadyRunning`` if another live process holds the
    lock. Stale locks (pid no longer alive) are silently replaced.

    Yields the lock file Path.
    """
    path = _lock_path(strategy_label)
    existing = _read_lock(path)
    if existing is not None:
        prev_pid = int(existing.get("pid", 0))
        if _pid_is_alive(prev_pid):
            raise RunnerAlreadyRunning(
                f"{strategy_label} already running as pid={prev_pid} "
                f"started_at={existing.get('started_at')} "
                f"python={existing.get('python_executable')}"
            )
        # Else stale; will be overwritten by _write_lock below.

    _write_lock(path)
    try:
        yield path
    finally:
        # Only delete the lock if it's still OUR lock. A subsequent
        # process that stole it (race condition) shouldn't be evicted
        # by our cleanup.
        current = _read_lock(path)
        if current is not None and int(current.get("pid", 0)) == os.getpid():
            try:
                path.unlink()
            except OSError:
                pass
