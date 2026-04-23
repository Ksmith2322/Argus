"""ops/silent_block_check.py — Silent-block detector.

Catches the "alive-but-mute" failure mode: a runner process is alive and
writing heartbeats, but its signal evaluation loop stopped producing
signals (data feed dead, session filter stuck, gate misconfigured, etc.).
Standard process-liveness watchdogs miss this entirely.

For each strategy in a defined signal-emission window, count signals
observed in the last N minutes. If zero signals AND we're >= halfway
into the expected window, flag as SILENT_BLOCK.

Output:
    argus_flow/logs/silent_block_alerts.json — per-strategy verdict
    stderr — summary line, exit code 1 if any SILENT_BLOCK flagged

Run:
    python -m ops.silent_block_check            # writes JSON + logs
    python -m ops.silent_block_check --verbose  # extra detail
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"
ALERT_STATE_PATH = LOGS_DIR / "silent_block_alert_state.json"
ALERT_COOLDOWN_MIN = 60  # don't re-alert same strategy within this window

# Load DISCORD_WEBHOOK_URL — same pattern as other argus_flow ops.
# .env file is loaded ad-hoc if present.
def _load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _post_discord(title: str, msg: str, color: int = 16753920) -> bool:
    """Best-effort Discord post. Returns True on success, False if no webhook
    configured or any error. Never raises."""
    _load_env()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    payload = {
        "embeds": [{
            "title": title,
            "description": msg[:1900],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _load_alert_state() -> dict:
    if not ALERT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alert_state(state: dict) -> None:
    try:
        ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] silent_block | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("silent_block_check")


@dataclass
class StrategyWatch:
    id: str
    signals_csv: str  # relative to repo root
    ts_col: str  # column name in CSV holding signal timestamp
    session_start_utc: int  # hour of day (0-23)
    session_end_utc: int    # hour of day (0-23) — exclusive
    min_signals_window_min: int = 60  # check this rolling window
    min_signals_expected: int = 1     # fewer than this during active session = silent
    setup_selective: bool = False     # if True, uses a longer rolling window before flagging


# Per-strategy calibration: session windows + how selective the strategy is.
# "setup_selective" strategies (mamba, cuebanks, gld_pm_long, nq_overnight, jpy_pm_short)
# only fire on specific setups — they can legitimately be quiet for hours during
# their session. For those, use a longer rolling window (240min = 4hr) so we
# don't false-positive every cycle.
#
# Fast-eval strategies (argus, spy_mean_rev, multi_orb, vix_intraday) evaluate
# every 5min bar during their sessions — if they're mute for 60min, something
# is genuinely wrong (data feed dead, runner crashed mid-loop, etc.).
STRATEGIES: list[StrategyWatch] = [
    # Fast-eval: 5m bars, signal every bar if eval loop is healthy
    StrategyWatch("argus_usdjpy",        "argus_flow/logs/usdjpy/signals.csv",        "ts",  7, 21, 60, 1, setup_selective=False),
    StrategyWatch("argus_gbpusd",        "argus_flow/logs/gbpusd/signals.csv",        "ts",  7, 21, 60, 1, setup_selective=False),
    StrategyWatch("argus_cadjpy",        "argus_flow/logs/cadjpy/signals.csv",        "ts",  7, 21, 60, 1, setup_selective=False),
    StrategyWatch("forge_spy_mean_rev",  "forge/logs/spy_mean_rev/signals.csv",       "ts", 14, 20, 60, 1, setup_selective=False),
    StrategyWatch("forge_multi_orb",     "forge/logs/multi_orb/signals.csv",          "ts", 14, 20, 60, 1, setup_selective=False),
    StrategyWatch("forge_vix_intraday",  "forge/logs/vix_intraday/signals.csv",       "ts", 14, 20, 60, 1, setup_selective=False),

    # Setup-selective: naturally sparse; 4hr window + still 1-signal floor for "totally mute"
    StrategyWatch("forge_gld_pm_long",   "forge/logs/gld_pm_long/signals.csv",        "ts", 18, 21, 240, 1, setup_selective=True),
    StrategyWatch("forge_jpy_pm_short",  "forge/logs/jpy_pm_short/signals.csv",       "ts", 19, 20, 240, 1, setup_selective=True),
    StrategyWatch("forge_nq_overnight",  "forge/logs/nq_overnight/signals.csv",       "ts", 20, 24, 240, 1, setup_selective=True),
    StrategyWatch("forge_nq_london_close",    "forge/logs/nq_london_close/signals.csv",    "ts", 15, 17, 120, 1, setup_selective=True),
    StrategyWatch("forge_aud_asian_breakout", "forge/logs/aud_asian_breakout/signals.csv", "ts",  0,  3, 120, 1, setup_selective=True),
    # Mamba uses 'timestamp' col (not 'ts') + narrow 13:25-14:30 UTC NY-open window (DST);
    # session_end=15 covers both DST and STD. setup_selective=True + 180min window
    # tolerates its sparse nature (signals often cluster at open then taper).
    StrategyWatch("forge_mamba",         "forge/logs/mamba/signals.csv",              "timestamp", 13, 16, 180, 1, setup_selective=True),
    # cuebanks: skip until its runner actually writes signals.csv (hasn't yet).
    # Adding back once the bridge starts producing data.
    # StrategyWatch("forge_cuebanks",   ...),
]


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _in_session(hour: int, start: int, end: int) -> bool:
    """Inclusive start, exclusive end. Wrap-around supported (start > end)."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def check_strategy(watch: StrategyWatch, now: datetime) -> dict:
    path = REPO / watch.signals_csv
    result = {
        "strategy": watch.id,
        "status": "OK",
        "reason": "",
        "in_session": _in_session(now.hour, watch.session_start_utc, watch.session_end_utc),
        "signals_last_window": 0,
        "window_minutes": watch.min_signals_window_min,
        "session_utc": f"{watch.session_start_utc:02d}-{watch.session_end_utc:02d}",
    }

    if not path.exists():
        result["status"] = "NO_SIGNALS_CSV"
        result["reason"] = f"{watch.signals_csv} missing"
        return result

    if not result["in_session"]:
        result["status"] = "OUT_OF_SESSION"
        result["reason"] = f"current UTC hour {now.hour} not in {result['session_utc']}"
        return result

    # Count signals in rolling window
    window_start = now - timedelta(minutes=watch.min_signals_window_min)
    count = 0
    try:
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = _parse_ts(row.get(watch.ts_col, "") or "")
                if ts and ts >= window_start:
                    count += 1
    except Exception as e:
        result["status"] = "READ_ERROR"
        result["reason"] = str(e)
        return result

    result["signals_last_window"] = count

    # Minutes into session (for "halfway in" heuristic)
    session_hours = watch.session_end_utc - watch.session_start_utc
    if session_hours <= 0:
        session_hours += 24  # wrap-around
    halfway_hour = watch.session_start_utc + (session_hours / 2)
    past_halfway = (now.hour >= halfway_hour) or (watch.session_start_utc > watch.session_end_utc and now.hour < watch.session_end_utc)

    if count < watch.min_signals_expected and past_halfway:
        result["status"] = "SILENT_BLOCK"
        result["reason"] = f"expected >= {watch.min_signals_expected} signals in last {watch.min_signals_window_min}min during session, got {count}"
    elif count < watch.min_signals_expected:
        result["status"] = "EARLY_SESSION_QUIET"
        result["reason"] = f"{count} signals in last {watch.min_signals_window_min}min; session started <halfway ago, not yet concerning"

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    results = [check_strategy(w, now) for w in STRATEGIES]
    flagged = [r for r in results if r["status"] == "SILENT_BLOCK"]

    out = {
        "checked_at": now.isoformat(),
        "strategies_checked": len(results),
        "silent_block_count": len(flagged),
        "results": results,
    }
    out_path = LOGS_DIR / "silent_block_alerts.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if flagged:
        log.warning("SILENT_BLOCK flagged on %d strategies: %s", len(flagged), [f["strategy"] for f in flagged])
        for f in flagged:
            log.warning("  %s: %s", f["strategy"], f["reason"])

        # Discord alert — with per-strategy cooldown so we don't spam every 3 min
        state = _load_alert_state()
        now_epoch = int(now.timestamp())
        new_to_alert = []
        for f in flagged:
            strat = f["strategy"]
            last = state.get(strat, {}).get("last_alert_epoch", 0)
            if now_epoch - last >= ALERT_COOLDOWN_MIN * 60:
                new_to_alert.append(f)
                state[strat] = {
                    "last_alert_epoch": now_epoch,
                    "last_reason": f["reason"],
                    "last_session": f.get("session_utc", ""),
                }
        # Clear cooldown for strategies no longer flagged (recovery)
        recovered = []
        flagged_ids = {f["strategy"] for f in flagged}
        for strat in list(state.keys()):
            if strat not in flagged_ids and state[strat].get("last_alert_epoch"):
                recovered.append(strat)
                state.pop(strat, None)

        if new_to_alert:
            lines = [f"**{f['strategy']}**: {f['reason']}" for f in new_to_alert]
            body = "Runner alive but emitting no signals during its expected session window.\n\n" + "\n".join(lines)
            sent = _post_discord("Silent-block alert", body, color=16753920)  # orange
            log.info("Discord alert sent=%s for %d strategies", sent, len(new_to_alert))
        else:
            log.info("flagged strategies all within cooldown; no Discord post")

        if recovered:
            _post_discord("Silent-block RECOVERED", "These strategies are emitting signals again:\n" + "\n".join(f"- {s}" for s in recovered), color=65280)
            log.info("recovery alert sent for: %s", recovered)

        _save_alert_state(state)
    else:
        log.info("no silent blocks detected (%d strategies checked)", len(results))
        # Clear all cooldown state when clean — simple reset pattern
        state = _load_alert_state()
        if state:
            recovered = list(state.keys())
            _post_discord("Silent-block CLEAR", "All previously-flagged strategies are healthy again:\n" + "\n".join(f"- {s}" for s in recovered), color=65280)
            _save_alert_state({})

    if args.verbose:
        for r in results:
            log.info("%s: %s (%s)", r["strategy"], r["status"], r["reason"])

    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
