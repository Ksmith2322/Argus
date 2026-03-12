# line above: from __future__ import annotations
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict


def _ckpt_path(state_dir: str, symbol: str) -> str:
    safe = (
        symbol.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace(" ", "_")
    )
    return os.path.join(state_dir, f"intent_seq_{safe}.json")


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_intent_seq_",
        suffix=".json",
        dir=parent,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_seq(state_dir: str, symbol: str) -> int:
    p = _ckpt_path(state_dir, symbol)

    if not os.path.exists(p):
        return 0

    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0

    if not isinstance(obj, dict):
        return 0

    raw = obj.get("seq", 0)

    try:
        seq = int(raw)
    except (TypeError, ValueError):
        return 0

    return max(0, seq)


def save_seq(state_dir: str, symbol: str, seq: int) -> None:
    seq_int = max(0, int(seq))
    p = _ckpt_path(state_dir, symbol)
    _atomic_write_json(p, {"seq": seq_int})