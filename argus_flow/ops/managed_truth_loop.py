"""Persistent managed-truth refresh daemon.

Replaces the `ArgusManagedTruth` scheduled task that was configured "Interactive
only" and failed silently whenever the user RDP-disconnected. This is a simple
always-on process: every INTERVAL_S, call `risk_oversight.main()` to refresh
`argus_flow/logs/risk_oversight_report.json` (the dashboard + fleet_sizing's
broker-truth source).

Launch like any forge runner:
    python -m argus_flow.ops.managed_truth_loop

Ctrl-C to stop. Survives disconnects because it's a user-mode Python process,
not a Task Scheduler job tied to the interactive session.
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
from datetime import datetime, timezone

from argus_flow.ops import risk_oversight

import json
import subprocess
from pathlib import Path as _Path


def _pid_alive(pid: int) -> bool:
    """Windows-friendly PID liveness check. os.kill with signal 0 doesn't
    work cross-platform for our purposes — use psutil if available, else
    fallback to a platform-dispatched check."""
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        import os as _os
        # On Windows, os.kill(pid, 0) raises OSError for dead PIDs; for live
        # it may raise PermissionError on denied but the process exists.
        _os.kill(pid, 0)
        return True
    except OSError:
        return False
_REPO = _Path(__file__).resolve().parents[2]
_SILENT_BLOCK_SCRIPT = _REPO / "ops" / "silent_block_check.py"
_MATURITY_SCRIPT = _REPO / "ops" / "operational_maturity.py"
_SCHEMA_SCRIPT = _REPO / "ops" / "schema_validator.py"
_RECONCILE_SCRIPT = _REPO / "ops" / "canonical_reconcile.py"
_BROKER_DISCONNECT_SCRIPT = _REPO / "ops" / "broker_disconnect_check.py"
_COHORT_FAILURE_SCRIPT = _REPO / "ops" / "cohort_failure_check.py"
_TWS_HEALTH_SCRIPT = _REPO / "ops" / "tws_health_probe.py"
_CIRCUIT_BREAKER_SCRIPT = _REPO / "ops" / "daily_loss_circuit_breaker.py"
_FLATTEN_EXECUTOR_SCRIPT = _REPO / "ops" / "flatten_eod_executor.py"
_COMPUTE_MFE_SCRIPT = _REPO / "ops" / "compute_mfe.py"
_BROKER_DRIFT_SCRIPT = _REPO / "ops" / "broker_drift_aggregator.py"
_AUTO_ALLOCATOR_SCRIPT = _REPO / "ops" / "auto_allocator.py"
_REC_ACTIONS_ALERT_SCRIPT = _REPO / "ops" / "recommended_actions_alert.py"
_READINESS_EVAL_SCRIPT = _REPO / "ops" / "readiness_eval.py"


INTERVAL_S = 180  # refresh every 3 minutes
MAX_CONSECUTIVE_FAILURES = 20  # ~1hr of failures before we exit loud
MATURITY_REFRESH_HOUR_UTC = 5  # rebuild operational_maturity report once a day at this UTC hour
HOURLY_CHECKS_INTERVAL_S = 3600  # schema + reconcile run hourly (vs silent_block which runs every cycle)

# Track last day we ran maturity refresh + last epoch we ran hourly checks
_last_maturity_day: str | None = None
_last_hourly_epoch: float = 0.0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] managed_truth | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("managed_truth_loop")


def main() -> int:
    log.info("managed_truth_loop starting — interval=%ds", INTERVAL_S)
    consecutive_failures = 0
    cycle = 0
    while True:
        cycle += 1
        start = time.monotonic()
        try:
            risk_oversight.main()
            # Also run silent_block_check each cycle — cheap (just reads CSVs)
            # and produces argus_flow/logs/silent_block_alerts.json that the
            # dashboard / watchdog can surface. Invoked via subprocess to
            # avoid dataclass/importlib quirks with dynamic imports.
            if _SILENT_BLOCK_SCRIPT.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(_SILENT_BLOCK_SCRIPT)],
                        cwd=str(_REPO),
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as sbe:
                    log.warning("silent_block_check failed this cycle: %s", sbe)
            # Broker disconnect check also runs every cycle — fast (just a log parse).
            if _BROKER_DISCONNECT_SCRIPT.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(_BROKER_DISCONNECT_SCRIPT)],
                        cwd=str(_REPO),
                        capture_output=True,
                        timeout=15,
                    )
                except Exception as e:
                    log.warning("broker_disconnect_check failed this cycle: %s", e)

            # Daily-loss circuit breaker — every cycle. Cheap (one TWS query +
            # JSON write). Touches HALT.flag automatically when daily PnL crosses
            # -2% / -4% thresholds.
            if _CIRCUIT_BREAKER_SCRIPT.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(_CIRCUIT_BREAKER_SCRIPT)],
                        cwd=str(_REPO),
                        capture_output=True,
                        timeout=20,
                    )
                except Exception as e:
                    log.warning("daily_loss_circuit_breaker failed this cycle: %s", e)

            # FLATTEN_EOD executor — runs ONLY if FLATTEN_EOD.flag exists.
            # The script no-ops if flag is absent, so safe to call every cycle.
            _flatten_flag = _REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"
            if _flatten_flag.exists() and _FLATTEN_EXECUTOR_SCRIPT.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(_FLATTEN_EXECUTOR_SCRIPT)],
                        cwd=str(_REPO),
                        capture_output=True,
                        timeout=60,
                    )
                except Exception as e:
                    log.warning("flatten_eod_executor failed this cycle: %s", e)

            # Hourly: schema validation + canonical reconcile + orphan lock cleanup.
            global _last_hourly_epoch
            now_epoch = time.time()
            if now_epoch - _last_hourly_epoch >= HOURLY_CHECKS_INTERVAL_S:
                # Circuit breaker runs every cycle (every 3 min via outer loop) — not hourly.
                # Pre-check it here too for safety.
                # FLATTEN executor runs only if FLATTEN_EOD.flag is present.
                for name, script in (("schema_validator", _SCHEMA_SCRIPT),
                                     ("canonical_reconcile", _RECONCILE_SCRIPT),
                                     ("cohort_failure_check", _COHORT_FAILURE_SCRIPT),
                                     ("tws_health_probe", _TWS_HEALTH_SCRIPT),
                                     ("compute_mfe", _COMPUTE_MFE_SCRIPT),
                                     ("broker_drift_aggregator", _BROKER_DRIFT_SCRIPT),
                                     ("auto_allocator", _AUTO_ALLOCATOR_SCRIPT),
                                     ("recommended_actions_alert", _REC_ACTIONS_ALERT_SCRIPT),
                                     ("readiness_eval", _READINESS_EVAL_SCRIPT)):
                    if script.exists():
                        try:
                            subprocess.run(
                                [sys.executable, str(script)],
                                cwd=str(_REPO),
                                capture_output=True,
                                timeout=30,
                            )
                        except Exception as e:
                            log.warning("%s failed this hour: %s", name, e)

                # Orphan lock cleanup — remove runner_*.json + runner_*.lock files
                # whose PID is dead. Previously done manually; now hourly-automated.
                try:
                    locks_dir = _REPO / "argus_flow" / "logs" / "_locks"
                    cleaned = 0
                    for json_file in locks_dir.glob("runner_*.json"):
                        try:
                            data = json.loads(json_file.read_text(encoding="utf-8"))
                            pid = int(data.get("pid", -1))
                            if pid > 0 and not _pid_alive(pid):
                                json_file.unlink()
                                lock_file = json_file.with_suffix(".lock")
                                if lock_file.exists():
                                    lock_file.unlink()
                                cleaned += 1
                        except Exception:
                            pass
                    if cleaned:
                        log.info("orphan-lock cleanup: removed %d stale runner locks", cleaned)
                except Exception as e:
                    log.warning("orphan-lock cleanup failed: %s", e)

                _last_hourly_epoch = now_epoch
                log.info("hourly checks completed (schema + reconcile + lock-cleanup)")

            # Once-per-UTC-day: rebuild operational_maturity report.
            # Fires on the first cycle after MATURITY_REFRESH_HOUR_UTC each day.
            global _last_maturity_day
            now_utc = datetime.now(timezone.utc)
            today = now_utc.strftime("%Y-%m-%d")
            if now_utc.hour >= MATURITY_REFRESH_HOUR_UTC and _last_maturity_day != today:
                if _MATURITY_SCRIPT.exists():
                    try:
                        res = subprocess.run(
                            [sys.executable, str(_MATURITY_SCRIPT)],
                            cwd=str(_REPO),
                            capture_output=True,
                            timeout=60,
                        )
                        if res.returncode == 0:
                            log.info("operational_maturity refreshed for %s", today)
                            _last_maturity_day = today
                        else:
                            log.warning(
                                "operational_maturity refresh returned rc=%d; will retry next cycle",
                                res.returncode,
                            )
                    except Exception as me:
                        log.warning("operational_maturity refresh failed: %s", me)
            consecutive_failures = 0
            elapsed = time.monotonic() - start
            log.info("cycle %d OK (%.2fs)", cycle, elapsed)
        except Exception as e:
            consecutive_failures += 1
            log.error(
                "cycle %d FAILED (streak=%d): %s",
                cycle, consecutive_failures, e,
            )
            traceback.print_exc(file=sys.stderr)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical(
                    "exceeded %d consecutive failures — exiting so watchdog respawns",
                    MAX_CONSECUTIVE_FAILURES,
                )
                return 2
        # Sleep until next cycle
        elapsed = time.monotonic() - start
        sleep_for = max(0.0, INTERVAL_S - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
