"""Shared logging setup for Forge sub-system runners.

Why: 4 of 8 Forge runners (themis, mamba, tori, cuebanks) had only
basicConfig with StreamHandler — when launched via Popen with stdout
redirected, log lines were unreliable. Themis went 4 days with a 0-byte
log file, making health audits unfalsifiable. This helper guarantees
every Forge runner writes to a rotating file under forge/logs/<name>/.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _in_test_context() -> bool:
    """True when imported under pytest / unittest.

    Fixes a bug where test_forge_dual_writes calling _close_paper_trade with a
    stub state wrote 'PAPER LONG closed' log lines into the production runner
    log (wick_gbpusd phantom-close, 2026-04-20). Under test, we skip the file
    handler so logs are stream-only and never touch forge/logs/<name>/runner.log.
    """
    if os.environ.get("FORGE_DISABLE_FILELOG"):
        return True
    if "pytest" in sys.modules or "unittest" in sys.modules:
        return True
    arg0 = (sys.argv[0] if sys.argv else "").lower()
    return "pytest" in arg0 or arg0.endswith(("unittest", "test_runner.py"))


def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a Forge runner's logger with both file and stream output.

    Writes to forge/logs/<name>/runner.log with 10MB rotation, 5 backups.
    Idempotent — safe to call from a runner that's also called by tests.

    Shared library loggers (``helio.signal_executor``, ``helio.ibkr_execution``)
    are also attached to the runner's file handler so silent-drop diagnostics
    (e.g. ``REAL_ENTRY FAILED``, ``BROKER_HAS_POSITION``, ``signal skipped``)
    surface in the runner's own log. Without this, runners using
    ``helio.signal_executor`` (cuebanks, multi_orb, etc) drop signals
    silently because the shared logger has no file handler attached.

    When running under pytest/unittest, the file handler is suppressed so test
    invocations don't pollute the production runner.log.
    """
    log_dir = REPO / "forge" / "logs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runner.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if any(getattr(h, "_forge_setup", False) for h in logger.handlers):
        return logger  # already configured

    file_handler = None
    if not _in_test_context():
        file_handler = RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler._forge_setup = True
        logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler._forge_setup = True
    logger.addHandler(stream_handler)

    logger.propagate = False

    # Pipe shared-library loggers into this runner's file handler so
    # signal_executor's drop reasons (REAL_ENTRY FAILED, BROKER_HAS_POSITION,
    # signal skipped, REAL_ENTRY EXCEPTION) are visible in the runner's log.
    # Each runner process has its own logging tree, so attaching here only
    # affects this process — no cross-runner duplication.
    for shared_name in ("helio.signal_executor", "helio.ibkr_execution"):
        shared = logging.getLogger(shared_name)
        if any(getattr(h, "_forge_setup", False) for h in shared.handlers):
            continue  # already wired by an earlier setup_logging() in this process
        shared.setLevel(level)
        if file_handler is not None:
            shared.addHandler(file_handler)
        shared.addHandler(stream_handler)
        shared.propagate = False

    return logger
