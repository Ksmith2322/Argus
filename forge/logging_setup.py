"""Shared logging setup for Forge sub-system runners.

Why: 4 of 8 Forge runners (themis, mamba, tori, cuebanks) had only
basicConfig with StreamHandler — when launched via Popen with stdout
redirected, log lines were unreliable. Themis went 4 days with a 0-byte
log file, making health audits unfalsifiable. This helper guarantees
every Forge runner writes to a rotating file under forge/logs/<name>/.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a Forge runner's logger with both file and stream output.

    Writes to forge/logs/<name>/runner.log with 10MB rotation, 5 backups.
    Idempotent — safe to call from a runner that's also called by tests.
    """
    log_dir = REPO / "forge" / "logs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runner.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if any(getattr(h, "_forge_setup", False) for h in logger.handlers):
        return logger  # already configured

    file_handler = RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler._forge_setup = True
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler._forge_setup = True
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger
