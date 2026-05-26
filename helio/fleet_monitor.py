"""helio/fleet_monitor.py -- Unified fleet observability + watchdog.

Single process that handles:
  1. Heartbeat staleness checks for all 5 systems (every 60s)
  2. Auto-restart crashed runners (Argus/Titan/Hermes/Apollo)
  3. Daily fleet snapshots (positions, PnL, risk)
  4. Discord alerts on staleness/crashes
  5. Trade quality metrics aggregation

Usage:
    python -m helio.fleet_monitor              # run forever
    python -m helio.fleet_monitor --once       # single check + exit
    python -m helio.fleet_monitor --snapshot   # take snapshot only
    python -m helio.fleet_monitor --legacy-execution-mode live
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from logging.handlers import RotatingFileHandler

_fleet_log_path = REPO / "argus_flow" / "logs" / "fleet_monitor.log"
_fleet_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] fleet | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.StreamHandler(),
        # Rotation: 5 MB × 3 backups. Caps worst-case disk use at ~20 MB and
        # prevents the kind of 50k-line log explosion we saw during the
        # 2026-04-17 hash-pin crash loop.
        RotatingFileHandler(_fleet_log_path, maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("fleet_monitor")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# Per-system config
SYSTEMS = {
    "argus": {
        "heartbeats": [
            REPO / "argus_flow" / "logs" / sym / "heartbeat.json"
            for sym in ["usdjpy", "gbpusd", "cadjpy"]
        ],
        "stale_threshold_s": 300,  # 5 min
        "process_match": "runner_unified",
        "restart_args": [
            "-m", "argus_flow.runner_unified", "--configs",
            "argus_flow/configs/usdjpy_mtf_paper_v1.json",
            "argus_flow/configs/gbpusd_range_paper_v1.json",
            "argus_flow/configs/cadjpy_mtf_paper_v1.json",
        ],
    },
    "titan": {
        # ARCHIVED 2026-05-20 (sunset doc). 0 ledger traffic; redundant role.
        "heartbeats": [REPO / "titan" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 4500,
        "process_match": "titan.runner",
        "restart_args": ["-m", "titan.runner", "--loop", "--interval-min", "60"],
        "supports_live_flag": True,
        "no_restart": True,
    },
    "hermes": {
        # ARCHIVED 2026-05-20. Earnings scanner role absorbed into forge_pead.
        "heartbeats": [REPO / "hermes" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 8400,
        "process_match": "hermes.runner",
        "restart_args": ["-m", "hermes.runner", "--loop", "--interval-min", "120", "--min-score", "80"],
        "supports_live_flag": True,
        "no_restart": True,
    },
    "apollo": {
        # ARCHIVED 2026-05-20. Universe data file (apollo/data/core_watchlist.json)
        # is now used by forge_pead directly; apollo runner not needed.
        "heartbeats": [REPO / "apollo" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 16200,
        "process_match": "apollo.runner",
        "restart_args": ["-m", "apollo.runner", "--loop", "--interval-min", "240", "--days", "14"],
        "supports_live_flag": True,
        "no_restart": True,
    },
    "forge_gdx_gld": {
        # SHADOW 2026-05-20 (sunset doc). Allocation 0; code retained; no_restart
        # so fleet_monitor doesn't auto-resurrect. Will be retired into a
        # generalized forge_coint_pairs strategy post-7/1 if pursued.
        "heartbeats": [REPO / "forge" / "logs" / "gdx_gld" / "heartbeat.json"],
        "stale_threshold_s": 5400,
        "process_match": "forge.gdx_gld_runner",
        "restart_args": ["-m", "forge.gdx_gld_runner", "--live", "--loop", "--interval-min", "60"],
        "no_restart": True,
    },
    "forge_atlas": {
        # ARCHIVED 2026-05-20. Architect: "keyword-rules sentiment, not regime
        # detection." Data files retained for any downstream readers.
        "heartbeats": [REPO / "forge" / "logs" / "atlas" / "heartbeat.json"],
        "stale_threshold_s": 600,
        "process_match": "forge.atlas.runner",
        "restart_args": ["-m", "forge.atlas.runner", "--loop", "--interval-sec", "120"],
        "no_restart": True,
    },
    "forge_themis": {
        # ARCHIVED 2026-05-20. Polymarket scanner; geoblocked + 0 ledger traffic.
        "heartbeats": [REPO / "forge" / "logs" / "themis" / "heartbeat.json"],
        "stale_threshold_s": 28800,
        "process_match": "forge.themis.runner",
        "restart_args": ["-m", "forge.themis.runner", "--loop", "--interval-min", "360"],
        "no_restart": True,
    },
    "forge_mamba": {
        # ARCHIVED 2026-05-20. YouTube-trader replica; 0 fills since 5/16 CBOT
        # routing fix. Redundant with cuebanks/tori (all same YM/MYM factor).
        "heartbeats": [REPO / "forge" / "logs" / "mamba" / "heartbeat.json"],
        "stale_threshold_s": 600,
        "process_match": "forge.mamba.runner",
        "restart_args": ["-m", "forge.mamba.runner", "--loop"],
        "no_restart": True,
    },
    "forge_tori": {
        # ARCHIVED 2026-05-20. Same factor as mamba/cuebanks; 0 fills.
        "heartbeats": [REPO / "forge" / "logs" / "tori" / "heartbeat.json"],
        "stale_threshold_s": 18000,
        "process_match": "forge.tori.runner",
        "restart_args": ["-m", "forge.tori.runner", "--loop"],
        "no_restart": True,
    },
    "forge_cuebanks": {
        # ARCHIVED 2026-05-20. Same factor as mamba/tori; 0 fills.
        "heartbeats": [REPO / "forge" / "logs" / "cuebanks" / "heartbeat.json"],
        "stale_threshold_s": 600,
        "process_match": "forge.cuebanks.runner",
        "restart_args": ["-m", "forge.cuebanks.runner", "--loop"],
        "no_restart": True,
    },
    "forge_vix_revert": {
        # ARCHIVED 2026-05-20. Short-vol scalper; same archetype as the
        # killed forge_vix_intraday. 0 fills. The vix-carry replacement
        # (forge_vix_carry) is in shadow research mode.
        "heartbeats": [REPO / "forge" / "logs" / "vix_revert" / "heartbeat.json"],
        "stale_threshold_s": 7200,
        "process_match": "forge.vix_revert_runner",
        "restart_args": ["-m", "forge.vix_revert_runner", "--loop"],
        "no_restart": True,
    },
    "forge_rebalance": {
        # ARCHIVED 2026-05-20 (the runner). The annual Russell-reconstitution
        # window is the only edge here; will be rebuilt as forge_russell_recon
        # event runner if pursued.
        "heartbeats": [REPO / "forge" / "logs" / "rebalance" / "heartbeat.json"],
        "stale_threshold_s": 90000,
        "process_match": "forge.rebalance_runner",
        "restart_args": ["-m", "forge.rebalance_runner", "--loop"],
        "no_restart": True,
    },
    "forge_wick_gbpusd": {
        # ARCHIVED 2026-05-20. 0 fills post-reset. FX wick-reversal on
        # GBPUSD competes with bank desks; weak thesis.
        "heartbeats": [REPO / "forge" / "logs" / "wick_gbpusd" / "heartbeat.json"],
        "stale_threshold_s": 7200,
        "process_match": "forge.wick_gbpusd.runner",
        "restart_args": ["-m", "forge.wick_gbpusd.runner", "--loop"],
        "_status_note": "Archived 2026-05-20 per sunset doc.",
        "no_restart": True,
    },
    "forge_gld_pm_long": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.gld_pm_long.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "gld_pm_long" / "heartbeat.json"),
        "artifact_max_age_s": 7200,  # 2h — runs at top of hours 18/19/20 UTC weekdays
        "no_restart": True,  # scheduled externally
    },
    # ─── 2026-05-26: v26 active roster (post-reset) ────────────────────
    # All wake every 5 min and write heartbeat each cycle; max_age 600s
    # flags STALE if a strategy has missed 2 heartbeats. No_restart=True
    # because ArgusV26FleetStartup scheduled task is the canonical bulk
    # restart authority (avoids fleet_monitor race-restarting individuals).
    # process_match uses trailing space for legacy15/style to disambiguate
    # from legacy15_regime/style_top3 (substring match).
    "forge_xs_momentum": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant baseline",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_sectors": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant sectors",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_sectors" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_style": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant style ",  # trailing space — distinguish from style_top3
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_style" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_legacy15": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant legacy15 ",  # trailing space — distinguish from legacy15_regime
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_legacy15" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_style_top3": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant style_top3",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_style_top3" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_legacy15_regime": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant legacy15_regime",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_legacy15_regime" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_xs_momentum_global47": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "--variant global47",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_global47" / "heartbeat.json"),
        "artifact_max_age_s": 600,
        "no_restart": True,
    },
    "forge_tail_hedge": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.tail_hedge.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "tail_hedge" / "heartbeat.json"),
        "artifact_max_age_s": 86400,  # 24h — regime-gated, only fires when SPY<200dma
        "no_restart": True,
    },
    "forge_tom_spy": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.tom_spy.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "tom_spy" / "heartbeat.json"),
        "artifact_max_age_s": 86400,  # 24h — daily wake at 19:40 UTC
        "no_restart": True,
    },
    "forge_nov_spy": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.nov_spy.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "nov_spy" / "heartbeat.json"),
        "artifact_max_age_s": 86400,  # 24h — daily wake at 19:40 UTC
        "no_restart": True,
    },
    "forge_xs_momentum_consensus": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.xs_momentum_consensus.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "xs_momentum_consensus" / "heartbeat.json"),
        "artifact_max_age_s": 86400,  # 24h — shadow runner, evaluates monthly + heartbeats hourly
        "no_restart": True,
    },
    "forge_uso_pm_long": {
        # 2026-05-26: hourly intraday on USO. Heartbeats at top of each
        # signal hour + every 5 min during eval loop. 2h max age covers
        # the 3-hour 18-21 UTC PM window with reasonable slack.
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.uso_pm_long.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "uso_pm_long" / "heartbeat.json"),
        "artifact_max_age_s": 7200,
        "no_restart": True,
    },
    "forge_ewz_breakout": {
        # 2026-05-26: daily 21-day breakout on EWZ. One evaluation per day
        # ~20:30 UTC, heartbeats every 5 min during loop. 26h max age
        # covers weekday-to-weekday gap (Fri eval to Mon eval = 72h with
        # weekend skip; use the 26h threshold and accept weekend staleness).
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.ewz_breakout.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "ewz_breakout" / "heartbeat.json"),
        "artifact_max_age_s": 93600,  # 26h
        "no_restart": True,
    },
    "forge_ief_jul_hold": {
        # 2026-05-26: one trade per year (enter 1st weekday of July, exit
        # last weekday of July). Daily wake ~19:45 UTC writes a noop
        # heartbeat. 26h max age covers normal weekday gap.
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.ief_jul_hold.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "ief_jul_hold" / "heartbeat.json"),
        "artifact_max_age_s": 93600,  # 26h
        "no_restart": True,
    },
    "forge_gld_jan_hold": {
        # 2026-05-26: one trade per year (enter 1st weekday of January,
        # exit last weekday of January). Daily wake ~19:50 UTC.
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.gld_jan_hold.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "gld_jan_hold" / "heartbeat.json"),
        "artifact_max_age_s": 93600,  # 26h
        "no_restart": True,
    },
    "forge_uso_jun_hold": {
        # 2026-05-26 v30: one trade per year (June, oil seasonal).
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.uso_jun_hold.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "uso_jun_hold" / "heartbeat.json"),
        "artifact_max_age_s": 93600,  # 26h
        "no_restart": True,
    },
    "forge_hyg_apr_hold": {
        # 2026-05-26 v30: one trade per year (April, high-yield risk-on).
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.hyg_apr_hold.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "hyg_apr_hold" / "heartbeat.json"),
        "artifact_max_age_s": 93600,  # 26h
        "no_restart": True,
    },
    "forge_nq_overnight": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.nq_overnight.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "nq_overnight" / "heartbeat.json"),
        "artifact_max_age_s": 7200,  # 2h — runs hourly during overnight session
        "no_restart": True,
    },
    "forge_jpy_pm_short": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "forge.jpy_pm_short.runner",
        "artifact_glob": str(REPO / "forge" / "logs" / "jpy_pm_short" / "heartbeat.json"),
        "artifact_max_age_s": 7200,  # 2h
        "no_restart": True,
    },
    "forge_multi_orb": {
        # KILLED 2026-05-07. Allocation 0.0 + start_all_runners.ps1 commented.
        # no_restart belt-and-suspenders so a future fleet_monitor without
        # --no-restart can't resurrect a thesis-exhausted strategy.
        "heartbeats": [REPO / "forge" / "logs" / "multi_orb" / "heartbeat.json"],
        "stale_threshold_s": 900,  # 15 min (evaluates every 5min during NY session)
        "process_match": "forge.multi_orb.runner",
        "restart_args": ["-m", "forge.multi_orb.runner", "--loop"],
        "no_restart": True,
    },
    "forge_vix_intraday": {
        # KILLED 2026-05-12. n=61, drift -175%, IR=-4.36 vs SPY.
        # Open UVXY position exits via existing TWS OCO bracket.
        "heartbeats": [REPO / "forge" / "logs" / "vix_intraday" / "heartbeat.json"],
        "stale_threshold_s": 1800,  # 30 min (15m bars)
        "process_match": "forge.vix_intraday.runner",
        "restart_args": ["-m", "forge.vix_intraday.runner", "--loop"],
        "no_restart": True,
    },
    "forge_spy_mean_rev": {
        # KILLED 2026-04-30, finalized 2026-05-12. Negative expectancy is
        # structural; future SPY mean-rev work should be v2 from scratch.
        "heartbeats": [REPO / "forge" / "logs" / "spy_mean_rev" / "heartbeat.json"],
        "stale_threshold_s": 900,  # 15 min (5m bars during 14-20 UTC)
        "process_match": "forge.spy_mean_rev.runner",
        "restart_args": ["-m", "forge.spy_mean_rev.runner", "--loop"],
        "no_restart": True,
    },
    "forge_nq_london_close": {
        # KILLED 2026-05-13. n=3 fills/20d, 5/5 phantom 417 contracts ($117M
        # notional from sizing-formula bug), redundant venue (mamba/cuebanks/
        # tori/nq_overnight already cover MNQ/YM). No recovery path to real-
        # money. no_restart=True so fleet_monitor cannot resurrect it.
        "heartbeats": [REPO / "forge" / "logs" / "nq_london_close" / "heartbeat.json"],
        "stale_threshold_s": 5400,  # 90 min (hourly cycle during London close session)
        "process_match": "forge.nq_london_close.runner",
        "restart_args": ["-m", "forge.nq_london_close.runner", "--loop"],
        "no_restart": True,
    },
    "forge_aud_asian_breakout": {
        # ARCHIVED 2026-05-20. 2 fills, both negative; capacity stress fails at 1×.
        "heartbeats": [REPO / "forge" / "logs" / "aud_asian_breakout" / "heartbeat.json"],
        "stale_threshold_s": 5400,
        "process_match": "forge.aud_asian_breakout.runner",
        "restart_args": ["-m", "forge.aud_asian_breakout.runner", "--loop"],
        "no_restart": True,
    },
    "forge_fomc_drift": {
        # ARCHIVED 2026-05-20. 0 directional fires; Architect noted the real
        # edge here is IV-crush (options), not directional drift. Future
        # redesign post-7/1 if options infra is built.
        "heartbeats": [REPO / "forge" / "logs" / "fomc_drift" / "heartbeat.json"],
        "stale_threshold_s": 7200,
        "process_match": "forge.fomc_drift.runner",
        "restart_args": ["-m", "forge.fomc_drift.runner", "--loop"],
        "no_restart": True,
    },
    "forge_tom_international": {
        # ARCHIVED 2026-05-20. Turn-of-month event runner; 0 fires.
        "heartbeats": [REPO / "forge" / "logs" / "tom_international" / "heartbeat.json"],
        "stale_threshold_s": 7200,
        "process_match": "forge.tom_international.runner",
        "restart_args": ["-m", "forge.tom_international.runner", "--loop"],
        "no_restart": True,
    },
    "dashboard": {
        "heartbeats": [],  # no heartbeat, check via process only
        "stale_threshold_s": 0,
        "process_match": "dashboard.py",
        "restart_args": ["ops/dashboard.py", "--port", "8080"],
    },
    # Scheduled-task systems: monitored by daily artifact freshness, not process aliveness.
    # Why: ares (sector rotation, monthly eval) and oracle (Polymarket scan) run as
    # scheduled jobs that produce a dated file each run; the process is short-lived.
    "ares": {
        "heartbeats": [],
        "stale_threshold_s": 0,
        "process_match": "ares.runner",
        "artifact_glob": str(REPO / "ares" / "logs" / "signal_*.json"),
        # 2026-04-23: ares is MONTHLY sector rotation (evaluates at month-end).
        # Previous 26h threshold dashboard-DOWN-flagged it every single day.
        # 35-day max so it only goes red if a month-end rotation genuinely fails.
        "artifact_max_age_s": 3024000,  # 35 days (monthly + buffer)
        "no_restart": True,
        "_status_note": "monthly sector rotation; DOWN between month-ends is by design",
    },
    # oracle (Polymarket) paused 2026-04-19 — geoblocked for US users, no
    # execution path. Re-add this entry + uncomment the block in
    # ops/run_cohort_report.ps1 if/when migrating to Kalshi.
}

PYTHON = r"C:\Argus\.venv\Scripts\python.exe"
SNAPSHOT_DIR = REPO / "argus_flow" / "logs" / "fleet_snapshots"
ALERT_COOLDOWN_S = 1800  # don't alert same issue more than once per 30 min
_last_alert: dict[str, float] = {}  # alert_key -> timestamp
_restart_history: dict[str, list[float]] = {}  # system -> recent restart epochs
_last_webhook_ping: float = 0.0  # last successful heartbeat ping to webhook
CRASH_LOOP_WINDOW_S = 600   # 10 min window
CRASH_LOOP_MAX = 3          # >3 restarts in window = loop
WEBHOOK_PING_INTERVAL_S = 4 * 3600  # send alive ping every 4h
PAUSE_ENTRIES_FILE = REPO / "PAUSE_ENTRIES"
PAUSE_ENTRIES_ALERT_AGE_S = 600  # alert if file older than 10 min
HASHES_FILE = REPO / "argus_flow" / "configs" / "hashes.json"
DISCORD_FAILURES_LOG = REPO / "argus_flow" / "logs" / "discord_failures.jsonl"
CRASH_LOOP_ALERTS_LOG = REPO / "argus_flow" / "logs" / "crash_loop_alerts.json"


# ── Heartbeat checks ─────────────────────────────────────────

def get_heartbeat_age(path: Path) -> float | None:
    """Return age in seconds, or None if file doesn't exist."""
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _argus_killed_symbols() -> set[str]:
    """Symbols whose Argus deployment stage is 'killed' — skip their heartbeats.

    Why: killed configs aren't running in runner_unified, so their per-pair
    heartbeat file goes stale forever and pollutes fleet_status.json.
    """
    registry = _load_json(REPO / "argus_flow" / "logs" / "deployment_registry.json")
    if not registry:
        return set()
    killed: set[str] = set()
    for r in registry.get("runners", []):
        if str(r.get("current_stage", "")).lower() == "killed":
            sym = str(r.get("symbol", "")).lower()
            if sym:
                killed.add(sym)
    return killed


def _read_heartbeat_runtime(hb_path: Path) -> dict:
    """Extract runtime truth fields from a heartbeat file (if JSON)."""
    data = _load_json(hb_path) or {}
    keys = ("entries_blocked", "entry_block_reason", "broker_connected",
            "consecutive_errors", "position", "reconciled_at", "strategy")
    return {k: data.get(k) for k in keys if k in data}


def _scan_fatals(log_path: Path, tail_lines: int = 200) -> tuple[int, str | None]:
    """Count FATAL lines in the last N lines of a log. Returns (count, most_recent_line)."""
    if not log_path.exists():
        return 0, None
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(64 * 1024, size)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-tail_lines:]
        fatal_lines = [ln for ln in lines if "FATAL" in ln or "[ERROR]" in ln.upper() and "fatal" in ln.lower()]
        return len(fatal_lines), (fatal_lines[-1] if fatal_lines else None)
    except Exception:
        return 0, None


def check_system_health(name: str, cfg: dict) -> dict:
    """Check if a system is healthy. Returns status dict with runtime-truth fields."""
    result = {"name": name, "status": "UNKNOWN", "stale_count": 0, "details": []}
    process_alive = is_process_running(cfg["process_match"])
    result["process_alive"] = process_alive

    skip_symbols = _argus_killed_symbols() if name == "argus" else set()

    # Scheduled-task path: check artifact freshness instead of process aliveness.
    artifact_glob = cfg.get("artifact_glob")
    if artifact_glob:
        import glob as _glob
        result["health_basis"] = "artifact_freshness"
        result["scheduled_one_shot"] = True
        matches = _glob.glob(artifact_glob)
        if not matches:
            result["status"] = "DOWN"
            result["details"].append(f"no artifact matches {Path(artifact_glob).name}")
            result["max_age_s"] = -1
            return result
        newest = max((Path(m).stat().st_mtime for m in matches), default=0)
        age = time.time() - newest
        result["max_age_s"] = int(age)
        if age > cfg["artifact_max_age_s"]:
            result["status"] = "STALE"
            result["details"].append(f"newest artifact {int(age)}s old (>{cfg['artifact_max_age_s']}s)")
        else:
            result["status"] = "OK"
        return result

    result["health_basis"] = "process_and_heartbeat"

    runtime_by_symbol: dict[str, dict] = {}
    any_entries_blocked = False
    any_broker_disconnected = False

    if cfg["heartbeats"]:
        ages = []
        for hb in cfg["heartbeats"]:
            if hb.parent.name.lower() in skip_symbols:
                continue
            age = get_heartbeat_age(hb)
            if age is None:
                result["stale_count"] += 1
                result["details"].append(f"{hb.parent.name}: NO_HEARTBEAT")
            elif age > cfg["stale_threshold_s"]:
                result["stale_count"] += 1
                ages.append(age)
                result["details"].append(f"{hb.parent.name}: stale {int(age)}s")
            else:
                ages.append(age)
                rt = _read_heartbeat_runtime(hb)
                if rt:
                    runtime_by_symbol[hb.parent.name] = rt
                    if rt.get("entries_blocked"):
                        any_entries_blocked = True
                    if rt.get("broker_connected") is False:
                        any_broker_disconnected = True
        if ages:
            result["max_age_s"] = int(max(ages))

    if runtime_by_symbol:
        result["runtime"] = runtime_by_symbol
        if any_entries_blocked:
            reasons = sorted({r.get("entry_block_reason") or "?" for r in runtime_by_symbol.values() if r.get("entries_blocked")})
            result["details"].append(f"entries_blocked: {','.join(reasons)}")
        if any_broker_disconnected:
            result["details"].append("broker_disconnected")

    # Argus-specific: scan runner_unified.log for recent FATALs
    if name == "argus":
        runner_log = REPO / "argus_flow" / "logs" / "runner_unified.log"
        fatal_count, last_fatal = _scan_fatals(runner_log)
        if fatal_count > 0:
            result["recent_fatal_count"] = fatal_count
            if last_fatal:
                result["last_fatal"] = last_fatal[-300:]
            result["details"].append(f"recent FATALs in runner_unified.log: {fatal_count}")

    if not process_alive:
        result["status"] = "DOWN"
    elif result["stale_count"] > 0:
        result["status"] = "STALE"
    elif any_entries_blocked or any_broker_disconnected:
        result["status"] = "BLOCKED"
    else:
        result["status"] = "OK"

    return result


def is_process_running(pattern: str) -> bool:
    """Check if a python process matches a substring pattern."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=10,
        )
        return pattern in result.stdout
    except Exception:
        return False


# ── Restart handler ─────────────────────────────────────────

def build_restart_args(cfg: dict, legacy_execution_mode: str = "paper") -> list[str]:
    args = list(cfg.get("restart_args", []))
    if cfg.get("supports_live_flag") and legacy_execution_mode == "live":
        args.append("--live")
    return args


def _hash_preflight(restart_args: list[str]) -> list[tuple[str, str, str]]:
    """Return mismatches [(filename, expected, actual)] between the configs in
    restart_args and the pinned registry. Empty list = all good or no configs.

    Why: prevents fleet_monitor from spinning a crash loop when an operator edits
    a config without re-pinning hashes.json — see 2026-04-17 incident.
    """
    if not HASHES_FILE.exists():
        return []
    try:
        pinned = json.loads(HASHES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    import hashlib
    mismatches: list[tuple[str, str, str]] = []
    for arg in restart_args:
        if not arg.endswith(".json"):
            continue
        cfg_path = REPO / arg if not Path(arg).is_absolute() else Path(arg)
        if not cfg_path.exists():
            continue
        expected = pinned.get(cfg_path.name)
        if expected is None:
            continue
        raw = cfg_path.read_text(encoding="utf-8")
        actual = hashlib.sha256(raw.encode()).hexdigest()[:16]
        if expected != actual:
            mismatches.append((cfg_path.name, expected, actual))
    return mismatches


def _register_restart(name: str) -> bool:
    """Record a restart attempt. Return True if within crash-loop budget,
    False if crash loop detected (caller should NOT restart).
    """
    now = time.time()
    hist = _restart_history.setdefault(name, [])
    # Drop entries outside the window
    cutoff = now - CRASH_LOOP_WINDOW_S
    hist[:] = [t for t in hist if t >= cutoff]
    if len(hist) >= CRASH_LOOP_MAX:
        return False
    hist.append(now)
    return True


def _emit_crash_loop_alert(name: str, count: int, reason: str) -> None:
    """Persist a crash-loop alert and fire a distinct-keyed Discord message."""
    CRASH_LOOP_ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "system": name,
        "restart_count_in_window": count,
        "window_s": CRASH_LOOP_WINDOW_S,
        "reason": reason,
    }
    try:
        existing = _load_json(CRASH_LOOP_ALERTS_LOG) or {"alerts": []}
        existing.setdefault("alerts", []).append(record)
        CRASH_LOOP_ALERTS_LOG.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        log.error(f"Could not write crash-loop alert log: {exc}")
    send_discord(
        f"**CRASH LOOP: {name.upper()}** {count} restarts in {CRASH_LOOP_WINDOW_S // 60} min. "
        f"Restarts suspended. Reason: {reason}",
        system=f"{name}_crashloop",
    )


def restart_system(name: str, cfg: dict, legacy_execution_mode: str = "paper") -> bool:
    """Auto-restart a crashed/stale system, with hash-pin preflight + crash-loop guard."""
    restart_args = build_restart_args(cfg, legacy_execution_mode=legacy_execution_mode)

    mismatches = _hash_preflight(restart_args)
    if mismatches:
        diff_summary = "; ".join(f"{fn}: pinned={exp} actual={act}" for fn, exp, act in mismatches)
        log.error(f"HASH_PREFLIGHT: {name} restart SKIPPED — config hash mismatch. {diff_summary}")
        send_discord(
            f"**HASH MISMATCH: {name.upper()}** restart SKIPPED. Update `hashes.json` then allow restart.\n{diff_summary}",
            system=f"{name}_hash_mismatch",
        )
        return False

    if not _register_restart(name):
        _emit_crash_loop_alert(name, len(_restart_history.get(name, [])), "restart budget exceeded")
        return False

    mode_label = "LIVE" if cfg.get("supports_live_flag") and legacy_execution_mode == "live" else "DEFAULT"
    log.warning(f"Restarting {name} ({mode_label} mode, args={restart_args})...")
    try:
        subprocess.Popen(
            [PYTHON] + restart_args,
            cwd=str(REPO),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
        )
        send_discord(f"**FLEET MONITOR: Restarted {name.upper()}** (was DOWN/STALE)", system=f"{name}_restart")
        return True
    except Exception as e:
        log.error(f"Restart failed for {name}: {e}")
        return False


def _log_discord_failure(reason: str, status: int | None, msg: str) -> None:
    DISCORD_FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "status": status,
        "msg_prefix": msg[:120],
    }
    try:
        with open(DISCORD_FAILURES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def send_discord(msg: str, system: str = "general", bypass_cooldown: bool = False) -> bool:
    """Send Discord alert with per-key cooldown. Returns True on HTTP 2xx.

    Why per-key cooldown: prior implementation used a single shared "general"
    bucket, so first restart muted all other fleet alerts for 30 min.
    Distinct keys per system/event let alerts coexist.

    Cooldown marking happens ONLY on confirmed 2xx so a transient POST failure
    cannot mute the next alert attempt for 30 minutes.
    """
    now = time.time()
    if not bypass_cooldown:
        last = _last_alert.get(system, 0)
        if now - last < ALERT_COOLDOWN_S:
            return False

    if not WEBHOOK_URL:
        _log_discord_failure("no_webhook_url", None, msg)
        return False
    try:
        import requests
        r = requests.post(WEBHOOK_URL, json={"content": msg[:2000]}, timeout=10)
        if 200 <= r.status_code < 300:
            if not bypass_cooldown:
                _last_alert[system] = now
            return True
        _log_discord_failure(f"http_{r.status_code}", r.status_code, msg)
        return False
    except Exception as e:
        _log_discord_failure(f"exc_{type(e).__name__}", None, msg)
        return False


def maybe_webhook_ping() -> None:
    """Emit a 4-hourly heartbeat so silence on the webhook is visible."""
    global _last_webhook_ping
    now = time.time()
    if now - _last_webhook_ping < WEBHOOK_PING_INTERVAL_S:
        return
    ok = send_discord(
        f"fleet_monitor alive @ {datetime.now(timezone.utc).isoformat()}",
        system="webhook_heartbeat",
        bypass_cooldown=True,
    )
    if ok:
        _last_webhook_ping = now


# ── Snapshots ──────────────────────────────────────────────

def take_snapshot() -> dict:
    """Capture current state of every system to a JSON snapshot."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "systems": {},
    }

    # Argus (AUDJPY killed 2026-04-14 + archived 2026-04-17; excluded here so
    # aggregate snapshots don't keep resurrecting dead-strategy data).
    argus_data = {"trades": 0, "win_rate": 0, "total_pnl": 0, "pairs": {}}
    for sym in ["usdjpy", "gbpusd", "cadjpy"]:
        trades_path = REPO / "argus_flow" / "logs" / sym / "trades.csv"
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    rows = [r for r in csv.DictReader(f) if r.get("experiment_valid", "").lower() == "true"]
                pnls = [float(r.get("pnl_pips", 0)) for r in rows]
                wins = sum(1 for p in pnls if p > 0)
                argus_data["trades"] += len(rows)
                argus_data["pairs"][sym] = {
                    "trades": len(rows),
                    "wins": wins,
                    "wr": round(wins / len(rows) * 100, 1) if rows else 0,
                    "pnl": round(sum(pnls), 2),
                }
                argus_data["total_pnl"] += sum(pnls)
            except Exception:
                pass
    if argus_data["trades"] > 0:
        total_wins = sum(p["wins"] for p in argus_data["pairs"].values())
        argus_data["win_rate"] = round(total_wins / argus_data["trades"] * 100, 1)
        argus_data["total_pnl"] = round(argus_data["total_pnl"], 2)
    snapshot["systems"]["argus"] = argus_data

    # Titan / Hermes / Apollo / Ares — read from positions + trades
    for sys_name in ["titan", "hermes", "apollo", "ares"]:
        sys_data = {"positions": 0, "trades": 0, "pnl": 0}

        pos_path = REPO / sys_name / "logs" / "positions.json"
        if pos_path.exists():
            try:
                pos = json.loads(pos_path.read_text())
                if sys_name == "apollo":
                    sys_data["positions"] = len(pos.get("positions", {}))
                elif sys_name == "ares":
                    sys_data["positions"] = len(pos.get("holdings", {}))
                else:
                    # titan/hermes use top-level dict (titan has special keys)
                    skip_keys = {"last_rebalance", "last_signal", "holdings"}
                    sys_data["positions"] = sum(
                        1 for k in pos.keys() if k not in skip_keys
                    )
            except Exception:
                pass

        trades_path = REPO / sys_name / "logs" / "trades.csv"
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    rows = list(csv.DictReader(f))
                sys_data["trades"] = len(rows)
                pnls = [float(r.get("pnl_pct", 0)) for r in rows]
                sys_data["pnl"] = round(sum(pnls), 2)
                if rows:
                    wins = sum(1 for p in pnls if p > 0)
                    sys_data["win_rate"] = round(wins / len(rows) * 100, 1)
            except Exception:
                pass

        snapshot["systems"][sys_name] = sys_data

    # Save snapshot
    fname = SNAPSHOT_DIR / f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    fname.write_text(json.dumps(snapshot, indent=2))
    log.info(f"Snapshot saved: {fname.name}")
    return snapshot


# ── Trade quality metrics ──────────────────────────────────

def collect_trade_metrics() -> dict:
    """Aggregate trade quality across all systems."""
    metrics = {}

    for sys_name in ["titan", "hermes", "apollo", "ares"]:
        orders_path = REPO / sys_name / "logs" / "orders.csv"
        if not orders_path.exists():
            metrics[sys_name] = {"orders": 0}
            continue
        try:
            with open(orders_path) as f:
                rows = list(csv.DictReader(f))
            metrics[sys_name] = {
                "orders": len(rows),
                "entries": sum(1 for r in rows if r.get("action") == "ENTRY"),
                "closes": sum(1 for r in rows if r.get("action") == "CLOSE"),
            }
        except Exception:
            metrics[sys_name] = {"orders": 0}

    return metrics


# ── Main monitor loop ───────────────────────────────────────

def _check_control_files() -> dict:
    """Surface PAUSE_ENTRIES / RESET_DRAWDOWN / KILL_SWITCH state in fleet_status."""
    out: dict = {}
    for name, path in [
        ("PAUSE_ENTRIES", PAUSE_ENTRIES_FILE),
        ("RESET_DRAWDOWN", REPO / "RESET_DRAWDOWN"),
        ("KILL_SWITCH", REPO / "KILL_SWITCH"),
    ]:
        if path.exists():
            age = time.time() - path.stat().st_mtime
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                content = ""
            out[name] = {"present": True, "age_s": int(age), "content": content[:200]}
        else:
            out[name] = {"present": False}
    return out


def _argus_all_brokers_healthy() -> bool:
    """Return True if every ACTIVE Argus pair (from SYSTEMS['argus'] config)
    has a fresh heartbeat with broker_connected=true and consecutive_errors=0.

    Uses the fleet_monitor's own SYSTEMS["argus"]["heartbeats"] list, which
    already reflects the 3 live pairs (USDJPY/GBPUSD/CADJPY post-AUDJPY kill).
    This prevents stale heartbeats from killed/archived strategies from
    blocking the auto-clear.
    """
    argus_cfg = SYSTEMS.get("argus", {})
    heartbeats = argus_cfg.get("heartbeats", [])
    if not heartbeats:
        return False
    any_pair = False
    for hb_path in heartbeats:
        if not hb_path.exists():
            return False
        age = time.time() - hb_path.stat().st_mtime
        if age > 300:
            return False
        data = _load_json(hb_path) or {}
        if data.get("broker_connected") is not True:
            return False
        if int(data.get("consecutive_errors") or 0) > 0:
            return False
        any_pair = True
    return any_pair


def _maybe_auto_clear_pause_entries(control_files_state: dict) -> str | None:
    """If PAUSE_ENTRIES was created by watchdog gateway-supervision AND every
    Argus pair now reports broker_connected=true, remove the file. Returns a
    reason string on success for logging/alerting.

    Conservative gates: requires content to start with "gateway_supervision"
    so we never auto-clear an operator-created pause. Also requires file
    age > 60s to avoid races with the watchdog itself.
    """
    pe = control_files_state.get("PAUSE_ENTRIES") or {}
    if not pe.get("present"):
        return None
    if pe.get("age_s", 0) < 60:
        return None
    content = (pe.get("content") or "").strip().lower()
    if "gateway_supervision" not in content:
        return None  # operator or other source created it — don't touch
    if not _argus_all_brokers_healthy():
        return None
    try:
        PAUSE_ENTRIES_FILE.unlink()
    except OSError:
        return None
    return f"PAUSE_ENTRIES auto-cleared (age {pe.get('age_s')}s, all Argus brokers healthy)"


def _snapshot_broker_equity() -> None:
    """Append broker equity to a history JSONL for charting. One-liner per cycle.
    Enables a broker-equity-derived curve (blueprint §18.8 week 2 deliverable)
    that is independent of trade-CSV derivation.
    """
    ro_path = REPO / "argus_flow" / "logs" / "risk_oversight_report.json"
    if not ro_path.exists():
        return
    try:
        data = _load_json(ro_path) or {}
        equity = data.get("broker_truth", {}).get("account_equity_usd")
        if equity is None:
            return
        hist_path = REPO / "argus_flow" / "logs" / "broker_equity_history.jsonl"
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "broker_equity_usd": float(equity),
            "fleet_pnl_usd": float(data.get("broker_truth", {}).get("fleet_unrealized_pnl_usd") or 0),
            "fleet_open_risk_usd": float(data.get("broker_truth", {}).get("fleet_open_risk_usd") or 0),
        }
        # Rate limit: one sample per 5 min max (the fleet_monitor cycles every
        # 60s but we don't need that density on the equity curve).
        if hist_path.exists() and hist_path.stat().st_size > 0:
            try:
                last_mtime = hist_path.stat().st_mtime
                if time.time() - last_mtime < 300:
                    return
            except OSError:
                pass
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        log.warning(f"broker equity snapshot failed: {exc}")


def _check_risk_state() -> dict:
    """Read portfolio_risk_state + risk_oversight + portfolio_guard; flag
    disagreements across the three sources. All three write their own truth
    of "can we trade?" — when they disagree, a consumer picking the wrong
    one can decide wrongly in silence.
    """
    rs_path = REPO / "argus_flow" / "logs" / "_risk" / "portfolio_risk_state.json"
    ro_path = REPO / "argus_flow" / "logs" / "risk_oversight_report.json"
    pg_path = REPO / "helio" / "logs" / "portfolio_guard.json"
    rs = _load_json(rs_path) or {}
    ro = _load_json(ro_path) or {}
    pg = _load_json(pg_path) or {}
    out = {
        "portfolio_risk_state": {
            "drawdown_pause": rs.get("drawdown_pause"),
            "peak_pnl": rs.get("peak_pnl"),
            "current_pnl": rs.get("current_pnl"),
            "ts": rs.get("ts"),
        },
        "risk_oversight": {
            "level": ro.get("level"),
            "status": ro.get("status"),
            "ts": ro.get("timestamp"),
        },
        "portfolio_guard": {
            "allowed": pg.get("allowed"),
            "reason": pg.get("reason"),
            "warnings": pg.get("warnings", []),
        },
    }
    disagreements = []
    if rs.get("drawdown_pause") is True and str(ro.get("level", "")).upper() == "GREEN":
        disagreements.append("drawdown_pause=true while oversight=GREEN")
    # portfolio_guard.allowed=False with oversight GREEN — the guard is
    # blocking entries while oversight thinks everything is fine. Could be
    # directional-bias cap, correlation, or concentration; either way the
    # UI should flag it, not pick one silently.
    if pg.get("allowed") is False and str(ro.get("level", "")).upper() == "GREEN":
        disagreements.append(f"portfolio_guard.allowed=false ({pg.get('reason', '?')}) while oversight=GREEN")
    if disagreements:
        out["disagreement"] = " | ".join(disagreements)
    return out


def run_check_cycle(auto_restart: bool = True, legacy_execution_mode: str = "paper") -> dict:
    """One full health check cycle. Returns status dict."""
    overall = {"ts": datetime.now(timezone.utc).isoformat(), "systems": {}}

    for name, cfg in SYSTEMS.items():
        status = check_system_health(name, cfg)
        overall["systems"][name] = status

        if status["status"] in ("DOWN", "STALE", "BLOCKED"):
            log.warning(f"{name}: {status['status']} | {' | '.join(status['details'][:3])}")
            if auto_restart and status["status"] == "DOWN" and not cfg.get("no_restart"):
                restart_system(name, cfg, legacy_execution_mode=legacy_execution_mode)
        else:
            log.info(f"{name}: OK")

    # Refresh portfolio_guard so its JSON reflects current state, not the
    # frozen snapshot from the last check_new_entry call. Without this the
    # dashboard can show blocked="true" based on positions that closed hours
    # ago, leading operators to think entries are stuck when they aren't.
    try:
        from helio.portfolio_guard import check_current as _pg_refresh
        _pg_refresh()
    except Exception as exc:
        log.warning(f"portfolio_guard refresh failed: {exc}")

    # Surface control files + risk state disagreement in fleet_status
    control = _check_control_files()
    overall["control_files"] = control
    overall["risk_state"] = _check_risk_state()

    # Auto-clear PAUSE_ENTRIES if watchdog-created and gateway is now healthy.
    # Blueprint §18.8 item: IBC/gateway resilience. Eliminates the recurring
    # morning-login ritual where PAUSE_ENTRIES stays up after gateway recovers.
    cleared = _maybe_auto_clear_pause_entries(control)
    if cleared:
        log.info(cleared)
        send_discord(f"**AUTO-CLEARED PAUSE_ENTRIES** — {cleared}", system="pause_entries_autoclear")
        # Re-read control files so fleet_status.json reflects the change this cycle
        overall["control_files"] = _check_control_files()

    # Sample broker equity for the history curve
    _snapshot_broker_equity()

    # PAUSE_ENTRIES age alert (distinct key — won't be swallowed by restart cooldown)
    pe = control.get("PAUSE_ENTRIES", {})
    if pe.get("present") and pe.get("age_s", 0) > PAUSE_ENTRIES_ALERT_AGE_S:
        send_discord(
            f"**PAUSE_ENTRIES active {pe['age_s'] // 60} min** — reason: `{pe.get('content', '?')}`. Entries are blocked fleet-wide.",
            system="pause_entries_age",
        )

    rs = overall["risk_state"]
    if rs.get("disagreement"):
        send_discord(
            f"**RISK STATE DRIFT**: {rs['disagreement']}",
            system="risk_state_drift",
        )

    maybe_webhook_ping()
    return overall


def write_status(status: dict) -> None:
    """Write current status to a file the dashboard can read."""
    out = REPO / "argus_flow" / "logs" / "fleet_status.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(status, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Fleet Monitor")
    parser.add_argument("--once", action="store_true", help="Single check + exit")
    parser.add_argument("--snapshot", action="store_true", help="Take snapshot only")
    parser.add_argument("--no-restart", action="store_true", help="Don't auto-restart")
    parser.add_argument("--interval-s", type=int, default=60, help="Check interval seconds")
    parser.add_argument("--snapshot-interval-h", type=int, default=24, help="Snapshot interval hours")
    parser.add_argument(
        "--legacy-execution-mode",
        choices=["paper", "live"],
        default="paper",
        help="Restart mode for legacy stock systems (paper-safe by default)",
    )
    args = parser.parse_args()

    if args.snapshot:
        snap = take_snapshot()
        print(json.dumps(snap, indent=2))
        return

    if args.once:
        status = run_check_cycle(
            auto_restart=not args.no_restart,
            legacy_execution_mode=args.legacy_execution_mode,
        )
        write_status(status)
        print(json.dumps(status, indent=2))
        return

    log.info("Fleet monitor starting...")
    log.info(
        f"Interval: {args.interval_s}s | Snapshot: every {args.snapshot_interval_h}h | "
        f"Auto-restart: {not args.no_restart} | Legacy mode: {args.legacy_execution_mode}"
    )

    last_snapshot_time = 0
    snapshot_interval_s = args.snapshot_interval_h * 3600

    try:
        while True:
            try:
                status = run_check_cycle(
                    auto_restart=not args.no_restart,
                    legacy_execution_mode=args.legacy_execution_mode,
                )
                write_status(status)

                # Daily snapshot
                if time.time() - last_snapshot_time > snapshot_interval_s:
                    take_snapshot()
                    last_snapshot_time = time.time()

            except Exception as e:
                log.error(f"Monitor cycle error: {e}")

            time.sleep(args.interval_s)

    except KeyboardInterrupt:
        log.info("Fleet monitor stopped")


if __name__ == "__main__":
    main()
