from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


REPO = Path(__file__).resolve().parents[1]
LOCK_DIR = REPO / "argus_flow" / "logs" / "_locks"


class ProcessLockError(RuntimeError):
    """Raised when another process already owns a named runtime lock."""


def _sanitize_lock_name(name: str) -> str:
    safe = []
    for ch in name.lower():
        safe.append(ch if ch.isalnum() or ch in ("-", "_") else "_")
    return "".join(safe).strip("_") or "argus"


def build_runner_lock_name(
    *,
    client_id: int,
    config_paths: list[str] | None,
    exclude: list[str] | None = None,
) -> str:
    stems = []
    for path in config_paths or []:
        stems.append(Path(path).stem.lower())
    stems = sorted(set(stems))
    excl = sorted(set((exclude or [])))
    stem_part = "__".join(stems) if stems else "auto_discovery"
    excl_part = "__".join(excl) if excl else "none"
    return _sanitize_lock_name(f"runner_c{client_id}_{stem_part}_exclude_{excl_part}")


def build_dashboard_lock_name(*, host: str, port: int) -> str:
    return _sanitize_lock_name(f"dashboard_{host}_{port}")


class ProcessLock:
    """Cross-process runtime lock backed by an OS file lock."""

    def __init__(self, name: str, lock_dir: Path | None = None):
        self.name = _sanitize_lock_name(name)
        self.lock_dir = lock_dir or LOCK_DIR
        self.path = self.lock_dir / f"{self.name}.lock"
        self.meta_path = self.lock_dir / f"{self.name}.json"
        self._fh = None

    def read_metadata(self) -> dict:
        try:
            payload = self.meta_path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}

    def acquire(self, metadata: dict | None = None) -> None:
        if self._fh is not None:
            return

        self.lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
            os.fsync(fh.fileno())

        try:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = self.read_metadata()
            fh.close()
            owner_str = ""
            if owner:
                owner_str = (
                    f" owner_pid={owner.get('pid', '?')}"
                    f" host={owner.get('host', '?')}"
                    f" started_at={owner.get('started_at', '?')}"
                )
            raise ProcessLockError(f"lock '{self.name}' is already held.{owner_str}")

        payload = {
            "lock_name": self.name,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if metadata:
            payload.update(metadata)

        self.meta_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
