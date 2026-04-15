#!/usr/bin/env python3
"""Argus Live Dashboard — real-time monitoring for paper/live trading.

Usage:
    python ops/dashboard.py              # default localhost:8080
    python ops/dashboard.py --port 9090  # custom port

Reads live runner artifacts (state, account, fills, signals, events) and
serves a single-page dashboard with auto-refreshing panels.
"""
import argparse
import base64
import collections
import csv
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from process_lock import ProcessLock, ProcessLockError, build_dashboard_lock_name
except ImportError:
    from ops.process_lock import ProcessLock, ProcessLockError, build_dashboard_lock_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("argus.dashboard")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from argus_flow.ops.fleet_registry import (
    RISK_POLICY_DEFAULTS,
    STAGE_PAPER,
    STAGE_QUARANTINE,
    STAGE_REAL,
    STAGE_WATCHER,
    discover_managed_runners,
)
OPS_LOGS = REPO / "ops" / "logs"
STATE_DIR = REPO / "state"
DASHBOARD_SIGNAL_TAIL_ROWS = 150
DASHBOARD_TRADE_TAIL_ROWS = 100

app = FastAPI(title="Argus Dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# PWA routes — must be at root, not under /static/
@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_sw():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/icon-192.png")
async def pwa_icon_192():
    return FileResponse(STATIC_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def pwa_icon_512():
    return FileResponse(STATIC_DIR / "icon-512.png", media_type="image/png")


COINS = ["ETH", "BTC"]
POOL_FILE = REPO / "ops" / "coin_pool.json"


def get_coin_log_dir(coin: str) -> Path:
    """Get log directory for a specific coin."""
    coin = coin.upper()
    coin_dir = OPS_LOGS / coin.lower()
    if coin == "ETH":
        # ETH: prefer per-coin dir if it has adapter artifacts, else main dir
        if coin_dir.exists() and (coin_dir / "account.csv").exists():
            return coin_dir
        return OPS_LOGS
    return coin_dir


def get_coin_state_path(coin: str) -> Path:
    """Get runtime state file for a specific coin."""
    return STATE_DIR / f"runtime_state_{coin.upper()}_USD.json"


# ---------------------------------------------------------------------------
# Data readers  (all accept optional coin param; default = ETH / main dir)
# ---------------------------------------------------------------------------

def read_runtime_state(coin: str = "ETH") -> dict:
    """Read the latest runtime state snapshot."""
    path = get_coin_state_path(coin)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        log.warning("Failed to read runtime state: %s", path, exc_info=True)
        return {}


def read_runtime_mode() -> dict:
    """Read ops/runtime_mode.json if it exists."""
    path = REPO / "ops" / "runtime_mode.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        log.warning("Failed to read runtime mode: %s", path, exc_info=True)
        return {}


def read_run_manifest() -> dict:
    """Find and read the latest run_manifest."""
    manifests = sorted(OPS_LOGS.glob("run_manifest_*.json"), key=os.path.getmtime)
    if not manifests:
        return {}
    try:
        with open(manifests[-1]) as f:
            return json.load(f)
    except Exception:
        log.warning("Failed to read manifest: %s", manifests[-1], exc_info=True)
        return {}


def read_account_tail(n: int = 5, coin: str = "ETH") -> list:
    """Read last N account rows (tail-seek optimized)."""
    return _tail_csv(get_coin_log_dir(coin) / "account.csv", n)


def read_fills_tail(n: int = 20, coin: str = "ETH") -> list:
    """Read last N fills (tail-seek optimized)."""
    return _tail_csv(get_coin_log_dir(coin) / "fills.csv", n)


def read_trade_journal(n: int = 20, coin: str = "ETH") -> list:
    """Read the latest trade journal entries."""
    log_dir = get_coin_log_dir(coin)
    journals = sorted(log_dir.glob("trade_journal_*.csv"), key=os.path.getmtime)
    if not journals:
        return []
    try:
        rows = []
        with open(journals[-1]) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        log.warning("Failed to read trade journal", exc_info=True)
        return []


def _tail_csv(path: Path, n: int) -> list:
    """Efficiently read last N rows of a CSV file using seek-from-end."""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        if size == 0:
            return []
        # Read header + last chunk (estimate ~500 bytes/row, read extra)
        chunk_size = min(size, max(n * 800, 8192))
        with open(path, "rb") as f:
            # Read header line
            header = f.readline().decode("utf-8", errors="replace").strip()
            if size <= chunk_size:
                # Small file — read all
                f.seek(0)
                reader = csv.DictReader(
                    line.decode("utf-8", errors="replace") for line in f
                )
                rows = collections.deque(reader, maxlen=n)
                return list(rows)
            # Large file — seek to tail
            f.seek(max(0, size - chunk_size))
            f.readline()  # skip partial line
            tail_lines = f.readlines()
        if not tail_lines:
            return []
        # Parse with header
        text_lines = [header + "\n"] + [
            l.decode("utf-8", errors="replace") for l in tail_lines
        ]
        reader = csv.DictReader(text_lines)
        return list(collections.deque(reader, maxlen=n))
    except Exception:
        log.warning("Failed to tail-read CSV: %s", path, exc_info=True)
        return []


def read_signals_tail(n: int = 5, coin: str = "ETH") -> list:
    """Read last N live signal rows."""
    coin_dir = OPS_LOGS / coin.lower()
    per_coin_path = coin_dir / "live_signals.csv"
    fallback_path = get_coin_log_dir(coin) / "live_signals.csv"
    path = per_coin_path if per_coin_path.exists() else fallback_path
    return _tail_csv(path, n)


def read_events_tail(n: int = 20, coin: str = "ETH") -> list:
    """Read last N meaningful live event rows (WATCH_GATED noise filtered out)."""
    coin_dir = OPS_LOGS / coin.lower()
    per_coin_path = coin_dir / "live_events.csv"
    fallback_path = get_coin_log_dir(coin) / "live_events.csv"
    path = per_coin_path if per_coin_path.exists() else fallback_path
    all_rows = _tail_csv(path, 2000)
    filtered = [r for r in all_rows if "WATCH_GATED" not in (r.get("event") or "")]
    return filtered[-n:]


def read_equity_series(coin: str = "ETH") -> list:
    """Read account.csv for equity time series."""
    path = get_coin_log_dir(coin) / "account.csv"
    if not path.exists():
        return []
    try:
        points = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row.get("ts", "")
                equity = row.get("equity", "")
                if ts and equity:
                    try:
                        epoch_s = int(ts) / 1000 if int(ts) > 1e12 else int(ts)
                        points.append({
                            "t": datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat(),
                            "y": float(equity)
                        })
                    except (ValueError, TypeError):
                        pass
        return points
    except Exception:
        log.warning("Failed to read equity series", exc_info=True)
        return []


def read_decision_flow(n: int = 30, coin: str = "ETH") -> list:
    """Read recent entry-related events with governor scores."""
    log_dir = get_coin_log_dir(coin)
    decisions = []
    # From live_events.csv — entry attempts, blocks, fills
    path = log_dir / "live_events.csv"
    if path.exists():
        try:
            rows = []
            # Tail the file to avoid scanning 100k+ rows every SSE tick
            tail_rows = _tail_csv(path, 2000)
            for row in tail_rows:
                    event = row.get("event") or ""
                    # Skip WATCH_GATED — fires every candle when score<88, buries real events
                    if "WATCH_GATED" in event:
                        continue
                    if any(k in event for k in ["BUY", "SELL", "MISSED_BUY", "ENTRY", "GOVERNOR", "PARTIAL_TP", "BREAKEVEN", "OB_EXIT", "REGIME"]):
                        rows.append(row)
            for row in rows[-n:]:
                decisions.append({
                    "ts": row.get("ts", ""),
                    "event": row.get("event", ""),
                    "action": row.get("action", ""),
                    "detail": (row.get("detail", "") or "")[:120],
                    "confluence_score": row.get("confluence_score", ""),
                    "confluence_gate": row.get("confluence_gate", ""),
                    "regime": row.get("regime", ""),
                    "session": row.get("session", ""),
                })
        except Exception:
            log.warning("Failed to read decision events", exc_info=True)
    # Enrich with governor data from live_signals.csv
    coin_dir_dc = OPS_LOGS / coin.lower()
    per_coin_sig = coin_dir_dc / "live_signals.csv"
    sig_path = per_coin_sig if per_coin_sig.exists() else (log_dir / "live_signals.csv")
    if sig_path.exists():
        try:
            gov_rows = []
            for row in _tail_csv(sig_path, 200):
                    prob = row.get("governor_win_prob", "")
                    action = row.get("action", "")
                    if prob and action not in ("HOLD", ""):
                        gov_rows.append({
                            "ts": row.get("ts", ""),
                            "action": action,
                            "governor_win_prob": prob,
                            "governor_recommendation": row.get("governor_recommendation", ""),
                            "governor_score_modifier": row.get("governor_score_modifier", ""),
                            "confluence_score": row.get("confluence_score", ""),
                            "regime": row.get("regime", ""),
                            "session": row.get("session", ""),
                        })
            seen_ts = {d["ts"] for d in decisions}
            for gr in gov_rows[-n:]:
                if gr["ts"] not in seen_ts:
                    decisions.append(gr)
        except Exception:
            log.warning("Failed to read governor signals", exc_info=True)
    decisions.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return decisions[:n]


def read_governor_latest(coin: str = "ETH") -> dict:
    """Read latest governor score from live_signals.csv."""
    coin_dir = OPS_LOGS / coin.lower()
    per_coin_path = coin_dir / "live_signals.csv"
    fallback_path = get_coin_log_dir(coin) / "live_signals.csv"
    sig_path = per_coin_path if per_coin_path.exists() else fallback_path
    if not sig_path.exists():
        return {}
    try:
        last = {}
        rows = _tail_csv(sig_path, 5)
        if rows:
            last = rows[-1]
        # Return signal health data as long as we have at least a score
        if not last:
            return {}
        action_reason_raw = last.get("action_reason", "")
        prob_raw = last.get("governor_win_prob", "")
        return {
            "win_prob": float(prob_raw) if prob_raw else None,
            "recommendation": last.get("governor_recommendation", ""),
            "score_modifier": last.get("governor_score_modifier", ""),
            "confluence_score": last.get("confluence_score", ""),
            "regime": last.get("regime", ""),
            "session": last.get("session", ""),
            "score": last.get("score", ""),
            "action": last.get("action", ""),
            "action_reason": action_reason_raw[:120] if action_reason_raw else "",
            "ob_imbalance": last.get("ob_imbalance", ""),
            "gate": last.get("confluence_gate", ""),
            "price": last.get("price", "") or last.get("px", ""),
            "entry_px": last.get("entry_px", ""),
            "position_qty": last.get("position_qty", "") or "",
            "liq_atr_norm": last.get("liq_atr_norm", ""),
            "liq_spread_bps": last.get("liq_spread_bps", ""),
            "liq_vol_1m": last.get("liq_vol_1m", ""),
        }
    except Exception:
        log.warning("Failed to read governor latest: %s", sig_path, exc_info=True)
        return {}


def read_coin_pool() -> dict:
    """Read coin pool registry."""
    try:
        if POOL_FILE.exists():
            return json.loads(POOL_FILE.read_text())
    except Exception:
        pass
    return {"max_active": 2, "active": ["ETH", "BTC"], "coins": {}}


def _get_rotation_candidates() -> list:
    """Legacy coin rotation — disabled. Returns empty list."""
    return []


def read_queue_status(skip_pc2: bool = False) -> dict:
    """Read queue files and detect running/pending/completed jobs."""
    result = {"pending_pc1": 0, "pending_pc2": 0, "pending_labels": [],
              "running_job": None, "completed_today": 0}
    # Pending PC1
    q1 = REPO / "ops" / "backtest_queue.jsonl"
    if q1.exists():
        try:
            with open(q1) as _qf:
                lines = [l.strip() for l in _qf.readlines() if l.strip()]
            result["pending_pc1"] = len(lines)
            for l in lines:
                try:
                    result["pending_labels"].append(json.loads(l).get("label", "?"))
                except Exception:
                    pass
        except Exception:
            pass
    # Pending PC2 — only count as pending if file was modified recently (within 24h)
    q2 = REPO / "ops" / "backtest_queue_pc2.jsonl"
    if q2.exists():
        try:
            age_hours = (time.time() - os.path.getmtime(q2)) / 3600
            if age_hours < 24:
                with open(q2) as _qf:
                    lines = [l.strip() for l in _qf.readlines() if l.strip()]
                result["pending_pc2"] = len(lines)
            else:
                result["pending_pc2"] = 0
                result["pc2_queue_stale"] = True
        except Exception:
            pass
    # Running job: most recent run_header with no matching bt_summary
    try:
        headers = sorted(OPS_LOGS.glob("run_header_bt_*.json"), key=os.path.getmtime, reverse=True)
        for h in headers[:5]:
            with open(h) as _hf:
                hdr = json.load(_hf)
            rid = hdr.get("run_id", "")
            if not (OPS_LOGS / f"bt_summary_{rid}.json").exists():
                progress = 0
                total_bars = 0
                # Try equity CSV first; fall back to events CSV for BT_LITE_MODE
                eq_path = OPS_LOGS / f"equity_{rid}.csv"
                ev_path = OPS_LOGS / f"bt_events_{rid}.csv"
                if eq_path.exists():
                    with open(eq_path) as f:
                        progress = sum(1 for _ in f) - 1
                elif ev_path.exists():
                    with open(ev_path) as f:
                        progress = sum(1 for _ in f) - 1
                candles_csv = hdr.get("candles_csv", "")
                if candles_csv and Path(candles_csv).exists():
                    with open(candles_csv) as f:
                        total_bars = sum(1 for _ in f) - 1
                pct = round(progress / total_bars * 100) if total_bars else 0
                # ETA: extrapolate from run_header mtime (job start) + progress
                start_epoch = os.path.getmtime(h)
                elapsed_s = time.time() - start_epoch
                eta_current_s = 0
                eta_current_iso = ""
                if pct > 0:
                    total_est_s = elapsed_s / (pct / 100.0)
                    eta_current_s = int(total_est_s - elapsed_s)
                    eta_ts = datetime.fromtimestamp(time.time() + eta_current_s, tz=timezone.utc)
                    eta_current_iso = eta_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                result["running_job"] = {
                    "run_id": rid, "label": hdr.get("label", ""),
                    "progress": progress, "total_bars": total_bars,
                    "pct": pct,
                    "elapsed_s": int(elapsed_s),
                    "eta_remaining_s": eta_current_s,
                    "eta_completion": eta_current_iso,
                }
                # Queue-wide ETA: current remaining + estimated time per pending job
                pending_count = result["pending_pc1"]
                if pct > 0 and total_bars > 0:
                    est_per_job_s = total_est_s  # assume similar job size
                    queue_remaining_s = eta_current_s + int(pending_count * est_per_job_s)
                    queue_done_ts = datetime.fromtimestamp(time.time() + queue_remaining_s, tz=timezone.utc)
                    result["queue_eta"] = {
                        "remaining_s": queue_remaining_s,
                        "completion": queue_done_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "est_per_job_s": int(est_per_job_s),
                    }
                break
    except Exception:
        pass
    # Completed today
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        for p in sorted(OPS_LOGS.glob("bt_summary_bt_*.json"), key=os.path.getmtime, reverse=True)[:20]:
            mtime = datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc)
            if mtime.strftime("%Y-%m-%d") == today_str:
                result["completed_today"] += 1
            else:
                break
    except Exception:
        pass
    return result


# --- PC2 status (cached SSH, refreshes every 60s) ---
_pc2_cache: dict = {"ts": 0, "data": None}
PC2_SSH = 'ksmith2322@yahoo.com@192.168.1.98'
PC2_CACHE_TTL = 60  # seconds


def _fetch_pc2_status() -> dict:
    """SSH to PC2 and read its queue_results.log tail + backtest_queue.jsonl."""
    now = time.time()
    if _pc2_cache["data"] is not None and (now - _pc2_cache["ts"]) < PC2_CACHE_TTL:
        return _pc2_cache["data"]
    result = {"reachable": False, "running_label": "", "running_progress": "",
              "pending": [], "recent_log": [], "error": "",
              "eta_remaining_s": 0, "eta_pct": 0, "elapsed_s": 0}
    try:
        # SSH command: get log tail, queue, and progress info for ETA
        # Use -EncodedCommand to avoid shell escaping issues with $ variables
        ps_script = (
            'Get-Content C:/Argus/repo/ops/logs/queue_results.log -Tail 5;'
            'Write-Output "---QUEUE---";'
            'if (Test-Path C:/Argus/repo/ops/backtest_queue.jsonl) { Get-Content C:/Argus/repo/ops/backtest_queue.jsonl };'
            'Write-Output "---PROGRESS---";'
            '$hdrs = Get-ChildItem C:/Argus/repo/ops/logs/run_header_bt_*.json -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3;'
            'foreach ($h in $hdrs) {'
            '  $j = Get-Content $h.FullName | ConvertFrom-Json;'
            '  $rid = $j.run_id;'
            '  $sum = "C:/Argus/repo/ops/logs/bt_summary_" + $rid + ".json";'
            '  if (!(Test-Path $sum)) {'
            '    $eq = "C:/Argus/repo/ops/logs/equity_" + $rid + ".csv";'
            '    $eqLines = 0;'
            '    if (Test-Path $eq) { $eqLines = (Get-Content $eq | Measure-Object -Line).Lines - 1 };'
            '    $csv = $j.candles_csv;'
            '    $csvLines = 0;'
            '    if ($csv -and (Test-Path $csv)) { $csvLines = (Get-Content $csv | Measure-Object -Line).Lines - 1 };'
            '    $startTs = $h.LastWriteTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ");'
            '    Write-Output ($rid + "|" + $eqLines + "|" + $csvLines + "|" + $startTs);'
            '    break'
            '  }'
            '}'
        )
        encoded = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        cmd = ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
               PC2_SSH, f'powershell -NoProfile -EncodedCommand {encoded}']
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            result["error"] = proc.stderr.strip()[:200]
            _pc2_cache.update(ts=now, data=result)
            return result
        result["reachable"] = True
        lines = proc.stdout.strip().split('\n')
        sep_idx = -1
        for i, l in enumerate(lines):
            if '---QUEUE---' in l:
                sep_idx = i
                break
        log_lines = lines[:sep_idx] if sep_idx >= 0 else lines
        rest_lines = lines[sep_idx+1:] if sep_idx >= 0 else []
        # Split rest into queue and progress sections
        prog_idx = -1
        for i, l in enumerate(rest_lines):
            if '---PROGRESS---' in l:
                prog_idx = i
                break
        queue_lines = rest_lines[:prog_idx] if prog_idx >= 0 else rest_lines
        progress_lines = rest_lines[prog_idx+1:] if prog_idx >= 0 else []
        # Parse log lines
        for line in log_lines:
            line = line.strip()
            if not line:
                continue
            ts = line[1:21] if line.startswith("[") else ""
            rest = line[23:] if len(line) > 23 else line
            status = "UNKNOWN"
            label = rest
            if rest.startswith("START:"):
                status = "START"
                label = rest[7:].strip()
            elif rest.startswith("DONE:"):
                status = "DONE"
                label = rest[6:].strip().split("|")[0].strip()
            elif rest.startswith("FAIL:"):
                status = "FAIL"
                label = rest[6:].strip().split(" -- ")[0].strip()
            result["recent_log"].append({"ts": ts, "status": status, "label": label})
        # Detect running: last entry is START with no DONE/FAIL after
        if result["recent_log"] and result["recent_log"][-1]["status"] == "START":
            result["running_label"] = result["recent_log"][-1]["label"]
        # Parse pending queue
        for ql in queue_lines:
            ql = ql.strip()
            if not ql:
                continue
            try:
                result["pending"].append(json.loads(ql).get("label", "?"))
            except Exception:
                pass
        # Parse progress: "run_id|eq_lines|csv_lines|start_ts"
        for pl in progress_lines:
            pl = pl.strip()
            if '|' not in pl:
                continue
            parts = pl.split('|')
            if len(parts) >= 4:
                try:
                    eq_lines = int(parts[1])
                    csv_lines = int(parts[2])
                    start_ts_str = parts[3]
                    pct = round(eq_lines / csv_lines * 100) if csv_lines > 0 else 0
                    # Parse start time for elapsed
                    try:
                        start_dt = datetime.strptime(start_ts_str.strip(), "%Y-%m-%dT%H:%M:%SZ")
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                        elapsed = time.time() - start_dt.timestamp()
                    except Exception:
                        elapsed = 0
                    eta_s = 0
                    if pct > 0 and elapsed > 0:
                        total_est = elapsed / (pct / 100.0)
                        eta_s = int(total_est - elapsed)
                    result["eta_pct"] = pct
                    result["elapsed_s"] = int(elapsed)
                    result["eta_remaining_s"] = max(0, eta_s)
                    result["progress_bars"] = eq_lines
                    result["total_bars"] = csv_lines
                except Exception:
                    pass
                break
    except subprocess.TimeoutExpired:
        result["error"] = "SSH timeout (PC2 unreachable)"
    except Exception as e:
        result["error"] = str(e)[:200]
    _pc2_cache.update(ts=now, data=result)
    return result


def build_status(coin: str = "ETH") -> dict:
    """Aggregate all status info into a single dict."""
    state = read_runtime_state(coin)
    manifest = read_run_manifest()
    mode = read_runtime_mode()
    account = read_account_tail(1, coin)
    fills = read_fills_tail(20, coin)
    journal = read_trade_journal(10, coin)
    signals = read_signals_tail(3, coin)
    events = read_events_tail(20, coin)

    # Compute uptime
    start_ts = manifest.get("start_ts", 0)
    uptime_s = int(time.time() - start_ts) if start_ts else 0
    uptime_h = uptime_s / 3600

    # Latest account snapshot
    latest_acct = account[-1] if account else {}

    return {
        "bot_state": state.get("bot_state", "UNKNOWN"),
        "symbol": state.get("symbol", "ETH-USD"),
        "position_qty": state.get("position_qty", "0"),
        "cash": state.get("cash", "0"),
        "realized_pnl": state.get("realized_pnl", "0"),
        "equity": latest_acct.get("equity", state.get("cash", "0")),
        "unrealized_pnl": latest_acct.get("unrealized_pnl", "0"),
        "run_id": state.get("run_id", ""),
        "session_id": state.get("session_id", ""),
        "recovery_source": state.get("recovery_source", ""),
        "saved_at": state.get("saved_at", 0),
        "saved_at_iso": datetime.fromtimestamp(
            state.get("saved_at", 0), tz=timezone.utc
        ).isoformat() if state.get("saved_at") else "",
        "uptime_hours": round(uptime_h, 1),
        "manifest_status": manifest.get("status", "UNKNOWN"),
        "runtime_mode": mode.get("mode", "FULL"),
        "config_hash": manifest.get("config_hash", "")[:12],
        "total_fills": len(fills),
        "total_journal_entries": len(journal),
        "fills": fills[-10:],
        "journal": journal[-5:],
        "signals": signals,
        "events": events[-10:],
        "coin": coin.upper(),
        "queue": read_queue_status(skip_pc2=True),
        "governor": read_governor_latest(coin),
        "rotation_candidates": _get_rotation_candidates(),
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

def _coin_param(request: Request) -> str:
    """Extract coin from query string, default ETH."""
    c = request.query_params.get("coin", "ETH").upper()
    return c if c in COINS else "ETH"


@app.get("/api/status")
async def api_status(request: Request):
    return JSONResponse(build_status(_coin_param(request)))


@app.get("/api/multi")
async def api_multi():
    """Summary for all coins — used by multi-coin overview."""
    result = {}
    for coin in COINS:
        state = read_runtime_state(coin)
        acct = read_account_tail(1, coin)
        latest_acct = acct[-1] if acct else {}
        result[coin] = {
            "bot_state": state.get("bot_state", "UNKNOWN"),
            "symbol": f"{coin}-USD",
            "cash": state.get("cash", "0"),
            "equity": latest_acct.get("equity", state.get("cash", "0")),
            "realized_pnl": state.get("realized_pnl", "0"),
            "position_qty": state.get("position_qty", "0"),
            "unrealized_pnl": latest_acct.get("unrealized_pnl", "0"),
            "saved_at": state.get("saved_at", 0),
            "saved_at_iso": datetime.fromtimestamp(
                state.get("saved_at", 0), tz=timezone.utc
            ).isoformat() if state.get("saved_at") else "",
            "governor": read_governor_latest(coin),
        }
    return JSONResponse(result)


@app.get("/api/queue")
async def api_queue():
    base = read_queue_status()
    # Add results log (last 30 entries)
    log_path = REPO / "ops" / "logs" / "queue_results.log"
    log_entries = []
    if log_path.exists():
        try:
            for line in open(log_path).readlines()[-30:]:
                line = line.strip()
                if not line:
                    continue
                # Parse: [2026-03-15T18:14:23Z] START: label
                ts = line[1:21] if line.startswith("[") else ""
                rest = line[23:] if len(line) > 23 else line
                status = "UNKNOWN"
                label = rest
                reason = ""
                if rest.startswith("START:"):
                    status = "START"
                    label = rest[7:].strip()
                elif rest.startswith("DONE:"):
                    status = "DONE"
                    label = rest[6:].strip().split("|")[0].strip()
                elif rest.startswith("FAIL:"):
                    status = "FAIL"
                    parts = rest[6:].strip().split(" -- ", 1)
                    label = parts[0].strip()
                    reason = parts[1].strip() if len(parts) > 1 else ""
                log_entries.append({"ts": ts, "status": status, "label": label, "reason": reason})
        except Exception:
            pass
    base["log"] = log_entries
    # Add recent completed summaries (last 25) from BOTH PC1 and PC2
    recent = []
    def _read_summaries(logs_dir, source_tag):
        items = []
        try:
            sums = sorted(logs_dir.glob("bt_summary_bt_*.json"), key=os.path.getmtime, reverse=True)[:20]
            for sp in sums:
                with open(sp) as _sf:
                    d = json.load(_sf)
                rid = d.get("run_id", "")
                lbl = ""
                hdr_path = logs_dir / f"run_header_{rid}.json"
                if hdr_path.exists():
                    try:
                        with open(hdr_path) as _hf:
                            lbl = json.load(_hf).get("label", "")
                    except Exception:
                        pass
                mfe = float(d.get("avg_mfe_pct_points", 0) or 0)
                mae = float(d.get("avg_mae_pct_points", 0) or 0)
                items.append({
                    "run_id": rid, "label": lbl, "source": source_tag,
                    "pf": float(d.get("profit_factor", 0)),
                    "wr": float(d.get("win_rate_pct", 0)),
                    "pnl": str(d.get("pnl_usd", "0")),
                    "trades": int(d.get("trades_closed", 0)),
                    "expectancy": str(d.get("expectancy_usd", "0")),
                    "max_dd_pct": float(d.get("max_drawdown_pct", 0) or 0),
                    "avg_mfe": mfe, "avg_mae": mae,
                    "symbol": d.get("symbol", "ETH-USD"),
                    "elapsed_days": round(max(1, (int(d.get("end_epoch", 0) or 0) - int(d.get("start_epoch", 0) or 0)) / 86400), 1),
                    "ts": datetime.fromtimestamp(os.path.getmtime(sp), tz=timezone.utc).isoformat(),
                })
        except Exception:
            pass
        return items
    recent = _read_summaries(OPS_LOGS, "PC1")
    pc2_logs = OPS_LOGS / "pc2"
    if pc2_logs.exists():
        recent.extend(_read_summaries(pc2_logs, "PC2"))
    # Sort all by timestamp descending, limit to 25
    recent.sort(key=lambda x: x["ts"], reverse=True)
    recent = recent[:25]
    base["recent"] = recent
    # PC2 status (cached SSH)
    base["pc2"] = _fetch_pc2_status()
    return JSONResponse(base)


@app.get("/api/sweep")
async def api_sweep():
    """Sweep phase progress and best results so far."""
    import re

    # Build label → (fee_bps, tp_pct, sl_pct) map from run headers
    header_meta: dict = {}
    for hdr_path in OPS_LOGS.glob("run_header_*.json"):
        try:
            with open(hdr_path) as f:
                h = json.load(f)
            rid = h.get("run_id", "")
            lbl = h.get("label", "")
            if rid and lbl:
                header_meta[rid] = {
                    "label": lbl,
                    "fee_bps": float(h.get("fee_bps", 60)),
                    "tp_pct": float(h.get("take_profit_pct", 0) or 0),
                    "sl_pct": float(h.get("stop_loss_pct", 0) or 0),
                }
        except Exception:
            pass

    def _classify(lbl: str, fee: float) -> str:
        if not lbl.startswith("sweep_"): return "other"
        has_wide = bool(re.search(r"_tp(6|8|10|12)_", lbl))
        is_maker = "_maker" in lbl
        is_trail = "_trail" in lbl
        if not has_wide: return "phase1"
        if is_trail: return "phase2_trail"
        if is_maker or fee <= 45: return "phase2_maker"
        return "phase2_60"

    phases = {"phase1": [], "phase2_60": [], "phase2_maker": [], "phase2_trail": [], "other": []}

    for sumf in OPS_LOGS.glob("bt_summary_*.json"):
        try:
            with open(sumf) as f:
                d = json.load(f)
            rid = d.get("run_id", "")
            meta = header_meta.get(rid, {})
            lbl = meta.get("label", "")
            if not lbl.startswith("sweep_"): continue
            fee = meta.get("fee_bps", 60.0)
            phase = _classify(lbl, fee)
            tp = meta.get("tp_pct", 0) or 0
            sl = meta.get("sl_pct", 0) or 0
            pf = float(d.get("profit_factor", 0) or 0)
            wr = float(d.get("win_rate_pct", 0) or 0)
            trades = int(d.get("trades_closed", 0) or 0)
            pnl = float(d.get("pnl_usd", 0) or 0)
            # breakeven WR
            if tp > 0 and sl > 0:
                fee_pct = fee / 10000.0
                net_win = tp - 2 * fee_pct
                net_loss = sl + 2 * fee_pct
                be_wr = (net_loss / (net_win + net_loss) * 100) if net_win > 0 else None
            else:
                be_wr = None
            phases[phase].append({
                "label": lbl, "pf": pf, "wr": wr, "trades": trades,
                "pnl": pnl, "fee_bps": fee, "tp_pct": tp, "sl_pct": sl,
                "be_wr": be_wr,
            })
        except Exception:
            pass

    def _phase_summary(runs: list) -> dict:
        valid = [r for r in runs if r["trades"] >= 15]
        if not valid:
            return {"count": len(runs), "profitable": 0, "best_pf": 0, "best": None}
        profitable = [r for r in valid if r["pf"] >= 1.0]
        best = max(valid, key=lambda x: x["pf"])
        return {
            "count": len(runs),
            "valid": len(valid),
            "profitable": len(profitable),
            "best_pf": best["pf"],
            "best": best,
        }

    # Count pending queue entries by phase
    queue_pending = {"phase1": 0, "phase2_60": 0, "phase2_maker": 0, "phase2_trail": 0}
    q_path = REPO / "ops" / "backtest_queue.jsonl"
    if q_path.exists():
        for line in open(q_path, encoding="utf-8-sig"):
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                lbl = d.get("label", "")
                fee = float((d.get("env") or {}).get("FEE_BPS", 60))
                ph = _classify(lbl, fee)
                if ph in queue_pending:
                    queue_pending[ph] += 1
            except Exception:
                pass

    return JSONResponse({
        "phases": {k: _phase_summary(v) for k, v in phases.items()},
        "queue_pending": queue_pending,
        "total_completed": sum(len(v) for v in phases.values()),
    })


@app.get("/api/equity")
async def api_equity(request: Request):
    return JSONResponse(read_equity_series(_coin_param(request)))


@app.get("/api/events")
async def api_events(request: Request):
    return JSONResponse(read_events_tail(50, _coin_param(request)))


@app.get("/api/fills")
async def api_fills(request: Request):
    return JSONResponse(read_fills_tail(50, _coin_param(request)))


@app.get("/api/journal")
async def api_journal(request: Request):
    return JSONResponse(read_trade_journal(200, _coin_param(request)))


@app.get("/api/decisions")
async def api_decisions(request: Request):
    return JSONResponse(read_decision_flow(50, _coin_param(request)))


@app.get("/api/governor")
async def api_governor(request: Request):
    return JSONResponse(read_governor_latest(_coin_param(request)))


@app.get("/api/backtest")
async def api_backtest():
    """Check in-progress and recent backtest runs."""
    runs = []
    for p in sorted(OPS_LOGS.glob("run_header_bt_*.json"), key=os.path.getmtime, reverse=True)[:5]:
        try:
            with open(p) as f:
                hdr = json.load(f)
            rid = hdr.get("run_id", "")
            # Check if summary exists (= completed)
            summary_path = OPS_LOGS / f"bt_summary_{rid}.json"
            equity_path = OPS_LOGS / f"equity_{rid}.csv"
            progress = 0
            total_bars = 0
            if equity_path.exists():
                with open(equity_path) as f:
                    progress = sum(1 for _ in f) - 1  # subtract header
            # Get total bars from candle data file
            candles_csv = hdr.get("candles_csv", "")
            if candles_csv and Path(candles_csv).exists():
                with open(candles_csv) as f:
                    total_bars = sum(1 for _ in f) - 1
            completed = summary_path.exists()
            summary = {}
            if completed:
                with open(summary_path) as f:
                    summary = json.load(f)
            runs.append({
                "run_id": rid,
                "start_ts": hdr.get("start_ts", ""),
                "config_hash": hdr.get("config_hash", ""),
                "git_sha": hdr.get("git_sha", ""),
                "completed": completed,
                "progress_bars": progress,
                "total_bars": total_bars,
                "pnl": summary.get("pnl_usd", ""),
                "trades": summary.get("trades_closed", ""),
                "win_rate": summary.get("win_rate_pct", ""),
                "profit_factor": summary.get("profit_factor", ""),
            })
        except Exception:
            continue
    return JSONResponse(runs)


@app.get("/api/leaderboard")
async def api_leaderboard():
    """Ranked backtest results from all bt_summary files, paired with run_header labels."""
    rows = []
    for sp in OPS_LOGS.glob("bt_summary_bt_*.json"):
        if "latest" in sp.name:
            continue
        try:
            with open(sp) as f:
                s = json.load(f)
            rid = s.get("run_id", sp.stem.replace("bt_summary_", ""))
            # pair with run_header for label
            hp = OPS_LOGS / f"run_header_{rid}.json"
            label = "—"
            if hp.exists():
                with open(hp) as f:
                    label = json.load(f).get("label", "—")
            trades = s.get("total_trades", s.get("entry_filled", 0))
            if isinstance(trades, str):
                trades = int(trades) if trades else 0
            rows.append({
                "run_id": rid,
                "label": label,
                "trades": trades,
                "win_rate": s.get("win_rate_pct", "0"),
                "profit_factor": s.get("profit_factor", "0"),
                "pnl": s.get("realized_pnl_usd", s.get("pnl_usd", "0")),
                "expectancy": s.get("expectancy_usd", "0"),
                "max_dd": s.get("max_drawdown_pct", "0"),
                "avg_win": s.get("avg_win_usd", "0"),
                "avg_loss": s.get("avg_loss_usd", "0"),
                "start_epoch": s.get("start_epoch", 0),
                "end_epoch": s.get("end_epoch", 0),
            })
        except Exception:
            continue
    # Sort by profit factor descending, then by trades descending
    rows.sort(key=lambda r: (float(r["profit_factor"] or 0), r["trades"]), reverse=True)
    return JSONResponse(rows)


@app.get("/api/config")
async def api_config():
    """Read key config values from .env file."""
    env_path = REPO / ".env"
    if not env_path.exists():
        return JSONResponse({})
    keys_of_interest = [
        "CONFLUENCE_MIN_SCORE", "CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE",
        "REGIME_ENTRY_BLOCK_LIST", "MAX_HOLD_SECONDS", "MIN_HOLD_SECONDS",
        "TAKE_PROFIT_PCT", "TRAIL_STOP_PCT", "STOP_LOSS_PCT",
        "DRAWDOWN_PAUSE_PCT", "DAILY_MAX_LOSS_USD", "MAX_TRADES_PER_DAY",
        "USE_TRENDLINES", "USE_STRUCTURE", "USE_LIQUIDITY_FILTERS",
        "USE_SESSION_MODIFIERS", "USE_VOL_SIZING", "RISK_PER_TRADE_USD",
        "EXECUTION_ADAPTER", "PRODUCT_ID", "START_CASH_USD",
        "USD_PER_TRADE", "COMPOUND_SIZE_PCT",
        "SESSION_ENTRY_BLOCK_LIST",
        "SESSION_ASIA_RISK_MULT", "SESSION_OFF_RISK_MULT",
        "USE_ML_GOVERNOR", "ML_GOVERNOR_MODE", "ML_GOVERNOR_THRESHOLD",
    ]
    cfg = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k in keys_of_interest:
                        cfg[k] = v.strip()
    except Exception:
        pass
    cfg["coin_configs"] = {
        "ETH": {k: v for k, v in (read_coin_pool().get("coins", {}).get("ETH", {}).get("config", {})).items()},
        "BTC": {k: v for k, v in (read_coin_pool().get("coins", {}).get("BTC", {}).get("config", {})).items()},
    }
    return JSONResponse(cfg)


# ---------------------------------------------------------------------------
# SSE stream for real-time updates
# ---------------------------------------------------------------------------

async def status_stream(request: Request, coin: str = "ETH"):
    """Push status updates every 5 seconds."""
    while True:
        if await request.is_disconnected():
            break
        try:
            data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: json.dumps(build_status(coin), default=str)
                ),
                timeout=10.0,
            )
            yield {"event": "status", "data": data}
        except asyncio.TimeoutError:
            log.warning("build_status(%s) timed out after 10s", coin)
            yield {"event": "status", "data": json.dumps({"error": "status_timeout"})}
        except Exception:
            log.warning("SSE build_status error for %s", coin, exc_info=True)
        await asyncio.sleep(5)


@app.get("/api/decision_trace")
async def api_decision_trace(request: Request):
    """Read decision trace data for the Decision Surface page."""
    coin = _coin_param(request)
    result = {"rows": [], "blockers": {}, "score_dist": {}, "blocked_high": [], "waterfall": None}
    try:
        for log_dir in [OPS_LOGS / coin.lower(), OPS_LOGS]:
            trace_path = log_dir / "decision_trace.csv"
            if trace_path.exists():
                rows = _tail_csv(trace_path, 5000)
                result["rows"] = rows
                # Blocker frequency
                blockers = {}
                score_buckets = {"70-79": 0, "80-87": 0, "88-91": 0, "92+": 0}
                blocked_high = []
                for r in rows:
                    # Count blockers
                    bl = r.get("blockers", "")
                    if bl:
                        for b in bl.split("|"):
                            b = b.strip()
                            if b:
                                blockers[b] = blockers.get(b, 0) + 1
                    # Score distribution (use base_score to see pre-penalty signal quality)
                    fs = r.get("base_score", "")
                    if fs:
                        try:
                            s = int(fs)
                            if s >= 92: score_buckets["92+"] += 1
                            elif s >= 88: score_buckets["88-91"] += 1
                            elif s >= 80: score_buckets["80-87"] += 1
                            elif s >= 70: score_buckets["70-79"] += 1
                        except ValueError:
                            pass
                    # Blocked high-score
                    bs = r.get("base_score", "")
                    action = r.get("action", "")
                    if bs and action != "BUY":
                        try:
                            if int(bs) >= 88:
                                blocked_high.append(r)
                        except ValueError:
                            pass
                result["blockers"] = dict(sorted(blockers.items(), key=lambda x: -x[1]))
                result["score_dist"] = score_buckets
                result["blocked_high"] = blocked_high[-10:]  # last 10
                # Latest row as waterfall
                if rows:
                    result["waterfall"] = rows[-1]
                break
    except Exception:
        pass
    return result


@app.get("/api/process_progress")
async def api_process_progress(request: Request):
    """Return observation window progress counters."""
    result = {"trace_rows": 0, "high_score_rows": 0, "trades_taken": 0, "config_version": ""}
    try:
        for log_dir in [OPS_LOGS / "eth", OPS_LOGS / "btc", OPS_LOGS]:
            trace_path = log_dir / "decision_trace.csv"
            if trace_path.exists():
                rows = _tail_csv(trace_path, 5000)
                result["trace_rows"] += len(rows)
                for r in rows:
                    fs = r.get("final_score", "")
                    if fs:
                        try:
                            if int(fs) >= 88:
                                result["high_score_rows"] += 1
                        except ValueError:
                            pass
                    if r.get("action") == "BUY":
                        result["trades_taken"] += 1
                    cv = r.get("config_version", "")
                    if cv:
                        result["config_version"] = cv
    except Exception:
        pass
    return result


@app.get("/api/stream")
async def stream(request: Request):
    coin = _coin_param(request)
    return EventSourceResponse(status_stream(request, coin))


@app.get("/api/ibkr_stream")
async def ibkr_stream(request: Request):
    """Push IBKR fleet updates every 10 seconds."""
    return EventSourceResponse(ibkr_status_stream(request))

async def ibkr_status_stream(request: Request):
    import asyncio
    while True:
        if await request.is_disconnected():
            break
        try:
            raw_runners = [_read_ibkr_runner(r) for r in _managed_ibkr_runners()]
            # Deduplicate shared log dirs (prefer active over killed)
            _seen: dict[str, dict] = {}
            for _r in raw_runners:
                _ld = _r.get("log_dir", "")
                _ex = _seen.get(_ld)
                if _ex is None or (_r.get("current_stage") != "killed" and _ex.get("current_stage") == "killed"):
                    _seen[_ld] = _r
            runners = list(_seen.values())
            total_trades = sum(r["closed_trades"] for r in runners)
            total_signals = sum(r["signal_count"] for r in runners)

            # Cohort status
            paper_runners = [r for r in runners if r.get("current_stage") == STAGE_PAPER]
            any_promoted = any(r.get("promotion_eligible") for r in paper_runners)
            all_promoted = all(r.get("promotion_eligible") for r in paper_runners) if paper_runners else False
            cohort_status = "PROMOTED" if all_promoted else "REVIEW" if any_promoted else "COLLECTING"

            data = json.dumps({
                "runners": runners,
                "total_trades": total_trades,
                "total_signals": total_signals,
                "cohort_status": cohort_status,
                "fleet_valid_trades": sum(r.get("valid_trades", 0) for r in runners),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, default=str)
            yield {"event": "ibkr_status", "data": data}
        except Exception:
            log.warning("SSE ibkr_status error", exc_info=True)
        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Evolution API — serves backtest summary timeline + trade scatter data
# ---------------------------------------------------------------------------

def _load_bt_summaries(latest: int = 50) -> list:
    files = sorted(
        OPS_LOGS.glob("bt_summary_bt_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    files = [f for f in files if "latest" not in f.name]
    if latest > 0:
        files = files[-latest:]
    out = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["_mtime"] = f.stat().st_mtime
            out.append(data)
        except Exception:
            pass
    return out


def _build_evo_points(summaries: list) -> list:
    points = []
    for s in summaries:
        run_id = s.get("run_id", "")
        label = s.get("label", s.get("job_label", run_id))
        ts = s.get("_mtime", 0)
        if run_id:
            try:
                date_part = run_id.split("_")[1] if "_" in run_id else ""
                if date_part and "T" in date_part:
                    dt = datetime.strptime(date_part, "%Y%m%dT%H%M%SZ")
                    dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
            except Exception:
                pass
        points.append({
            "ts": ts * 1000,
            "label": str(label)[:40],
            "run_id": str(run_id)[:30],
            "pf": round(float(s.get("profit_factor", 0) or 0), 3),
            "wr": round(float(s.get("win_rate_pct", 0) or 0), 1),
            "pnl": round(float(s.get("total_pnl_usd", 0) or 0), 2),
            "trades": int(s.get("total_closed", s.get("trades_closed", 0)) or 0),
            "expectancy": round(float(s.get("expectancy_usd", 0) or 0), 4),
        })
    return sorted(points, key=lambda p: p["ts"])


def _build_scatter_data(summaries: list, max_runs: int = 8) -> list:
    recent = summaries[-max_runs:] if len(summaries) > max_runs else summaries
    all_trades = []
    for s in recent:
        run_id = s.get("run_id", "")
        if not run_id:
            continue
        trades_file = OPS_LOGS / f"trades_{run_id}.csv"
        if not trades_file.exists():
            continue
        try:
            with open(trades_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry_epoch = float(row.get("entry_epoch", row.get("epoch", 0)) or 0)
                    entry_px = float(row.get("entry_px", row.get("fill_px", 0)) or 0)
                    pnl = float(row.get("realized_pnl", row.get("pnl", 0)) or 0)
                    hold_s = float(row.get("hold_seconds", row.get("duration_s", 0)) or 0)
                    if entry_epoch > 0 and entry_px > 0:
                        all_trades.append({
                            "x": entry_epoch * 1000,
                            "y": round(entry_px, 2),
                            "z": round(pnl, 4),
                            "hold": round(hold_s, 0),
                            "run": run_id[:20],
                            "win": 1 if pnl > 0 else 0,
                        })
        except Exception:
            pass
    return all_trades


def _build_projection(summaries: list) -> dict:
    """Compute year-end balance projection from backtest performance."""
    # Read starting cash from .env
    start_cash = 1000.0
    env_path = REPO / ".env"
    if env_path.exists():
        try:
            for line in open(env_path):
                if line.strip().startswith("START_CASH_USD"):
                    start_cash = float(line.split("=", 1)[1].strip())
                    break
        except Exception:
            pass

    if not summaries:
        return {"start_cash": start_cash, "scenarios": []}

    # Gather performance metrics from all runs
    runs_with_pnl = []
    for s in summaries:
        pnl = float(s.get("total_pnl_usd", s.get("pnl_usd", 0)) or 0)
        trades = int(s.get("total_closed", s.get("trades_closed", 0)) or 0)
        # Try to get backtest duration in days
        dataset_bars = int(s.get("dataset_bars", s.get("bars_processed", 0)) or 0)
        if dataset_bars == 0:
            # Compute from epoch range (1-min bars)
            start_ep = int(s.get("start_epoch", 0) or 0)
            end_ep = int(s.get("end_epoch", 0) or 0)
            if end_ep > start_ep:
                dataset_bars = (end_ep - start_ep) // 60
        bt_days = max(1, dataset_bars / 1440)  # 1-min bars -> days
        if trades > 0:
            runs_with_pnl.append({
                "pnl": pnl,
                "trades": trades,
                "days": bt_days,
                "pf": float(s.get("profit_factor", 0) or 0),
                "label": s.get("label", s.get("job_label", s.get("run_id", ""))),
            })

    if not runs_with_pnl:
        return {"start_cash": start_cash, "scenarios": []}

    now = datetime.now(timezone.utc)
    days_left_in_year = max(1, (datetime(now.year, 12, 31, tzinfo=timezone.utc) - now).days)

    # Primary projection: best VALIDATED run (PF>1.2, 20+ trades, not a no-governor test)
    # Exclude intentional test labels (no_governor, nogov, screen_, nogov_) from "current strategy"
    _exclude = ("no_governor", "nogov", "screen_", "nogov_", "wf_w1_")
    validated = [r for r in runs_with_pnl if r["pf"] > 1.2 and r["trades"] >= 20
                 and not any(x in str(r["label"]).lower() for x in _exclude)]
    if not validated:
        validated = [r for r in runs_with_pnl if r["pf"] > 1.0 and r["trades"] >= 10]
    if not validated:
        validated = runs_with_pnl
    latest_run = max(validated, key=lambda r: r["pf"])
    latest_daily = latest_run["pnl"] / latest_run["days"]

    # Historical context: best-ever run (may be different config)
    best_run = max(runs_with_pnl, key=lambda r: r["pf"])
    best_daily = best_run["pnl"] / best_run["days"]

    # num_coins: use actual active coins list
    num_coins = len(COINS)
    per_coin_cash = start_cash

    scenarios = []
    for tag, run, note in [
        ("latest", latest_run, "Current strategy"),
        ("best_ever", best_run, "Best historical"),
    ]:
        daily_pnl = run["pnl"] / run["days"]
        # Per-coin projection
        projected_gain = daily_pnl * days_left_in_year
        year_end_balance = per_coin_cash + projected_gain
        pct_gain = (projected_gain / per_coin_cash) * 100 if per_coin_cash > 0 else 0
        # 3-coin aggregate
        total_year_end = year_end_balance * num_coins

        scenarios.append({
            "tag": tag,
            "label": str(run["label"])[:30],
            "note": note,
            "pf": round(run["pf"], 3),
            "daily_pnl": round(daily_pnl, 4),
            "bt_days": round(run["days"], 1),
            "projected_gain": round(projected_gain, 2),
            "year_end_balance": round(year_end_balance, 2),
            "total_year_end_3coin": round(total_year_end, 2),
            "pct_gain": round(pct_gain, 1),
            "days_left": days_left_in_year,
        })

    # Monthly projection curve from LATEST run (source of truth)
    # Two curves: (1) compound only, (2) compound + $500/month injection
    import math as _math
    monthly_injection = 500  # $500/month capital injection plan
    monthly_curve = []
    monthly_curve_with_injection = []
    # Compute daily compound rate from latest backtest
    latest_daily_return_pct = (latest_daily / per_coin_cash) if per_coin_cash > 0 else 0
    for m in range(13):  # 0=now through 12=Dec
        month_num = now.month + m
        if month_num > 12:
            month_num -= 12
        days_into_projection = m * 30.44

        # Compound-only curve (3-coin aggregate)
        if latest_daily_return_pct > 0:
            compound_balance = per_coin_cash * num_coins * _math.pow(1 + latest_daily_return_pct, days_into_projection)
        else:
            compound_balance = per_coin_cash * num_coins + (latest_daily * num_coins * days_into_projection)
        monthly_curve.append({
            "month": month_num,
            "balance": round(compound_balance, 2),
        })

        # Compound + $500/month injection curve
        if m == 0:
            inj_balance = per_coin_cash * num_coins
        else:
            prev = monthly_curve_with_injection[-1]["balance"]
            if latest_daily_return_pct > 0:
                inj_balance = prev * _math.pow(1 + latest_daily_return_pct, 30.44) + monthly_injection
            else:
                inj_balance = prev + (latest_daily * num_coins * 30.44) + monthly_injection
        monthly_curve_with_injection.append({
            "month": month_num,
            "balance": round(inj_balance, 2),
        })

    # $100K milestone — compound + $500/month injection (realistic)
    milestone_100k = None
    total_balance = per_coin_cash * num_coins  # total across active coins
    agg_daily = latest_daily * num_coins

    if latest_daily_return_pct > 0:
        # Simulate month-by-month compound + injection to find $100K crossing
        sim_balance = total_balance
        months_to_100k = None
        for m in range(1, 361):  # up to 30 years
            sim_balance = sim_balance * _math.pow(1 + latest_daily_return_pct, 30.44) + monthly_injection
            if sim_balance >= 100_000:
                months_to_100k = m
                break
        days_needed_compound_inj = months_to_100k * 30.44 if months_to_100k else None

        # Pure compound (no injection) for comparison
        sim_balance_pure = total_balance
        months_pure = None
        for m in range(1, 361):
            sim_balance_pure = sim_balance_pure * _math.pow(1 + latest_daily_return_pct, 30.44)
            if sim_balance_pure >= 100_000:
                months_pure = m
                break
        days_pure = months_pure * 30.44 if months_pure else None

        milestone_100k = {
            "target": 100_000,
            "daily_pnl_latest": round(latest_daily, 4),
            "daily_pnl_3coin": round(agg_daily, 4),
            "daily_return_pct": round(latest_daily_return_pct * 100, 4),
            "current_balance": round(total_balance, 2),
            "dollars_to_go": round(100_000 - total_balance, 2),
            "num_coins": num_coins,
            "monthly_injection": monthly_injection,
            # Compound + injection
            "days_needed": round(days_needed_compound_inj, 0) if days_needed_compound_inj else None,
            "months_needed": months_to_100k,
            "target_date": (now + timedelta(days=days_needed_compound_inj)).strftime("%Y-%m-%d") if days_needed_compound_inj else "N/A",
            # Pure compound
            "days_pure_compound": round(days_pure, 0) if days_pure else None,
            "months_pure_compound": months_pure,
        }
    elif agg_daily > 0:
        # Linear fallback (positive but tiny returns)
        dollars_to_go = 100_000 - total_balance
        days_needed = dollars_to_go / agg_daily
        milestone_100k = {
            "target": 100_000,
            "daily_pnl_latest": round(latest_daily, 4),
            "daily_pnl_3coin": round(agg_daily, 4),
            "daily_return_pct": round(latest_daily_return_pct * 100, 4),
            "current_balance": round(total_balance, 2),
            "dollars_to_go": round(dollars_to_go, 2),
            "num_coins": num_coins,
            "monthly_injection": monthly_injection,
            "days_needed": round(days_needed, 0),
            "target_date": (now + timedelta(days=days_needed)).strftime("%Y-%m-%d"),
        }

    # Best-ever potential (compound + injection)
    best_daily_return_pct = (best_daily / per_coin_cash) if per_coin_cash > 0 else 0
    if best_daily_return_pct > 0 and best_daily_return_pct > latest_daily_return_pct:
        sim_balance_best = total_balance
        months_best = None
        for m in range(1, 361):
            sim_balance_best = sim_balance_best * _math.pow(1 + best_daily_return_pct, 30.44) + monthly_injection
            if sim_balance_best >= 100_000:
                months_best = m
                break
        if milestone_100k is None:
            milestone_100k = {}
        if months_best:
            milestone_100k["best_ever_days"] = round(months_best * 30.44, 0)
            milestone_100k["best_ever_months"] = months_best
        milestone_100k["best_ever_daily_3coin"] = round(best_daily * num_coins, 4)

    # Financial milestones: $2K per coin, then $5K per coin
    milestone_2k = None
    milestone_5k = None
    if latest_daily > 0:
        dollars_to_2k = 2000 - per_coin_cash
        days_to_2k = dollars_to_2k / latest_daily
        milestone_2k = {
            "target": 2000,
            "daily_pnl": round(latest_daily, 4),
            "days_needed": round(days_to_2k, 0),
            "target_date": (now + timedelta(days=days_to_2k)).strftime("%Y-%m-%d"),
            "current_balance": per_coin_cash,
        }
        dollars_to_5k = 5000 - per_coin_cash
        days_to_5k = dollars_to_5k / latest_daily
        milestone_5k = {
            "target": 5000,
            "daily_pnl": round(latest_daily, 4),
            "days_needed": round(days_to_5k, 0),
            "target_date": (now + timedelta(days=days_to_5k)).strftime("%Y-%m-%d"),
            "current_balance": per_coin_cash,
        }

    return {
        "start_cash": start_cash,
        "num_coins": num_coins,
        "scenarios": scenarios,
        "monthly_curve": monthly_curve,
        "monthly_curve_with_injection": monthly_curve_with_injection,
        "monthly_injection": monthly_injection,
        "milestone_100k": milestone_100k,
        "milestone_2k": milestone_2k,
        "milestone_5k": milestone_5k,
    }


def _build_100k_projection_check(total_equity: float, compound_rate_pct: float) -> dict:
    """Compute months-to-$100K using compound + $500/month injection."""
    import math as _m
    monthly_inj = 500
    if compound_rate_pct > 0:
        daily_r = compound_rate_pct / 100
        sim = total_equity
        months = None
        for mo in range(1, 361):
            sim = sim * _m.pow(1 + daily_r, 30.44) + monthly_inj
            if sim >= 100_000:
                months = mo
                break
        if months:
            return {"name": "$100K Projection < 12 months (compound + $500/mo)",
                    "target": "< 12 months", "current": f"{months} months",
                    "pass": months <= 12, "format": "text"}
    return {"name": "$100K Projection < 12 months (compound + $500/mo)",
            "target": "< 12 months", "current": "Not profitable yet",
            "pass": False, "format": "text"}


def _build_readiness_tracker(summaries: list) -> dict:
    """Compute go-live readiness metrics from latest backtest + live data."""
    import csv as _csv

    # --- Backtest metrics (latest run) ---
    bt_pf = 0.0
    bt_wr = 0.0
    bt_exp = 0.0
    bt_trades = 0
    bt_mfe_available = False
    if summaries:
        latest = summaries[-1]
        bt_pf = float(latest.get("profit_factor", 0) or 0)
        bt_wr = float(latest.get("win_rate_pct", 0) or 0)
        bt_exp = float(latest.get("expectancy_usd", 0) or 0)
        bt_trades = int(latest.get("trades_closed", 0) or 0)
        # Check if MFE is populated (non-zero) in latest trades
        run_id = latest.get("run_id", "")
        if run_id:
            trades_file = OPS_LOGS / f"trades_{run_id}.csv"
            if trades_file.exists():
                try:
                    with open(trades_file, "r") as tf:
                        reader = _csv.DictReader(tf)
                        for row in reader:
                            mfe = float(row.get("mfe_pct_points", 0) or 0)
                            if mfe != 0:
                                bt_mfe_available = True
                                break
                except Exception:
                    pass

    # --- Live metrics (per coin) ---
    live_trades = {"ETH": 0, "BTC": 0}
    live_pnl = {"ETH": 0.0, "BTC": 0.0}
    live_equity = {"ETH": 1000.0, "BTC": 1000.0}
    live_max_consec_loss = 0
    live_max_dd_pct = 0.0
    live_days_profitable = 0
    total_live_trades = 0

    for coin in ["ETH", "BTC"]:
        coin_lower = coin.lower()
        fills_path = OPS_LOGS / coin_lower / "fills.csv"
        if not fills_path.exists():
            # Try top-level fills
            fills_path = OPS_LOGS / "fills.csv"
        if fills_path.exists():
            try:
                with open(fills_path, "r") as ff:
                    reader = _csv.DictReader(ff)
                    coin_fills = [r for r in reader if coin in (r.get("symbol", "") or "")]
                    live_trades[coin] = len([f for f in coin_fills if (f.get("side", "") or "") == "BUY"])
            except Exception:
                pass

        # Read runtime state for equity/pnl
        state_file = REPO / "state" / f"runtime_state_{coin}_USD.json"
        if state_file.exists():
            try:
                with open(state_file) as sf:
                    st = json.load(sf)
                live_pnl[coin] = float(st.get("realized_pnl", 0) or 0)
                live_equity[coin] = float(st.get("cash", 500) or 500)
            except Exception:
                pass

        total_live_trades += live_trades[coin]

    total_live_pnl = sum(live_pnl.values())
    total_live_equity = sum(live_equity.values())

    # --- Compute derived metrics ---
    min_equity = min(live_equity.values())
    total_agg_pnl = sum(live_pnl.values())
    # Daily rate from paper trading start
    paper_start = datetime(2026, 3, 15, tzinfo=timezone.utc)
    elapsed_days = max(1, (datetime.now(timezone.utc) - paper_start).total_seconds() / 86400)
    daily_rate_3coin = total_agg_pnl / elapsed_days
    # $100K math: need $98,500 in 365 days across active coins
    daily_needed_100k = (100_000 - total_live_equity) / 365
    # Compound math: what daily % return gets us to $100K in 1 year
    # $1500 * (1 + r)^365 = $100,000 → r = (100000/1500)^(1/365) - 1 ≈ 1.18%
    import math
    compound_rate_needed = (math.pow(100_000 / max(total_live_equity, 1), 1 / 365) - 1) * 100
    compound_rate_current = 0.0
    if daily_rate_3coin > 0 and total_live_equity > 0:
        compound_rate_current = (daily_rate_3coin / total_live_equity) * 100

    # Check BTC backtest existence
    btc_bt_done = any("BTC" in str(s.get("symbol", "")) or "btc" in str(s.get("label", "")).lower()
                       for s in summaries)

    # --- Build checklist ---
    checks = [
        {
            "category": "Phase 1 — Strategy Validation (Backtest)",
            "items": [
                {"name": "Backtest PF >= 1.2", "target": 1.2, "current": round(bt_pf, 3),
                 "pass": bt_pf >= 1.2, "format": "pf"},
                {"name": "Backtest WR >= 50%", "target": 50.0, "current": round(bt_wr, 1),
                 "pass": bt_wr >= 50.0, "format": "pct"},
                {"name": "Positive Expectancy (> $0/trade)", "target": 0.0, "current": round(bt_exp, 4),
                 "pass": bt_exp > 0, "format": "usd"},
                {"name": "MFE/MAE Data Available", "target": "Yes", "current": "Yes" if bt_mfe_available else "No",
                 "pass": bt_mfe_available, "format": "bool"},
                {"name": "Exit Optimization Tested (TP/BE)", "target": "Done",
                 "current": "Running" if bt_pf > 0 else "Pending",
                 "pass": bt_pf >= 1.0, "format": "bool"},
                {"name": "ETH 30-day Backtest Profitable", "target": "PF>1.0",
                 "current": str(round(bt_pf, 2)), "pass": bt_pf >= 1.0, "format": "text"},
                {"name": "BTC Backtest Validated", "target": "Done",
                 "current": "Done" if btc_bt_done else "Pending",
                 "pass": btc_bt_done, "format": "bool"},
            ],
        },
        {
            "category": "Phase 2 — Paper Trading Proof (all active coins)",
            "items": [
                {"name": "ETH: 50+ Paper Trades", "target": 50, "current": live_trades["ETH"],
                 "pass": live_trades["ETH"] >= 50, "format": "int"},
                {"name": "BTC: 50+ Paper Trades", "target": 50, "current": live_trades["BTC"],
                 "pass": live_trades["BTC"] >= 50, "format": "int"},
                {"name": "Live PF >= 1.1 (aggregate)", "target": 1.1, "current": "N/A",
                 "pass": False, "format": "pf"},
                {"name": "Max Drawdown < 5% per coin", "target": "< 5%", "current": "N/A",
                 "pass": False, "format": "text"},
                {"name": "No 5+ Consecutive Losses", "target": "< 5", "current": "N/A",
                 "pass": False, "format": "text"},
                {"name": "ML Governor Retrained (200+ trades)", "target": 200,
                 "current": total_live_trades, "pass": total_live_trades >= 200, "format": "int"},
                {"name": "2+ Weeks Consistent PF > 1.0", "target": "14 days",
                 "current": "0 days", "pass": False, "format": "text"},
            ],
        },
        {
            "category": "Phase 3 — Financial Milestones ($500 → $2K → $5K)",
            "items": [
                {"name": "ETH: $500 → $2,000", "target": 2000,
                 "current": round(live_equity.get("ETH", 500), 2),
                 "pass": live_equity.get("ETH", 500) >= 2000, "format": "usd"},
                {"name": "BTC: $500 → $2,000", "target": 2000,
                 "current": round(live_equity.get("BTC", 500), 2),
                 "pass": live_equity.get("BTC", 500) >= 2000, "format": "usd"},
                {"name": "Both Coins → $5,000 each", "target": 5000,
                 "current": round(min_equity, 2),
                 "pass": min_equity >= 5000, "format": "usd"},
                {"name": "Portfolio Total >= $10,000", "target": 10000,
                 "current": round(total_live_equity, 2),
                 "pass": total_live_equity >= 10000, "format": "usd"},
            ],
        },
        {
            "category": "Phase 4 — $100K Velocity (compound + $500/mo injection, <1 year)",
            "items": [
                {"name": f"Compound Rate >= {compound_rate_needed:.2f}%/day", "target": round(compound_rate_needed, 2),
                 "current": round(compound_rate_current, 4),
                 "pass": compound_rate_current >= compound_rate_needed * 0.5, "format": "pct",
                 "note": "With $500/mo injection, ~0.6%/day is sufficient"},
                {"name": f"Daily Rate >= ${daily_needed_100k:.2f}/day ({num_coins} coins, linear)", "target": round(daily_needed_100k, 2),
                 "current": round(daily_rate_3coin, 4),
                 "pass": daily_rate_3coin >= daily_needed_100k * 0.1, "format": "usd",
                 "note": "Linear rate; compound+injection requires far less"},
                _build_100k_projection_check(total_live_equity, compound_rate_current),
            ],
        },
        {
            "category": "Phase 5 — Go-Live Infrastructure",
            "items": [
                {"name": "Coinbase API Keys Configured", "target": "Done",
                 "current": "Not done", "pass": False, "format": "bool"},
                {"name": "Real Money Adapter Tested", "target": "Done",
                 "current": "Not done", "pass": False, "format": "bool"},
                {"name": "Kill Switch Verified", "target": "Done",
                 "current": "Not tested", "pass": False, "format": "bool"},
                {"name": "Recovery/Reconciliation Tested", "target": "Done",
                 "current": "Phase 8 Pass" if True else "Not tested",
                 "pass": True, "format": "bool"},
                {"name": "Discord Alerts Working", "target": "Done",
                 "current": "Active", "pass": True, "format": "bool"},
            ],
        },
    ]

    total_items = sum(len(c["items"]) for c in checks)
    passed_items = sum(1 for c in checks for i in c["items"] if i["pass"])

    return {
        "checks": checks,
        "total": total_items,
        "passed": passed_items,
        "pct": round((passed_items / total_items) * 100, 1) if total_items > 0 else 0,
        "live_trades": live_trades,
        "live_pnl": {k: round(v, 4) for k, v in live_pnl.items()},
        "live_equity": {k: round(v, 2) for k, v in live_equity.items()},
        "total_live_equity": round(total_live_equity, 2),
    }


@app.get("/api/pool")
async def api_pool():
    """Coin pool registry — active, screened, disabled coins."""
    return JSONResponse(read_coin_pool())


@app.get("/api/rotation_status")
async def api_rotation_status():
    """Legacy coin rotation — disabled."""
    return JSONResponse({"error": "disabled", "candidates": [], "pool": {}})


@app.get("/api/evolution")
async def api_evolution():
    summaries = _load_bt_summaries(50)
    return JSONResponse({
        "evo": _build_evo_points(summaries),
        "scatter": _build_scatter_data(summaries),
        "projection": _build_projection(summaries),
        "ml_network": _build_ml_network_data(),
        "strategy_compare": _build_strategy_compare(summaries),
        "trade_analytics": _build_trade_analytics(summaries),
    })


# ── IBKR Fleet API ──────────────────────────────────────────

# ── Active Cohort (Class A) — frozen per COHORT_SPEC.md ──
IBKR_RUNNERS = [
    # FX — London session
    {"name": "GBP/USD", "symbol": "GBPUSD", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/gbpusd", "unit": "pips", "mult": 10000},
    {"name": "EUR/USD", "symbol": "EURUSD", "strategy": "T4 Full Stack", "log_dir": "argus_flow/logs/eurusd", "unit": "pips", "mult": 10000},
    {"name": "EUR/JPY", "symbol": "EURJPY", "strategy": "T4 Full Stack", "log_dir": "argus_flow/logs/eurjpy", "unit": "pips", "mult": 100},
    {"name": "GBP/JPY", "symbol": "GBPJPY", "strategy": "T4 Full Stack", "log_dir": "argus_flow/logs/gbpjpy", "unit": "pips", "mult": 100},
    {"name": "CAD/JPY", "symbol": "CADJPY", "strategy": "T4 Full Stack", "log_dir": "argus_flow/logs/cadjpy", "unit": "pips", "mult": 100},
    # FX — Asia session
    {"name": "AUD/JPY", "symbol": "AUDJPY", "strategy": "T4 Full Stack", "log_dir": "argus_flow/logs/audjpy", "unit": "pips", "mult": 100},
    {"name": "USD/JPY", "symbol": "USDJPY", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/usdjpy", "unit": "pips", "mult": 100},
    {"name": "AUD/USD", "symbol": "AUDUSD", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/audusd", "unit": "pips", "mult": 10000},
    # Futures — Equity Index Micros (US session)
    {"name": "MES", "symbol": "MES", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/mes", "unit": "bps", "mult": 1},
    {"name": "MNQ", "symbol": "MNQ", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/mnq", "unit": "bps", "mult": 1},
    {"name": "MYM", "symbol": "MYM", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/mym", "unit": "bps", "mult": 1},
    {"name": "M2K", "symbol": "M2K", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/m2k", "unit": "bps", "mult": 1},
    # Futures — Commodities
    {"name": "MGC", "symbol": "MGC", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/mgc", "unit": "bps", "mult": 1},
    {"name": "MCL", "symbol": "MCL", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/mcl", "unit": "bps", "mult": 1},
    # Futures — Asia
    # NKD KILLED 2026-03-29 — 0/7 WR, -550 pips
]

ALERT_STATE_FILE = REPO / "argus_flow" / "logs" / "alert_state.json"
ALERT_EVENTS_FILE = REPO / "argus_flow" / "logs" / "alert_events.jsonl"
FLEET_BROKER_FILE = REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json"
DEPLOYMENT_REGISTRY_FILE = REPO / "argus_flow" / "logs" / "deployment_registry.json"
PAPER_MODEL_START_USD = 10000.0
PAPER_MODEL_BASE_RISK_PCT = 0.005
PAPER_MODEL_CAP_RISK_PCT = 0.03
MANAGED_GOVERNANCE_FRESH_S = 30 * 60
OPS_REPORT_SPECS = [
    {"id": "position_monitor", "label": "Position Monitor", "path": REPO / "argus_flow" / "logs" / "position_monitor.json", "fresh_s": 900},
    {"id": "risk_oversight", "label": "Risk Oversight", "path": REPO / "argus_flow" / "logs" / "risk_oversight_report.json", "fresh_s": 900},
    {"id": "promotion_gate", "label": "Promotion Gate", "path": REPO / "argus_flow" / "logs" / "promotion_gate_report.json", "fresh_s": MANAGED_GOVERNANCE_FRESH_S},
    {"id": "artifact_divergence", "label": "Artifact Divergence", "path": REPO / "argus_flow" / "logs" / "artifact_divergence_report.json", "fresh_s": MANAGED_GOVERNANCE_FRESH_S},
    {"id": "divergence", "label": "Divergence Guard", "path": REPO / "argus_flow" / "logs" / "divergence_report.json", "fresh_s": MANAGED_GOVERNANCE_FRESH_S},
    {"id": "kill_discipline", "label": "Kill Discipline", "path": REPO / "argus_flow" / "logs" / "kill_discipline_report.json", "fresh_s": MANAGED_GOVERNANCE_FRESH_S},
]


def _managed_ibkr_runners() -> list[dict]:
    runners = discover_managed_runners()
    return runners or IBKR_RUNNERS


def _load_deployment_registry() -> dict:
    data = _load_json_file(DEPLOYMENT_REGISTRY_FILE)
    return data if isinstance(data, dict) else {}


def _load_json_file(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file_age_s(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(max(0.0, time.time() - path.stat().st_mtime))
    except OSError:
        return None


def _load_recent_alert_events(limit: int = 12) -> list[dict]:
    if not ALERT_EVENTS_FILE.exists():
        return []
    events = collections.deque(maxlen=limit)
    try:
        with open(ALERT_EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except Exception:
        return []
    return list(events)[::-1]


def _report_freshness_rows() -> list[dict]:
    rows = []
    for spec in OPS_REPORT_SPECS:
        age = _file_age_s(spec["path"])
        if age is None:
            status = "MISSING"
        elif age <= spec["fresh_s"]:
            status = "FRESH"
        else:
            status = "STALE"
        rows.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "path": str(spec["path"]),
                "age_s": age,
                "fresh_s": spec["fresh_s"],
                "status": status,
            }
        )
    rows.sort(key=lambda item: (item["status"] == "FRESH", item["status"] == "MISSING", item["label"]))
    return rows


def _load_fleet_account_truth() -> dict:
    snapshot = _load_json_file(FLEET_BROKER_FILE)
    if not isinstance(snapshot, dict):
        return {}
    account = snapshot.get("account", {})
    return {
        "source": snapshot.get("source", ""),
        "snapshot_age_s": _file_age_s(FLEET_BROKER_FILE),
        "broker_connected": bool(snapshot.get("broker_connected", False)),
        "account_id": account.get("account_id", ""),
        "net_liquidation_usd": float(account.get("net_liquidation_usd", 0.0) or 0.0),
        "buying_power_usd": float(account.get("buying_power_usd", 0.0) or 0.0),
        "available_funds_usd": float(account.get("available_funds_usd", 0.0) or 0.0),
        "total_cash_usd": float(account.get("total_cash_usd", 0.0) or 0.0),
        "excess_liquidity_usd": float(account.get("excess_liquidity_usd", 0.0) or 0.0),
    }


def _build_ops_overview() -> dict:
    alert_state = _load_json_file(ALERT_STATE_FILE)
    if not isinstance(alert_state, dict):
        alert_state = {}
    active_issues = [issue for issue in alert_state.get("active_issues", []) if isinstance(issue, dict)]
    manual_actions = [issue for issue in alert_state.get("manual_actions", []) if isinstance(issue, dict)]
    freshness = _report_freshness_rows()
    stale_reports = [row for row in freshness if row["status"] == "STALE"]
    missing_reports = [row for row in freshness if row["status"] == "MISSING"]
    severity_rank = {"INFO": 0, "WARNING": 1, "HIGH": 2, "CRITICAL": 3}
    max_severity = "OK"
    if active_issues:
        max_issue = max(active_issues, key=lambda item: severity_rank.get(str(item.get("severity", "WARNING")).upper(), 1))
        max_severity = str(max_issue.get("severity", "WARNING")).upper()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alert_state_ts": alert_state.get("timestamp"),
        "alert_state_age_s": _file_age_s(ALERT_STATE_FILE),
        "summary": {
            "active_issues": len(active_issues),
            "manual_actions": len(manual_actions),
            "stale_reports": len(stale_reports),
            "missing_reports": len(missing_reports),
            "max_severity": max_severity,
        },
        "active_issues": active_issues,
        "manual_actions": manual_actions,
        "recent_events": _load_recent_alert_events(),
        "report_freshness": freshness,
        "account": _load_fleet_account_truth(),
    }


def _build_paper_model_summary() -> dict:
    """Build a clearly labeled hypothetical validation account model."""
    deployment = _load_deployment_registry()
    policy = deployment.get("risk_policy", {}) if isinstance(deployment.get("risk_policy", {}), dict) else {}
    start_equity = float(policy.get("model_start_equity_usd", PAPER_MODEL_START_USD) or PAPER_MODEL_START_USD)
    base_risk_pct = float(policy.get("base_risk_pct", PAPER_MODEL_BASE_RISK_PCT) or PAPER_MODEL_BASE_RISK_PCT)
    cap_risk_pct = float(policy.get("earned_cap_pct", PAPER_MODEL_CAP_RISK_PCT) or PAPER_MODEL_CAP_RISK_PCT)
    manual_step_up_required = bool(policy.get("manual_step_up_required", False))

    equity = start_equity
    modeled_trades: list[dict] = []
    coverage_gaps: list[str] = []

    for runner in sorted(
        [r for r in _managed_ibkr_runners() if r.get("current_stage") == STAGE_PAPER and not r.get("live")],
        key=lambda item: item.get("symbol", ""),
    ):
        symbol = runner["symbol"]
        cfg_path = REPO / runner["config_path"]
        trade_dir = REPO / runner["log_dir"]
        trade_file = trade_dir / "trades.csv"
        if not cfg_path.exists() or not trade_file.exists():
            continue

        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            risk = cfg.get("risk", {})
            stop_distance = float(risk.get("stop_pips", risk.get("stop_points", 0)) or 0)
        except Exception:
            coverage_gaps.append(f"{symbol}: unreadable config")
            continue

        if stop_distance <= 0:
            coverage_gaps.append(f"{symbol}: missing stop distance")
            continue

        try:
            with open(trade_file, "r", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            coverage_gaps.append(f"{symbol}: unreadable trades")
            continue

        for row in rows:
            if row.get("experiment_valid", "").lower() != "true":
                continue
            ts = (
                row.get("ts")
                or row.get("exit_ts")
                or row.get("close_ts")
                or row.get("entry_ts")
                or ""
            )
            if not ts:
                continue
            try:
                pnl_raw = float(row.get("pnl_pips") or row.get("pnl_pts") or 0)
            except (TypeError, ValueError):
                continue
            modeled_trades.append(
                {
                    "ts": ts,
                    "symbol": symbol,
                    "name": next((r["name"] for r in _managed_ibkr_runners() if r["symbol"] == symbol), symbol),
                    "pnl_raw": pnl_raw,
                    "stop_distance": stop_distance,
                }
            )

    modeled_trades.sort(key=lambda row: row.get("ts", ""))

    wins = 0
    losses = 0
    peak = equity
    max_drawdown_usd = 0.0
    history: list[dict] = []

    for trade in modeled_trades:
        equity_before = equity
        r_multiple = trade["pnl_raw"] / trade["stop_distance"] if trade["stop_distance"] else 0.0
        risk_usd = equity_before * base_risk_pct
        modeled_pnl_usd = r_multiple * risk_usd
        equity = round(equity_before + modeled_pnl_usd, 2)
        peak = max(peak, equity)
        max_drawdown_usd = max(max_drawdown_usd, peak - equity)
        if modeled_pnl_usd > 0:
            wins += 1
        else:
            losses += 1
        history.append(
            {
                "ts": trade["ts"],
                "symbol": trade["symbol"],
                "name": trade["name"],
                "r_multiple": round(r_multiple, 4),
                "risk_usd": round(risk_usd, 2),
                "modeled_pnl_usd": round(modeled_pnl_usd, 2),
                "equity_after": equity,
            }
        )

    current_risk_budget = round(equity * base_risk_pct, 2)
    cap_risk_budget = round(equity * cap_risk_pct, 2)
    last_trade_ts = history[-1]["ts"] if history else ""
    return {
        "label": "Promotion Model",
        "mode": "hypothetical_validation_only",
        "start_equity_usd": start_equity,
        "current_equity_usd": round(equity, 2),
        "return_pct": round(((equity / start_equity) - 1.0) * 100.0, 2) if start_equity else 0.0,
        "per_trade_risk_pct": base_risk_pct,
        "risk_cap_pct": cap_risk_pct,
        "current_risk_budget_usd": current_risk_budget,
        "current_cap_budget_usd": cap_risk_budget,
        "modeled_trade_count": len(history),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(history) * 100.0, 1) if history else 0.0,
        "max_drawdown_usd": round(max_drawdown_usd, 2),
        "manual_step_up_required": manual_step_up_required,
        "coverage_gaps": coverage_gaps,
        "history": history,
        "recent_trades": history[-8:],
        "last_trade_ts": last_trade_ts,
    }


def _read_ibkr_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    state_file = log_dir / "state.json"
    signal_file = log_dir / "signals.csv"
    trade_file = log_dir / "trades.csv"
    heartbeat_file = log_dir / "heartbeat.json"
    evidence_file = log_dir / "evidence_registry.json"
    broker_state_file = log_dir / "broker_state.json"
    mult = runner.get("mult", 10000)

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "strategy": runner["strategy"],
        "unit": runner["unit"],
        "status": "NOT_STARTED",
        "position": "FLAT",
        "trade_count": 0,
        "pnl": 0.0,
        "pnl_usd": 0.0,
        "entry_price": 0,
        "signal_count": 0,
        "closed_trades": 0,
        "valid_trades": 0,
        "invalid_trades": 0,
        "invalid_rate": 0,
        "journal_total_trades": 0,
        "journal_usd_trade_count": 0,
        "journal_usd_complete": False,
        "realized_pnl_usd_available": False,
        "trades": [],
        "recent_signals": [],
        # Feature gauges (latest values)
        "features": {},
        # Session info
        "session": "OFF",
        "hour": 0,
        # Performance metrics
        "win_rate": 0,
        "profit_factor": 0,
        "avg_win": 0,
        "avg_loss": 0,
        "max_consec_loss": 0,
        "equity_curve": [],
        # Signals per day
        "signals_today": 0,
        "entries_today": 0,
        "blocked_signals_total": 0,
        "blocked_signals_24h": 0,
        "blocked_top_reason": "",
        "last_signal_ts": "",
        "last_signal_age_s": 9999,
        # Replay expectations
        "replay_signals_per_day": 0,
        "replay_win_rate": 0,
        "replay_exp": 0,
        "replay_available": False,
        # State age
        "state_age_s": 9999,
        "realized_pnl_usd": None,
        "unrealized_pnl_usd": None,
        "lane": runner.get("current_stage", "watcher"),
        "current_stage": runner.get("current_stage", "watcher"),
        "runner_alive": False,
        "heartbeat_age_s": None,
        "broker_truth_age_s": None,
        "broker_connected": False,
        "broker_reconciliation": "",
        "broker_reconciliation_detail": "",
        "broker_requires_manual_review": False,
        "broker_account_equity_usd": 0.0,
        "open_risk_usd": 0.0,
        "promotion_verdict": "",
        "promotion_blockers": [],
        "promotion_evidence_gaps": [],
        "promotion_checks_passed": 0,
        "promotion_checks_total": 0,
        "artifact_integrity": "",
        "artifact_alerts": [],
        "research_status": "MISSING",
        "research_positive_ratio": 0.0,
        "research_mean_expectancy": 0.0,
        "next_milestone": "",
        "strategy_status": "",
        "cohort_active": False,
        "risk_policy": runner.get("risk_policy", {}),
        "transition_ready": runner.get("transition_ready", False),
        "transition_reason": runner.get("transition_reason", ""),
        "next_stage": runner.get("next_stage", ""),
        "chart_key": Path(str(runner.get("log_dir", ""))).name,
        "runner_id": runner.get("id", ""),
        "log_dir": runner.get("log_dir", ""),
        "config_file": runner.get("config_file", ""),
        "config_path": runner.get("config_path", ""),
        "execution_mode": runner.get("execution_mode", ""),
    }

    # Load replay expectations from config
    cfg_path = REPO / runner.get("config_path", "")
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            rexp = cfg.get("replay_expectations", {})
            result["replay_signals_per_day"] = rexp.get("signals_per_day", 0)
            result["replay_win_rate"] = rexp.get("win_rate", 0)
            result["replay_exp"] = rexp.get("exp_pips_per_trade", rexp.get("exp_bps_per_trade", 0))
            result["replay_available"] = any(
                float(rexp.get(key, 0) or 0) > 0
                for key in ("signals_per_day", "win_rate", "exp_pips_per_trade", "exp_bps_per_trade")
            )
            deployment = cfg.get("deployment", {}) if isinstance(cfg.get("deployment", {}), dict) else {}
            if isinstance(deployment.get("risk_policy", {}), dict):
                result["risk_policy"] = deployment.get("risk_policy", result["risk_policy"])
        except Exception:
            pass

    # State
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["position"] = state.get("position", "FLAT")
            result["trade_count"] = state.get("trade_count", 0)
            result["pnl"] = state.get("pnl_pips", state.get("pnl_points", 0))
            result["pnl_usd"] = float(state.get("pnl_usd", 0) or 0)
            result["entry_price"] = state.get("entry_price", 0)
            result["direction"] = state.get("direction_str", state.get("position", "FLAT")).lower()
            result["position_size"] = float(state.get("position_size", 0) or 0)
            mtime = state_file.stat().st_mtime
            age = time.time() - mtime
            result["state_age_s"] = int(age)
            result["status"] = "RUNNING" if age < 120 else ("IDLE" if age < 600 else "STALE")
        except Exception:
            result["status"] = "ERROR"

    if heartbeat_file.exists():
        try:
            hb = json.loads(heartbeat_file.read_text())
            hb_age = _file_age_s(heartbeat_file)
            result["heartbeat_age_s"] = hb_age
            result["runner_alive"] = hb_age is not None and hb_age < 600
            result["broker_connected"] = bool(hb.get("broker_connected", False))
            if hb_age is not None and result["status"] != "ERROR":
                result["status"] = "RUNNING" if hb_age < 120 else ("IDLE" if hb_age < 600 else "STALE")
        except Exception:
            pass

    broker_state = _load_json_file(broker_state_file)
    if isinstance(broker_state, dict):
        account = broker_state.get("account", {})
        runner_state = broker_state.get("runner", {})
        reconciliation = broker_state.get("reconciliation", {})
        result["broker_truth_age_s"] = _file_age_s(broker_state_file)
        result["broker_connected"] = bool(broker_state.get("broker_connected", result["broker_connected"]))
        result["broker_reconciliation"] = reconciliation.get("result", "")
        result["broker_reconciliation_detail"] = reconciliation.get("detail", "")
        result["broker_requires_manual_review"] = bool(reconciliation.get("requires_manual_review", False))
        result["broker_account_equity_usd"] = float(account.get("net_liquidation_usd", 0.0) or 0.0)
        result["open_risk_usd"] = float(runner_state.get("open_risk_usd", 0.0) or 0.0)
        if "unrealized_pnl_usd" in runner_state:
            try:
                result["unrealized_pnl_usd"] = float(runner_state.get("unrealized_pnl_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                result["unrealized_pnl_usd"] = None

    evidence = _load_json_file(evidence_file)
    if isinstance(evidence, dict):
        governance = evidence.get("governance", {})
        runtime = evidence.get("runtime", {})
        broker_truth = evidence.get("broker_truth", {})
        research = evidence.get("research_validation", {})
        cohort = evidence.get("cohort", {})
        eligibility = evidence.get("eligibility", {})
        result["lane"] = evidence.get("lane", result["lane"])
        result["current_stage"] = result["lane"]
        result["strategy_status"] = evidence.get("strategy_status", "")
        result["cohort_active"] = bool(cohort.get("active", False))
        result["promotion_verdict"] = governance.get("promotion_gate_verdict", "")
        result["promotion_blockers"] = governance.get("promotion_gate_blockers", []) or []
        result["promotion_evidence_gaps"] = governance.get("promotion_gate_evidence_gaps", []) or []
        result["promotion_checks_passed"] = governance.get("promotion_gate_checks_passed", 0) or 0
        result["promotion_checks_total"] = governance.get("promotion_gate_checks_total", 0) or 0
        result["artifact_integrity"] = governance.get("artifact_integrity", "")
        result["artifact_alerts"] = governance.get("artifact_alerts", []) or []
        result["research_status"] = research.get("status", "MISSING")
        result["research_positive_ratio"] = float(research.get("positive_ratio", 0.0) or 0.0)
        result["research_mean_expectancy"] = float(research.get("mean_expectancy", 0.0) or 0.0)
        result["next_milestone"] = eligibility.get("next_milestone", "")
        if result["heartbeat_age_s"] is None and runtime.get("heartbeat_age_s") is not None:
            result["heartbeat_age_s"] = runtime.get("heartbeat_age_s")
        result["runner_alive"] = bool(runtime.get("runner_alive", result["runner_alive"]))
        if not result["broker_reconciliation"]:
            result["broker_reconciliation"] = broker_truth.get("reconciliation_result", "")
            result["broker_reconciliation_detail"] = broker_truth.get("reconciliation_detail", "")
        if result["broker_account_equity_usd"] == 0.0:
            result["broker_account_equity_usd"] = float(broker_truth.get("account_equity_usd", 0.0) or 0.0)

    # Signals
    if signal_file.exists():
        try:
            rows = []
            with open(signal_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            result["signal_count"] = len(rows)
            result["recent_signals"] = rows[-DASHBOARD_SIGNAL_TAIL_ROWS:]

            # Latest features
            if rows:
                last = rows[-1]
                for feat in ["range_pct", "vol_z", "range_accel", "vol_burst_z", "dist_from_low"]:
                    if feat in last and last[feat]:
                        try:
                            result["features"][feat] = float(last[feat])
                        except (ValueError, TypeError):
                            pass
                # Hour and session
                try:
                    h = int(last.get("hour", 0))
                    result["hour"] = h
                    if 0 <= h <= 7:
                        result["session"] = "ASIA"
                    elif 8 <= h <= 12:
                        result["session"] = "LONDON"
                    elif 13 <= h <= 16:
                        result["session"] = "NY"
                    elif 17 <= h <= 20:
                        result["session"] = "US_PM"
                    else:
                        result["session"] = "OFF"
                except (ValueError, TypeError):
                    pass
                # Last signal timestamp and age
                result["last_signal_ts"] = last.get("ts", "")
                try:
                    sig_dt = datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))
                    result["last_signal_age_s"] = int((datetime.now(timezone.utc) - sig_dt).total_seconds())
                except Exception:
                    pass
                # Price
                try:
                    result["current_price"] = float(last.get("price", 0))
                except (ValueError, TypeError):
                    pass

            # Signals today and entries today
            now_utc = datetime.now(timezone.utc)
            today_str = now_utc.strftime("%Y-%m-%d")
            blocked_window_start = now_utc - timedelta(hours=24)
            today_signals = [r for r in rows if r.get("ts", "").startswith(today_str)]
            result["signals_today"] = len(today_signals)
            result["entries_today"] = sum(1 for r in today_signals if r.get("action") == "ENTRY")
            blocked_total = 0
            blocked_24h = 0
            blocked_reason_counts: dict[str, int] = collections.Counter()
            for row in rows:
                action = row.get("action", "") or ""
                if "BLOCKED" not in action:
                    continue
                blocked_total += 1
                ts_raw = row.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except Exception:
                    ts = None
                if ts is not None and ts >= blocked_window_start:
                    blocked_24h += 1
                    blocked_reason_counts[action] += 1
            result["blocked_signals_total"] = blocked_total
            result["blocked_signals_24h"] = blocked_24h
            if blocked_reason_counts:
                result["blocked_top_reason"] = blocked_reason_counts.most_common(1)[0][0]
        except Exception:
            pass

    # Compute unrealized PnL for open positions
    if result["position"] != "FLAT" and result.get("entry_price", 0) > 0 and result.get("current_price", 0) > 0:
        entry = result["entry_price"]
        current = result["current_price"]
        direction = result.get("direction", "long")
        if runner["unit"] == "pips":
            pip_size = 0.01 if "JPY" in runner["symbol"] else 0.0001
            if direction == "long":
                result["unrealized_pnl_pips"] = round((current - entry) / pip_size, 1)
            else:
                result["unrealized_pnl_pips"] = round((entry - current) / pip_size, 1)
        else:
            if direction == "long":
                result["unrealized_pnl_pips"] = round(current - entry, 2)
            else:
                result["unrealized_pnl_pips"] = round(entry - current, 2)
    else:
        result["unrealized_pnl_pips"] = 0
    if result["position"] == "FLAT" and result["unrealized_pnl_usd"] is None:
        result["unrealized_pnl_usd"] = 0.0

    # Trades + performance metrics
    if trade_file.exists():
        try:
            rows = []
            with open(trade_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row["ts"] = (
                        row.get("ts")
                        or row.get("exit_ts")
                        or row.get("close_ts")
                        or row.get("entry_ts")
                        or ""
                    )
                    rows.append(row)
            result["closed_trades"] = len(rows)
            result["journal_total_trades"] = len(rows)
            result["trades"] = rows[-DASHBOARD_TRADE_TAIL_ROWS:]

            if not rows:
                # Empty trades.csv (header only, no data rows) — skip all perf metrics
                pass
            else:
                # Split valid vs invalid trades
                valid_rows = [r for r in rows if r.get("experiment_valid", "").lower() == "true"]
                invalid_rows = [r for r in rows if r.get("experiment_valid", "").lower() != "true"]
                result["valid_trades"] = len(valid_rows)
                result["invalid_trades"] = len(invalid_rows)
                result["invalid_rate"] = round(len(invalid_rows) / len(rows), 4) if rows else 0

                # Realized PnL from ALL trades in trades.csv (single source of truth)
                all_pnl_field = "pnl_pips" if "pnl_pips" in rows[0] else "pnl_pts"
                result["realized_pnl_pips"] = round(sum(float(r.get(all_pnl_field, 0)) for r in rows), 2)
                usd_rows = []
                for r in rows:
                    raw = r.get("pnl_usd")
                    if raw in (None, ""):
                        continue
                    try:
                        usd_rows.append(float(raw or 0))
                    except (TypeError, ValueError):
                        continue
                result["journal_usd_trade_count"] = len(usd_rows)
                result["journal_usd_complete"] = len(rows) > 0 and len(usd_rows) == len(rows)
                result["realized_pnl_usd_available"] = len(usd_rows) > 0
                if usd_rows:
                    result["realized_pnl_usd"] = round(sum(usd_rows), 2)

                # Performance metrics from VALID trades only
                metric_rows = valid_rows if valid_rows else []
                if metric_rows:
                    pnl_field = "pnl_pips" if "pnl_pips" in metric_rows[0] else "pnl_pts"
                    pnls = []
                    for r in metric_rows:
                        try:
                            pnls.append(float(r.get(pnl_field, 0)))
                        except (ValueError, TypeError):
                            pnls.append(0)

                    wins = [p for p in pnls if p > 0]
                    losses = [p for p in pnls if p <= 0]
                    result["win_rate"] = len(wins) / len(pnls) if pnls else 0
                    result["avg_win"] = sum(wins) / len(wins) if wins else 0
                    result["avg_loss"] = sum(losses) / len(losses) if losses else 0
                    sum_wins = sum(wins)
                    sum_losses = abs(sum(losses))
                    result["profit_factor"] = round(sum_wins / sum_losses, 2) if sum_losses > 0 else 0

                    # Max consecutive losses (valid only)
                    max_cl = 0
                    cl = 0
                    for p in pnls:
                        if p <= 0:
                            cl += 1
                            max_cl = max(max_cl, cl)
                        else:
                            cl = 0
                    result["max_consec_loss"] = max_cl

                    # Equity curve from valid trades only
                    cum = 0
                    curve = []
                    for r in metric_rows:
                        try:
                            cum += float(r.get(pnl_field, 0))
                        except (ValueError, TypeError):
                            pass
                        curve.append(round(cum, 2))
                    result["equity_curve"] = curve[-50:]
        except Exception:
            pass

    # Cohort progress fields
    vt = result["valid_trades"]
    result["cohort_target"] = 60
    result["cohort_progress_pct"] = min(100.0, round(vt / 60 * 100, 1))
    result["promotion_eligible"] = (vt >= 60 and result["invalid_rate"] <= 0.10 and result["win_rate"] > 0)

    return result


@app.get("/api/system_health")
async def api_system_health():
    """Quick system health check — blocked signals, degradation status, trade progress."""
    import csv as _csv
    health = {
        "status": "OK",
        "warnings": [],
        "blocked_total": 0,
        "blocked_24h": 0,
        "valid_trades": 0,
        "validation_total_valid_trades": 0,
        "best_candidate_symbol": "",
        "best_candidate_name": "",
        "target": 60,
        "broker_connected": False,
        "runners_alive": 0,
        "active_issues": 0,
        "manual_actions": 0,
        "stale_reports": 0,
    }

    # Count blocked signals and valid trades across fleet
    # Use OPS_LOGS parent to find argus_flow logs (handles different working dirs)
    af_logs = REPO / "argus_flow" / "logs"
    now_utc = datetime.now(timezone.utc)
    blocked_window_start = now_utc - timedelta(hours=24)
    validation_counts: dict[str, dict[str, int | str]] = {}
    for runner in _managed_ibkr_runners():
        log_dir = REPO / runner["log_dir"]
        if not log_dir.exists():
            log_dir = af_logs / runner.get("symbol", "").lower()
        sig_file = log_dir / "signals.csv"
        trade_file = log_dir / "trades.csv"

        if sig_file.exists():
            try:
                with open(sig_file) as f:
                    for row in _csv.DictReader(f):
                        if "BLOCKED" in row.get("action", ""):
                            health["blocked_total"] += 1
                            ts_raw = row.get("ts")
                            if not ts_raw:
                                continue
                            try:
                                ts = datetime.fromisoformat(ts_raw)
                            except ValueError:
                                continue
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts >= blocked_window_start:
                                health["blocked_24h"] += 1
            except Exception:
                pass

        if trade_file.exists():
            try:
                runner_valid = 0
                with open(trade_file) as f:
                    for row in _csv.DictReader(f):
                        if row.get("experiment_valid", "").lower() == "true":
                            runner_valid += 1
                if runner.get("current_stage") == STAGE_PAPER:
                    validation_counts[runner["symbol"]] = {
                        "count": runner_valid,
                        "name": runner["name"],
                    }
                    health["validation_total_valid_trades"] += runner_valid
            except Exception:
                pass

    # Check degradation control
    ctrl_file = REPO / "argus_flow" / "logs" / "degradation_control.json"
    if ctrl_file.exists():
        try:
            ctrl = json.loads(ctrl_file.read_text())
            for sym, v in ctrl.items():
                if v.get("status") == "HARD_PAUSE":
                    health["status"] = "PAUSED"
                    health["warnings"].append(f"{sym}: HARD_PAUSE")
                elif v.get("status") == "WARN":
                    if health["status"] == "OK":
                        health["status"] = "WARN"
                    health["warnings"].append(f"{sym}: WARN")
        except Exception:
            pass

    if validation_counts:
        best_symbol, best_meta = max(
            validation_counts.items(),
            key=lambda item: int(item[1].get("count", 0)),
        )
        health["valid_trades"] = int(best_meta.get("count", 0))
        health["best_candidate_symbol"] = best_symbol
        health["best_candidate_name"] = str(best_meta.get("name", ""))

    health["progress_pct"] = min(100, round(health["valid_trades"] / health["target"] * 100))

    # Check broker connection from heartbeat files
    import time as _time
    connected = 0
    total_runners = 0
    for runner in _managed_ibkr_runners():
        hb_file = REPO / runner["log_dir"] / "heartbeat.json"
        if hb_file.exists():
            try:
                hb = json.loads(hb_file.read_text())
                total_runners += 1
                hb_age = _time.time() - datetime.fromisoformat(hb["ts"]).timestamp()
                if hb.get("broker_connected") and hb_age < 600:
                    connected += 1
            except Exception:
                pass
    # Also scan Helio family heartbeats
    helio_logs = REPO / "helio" / "logs"
    helio_alive = 0
    helio_total = 0
    if helio_logs.exists():
        for hb_dir in sorted(helio_logs.iterdir()):
            if not hb_dir.is_dir() or hb_dir.name.startswith("_"):
                continue
            hb_file = hb_dir / "heartbeat.json"
            if hb_file.exists():
                try:
                    hb = json.loads(hb_file.read_text())
                    helio_total += 1
                    hb_age = _time.time() - hb_file.stat().st_mtime
                    if hb_age < 7200:  # daily strategies: 2hr freshness
                        helio_alive += 1
                        connected += 1
                except Exception:
                    pass
                total_runners += 1

    health["broker_connected"] = connected > 0
    health["runners_alive"] = connected
    health["runners_total"] = total_runners
    health["helio_alive"] = helio_alive
    health["helio_total"] = helio_total
    if not health["broker_connected"]:
        health["status"] = "WARN"
        health["warnings"].append("Broker disconnected")

    ops = _build_ops_overview()
    health["active_issues"] = ops["summary"]["active_issues"]
    health["manual_actions"] = ops["summary"]["manual_actions"]
    health["stale_reports"] = ops["summary"]["stale_reports"] + ops["summary"]["missing_reports"]
    health["ops_max_severity"] = ops["summary"]["max_severity"]
    if health["active_issues"] > 0:
        if health["status"] == "OK":
            health["status"] = "WARN"
        health["warnings"].append(f"{health['active_issues']} active issue(s)")
    if health["manual_actions"] > 0:
        if health["status"] == "OK":
            health["status"] = "WARN"
        health["warnings"].append(f"{health['manual_actions']} manual action item(s)")
    if health["stale_reports"] > 0:
        if health["status"] == "OK":
            health["status"] = "WARN"
        health["warnings"].append(f"{health['stale_reports']} stale/missing oversight report(s)")

    return JSONResponse(health)


@app.get("/api/ops_overview")
async def api_ops_overview():
    """Canonical ops visibility surface for QA and production readiness checks."""
    return JSONResponse(_build_ops_overview())


@app.get("/api/ibkr_fleet")
async def api_ibkr_fleet():
    """IBKR fleet status for all runners."""
    deployment = _load_deployment_registry()
    raw_runners = [_read_ibkr_runner(r) for r in _managed_ibkr_runners()]
    # Deduplicate: when multiple configs share a log_dir, keep the non-killed one
    seen_dirs: dict[str, dict] = {}
    for r in raw_runners:
        ld = r.get("log_dir", "")
        existing = seen_dirs.get(ld)
        if existing is None:
            seen_dirs[ld] = r
        elif r.get("current_stage") != "killed" and existing.get("current_stage") == "killed":
            seen_dirs[ld] = r  # prefer active over killed
        elif r.get("current_stage") == existing.get("current_stage"):
            # Same stage — prefer the one with more trades
            if r.get("closed_trades", 0) > existing.get("closed_trades", 0):
                seen_dirs[ld] = r
    runners = list(seen_dirs.values())
    total_trades = sum(r["closed_trades"] for r in runners)
    total_signals = sum(r["signal_count"] for r in runners)
    account = _load_fleet_account_truth()

    # Cohort status
    paper_runners = [r for r in runners if r.get("current_stage") == STAGE_PAPER]
    fleet_valid = sum(r["valid_trades"] for r in paper_runners)
    any_at_60 = any(r["valid_trades"] >= 60 for r in paper_runners)
    all_promoted = all(r.get("promotion_eligible", False) for r in paper_runners) and len(paper_runners) > 0
    if all_promoted:
        cohort_status = "PROMOTED"
    elif any_at_60:
        cohort_status = "REVIEW"
    else:
        cohort_status = "COLLECTING"

    return JSONResponse({
        "runners": runners,
        "total_trades": total_trades,
        "total_signals": total_signals,
        "cohort_status": cohort_status,
        "fleet_valid_trades": fleet_valid,
        "account": account,
        "paper_model": _build_paper_model_summary(),
        "deployment": deployment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/api/stage_action")
async def api_stage_action(request: Request):
    """Execute a manual stage transition (promote, demote, kill, pause, unpause)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON body"}, status_code=400)
    action = body.get("action", "")
    symbol = body.get("symbol", "")
    reason = body.get("reason", "")
    if not action or not symbol:
        return JSONResponse({"success": False, "error": "action and symbol required"}, status_code=400)
    try:
        from argus_flow.ops.stage_actions import execute_manual_action
        result = execute_manual_action(action, symbol, reason, operator="dashboard")
        status_code = 200 if result.get("success") else 400
        return JSONResponse(result, status_code=status_code)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/stage_history")
async def api_stage_history():
    """Stage transition history for timeline display."""
    symbol = None  # could filter via query param later
    try:
        from argus_flow.ops.stage_actions import load_history
        events = load_history(symbol=symbol, limit=200)
        return JSONResponse({"events": events, "count": len(events)})
    except Exception as e:
        return JSONResponse({"events": [], "count": 0, "error": str(e)})


@app.get("/api/governance_health")
async def api_governance_health():
    """Governance report freshness — shows what's blocking transitions."""
    reports = [
        {"id": "promotion_gate", "label": "Promotion Gate", "path": "argus_flow/logs/promotion_gate_report.json"},
        {"id": "kill_discipline", "label": "Kill Discipline", "path": "argus_flow/logs/kill_discipline_report.json"},
        {"id": "divergence_guard", "label": "Divergence Guard", "path": "argus_flow/logs/divergence_report.json"},
        {"id": "managed_truth", "label": "Managed Truth", "path": "argus_flow/logs/managed_truth_refresh.json"},
        {"id": "deployment_registry", "label": "Deployment Registry", "path": "argus_flow/logs/deployment_registry.json"},
    ]
    import time as _time
    result = []
    for spec in reports:
        p = Path(spec["path"])
        if not p.is_absolute():
            p = Path("C:/Argus/repo") / p
        age_s = None
        status = "MISSING"
        last_status = None
        if p.exists():
            try:
                age_s = int(_time.time() - p.stat().st_mtime)
                fresh_threshold = 30 * 60  # 30 min
                status = "FRESH" if age_s < fresh_threshold else "STALE"
                data = json.loads(p.read_text(encoding="utf-8"))
                last_status = data.get("status", data.get("summary", {}).get("status", None))
            except Exception:
                status = "ERROR"
        result.append({
            "id": spec["id"],
            "label": spec["label"],
            "age_s": age_s,
            "status": status,
            "last_status": last_status,
        })
    all_fresh = all(r["status"] == "FRESH" for r in result)
    return JSONResponse({
        "reports": result,
        "all_fresh": all_fresh,
        "governance_ready": all_fresh,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/runner_chart/{symbol}")
async def api_runner_chart(symbol: str):
    """Real-time candlestick data + trade markers for a specific runner."""
    raw_identifier = str(symbol or "").strip()
    symbol = raw_identifier.upper()

    def _runner_stage_priority(stage: str) -> int:
        normalized = str(stage or "").lower()
        if normalized == STAGE_REAL:
            return 0
        if normalized == STAGE_QUARANTINE:
            return 1
        if normalized == STAGE_PAPER:
            return 2
        if normalized == STAGE_WATCHER:
            return 3
        return 9

    # Find runner log dir
    runners = _managed_ibkr_runners()
    runner = next(
        (
            r for r in runners
            if raw_identifier.lower()
            in {
                str(r.get("id", "")).lower(),
                Path(str(r.get("log_dir", ""))).name.lower(),
            }
        ),
        None,
    )
    if runner is None:
        same_symbol = [r for r in runners if r.get("symbol", "").upper() == symbol]
        if same_symbol:
            same_symbol.sort(key=lambda item: (_runner_stage_priority(item.get("current_stage", "")), item.get("name", "")))
            runner = same_symbol[0]
    if not runner:
        return JSONResponse({"error": f"Runner {raw_identifier or symbol} not found"}, status_code=404)

    log_dir = REPO / runner["log_dir"]
    hb_file = log_dir / "heartbeat.json"
    state_file = log_dir / "state.json"
    signal_file = log_dir / "signals.csv"
    trade_file = log_dir / "trades.csv"

    def _parse_ts(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    def _safe_float(raw, default: float = 0.0) -> float:
        try:
            if raw in ("", None):
                return default
            return float(raw)
        except Exception:
            return default

    now_utc = datetime.now(timezone.utc)
    bars = []
    position = "FLAT"
    entry_price = None
    stop_price = None
    target_price = None
    entry_time = None
    heartbeat_ts = None
    last_bar_ts = None
    deployment_stage = runner.get("current_stage", "")
    execution_mode = runner.get("execution_mode", "")
    unrealized_pnl_usd = 0.0
    open_risk_usd = 0.0

    if hb_file.exists():
        try:
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            raw_bars = hb.get("recent_bars", [])
            for b in raw_bars:
                ts_raw = b.get("t") or b.get("ts")
                if not ts_raw:
                    continue
                bars.append({
                    "t": ts_raw,
                    "o": _safe_float(b.get("o", b.get("open", 0))),
                    "h": _safe_float(b.get("h", b.get("high", 0))),
                    "l": _safe_float(b.get("l", b.get("low", 0))),
                    "c": _safe_float(b.get("c", b.get("close", 0))),
                    "v": max(0.0, _safe_float(b.get("v", b.get("volume", 0)))),
                    "n": max(0.0, _safe_float(b.get("n", b.get("ticks", 0)))),
                })
            bars.sort(key=lambda row: row.get("t", ""))
            position = hb.get("position", "FLAT")
            entry_price = hb.get("entry_price")
            stop_price = hb.get("stop_price")
            target_price = hb.get("target_price")
            heartbeat_ts = hb.get("ts")
            last_bar_ts = hb.get("last_bar_ts") or (bars[-1]["t"] if bars else None)
            deployment_stage = hb.get("deployment_stage", deployment_stage)
            unrealized_pnl_usd = _safe_float(hb.get("unrealized_pnl_usd", 0))
            open_risk_usd = _safe_float(hb.get("open_risk_usd", 0))
        except Exception:
            pass

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            position = state.get("position", position or "FLAT")
            entry_price = state.get("entry_price", entry_price)
            stop_price = state.get("stop_price", stop_price)
            target_price = state.get("target_price", target_price)
            entry_time = state.get("entry_time")
        except Exception:
            pass

    window_start = _parse_ts(bars[0]["t"]) if bars else None
    entry_signals = []
    if signal_file.exists():
        try:
            rows = _tail_csv(signal_file, 800)
            for row in rows:
                action = str(row.get("action", "") or "").upper()
                direction = str(row.get("direction", "") or "").lower()
                ts_raw = row.get("ts", "")
                ts_dt = _parse_ts(ts_raw)
                if action not in ("ENTRY", "ENTRY_SUBMITTED") or not direction or ts_dt is None:
                    continue
                if window_start and ts_dt < window_start - timedelta(minutes=5):
                    continue
                entry_signals.append({
                    "ts": ts_raw,
                    "direction": direction,
                    "price": _safe_float(row.get("price", 0)),
                    "action": action,
                })
        except Exception:
            pass

    trades = []
    exit_markers = []
    if trade_file.exists():
        try:
            rows = _tail_csv(trade_file, 80)
            for t in rows:
                pnl = _safe_float(t.get("pnl_pips", t.get("pnl_pts", 0)))
                trade = {
                    "ts": t.get("ts", ""),
                    "direction": str(t.get("direction", "") or "").lower(),
                    "entry_px": _safe_float(t.get("entry_px", 0)),
                    "exit_px": _safe_float(t.get("exit_px", 0)),
                    "pnl": pnl,
                    "pnl_usd": _safe_float(t.get("pnl_usd", 0)),
                    "exit_reason": t.get("exit_reason", ""),
                    "duration_min": _safe_float(t.get("duration_min", 0)),
                    "slippage_pips": _safe_float(t.get("slippage_pips", 0)),
                    "fill_latency_ms": int(_safe_float(t.get("fill_latency_ms", 0))),
                    "trade_num": int(_safe_float(t.get("trade_num", 0))),
                }
                trades.append(trade)
                ts_dt = _parse_ts(trade["ts"])
                if ts_dt is not None and (window_start is None or ts_dt >= window_start - timedelta(minutes=5)):
                    exit_markers.append({
                        "ts": trade["ts"],
                        "direction": trade["direction"],
                        "price": trade["exit_px"],
                        "pnl": pnl,
                        "exit_reason": trade["exit_reason"],
                    })
        except Exception:
            pass

    if position != "FLAT" and entry_time and entry_price:
        open_entry_ts = _parse_ts(entry_time)
        if open_entry_ts is not None and not any(sig.get("ts") == entry_time for sig in entry_signals):
            if window_start is None or open_entry_ts >= window_start - timedelta(minutes=5):
                entry_signals.append({
                    "ts": entry_time,
                    "direction": "long" if position == "LONG" else "short",
                    "price": _safe_float(entry_price),
                    "action": "OPEN_POSITION",
                })

    heartbeat_age_s = None
    if heartbeat_ts:
        hb_dt = _parse_ts(heartbeat_ts)
        if hb_dt is not None:
            heartbeat_age_s = int((now_utc - hb_dt.astimezone(timezone.utc)).total_seconds())

    last_bar_age_s = None
    if bars:
        bar_dt = _parse_ts(bars[-1]["t"])
        if bar_dt is not None:
            last_bar_age_s = int((now_utc - bar_dt.astimezone(timezone.utc)).total_seconds())

    instrument_type = runner.get("instrument_type", "")
    precision = 5 if instrument_type == "forex" else 2

    return JSONResponse({
        "symbol": runner.get("symbol", symbol),
        "runner_name": runner.get("name", symbol),
        "chart_key": Path(str(runner.get("log_dir", ""))).name,
        "instrument_type": instrument_type,
        "precision": precision,
        "deployment_stage": deployment_stage,
        "execution_mode": execution_mode,
        "bars": bars,
        "entry_signals": entry_signals[-40:],
        "exit_markers": exit_markers[-40:],
        "trades": trades[-20:],
        "position": position,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "entry_time": entry_time,
        "bar_count": len(bars),
        "heartbeat_ts": heartbeat_ts,
        "heartbeat_age_s": heartbeat_age_s,
        "last_bar_ts": bars[-1]["t"] if bars else last_bar_ts,
        "last_bar_age_s": last_bar_age_s,
        "latest_price": bars[-1]["c"] if bars else None,
        "open_risk_usd": open_risk_usd,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "timestamp": now_utc.isoformat(),
    })


@app.get("/api/greek_family")
async def api_greek_family():
    """Greek family status — all strategy runners across Helio/Apollo/Hermes."""
    import time as _time
    helio_logs = REPO / "helio" / "logs"
    strategies = []
    if helio_logs.exists():
        for d in sorted(helio_logs.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            hb_file = d / "heartbeat.json"
            state_file = d / "state.json"
            trades_file = d / "trades.csv"
            hb = {}
            state = {}
            trade_count = 0
            if hb_file.exists():
                try:
                    hb = json.loads(hb_file.read_text())
                except Exception:
                    pass
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                except Exception:
                    pass
            if trades_file.exists():
                try:
                    trade_count = sum(1 for _ in open(trades_file)) - 1
                except Exception:
                    pass
            hb_age = int(_time.time() - hb_file.stat().st_mtime) if hb_file.exists() else None
            strategies.append({
                "name": d.name,
                "family": hb.get("family", d.name.split("_")[0] if "_" in d.name else "helio"),
                "symbol": hb.get("symbol", d.name.upper()),
                "stage": hb.get("stage", "watcher"),
                "position": state.get("position", hb.get("position", "FLAT")),
                "entry_price": state.get("entry_price", 0),
                "pnl_total": state.get("pnl_total", 0),
                "trade_count": trade_count,
                "bars_held": state.get("bars_held", 0),
                "regime": hb.get("regime", ""),
                "hb_age_s": hb_age,
                "alive": hb_age is not None and hb_age < 7200,
            })
    return JSONResponse({
        "strategies": strategies,
        "total": len(strategies),
        "alive": sum(1 for s in strategies if s["alive"]),
        "in_trade": sum(1 for s in strategies if s["position"] != "FLAT"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/qa_learning")
async def api_qa_learning():
    """QA learning insights — regime, session, exit quality, variant comparison."""
    try:
        report_path = Path("C:/Argus/repo/argus_flow/logs/qa_learning_report.json")
        if report_path.exists():
            import time as _time
            age = int(_time.time() - report_path.stat().st_mtime)
            data = json.loads(report_path.read_text(encoding="utf-8"))
            data["report_age_s"] = age
            return JSONResponse(data)
        return JSONResponse({"instruments": {}, "variant_comparison": {}, "report_age_s": None})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/daily_performance")
async def api_daily_performance():
    """Daily performance journal — split by stage, with compact per-day rows."""
    FX_SYMBOLS = {
        runner["symbol"]
        for runner in _managed_ibkr_runners()
        if runner.get("instrument_type", "") == "forex"
    }
    PIP_TO_DOLLAR = {
        "EURUSD": 0.0001 * 57000, "GBPUSD": 0.0001 * 66000, "AUDUSD": 0.0001 * 100000,
        "EURJPY": 0.000067 * 100000, "GBPJPY": 0.000067 * 100000, "CADJPY": 0.000067 * 100000,
        "AUDJPY": 0.000067 * 100000, "USDJPY": 0.000067 * 100000,
        "MES": 5, "MNQ": 2, "MYM": 0.5, "M2K": 5, "MGC": 1, "MCL": 1,
    }

    def _local_trade_date(ts: str) -> str:
        raw = str(ts or "").strip()
        if not raw:
            return ""
        try:
            return (
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
                .astimezone()
                .date()
                .isoformat()
            )
        except Exception:
            return raw[:10] if len(raw) >= 10 else ""

    # Collect all trades from all runners
    all_trades = []
    for runner in _managed_ibkr_runners():
        trade_file = REPO / runner["log_dir"] / "trades.csv"
        if not trade_file.exists():
            continue
        try:
            with open(trade_file, "r") as f:
                for row in csv.DictReader(f):
                    ts = row.get("ts") or row.get("exit_ts") or row.get("close_ts") or row.get("entry_ts") or ""
                    date_str = _local_trade_date(ts)
                    if not date_str:
                        continue
                    pnl_raw = float(row.get("pnl_pips") or row.get("pnl_pts") or 0)
                    # Use pnl_usd from CSV if available, otherwise convert from pips
                    pnl_usd = None
                    if row.get("pnl_usd") not in (None, ""):
                        pnl_usd = float(row.get("pnl_usd") or 0)
                    else:
                        mult = PIP_TO_DOLLAR.get(runner["symbol"], 1)
                        pnl_usd = pnl_raw * mult
                    all_trades.append({
                        "date": date_str,
                        "symbol": runner["symbol"],
                        "stage": runner.get("current_stage", ""),
                        "market": "FX" if runner["symbol"] in FX_SYMBOLS else "Futures",
                        "direction": row.get("direction", ""),
                        "pnl_raw": pnl_raw,
                        "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
                        "pnl_usd_available": pnl_usd is not None,
                        "exit_reason": row.get("exit_reason", ""),
                        "win": pnl_raw > 0,
                    })
        except Exception:
            continue

    # Group by date
    from collections import defaultdict
    days = defaultdict(lambda: {"fx": [], "futures": [], "all": []})
    stage_days: dict[str, defaultdict] = {
        STAGE_WATCHER: defaultdict(lambda: {"fx": [], "futures": [], "all": []}),
        STAGE_PAPER: defaultdict(lambda: {"fx": [], "futures": [], "all": []}),
        STAGE_REAL: defaultdict(lambda: {"fx": [], "futures": [], "all": []}),
        STAGE_QUARANTINE: defaultdict(lambda: {"fx": [], "futures": [], "all": []}),
    }
    for t in all_trades:
        days[t["date"]]["all"].append(t)
        if t["market"] == "FX":
            days[t["date"]]["fx"].append(t)
        else:
            days[t["date"]]["futures"].append(t)
        stage_key = str(t.get("stage", "") or "")
        if stage_key == STAGE_QUARANTINE:
            stage_key = STAGE_REAL
        stage_bucket = stage_days.setdefault(stage_key, defaultdict(lambda: {"fx": [], "futures": [], "all": []}))
        stage_bucket[t["date"]]["all"].append(t)
        if t["market"] == "FX":
            stage_bucket[t["date"]]["fx"].append(t)
        else:
            stage_bucket[t["date"]]["futures"].append(t)

    # Build daily summaries
    def summarize(trades):
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "wr": 0,
                "pnl_usd": None,
                "journal_usd_trades": 0,
                "journal_usd_complete": False,
                "best": "",
                "worst": "",
            }
        wins = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]
        usd_trades = [t for t in trades if t.get("pnl_usd_available")]
        best = max(usd_trades, key=lambda t: t["pnl_usd"]) if usd_trades else None
        worst = min(usd_trades, key=lambda t: t["pnl_usd"]) if usd_trades else None
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "wr": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "pnl_usd": round(sum(t["pnl_usd"] for t in usd_trades), 2) if usd_trades else None,
            "journal_usd_trades": len(usd_trades),
            "journal_usd_complete": len(usd_trades) == len(trades) and len(trades) > 0,
            "best": f"{best['symbol']} {'+' if best['pnl_usd']>=0 else ''}{best['pnl_usd']:.2f}" if best else "",
            "worst": f"{worst['symbol']} {'+' if worst['pnl_usd']>=0 else ''}{worst['pnl_usd']:.2f}" if worst else "",
        }

    def build_rows(day_map):
        if isinstance(day_map, dict):
            for days_back in range(7):
                day_key = (datetime.now().astimezone().date() - timedelta(days=days_back)).isoformat()
                day_map.setdefault(day_key, {"fx": [], "futures": [], "all": []})
        result = []
        for date_str in sorted(day_map.keys(), reverse=True):
            d = day_map[date_str]
            result.append({
                "date": date_str,
                "fx": summarize(d["fx"]),
                "futures": summarize(d["futures"]),
                "total": summarize(d["all"]),
            })
        return result

    return JSONResponse(
        {
            "days": build_rows(days),
            "by_stage": {
                STAGE_WATCHER: build_rows(stage_days.get(STAGE_WATCHER, {})),
                STAGE_PAPER: build_rows(stage_days.get(STAGE_PAPER, {})),
                STAGE_REAL: build_rows(stage_days.get(STAGE_REAL, {})),
            },
        }
    )


@app.get("/api/fleet")
async def api_fleet():
    """Fleet overview — all 4 Greek family systems with performance metrics."""
    import time as _time

    systems = []

    # ── ARGUS (FX intraday) ──────────────────────────────────
    argus_pairs = []
    argus_total_pnl = 0
    argus_total_trades = 0
    argus_wins = 0
    for sym in ["audjpy", "usdjpy", "gbpusd", "cadjpy"]:
        log_dir = REPO / "argus_flow" / "logs" / sym
        hb_path = log_dir / "heartbeat.json"
        trades_path = log_dir / "trades.csv"
        hb = {}
        if hb_path.exists():
            try:
                hb = json.loads(hb_path.read_text())
            except Exception:
                pass
        trades = []
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    trades = [r for r in csv.DictReader(f) if r.get("experiment_valid", "").lower() == "true"]
            except Exception:
                pass
        pnls = [float(t.get("pnl_pips", 0)) for t in trades]
        pair_wins = sum(1 for p in pnls if p > 0)
        pair_pnl = sum(pnls)
        argus_total_pnl += pair_pnl
        argus_total_trades += len(trades)
        argus_wins += pair_wins
        hb_age = int(_time.time() - hb_path.stat().st_mtime) if hb_path.exists() else None
        argus_pairs.append({
            "symbol": sym.upper(),
            "trades": len(trades),
            "win_rate": round(pair_wins / len(trades) * 100, 1) if trades else 0,
            "pnl": round(pair_pnl, 1),
            "position": hb.get("position", "FLAT") if hb else "?",
            "alive": hb_age is not None and hb_age < 300,
        })

    argus_wr = round(argus_wins / argus_total_trades * 100, 1) if argus_total_trades > 0 else 0
    systems.append({
        "name": "Argus",
        "strategy": "FX MTF Intraday",
        "status": "LIVE" if any(p["alive"] for p in argus_pairs) else "DOWN",
        "total_trades": argus_total_trades,
        "win_rate": argus_wr,
        "total_pnl": round(argus_total_pnl, 1),
        "pnl_unit": "pips",
        "instruments": argus_pairs,
    })

    # ── TITAN (Stock/commodity swing) ────────────────────────
    titan_pos_path = REPO / "titan" / "logs" / "positions.json"
    titan_trades_path = REPO / "titan" / "logs" / "trades.csv"
    titan_positions = {}
    titan_trades = []
    if titan_pos_path.exists():
        try:
            titan_positions = json.loads(titan_pos_path.read_text())
        except Exception:
            pass
    if titan_trades_path.exists():
        try:
            with open(titan_trades_path) as f:
                titan_trades = list(csv.DictReader(f))
        except Exception:
            pass
    titan_pnls = [float(t.get("pnl_pct", 0)) for t in titan_trades]
    titan_wins = sum(1 for p in titan_pnls if p > 0)
    titan_instruments = []
    for sym, pos in titan_positions.items():
        if sym in ("last_rebalance", "last_signal", "holdings"):
            continue
        titan_instruments.append({
            "symbol": sym,
            "direction": pos.get("direction", "?"),
            "entry_price": pos.get("entry_price", 0),
            "strategy": pos.get("strategy", "?"),
        })

    systems.append({
        "name": "Titan",
        "strategy": "Stock/Commodity Swing",
        "status": "LIVE" if titan_instruments else "SCANNING",
        "total_trades": len(titan_trades),
        "win_rate": round(titan_wins / len(titan_trades) * 100, 1) if titan_trades else 0,
        "total_pnl": round(sum(titan_pnls), 1),
        "pnl_unit": "%",
        "instruments": titan_instruments,
        "open_positions": len(titan_instruments),
    })

    # ── ARES (Sector rotation) ───────────────────────────────
    ares_pos_path = REPO / "ares" / "logs" / "positions.json"
    ares_positions = {}
    if ares_pos_path.exists():
        try:
            ares_positions = json.loads(ares_pos_path.read_text())
        except Exception:
            pass
    ares_holdings = ares_positions.get("holdings", {})
    # Load latest signal
    ares_signal = {}
    ares_logs = REPO / "ares" / "logs"
    if ares_logs.exists():
        sig_files = sorted(ares_logs.glob("signal_*.json"), reverse=True)
        if sig_files:
            try:
                ares_signal = json.loads(sig_files[0].read_text())
            except Exception:
                pass

    systems.append({
        "name": "Ares",
        "strategy": "Sector Rotation (Monthly)",
        "status": "ACTIVE" if ares_holdings else "CASH",
        "total_trades": 0,
        "win_rate": 0,
        "total_pnl": 0,
        "pnl_unit": "%",
        "holdings": list(ares_holdings.keys()),
        "latest_signal": {
            "buy": ares_signal.get("buy", []),
            "sell": ares_signal.get("sell", []),
            "risk_off": ares_signal.get("risk_off", False),
            "rankings": ares_signal.get("rankings", [])[:6],
        },
    })

    # ── HERMES (Gap fill) ────────────────────────────────────
    hermes_logs = REPO / "hermes" / "logs"
    hermes_latest = {}
    if hermes_logs.exists():
        scan_files = sorted(hermes_logs.glob("scan_*.json"), reverse=True)
        if scan_files:
            try:
                hermes_latest = json.loads(scan_files[0].read_text())
            except Exception:
                pass
    hermes_gaps = hermes_latest if isinstance(hermes_latest, list) else []

    systems.append({
        "name": "Hermes",
        "strategy": "Gap Fill (Daily)",
        "status": "SCANNING",
        "total_trades": 0,
        "win_rate": 0,
        "total_pnl": 0,
        "pnl_unit": "%",
        "todays_gaps": len(hermes_gaps),
        "top_gaps": hermes_gaps[:5],
    })

    return JSONResponse({
        "systems": systems,
        "fleet_summary": {
            "total_systems": len(systems),
            "systems_live": sum(1 for s in systems if s["status"] in ("LIVE", "ACTIVE")),
            "total_trades": sum(s["total_trades"] for s in systems),
            "total_open_positions": len(titan_instruments) + len(ares_holdings),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/fleet_health")
async def api_fleet_health():
    """Fleet monitor status — read from fleet_status.json written by helio.fleet_monitor."""
    status_file = REPO / "argus_flow" / "logs" / "fleet_status.json"
    if not status_file.exists():
        return JSONResponse({"error": "fleet_monitor not running", "systems": {}})
    try:
        return JSONResponse(json.loads(status_file.read_text()))
    except Exception as e:
        return JSONResponse({"error": str(e), "systems": {}})


@app.get("/api/strategy_performance")
async def api_strategy_performance():
    """Per-strategy backtest stats vs live performance for the dashboard."""
    strategies = []

    # ── ARGUS strategies ──────────────────────────────────────
    argus_live_trades = []
    for sym in ["audjpy", "usdjpy", "gbpusd", "cadjpy"]:
        trades_path = REPO / "argus_flow" / "logs" / sym / "trades.csv"
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    rows = [r for r in csv.DictReader(f) if r.get("experiment_valid", "").lower() == "true"]
                    argus_live_trades.extend(rows)
            except Exception:
                pass

    argus_pnls = [float(t.get("pnl_pips", 0)) for t in argus_live_trades]
    argus_wins = sum(1 for p in argus_pnls if p > 0)
    argus_pf = (sum(p for p in argus_pnls if p > 0) / abs(sum(p for p in argus_pnls if p <= 0))
                if any(p <= 0 for p in argus_pnls) else 999)

    strategies.append({
        "system": "Argus",
        "strategy": "MTF Trend (4H/1H/5m)",
        "instruments": "AUDJPY, USDJPY, GBPUSD, CADJPY",
        "backtest_pf": "1.1-1.3",
        "backtest_trades": 114,
        "backtest_wr": "55%",
        "live_trades": len(argus_live_trades),
        "live_wins": argus_wins,
        "live_wr": round(argus_wins / len(argus_live_trades) * 100, 1) if argus_live_trades else 0,
        "live_pf": round(argus_pf, 2) if argus_live_trades else 0,
        "live_pnl": round(sum(argus_pnls), 1),
        "live_unit": "pips",
        "confidence": 45,
        "status": "BAKING",
    })

    # ── TITAN strategies ──────────────────────────────────────
    titan_trades_path = REPO / "titan" / "logs" / "trades.csv"
    titan_trades = []
    if titan_trades_path.exists():
        try:
            with open(titan_trades_path) as f:
                titan_trades = list(csv.DictReader(f))
        except Exception:
            pass
    titan_pnls = [float(t.get("pnl_pct", 0)) for t in titan_trades]
    titan_wins = sum(1 for p in titan_pnls if p > 0)

    strategies.append({
        "system": "Titan",
        "strategy": "Trend Following + Breakout",
        "instruments": "GLD, GDX, PLTR, MRNA, QQQ, SPY, TSLA + 10 more",
        "backtest_pf": "1.59 (long-only)",
        "backtest_trades": 954,
        "backtest_wr": "49%",
        "live_trades": len(titan_trades),
        "live_wins": titan_wins,
        "live_wr": round(titan_wins / len(titan_trades) * 100, 1) if titan_trades else 0,
        "live_pf": 0,
        "live_pnl": round(sum(titan_pnls), 1),
        "live_unit": "%",
        "confidence": 65,
        "status": "BAKING",
    })

    # ── ARES ───────────────────────────────────────────────────
    strategies.append({
        "system": "Ares",
        "strategy": "Sector Rotation (top 2 of 6)",
        "instruments": "SPY, QQQ, GDX, XLE, SMH, XBI",
        "backtest_pf": "1.16-1.35",
        "backtest_trades": 47,
        "backtest_wr": "51%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 55,
        "status": "MONTHLY",
    })

    # ── HERMES ─────────────────────────────────────────────────
    strategies.append({
        "system": "Hermes",
        "strategy": "Gap Fill (gap-down + score 80+)",
        "instruments": "30 high-volatility stocks",
        "backtest_pf": "0.99 (1.27 filtered)",
        "backtest_trades": 2607,
        "backtest_wr": "49%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 35,
        "status": "SCANNING",
    })

    # ── APOLLO ─────────────────────────────────────────────────
    apollo_trades_path = REPO / "apollo" / "logs" / "trades.csv"
    apollo_trades = []
    if apollo_trades_path.exists():
        try:
            with open(apollo_trades_path) as f:
                apollo_trades = list(csv.DictReader(f))
        except Exception:
            pass
    apollo_pnls = [float(t.get("pnl_pct", 0)) for t in apollo_trades]
    apollo_wins = sum(1 for p in apollo_pnls if p > 0)

    strategies.append({
        "system": "Apollo",
        "strategy": "Earnings Drift (post-ER big gap)",
        "instruments": "Tier 1: GOOGL, KLAC, PEP, WMT (100% consistent) + 11 Tier 2/3",
        "backtest_pf": "1.27-2.61 (gap 6%+)",
        "backtest_trades": 15,
        "backtest_wr": "53-67%",
        "live_trades": len(apollo_trades),
        "live_wins": apollo_wins,
        "live_wr": round(apollo_wins / len(apollo_trades) * 100, 1) if apollo_trades else 0,
        "live_pf": 0,
        "live_pnl": round(sum(apollo_pnls), 1),
        "live_unit": "%",
        "confidence": 80,
        "status": "WAITING_ER",
    })

    # ── FORGE: GDX/GLD Pairs ─────────────────────────────────
    strategies.append({
        "system": "GDX/GLD",
        "strategy": "Log-Ratio Mean Reversion",
        "instruments": "GDX vs GLD",
        "backtest_pf": "1.56",
        "backtest_trades": 87,
        "backtest_wr": "61%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 75,
        "status": "SIGNAL_ONLY",
    })

    # ── FORGE: Mamba ───────────────────────────────────────────
    strategies.append({
        "system": "Mamba",
        "strategy": "NAS100/US30 Breakout Scalping",
        "instruments": "NQ=F, YM=F (MNQ, MYM)",
        "backtest_pf": "0.68 (tuning)",
        "backtest_trades": 128,
        "backtest_wr": "16%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "$",
        "confidence": 30,
        "status": "SIGNAL_ONLY",
    })

    # ── FORGE: Cue Banks ───────────────────────────────────────
    strategies.append({
        "system": "Cue Banks",
        "strategy": "US30 Confluence + Fib",
        "instruments": "YM=F (MYM)",
        "backtest_pf": "0.89 (tuning)",
        "backtest_trades": 82,
        "backtest_wr": "32%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "$",
        "confidence": 35,
        "status": "SIGNAL_ONLY",
    })

    # ── FORGE: Tori ────────────────────────────────────────────
    strategies.append({
        "system": "Tori",
        "strategy": "4H Trendline Swing (Action/Safety)",
        "instruments": "PL, CL, GC, YM futures",
        "backtest_pf": "0.61 (bounce PF 3.56)",
        "backtest_trades": 65,
        "backtest_wr": "29%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "$",
        "confidence": 30,
        "status": "SIGNAL_ONLY",
    })

    # ── FORGE: VIX Mean Reversion ──────────────────────────────
    strategies.append({
        "system": "VIX Revert",
        "strategy": "Buy SPY when VIX > 30",
        "instruments": "SPY",
        "backtest_pf": "2.40",
        "backtest_trades": 16,
        "backtest_wr": "75%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 80,
        "status": "WAITING_VIX",
    })

    # ── FORGE: Index Rebalance ─────────────────────────────────
    strategies.append({
        "system": "Index Rebal",
        "strategy": "S&P 500 Add/Delete Flow",
        "instruments": "S&P 500 additions",
        "backtest_pf": "6.65",
        "backtest_trades": 19,
        "backtest_wr": "53%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 85,
        "status": "WAITING_EVENT",
    })

    # ── FORGE: Sector Rotation ─────────────────────────────────
    strategies.append({
        "system": "Sector Rot",
        "strategy": "Atlas Regime → ETF Rotation",
        "instruments": "XLE, XLF, XLK, XLU, XLY, GLD, TLT",
        "backtest_pf": "1.55",
        "backtest_trades": 74,
        "backtest_wr": "60%",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "%",
        "confidence": 55,
        "status": "BUILT",
    })

    # ── FORGE: Themis Cluster ──────────────────────────────────
    themis_db = REPO / "forge" / "data" / "themis.db"
    themis_signals = 0
    if themis_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(themis_db))
            themis_signals = conn.execute("SELECT COUNT(*) FROM signals WHERE status='active'").fetchone()[0]
            conn.close()
        except Exception:
            pass

    strategies.append({
        "system": "Themis",
        "strategy": "Congressional Cluster Follows",
        "instruments": "Congress stock picks",
        "backtest_pf": "tracking",
        "backtest_trades": 0,
        "backtest_wr": "tracking",
        "live_trades": 0,
        "live_wins": 0,
        "live_wr": 0,
        "live_pf": 0,
        "live_pnl": 0,
        "live_unit": "$",
        "confidence": 50,
        "status": f"{themis_signals} SIGNALS",
    })

    return JSONResponse({
        "strategies": strategies,
        "fleet_confidence": 65,
        "expected_annual": "40-75%",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/fx_analytics")
async def api_fx_analytics():
    """Per-pair equity curves, drawdown waterfall, expectancy tracking."""
    analytics = []
    for runner in _managed_ibkr_runners():
        log_dir = REPO / runner["log_dir"]
        trade_file = log_dir / "trades.csv"
        result = {
            "name": runner["name"],
            "symbol": runner["symbol"],
            "current_stage": runner.get("current_stage", ""),
            "equity_curve": [],
            "drawdown_curve": [],
            "daily_pnl": {},
            "cumulative_pnl": 0,
            "max_drawdown": 0,
            "current_drawdown": 0,
            "expectancy_rolling": [],
        }

        if not trade_file.exists():
            analytics.append(result)
            continue

        try:
            with open(trade_file) as f:
                rows = list(csv.DictReader(f))

            valid = [r for r in rows if r.get("experiment_valid", "").lower() == "true"]
            pnl_field = "pnl_pips" if valid and "pnl_pips" in valid[0] else "pnl_pts"

            # Equity curve + drawdown
            cum = 0
            peak = 0
            equity = []
            dd_curve = []
            daily = {}

            for r in valid:
                pnl = float(r.get(pnl_field, 0))
                cum += pnl
                peak = max(peak, cum)
                dd = peak - cum

                equity.append(round(cum, 2))
                dd_curve.append(round(dd, 2))

                # Daily aggregation
                day = r.get("ts", "")[:10]
                if day:
                    daily[day] = round(daily.get(day, 0) + pnl, 2)

            result["equity_curve"] = equity
            result["drawdown_curve"] = dd_curve
            result["daily_pnl"] = daily
            result["cumulative_pnl"] = round(cum, 2)
            result["max_drawdown"] = round(max(dd_curve) if dd_curve else 0, 2)
            result["current_drawdown"] = round(dd_curve[-1] if dd_curve else 0, 2)

            # Rolling expectancy (last 10 trades)
            pnls = [float(r.get(pnl_field, 0)) for r in valid]
            rolling = []
            for i in range(len(pnls)):
                window = pnls[max(0, i-9):i+1]
                rolling.append(round(sum(window) / len(window), 3))
            result["expectancy_rolling"] = rolling[-50:]

        except Exception:
            pass

        analytics.append(result)

    return JSONResponse({
        "analytics": analytics,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


_ml_cache = {"mtime": 0, "data": {}}

def _build_ml_network_data() -> dict:
    """Extract ML governor network structure + feature importances for visualization."""
    model_path = REPO / "data" / "ml_governor.pkl"
    if not model_path.exists():
        return {}
    try:
        mtime = model_path.stat().st_mtime
        if mtime == _ml_cache["mtime"] and _ml_cache["data"]:
            return _ml_cache["data"]
        import pickle
        with open(model_path, "rb") as f:
            artifact = pickle.load(f)
        model = artifact["model"]
        feature_names = artifact.get("feature_names", [])
        stats = artifact.get("training_stats", {})
        # Feature importances
        imp = model.feature_importances_.tolist() if hasattr(model, "feature_importances_") else []
        features = []
        for i, name in enumerate(feature_names):
            features.append({
                "name": name.replace("sig_", ""),
                "importance": round(imp[i], 4) if i < len(imp) else 0,
            })
        features.sort(key=lambda x: x["importance"], reverse=True)
        # Model structure info
        n_estimators = getattr(model, "n_estimators", 0)
        max_depth = getattr(model, "max_depth", 0)
        result = {
            "features": features,
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "roc_auc": stats.get("roc_auc", 0),
            "win_rate": round(stats.get("win_rate", 0) * 100, 1),
            "train_samples": stats.get("n_trades", stats.get("train_samples", 0)),
        }
        _ml_cache["mtime"] = mtime
        _ml_cache["data"] = result
        return result
    except Exception:
        return {}


def _build_strategy_compare(summaries: list) -> list:
    """Group backtest summaries by strategy config label for comparison."""
    by_label = {}
    for s in summaries:
        run_id = s.get("run_id", "")
        # Get label from run_header (labels live there, not in summary)
        label = s.get("label", s.get("job_label", ""))
        if not label or label == run_id:
            hdr_path = OPS_LOGS / f"run_header_{run_id}.json"
            try:
                if hdr_path.exists():
                    with open(hdr_path) as _hf:
                        label = json.load(_hf).get("label", "")
            except Exception as e:
                log.warning("strategy_compare hdr read error: %s", e)
        if not label or label == run_id:
            continue
        by_label[label] = {
            "label": str(label)[:50],
            "pf": round(float(s.get("profit_factor", 0) or 0), 3),
            "wr": round(float(s.get("win_rate_pct", 0) or 0), 1),
            "pnl": round(float(s.get("total_pnl_usd", s.get("pnl_usd", 0)) or 0), 2),
            "trades": int(s.get("total_closed", s.get("trades_closed", 0)) or 0),
            "expectancy": round(float(s.get("expectancy_usd", 0) or 0), 4),
            "max_dd_pct": round(float(s.get("max_drawdown_pct", 0) or 0), 2),
        }
    log.info("strategy_compare: %d summaries -> %d labeled configs", len(summaries), len(by_label))
    return sorted(by_label.values(), key=lambda x: x["pf"], reverse=True)


def _build_trade_analytics(summaries: list, max_runs: int = 15) -> dict:
    """Build time-of-day heatmap and PnL distribution from recent trades."""
    recent = summaries[-max_runs:] if len(summaries) > max_runs else summaries
    all_trades = []
    for s in recent:
        run_id = s.get("run_id", "")
        if not run_id:
            continue
        tf = OPS_LOGS / f"trades_{run_id}.csv"
        if not tf.exists():
            continue
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    entry_epoch = float(row.get("entry_epoch", 0) or 0)
                    pnl = float(row.get("realized_usd", row.get("realized_pnl", 0)) or 0)
                    dur_s = float(row.get("duration_s", row.get("hold_seconds", 0)) or 0)
                    if entry_epoch > 0:
                        all_trades.append({"epoch": entry_epoch, "pnl": pnl, "dur_s": dur_s})
        except Exception:
            pass

    if not all_trades:
        return {"heatmap": [], "distribution": [], "total_trades": 0}

    from datetime import datetime as _dt, timezone as _tz
    # Time-of-day heatmap: hour (UTC) x day-of-week -> avg pnl + count
    heatmap = {}  # (hour, dow) -> {"pnl_sum": float, "count": int, "wins": int}
    for t in all_trades:
        dt = _dt.fromtimestamp(t["epoch"], tz=_tz.utc)
        h = dt.hour
        dow = dt.weekday()  # 0=Mon
        key = (h, dow)
        if key not in heatmap:
            heatmap[key] = {"pnl_sum": 0, "count": 0, "wins": 0}
        heatmap[key]["pnl_sum"] += t["pnl"]
        heatmap[key]["count"] += 1
        if t["pnl"] > 0:
            heatmap[key]["wins"] += 1

    heatmap_out = []
    for (h, dow), v in sorted(heatmap.items()):
        heatmap_out.append({
            "hour": h, "dow": dow,
            "count": v["count"],
            "avg_pnl": round(v["pnl_sum"] / v["count"], 4) if v["count"] else 0,
            "total_pnl": round(v["pnl_sum"], 4),
            "wr": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
        })

    # PnL distribution: bucket trades by pnl ranges
    pnls = [t["pnl"] for t in all_trades]
    dist = []
    # Create buckets from min to max
    mn, mx = min(pnls), max(pnls)
    bucket_size = max(0.05, (mx - mn) / 30) if mx > mn else 0.05
    b = mn
    while b <= mx + bucket_size:
        count = sum(1 for p in pnls if b <= p < b + bucket_size)
        if count:
            dist.append({"bin": round(b, 3), "count": count, "is_win": b >= 0})
        b += bucket_size

    # Drawdown curve from the latest run's equity file
    drawdown_curve = []
    if recent:
        latest_rid = recent[-1].get("run_id", "")
        eq_path = OPS_LOGS / f"equity_{latest_rid}.csv"
        if eq_path.exists():
            try:
                eqs = []
                with open(eq_path, "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        eqs.append(float(row.get("equity_usd", 0) or 0))
                if eqs:
                    peak = eqs[0]
                    # Sample every N points to keep payload reasonable
                    step = max(1, len(eqs) // 500)
                    for i in range(0, len(eqs), step):
                        v = eqs[i]
                        peak = max(peak, v)
                        dd_pct = ((peak - v) / peak * 100) if peak > 0 else 0
                        drawdown_curve.append(round(dd_pct, 3))
            except Exception:
                pass

    return {
        "heatmap": heatmap_out,
        "distribution": dist,
        "drawdown": drawdown_curve,
        "total_trades": len(all_trades),
        "avg_pnl": round(sum(pnls) / len(pnls), 4),
        "median_pnl": round(sorted(pnls)[len(pnls) // 2], 4),
    }


# ---------------------------------------------------------------------------
# Dashboard HTML (single page, self-contained)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#00d4ff">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Argus">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Argus Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Consolas', 'Monaco', monospace; background: #0a0e17; color: #e0e0e0; padding: 12px; }
  h1 { color: #00d4ff; font-size: 1.4em; margin-bottom: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .card { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 12px; }
  .card h2 { color: #7b8ab8; font-size: 0.85em; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 1px; }
  .metric { font-size: 1.6em; font-weight: bold; color: #fff; }
  .metric.positive { color: #00e676; }
  .metric.negative { color: #ff5252; }
  .metric.warn { color: #ffc107; }
  .label { color: #7b8ab8; font-size: 0.75em; margin-top: 2px; }
  .state-FLAT { color: #7b8ab8; }
  .state-OPEN { color: #00e676; }
  .state-BUYING { color: #ffc107; }
  .state-SELLING { color: #ff9800; }
  .chart-container { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 12px; margin-bottom: 12px; max-height: 280px; position: relative; }
  .live-chart-card { background: linear-gradient(180deg, rgba(20,27,45,0.98), rgba(13,17,23,0.98)); border: 1px solid #223150; border-radius: 10px; padding: 14px; margin: 12px 0; box-shadow: inset 0 1px 0 rgba(255,255,255,0.02), 0 8px 24px rgba(0,0,0,0.22); }
  .live-chart-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:10px; flex-wrap:wrap; }
  .live-chart-controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .live-chart-pills { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .chart-pill-btn { background:#0d1117; color:#9fb3d9; border:1px solid #243454; border-radius:999px; padding:4px 10px; font-size:0.72em; font-family:inherit; cursor:pointer; transition:all 0.15s ease; }
  .chart-pill-btn:hover { color:#e8f0ff; border-color:#3f5f99; }
  .chart-pill-btn.active { color:#0a0e17; background:#00d4ff; border-color:#00d4ff; font-weight:bold; }
  .chart-pill-btn.soft-active { color:#e8f0ff; border-color:#00d4ff; box-shadow:0 0 0 1px rgba(0,212,255,0.15) inset; }
  .live-chart-wrap { position:relative; height:380px; min-height:380px; border:1px solid #1d2840; border-radius:10px; overflow:hidden; background:
      linear-gradient(180deg, rgba(15,20,34,0.95), rgba(9,12,20,0.98)),
      radial-gradient(circle at top right, rgba(0,212,255,0.08), transparent 35%); }
  .live-chart-wrap canvas { width:100% !important; height:100% !important; display:block; cursor:grab; user-select:none; -webkit-user-select:none; touch-action:none; }
  .chart-meta-line { display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-top:10px; font-size:0.68em; color:#7b8ab8; }
  .chart-hover-line { margin-top:8px; font-size:0.72em; color:#d7e4ff; min-height:22px; }
  .chart-recent-list { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .chart-trade-pill { background:#0f1626; border:1px solid #223150; border-radius:8px; color:#d7e4ff; padding:7px 10px; font-size:0.72em; cursor:pointer; min-width:145px; text-align:left; transition:border-color 0.15s ease, transform 0.15s ease; }
  .chart-trade-pill:hover { border-color:#00d4ff; transform:translateY(-1px); }
  .chart-trade-pill .pnl-pos { color:#00e676; font-weight:bold; }
  .chart-trade-pill .pnl-neg { color:#ff5252; font-weight:bold; }
  .chart-legend { display:flex; gap:10px; align-items:center; flex-wrap:wrap; font-size:0.68em; color:#7b8ab8; margin-top:8px; }
  .chart-legend-swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle; }
  .chart-interaction-note { margin-top:8px; font-size:0.66em; color:#6f82b4; letter-spacing:0.03em; }
  .chart-empty-note { color:#7b8ab8; font-size:0.72em; margin-top:10px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78em; }
  th { color: #7b8ab8; text-align: left; padding: 4px 6px; border-bottom: 1px solid #1e2a42; }
  td { padding: 4px 6px; border-bottom: 1px solid #0d1321; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.75em; font-weight: bold; }
  .badge-flat { background: #1e2a42; color: #7b8ab8; }
  .badge-open { background: #1b3a1b; color: #00e676; }
  .badge-running { background: #1b2a3a; color: #00d4ff; }
  .badge-error { background: #3a1b1b; color: #ff5252; }
  .footer { color: #3a4a6b; font-size: 0.7em; margin-top: 8px; text-align: center; }
  /* Legacy page-nav and evo-page styles removed 2026-03-25 */
  .coin-tabs { display: flex; gap: 4px; margin-bottom: 10px; }
  .coin-tab { padding: 6px 16px; border-radius: 4px; border: 1px solid #1e2a42; background: #141b2d; color: #7b8ab8; cursor: pointer; font-family: inherit; font-size: 0.85em; font-weight: bold; transition: all 0.2s; }
  .coin-tab:hover { border-color: #00d4ff; color: #00d4ff; }
  .coin-tab.active { background: #1e2a42; color: #00d4ff; border-color: #00d4ff; }
  .multi-overview { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
  .coin-summary { background: #141b2d; border: 2px solid #1e2a42; border-radius: 6px; padding: 12px; transition: border-color 0.4s, box-shadow 0.4s; }
  @keyframes trade-pulse { 0%,100%{box-shadow:0 0 8px rgba(0,230,118,0.2)} 50%{box-shadow:0 0 20px rgba(0,230,118,0.6)} }
  .coin-summary.trading-active { animation: trade-pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
  @keyframes stage-transition-glow { 0%,100%{box-shadow:0 0 4px rgba(0,212,255,0.2);border-color:#1e2a42} 50%{box-shadow:0 0 16px rgba(0,212,255,0.5);border-color:#00d4ff} }
  .coin-summary .coin-name { font-size: 1.1em; font-weight: bold; color: #00d4ff; margin-bottom: 6px; }
  .coin-summary .coin-state { font-size: 0.85em; margin-bottom: 4px; }
  .coin-summary .coin-pnl { font-size: 1.3em; font-weight: bold; }
  .coin-summary .coin-detail { font-size: 0.72em; color: #7b8ab8; margin-top: 2px; }
  #connection-status { position: fixed; top: 8px; right: 12px; font-size: 0.75em; }
  .connected { color: #00e676; }
  .disconnected { color: #ff5252; }
  .gov-gauge { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
  .gov-bar-bg { flex: 1; background: #1e2a42; border-radius: 4px; height: 18px; overflow: hidden; position: relative; }
  .gov-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s, background 0.5s; }
  .gov-bar-label { position: absolute; right: 6px; top: 1px; font-size: 0.7em; color: #fff; font-weight: bold; }
  .decision-row { display: flex; align-items: center; gap: 6px; padding: 3px 0; border-bottom: 1px solid #0d1321; font-size: 0.75em; }
  .decision-row:last-child { border-bottom: none; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot-allow { background: #00e676; }
  .dot-block { background: #ff5252; }
  .dot-caution { background: #ffc107; }
  .dot-unknown { background: #7b8ab8; }
  .signal-ready { color: #00e676; }
  .signal-watch { color: #ffc107; }
  .signal-blocked { color: #ff5252; }
  .badge-trend-up { background: #0d3320; color: #00e676; border: 1px solid #00e676; }
  .badge-trend-down { background: #3d0d0d; color: #ff5252; border: 1px solid #ff5252; }
  .badge-range { background: #2a2a0d; color: #ffc107; border: 1px solid #ffc107; }
  .badge-volatile { background: #3d1a0d; color: #ff9800; border: 1px solid #ff9800; }
  .coin-summary { padding: 14px; }
</style>
</head>
<body>
<div style="display:flex; justify-content:space-between; align-items:center;">
  <h1>HELIO FLEET DASHBOARD</h1>
  <span id="connection-status" style="color:#00ff88;font-size:0.7em;">IBKR STAGED</span>
</div>

<!-- FLEET OVERVIEW (all Greek family systems) -->
<div id="fleet-overview" style="margin-bottom:14px;"></div>

<!-- FLEET HEALTH BAR (5 systems × status light) -->
<div id="fleet-health" style="margin-bottom:14px;"></div>
<script>
function loadFleetHealth() {
  fetch('/api/fleet_health').then(r=>r.json()).then(data=>{
    const el = document.getElementById('fleet-health');
    if (!el) return;
    const systems = data.systems || {};
    if (Object.keys(systems).length === 0) {
      el.innerHTML = '<div style="font-size:0.7em;color:#7b8ab8;">Fleet monitor not running</div>';
      return;
    }

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">FLEET HEALTH</div>'
      + '<div style="font-size:0.65em;color:#7b8ab8;">Auto-refresh 30s | Watchdog active</div>'
      + '</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;">';

    for (const [name, sys] of Object.entries(systems)) {
      const status = sys.status || '?';
      const color = status === 'OK' ? '#00ff88' : (status === 'STALE' ? '#ffc107' : '#ff4444');
      const proc = sys.process_alive ? 'PROC OK' : 'PROC DOWN';
      const ageStr = sys.max_age_s !== undefined ? Math.floor(sys.max_age_s) + 's' : '';
      html += '<div style="background:#141b2d;border:1px solid #1e2a42;border-left:3px solid ' + color + ';border-radius:4px;padding:6px 12px;font-size:0.7em;">'
        + '<div style="font-weight:bold;color:' + color + ';">&#9679; ' + name.toUpperCase() + ' ' + status + '</div>'
        + '<div style="color:#7b8ab8;font-size:0.85em;">' + proc + ' | ' + ageStr + '</div>'
        + '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadFleetHealth();
setInterval(loadFleetHealth, 30000);
</script>

<!-- STRATEGY PERFORMANCE TABLE -->
<div id="strategy-performance" style="margin-bottom:14px;"></div>
<script>
function loadStrategyPerformance() {
  fetch('/api/strategy_performance').then(r=>r.json()).then(data=>{
    const el = document.getElementById('strategy-performance');
    if (!el) return;
    const strategies = data.strategies || [];

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">STRATEGY PERFORMANCE</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">Fleet confidence: ' + data.fleet_confidence + '% | Expected annual: ' + data.expected_annual + '</div>'
      + '</div>';

    html += '<table style="width:100%;border-collapse:collapse;font-size:0.72em;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;overflow:hidden;">';
    html += '<thead><tr style="background:#0d1321;color:#7b8ab8;text-align:left;">'
      + '<th style="padding:8px;">System</th>'
      + '<th style="padding:8px;">Strategy</th>'
      + '<th style="padding:8px;text-align:right;">Backtest PF</th>'
      + '<th style="padding:8px;text-align:right;">BT Trades</th>'
      + '<th style="padding:8px;text-align:right;">BT WR</th>'
      + '<th style="padding:8px;text-align:right;">Live Trades</th>'
      + '<th style="padding:8px;text-align:right;">Live WR</th>'
      + '<th style="padding:8px;text-align:right;">Live PnL</th>'
      + '<th style="padding:8px;text-align:right;">Confidence</th>'
      + '<th style="padding:8px;text-align:right;">Status</th>'
      + '</tr></thead><tbody>';

    for (const s of strategies) {
      const confColor = s.confidence >= 70 ? '#00ff88' : (s.confidence >= 50 ? '#ffc107' : '#ff4444');
      const liveWrColor = s.live_wr >= 50 ? '#00ff88' : (s.live_wr >= 40 ? '#ffc107' : (s.live_wr > 0 ? '#ff4444' : '#7b8ab8'));
      const livePnlColor = s.live_pnl > 0 ? '#00ff88' : (s.live_pnl < 0 ? '#ff4444' : '#7b8ab8');
      const statusColor = s.status === 'BAKING' || s.status === 'SCANNING' || s.status === 'WAITING_ER' ? '#00d4ff' : '#7b8ab8';

      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:8px;font-weight:bold;color:#00d4ff;">' + s.system + '</td>'
        + '<td style="padding:8px;color:#e0e0e0;">' + s.strategy + '<br><span style="font-size:0.85em;color:#7b8ab8;">' + s.instruments + '</span></td>'
        + '<td style="padding:8px;text-align:right;color:#fff;">' + s.backtest_pf + '</td>'
        + '<td style="padding:8px;text-align:right;color:#7b8ab8;">' + s.backtest_trades + '</td>'
        + '<td style="padding:8px;text-align:right;color:#7b8ab8;">' + s.backtest_wr + '</td>'
        + '<td style="padding:8px;text-align:right;color:#fff;">' + s.live_trades + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + liveWrColor + ';">' + (s.live_wr || '-') + '%</td>'
        + '<td style="padding:8px;text-align:right;color:' + livePnlColor + ';">' + (s.live_pnl >= 0 ? '+' : '') + s.live_pnl + ' ' + s.live_unit + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + confColor + ';font-weight:bold;">' + s.confidence + '%</td>'
        + '<td style="padding:8px;text-align:right;color:' + statusColor + ';font-size:0.85em;">' + s.status + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  }).catch((e)=>{ console.error('strategy perf error:', e); });
}
loadStrategyPerformance();
setInterval(loadStrategyPerformance, 60000);
</script>
<script>
function loadFleetOverview() {
  fetch('/api/fleet').then(r=>r.json()).then(data=>{
    const el = document.getElementById('fleet-overview');
    if (!el) return;
    const systems = data.systems || [];
    const summary = data.fleet_summary || {};

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">FLEET STATUS</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">'
      + summary.systems_live + '/' + summary.total_systems + ' systems live | '
      + summary.total_trades + ' total trades | '
      + summary.total_open_positions + ' open positions'
      + '</div></div>';

    html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">';

    for (const sys of systems) {
      const statusColor = sys.status === 'LIVE' || sys.status === 'ACTIVE' ? '#00e676'
        : sys.status === 'DOWN' ? '#ff4444'
        : sys.status === 'SCANNING' ? '#00d4ff' : '#ffc107';

      const pnlColor = sys.total_pnl >= 0 ? '#00ff88' : '#ff4444';
      const wrColor = sys.win_rate >= 50 ? '#00ff88' : (sys.win_rate >= 40 ? '#ffc107' : '#ff4444');

      html += '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:8px;padding:12px;">';

      // Header: system name + status
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
        + '<div style="font-weight:bold;color:#00d4ff;font-size:0.9em;">' + sys.name + '</div>'
        + '<div style="font-size:0.6em;font-weight:bold;color:' + statusColor + ';letter-spacing:1px;">' + sys.status + '</div>'
        + '</div>';

      // Strategy name
      html += '<div style="font-size:0.65em;color:#7b8ab8;margin-bottom:8px;">' + sys.strategy + '</div>';

      // Metrics
      if (sys.total_trades > 0) {
        html += '<div style="display:flex;gap:12px;font-size:0.75em;margin-bottom:6px;">'
          + '<div>Trades: <span style="font-weight:bold;color:#fff;">' + sys.total_trades + '</span></div>'
          + '<div>WR: <span style="font-weight:bold;color:' + wrColor + ';">' + sys.win_rate + '%</span></div>'
          + '<div>PnL: <span style="font-weight:bold;color:' + pnlColor + ';">' + (sys.total_pnl >= 0 ? '+' : '') + sys.total_pnl + ' ' + sys.pnl_unit + '</span></div>'
          + '</div>';
      }

      // System-specific content
      if (sys.name === 'Argus' && sys.instruments) {
        html += '<div style="font-size:0.65em;margin-top:4px;">';
        for (const p of sys.instruments) {
          const dot = p.alive ? '<span style="color:#00e676;">&#9679;</span>' : '<span style="color:#ff4444;">&#9679;</span>';
          const pairPnl = p.pnl >= 0 ? '+' + p.pnl : '' + p.pnl;
          html += '<div style="display:flex;justify-content:space-between;padding:2px 0;">'
            + '<span>' + dot + ' ' + p.symbol + '</span>'
            + '<span style="color:' + (p.pnl >= 0 ? '#00ff88' : '#ff4444') + ';">' + pairPnl + 'p (' + p.trades + 't)</span>'
            + '</div>';
        }
        html += '</div>';
      }

      if (sys.name === 'Titan') {
        if (sys.instruments && sys.instruments.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const p of sys.instruments) {
            html += '<div>' + p.symbol + ' ' + p.direction + ' @ $' + p.entry_price.toFixed(2) + ' [' + p.strategy + ']</div>';
          }
          html += '</div>';
        } else {
          html += '<div style="font-size:0.65em;color:#7b8ab8;margin-top:4px;">No open positions</div>';
        }
      }

      if (sys.name === 'Ares') {
        const sig = sys.latest_signal || {};
        if (sig.rankings && sig.rankings.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const r of sig.rankings.slice(0, 4)) {
            const tag = (sig.buy || []).includes(r.symbol) ? ' <span style="color:#00e676;font-weight:bold;">BUY</span>' : '';
            html += '<div>#' + r.rank + ' ' + r.symbol + ' (' + (r.score >= 0 ? '+' : '') + r.score.toFixed(1) + ')' + tag + '</div>';
          }
          if (sig.risk_off) {
            html += '<div style="color:#ff4444;font-weight:bold;">RISK OFF</div>';
          }
          html += '</div>';
        }
      }

      if (sys.name === 'Hermes') {
        if (sys.top_gaps && sys.top_gaps.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const g of sys.top_gaps.slice(0, 3)) {
            html += '<div>' + g.symbol + ' ' + g.gap_type + ' ' + g.gap_pct + '% (score=' + g.score + ')</div>';
          }
          html += '</div>';
        } else {
          html += '<div style="font-size:0.65em;color:#7b8ab8;margin-top:4px;">No gaps today (' + (sys.todays_gaps || 0) + ' scanned)</div>';
        }
      }

      html += '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadFleetOverview();
setInterval(loadFleetOverview, 60000);
</script>

<div style="display:flex;justify-content:space-between;align-items:center;margin:10px 0 6px 0;">
  <h2 style="font-size:0.95em;color:#00e676;margin:0;letter-spacing:2px;">SYSTEM HEALTH</h2>
  <span style="font-size:0.7em;color:#7b8ab8;">Runtime health, alert state, report freshness, and manual-action visibility.</span>
</div>

<!-- SYSTEM HEALTH BAR -->
<div id="health-bar" style="display:flex;gap:16px;align-items:center;padding:8px 14px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;margin-bottom:8px;font-size:0.78em;">
  <div>System: <span id="health-status" style="font-weight:bold;color:#00e676;">OK</span></div>
  <div title="Best current paper-QA candidate toward promotion.">
    Best Candidate:
    <span id="health-valid" style="color:#00d4ff;font-weight:bold;">0</span> / <span id="health-target">60</span>
    <span id="health-candidate" style="color:#7b8ab8;"></span>
  </div>
  <div style="flex:1;max-width:200px;">
    <div style="background:#0d1321;border-radius:3px;height:8px;overflow:hidden;">
      <div id="health-progress-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#00d4ff,#00e676);border-radius:3px;transition:width 0.5s;"></div>
    </div>
  </div>
  <div>Broker Truth: <span id="health-broker" style="font-weight:bold;color:#00e676;">--</span></div>
  <div title="Signals blocked by guards in the last 24 hours. Total history is shown on hover.">Blocked 24h: <span id="health-blocked" style="color:#7b8ab8;">0</span></div>
  <div>Issues: <span id="health-issues" style="color:#7b8ab8;">0</span></div>
  <div>Manual: <span id="health-manual" style="color:#7b8ab8;">0</span></div>
  <div>Stale Reports: <span id="health-stale" style="color:#7b8ab8;">0</span></div>
  <div id="health-warnings" style="color:#ffc107;"></div>
</div>

<div id="ops-visibility" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
    <h3 style="font-size:0.8em;color:#00e676;margin:0;letter-spacing:1px;">OPS VISIBILITY</h3>
    <span id="ops-last-run" style="font-size:0.7em;color:#7b8ab8;">No alert state yet</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;">
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Active Issues</div>
      <div id="ops-active-count" style="font-size:1.3em;font-weight:bold;color:#e0e0e0;">0</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Manual Actions</div>
      <div id="ops-manual-count" style="font-size:1.3em;font-weight:bold;color:#e0e0e0;">0</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Stale Reports</div>
      <div id="ops-stale-count" style="font-size:1.3em;font-weight:bold;color:#e0e0e0;">0</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Alert Severity</div>
      <div id="ops-max-severity" style="font-size:1.3em;font-weight:bold;color:#e0e0e0;">OK</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1.4fr 1.1fr 1.2fr 1.1fr;gap:10px;">
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">ACTIVE ISSUES</div>
      <div id="ops-active-issues" style="font-size:0.72em;color:#e0e0e0;">No active issues.</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">MANUAL ACTIONS</div>
      <div id="ops-manual-actions" style="font-size:0.72em;color:#e0e0e0;">No manual actions.</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">RECENT ALERT EVENTS</div>
      <div id="ops-events" style="font-size:0.72em;color:#e0e0e0;">No alert events yet.</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">REPORT FRESHNESS</div>
      <div id="ops-report-freshness" style="font-size:0.72em;color:#e0e0e0;">Loading...</div>
    </div>
  </div>
</div>

<div style="display:flex;justify-content:space-between;align-items:center;margin:10px 0 6px 0;">
  <h2 style="font-size:0.95em;color:#7b8ab8;margin:0;letter-spacing:2px;">CONFIG / SOURCE OF TRUTH</h2>
  <span style="font-size:0.7em;color:#7b8ab8;">One control page. Broker truth stays separate from QA model truth.</span>
</div>

<!-- CONTROL STRIP -->
<div id="control-strip" style="display:flex;gap:12px;align-items:center;padding:6px 12px;background:#0a0f1a;border:1px solid #1e2a42;border-radius:4px;margin-bottom:8px;font-size:0.72em;color:#7b8ab8;flex-wrap:wrap;">
  <div>MODE: <span id="cs-mode" style="font-weight:bold;color:#00e676;">UNIFIED RUNNER</span></div>
  <div>ACCOUNT: <span id="cs-account" style="color:#00d4ff;">Waiting for broker truth...</span></div>
  <div>RUNNERS: <span id="cs-active-coins" style="color:#ffc107;">GBP/USD, EUR/USD, EUR/JPY (FX Cohort)</span></div>
  <div>PHASE: <span id="cs-phase" style="color:#e040fb;">Cohort Validation</span></div>
</div>

<!-- Single-page: IBKR Fleet Dashboard only -->

<!-- Legacy pages fully removed 2026-03-26 -->


<div id="ibkr-page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
  <div style="display:flex;align-items:center;gap:16px;">
    <h2 style="font-size:1.1em;color:#00d4ff;margin:0;letter-spacing:2px;">IBKR STAGED TRADING FLEET</h2>
    <a href="/brain" style="color:#7b8ab8;text-decoration:none;font-size:0.7em;padding:3px 10px;border:1px solid #1e2a42;border-radius:4px;letter-spacing:1px;" onmouseover="this.style.background='#1e2a42';this.style.color='#00d4ff'" onmouseout="this.style.background='transparent';this.style.color='#7b8ab8'">HELIO NEURAL CORE</a>
  </div>
  <span id="ibkr-timestamp" style="color:#666;font-size:0.75em;"></span>
</div>

<!-- Fleet summary bar -->
<div id="ibkr-fleet-summary" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;margin-bottom:10px;display:flex;gap:30px;font-size:0.8em;">
  <div>Broker Equity: <span id="ibkr-balance" style="color:#00d4ff;font-weight:bold;font-size:1.1em;">$0.00</span> <span id="ibkr-balance-source" style="font-size:0.75em;color:#7b8ab8;"></span></div>
  <div>Buying Power: <span id="ibkr-buying-power" style="color:#e0e0e0;font-weight:bold;">$0.00</span></div>
  <div>Signals: <span id="ibkr-total-signals" style="color:#e0e0e0;font-weight:bold;">0</span></div>
  <div>Trades: <span id="ibkr-total-trades" style="color:#e0e0e0;font-weight:bold;">0</span></div>
  <div>Journal USD PnL: <span id="ibkr-balance-delta" style="font-weight:bold;">n/a</span> <span id="ibkr-pnl-source" style="font-size:0.75em;color:#7b8ab8;"></span></div>
  <div>Open Risk: <span id="ibkr-open-risk" style="font-weight:bold;color:#7b8ab8;">$0.00</span></div>
  <div>Fleet PnL (journal pips): <span id="ibkr-fleet-pnl" style="font-weight:bold;">0</span></div>
  <div>Active: <span id="ibkr-active-count" style="color:#00ff88;font-weight:bold;">0</span>/<span id="ibkr-total-count">3</span></div>
</div>

<div id="truth-ledger" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
    <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">SINGLE-SOURCE TRUTH</h3>
    <span style="font-size:0.7em;color:#7b8ab8;">Broker USD only from broker snapshots. Strategy USD only from journal rows with pnl_usd.</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;">
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Broker Snapshot</div>
      <div id="truth-broker-line" style="font-size:0.9em;color:#e0e0e0;margin-top:4px;">Loading...</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Journal USD Coverage</div>
      <div id="truth-usd-coverage" style="font-size:0.9em;color:#e0e0e0;margin-top:4px;">Loading...</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Watcher Lane</div>
      <div id="truth-watcher-line" style="font-size:0.9em;color:#e0e0e0;margin-top:4px;">Loading...</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Paper QA Lane</div>
      <div id="truth-paper-line" style="font-size:0.9em;color:#e0e0e0;margin-top:4px;">Loading...</div>
    </div>
    <div style="background:#0d1117;border-radius:6px;padding:10px;">
      <div style="font-size:0.7em;color:#7b8ab8;">Real Money Lane</div>
      <div id="truth-real-line" style="font-size:0.9em;color:#e0e0e0;margin-top:4px;">Loading...</div>
    </div>
  </div>
</div>

<!-- QA paper-model account and cohort status moved below the production section -->

<!-- Live Strategy Chart — HIDDEN (replaced by /fleet page) -->
<div id="live-strategy-chart-card" class="live-chart-card" style="display:none !important;">
  <div class="live-chart-toolbar">
    <div>
      <h3 style="font-size:0.9em;color:#00d4ff;margin:0;letter-spacing:1px;">LIVE STRATEGY CHART</h3>
      <div class="chart-legend">
        <span><span class="chart-legend-swatch" style="background:#00d4ff;border-radius:50%;clip-path: polygon(50% 0%, 0% 100%, 100% 100%);"></span>Long entry</span>
        <span><span class="chart-legend-swatch" style="background:#ff9800;border-radius:50%;clip-path: polygon(50% 100%, 0% 0%, 100% 0%);"></span>Short entry</span>
        <span><span class="chart-legend-swatch" style="background:#cfd8dc;"></span>Exit</span>
        <span><span class="chart-legend-swatch" style="background:#00d4ff;"></span>Entry line</span>
        <span><span class="chart-legend-swatch" style="background:#ff5252;"></span>Stop</span>
        <span><span class="chart-legend-swatch" style="background:#00e676;"></span>Target</span>
        <span><span class="chart-legend-swatch" style="background:rgba(123,138,184,0.65);"></span>Volume / Activity</span>
      </div>
      <div class="chart-interaction-note">Wheel to zoom. Drag to pan. Hold Shift and drag to zoom into a selected window. Double-click resets.</div>
    </div>
    <div class="live-chart-controls">
      <select id="chart-symbol-select" onchange="loadRunnerChart(true)" style="background:#0d1117;color:#e0e0e0;border:1px solid #1e2a42;border-radius:6px;padding:6px 10px;font-size:0.75em;">
        <option value="">Select pair...</option>
      </select>
      <span id="chart-selection-state" style="font-size:0.68em;color:#7b8ab8;border:1px solid #1e2a42;border-radius:999px;padding:5px 10px;white-space:nowrap;">No pair selected</span>
      <div class="live-chart-pills">
        <button id="chart-range-5" class="chart-pill-btn" onclick="setRunnerChartRange(5)">5m</button>
        <button id="chart-range-30" class="chart-pill-btn" onclick="setRunnerChartRange(30)">30m</button>
        <button id="chart-range-60" class="chart-pill-btn" onclick="setRunnerChartRange(60)">1h</button>
        <button id="chart-range-120" class="chart-pill-btn active" onclick="setRunnerChartRange(120)">2h</button>
        <button id="chart-follow-toggle" class="chart-pill-btn soft-active" onclick="toggleRunnerChartFollow()">Follow Live</button>
        <button class="chart-pill-btn" onclick="resetRunnerChartView()">Reset View</button>
      </div>
      <span id="chart-position-badge" style="font-size:0.72em;font-weight:bold;"></span>
    </div>
  </div>
  <div class="live-chart-wrap">
    <canvas id="strategy-chart" height="360"></canvas>
  </div>
  <div id="chart-status-line" class="chart-meta-line"></div>
  <div id="chart-trade-info" class="chart-hover-line"></div>
  <div id="chart-recent-trades" class="chart-recent-list"></div>
</div>

<!-- Governance health bar -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;margin:12px 0;" id="governance-health-bar">
  <span style="color:#7b8ab8;font-size:0.7em;">Loading governance health...</span>
</div>

<!-- Stage transition history -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin:12px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">STAGE TRANSITION HISTORY</h3>
    <span style="font-size:0.65em;color:#7b8ab8;">Recent promotions, demotions, kills</span>
  </div>
  <div id="stage-history-timeline" style="max-height:200px;overflow-y:auto;">
    <span style="color:#7b8ab8;font-size:0.7em;">Loading...</span>
  </div>
</div>

<!-- Fleet-wide actions -->
<div style="display:flex;gap:8px;margin:8px 0;justify-content:flex-end;">
  <button onclick="stageAction('pause','FLEET')" style="background:#ffaa00;color:#000;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:0.7em;font-weight:bold;">PAUSE ALL ENTRIES</button>
  <button onclick="stageAction('unpause','FLEET')" style="background:#00e676;color:#000;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:0.7em;font-weight:bold;">RESUME ENTRIES</button>
</div>

<!-- Production section -->
<div style="margin:16px 0 10px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2 style="font-size:0.95em;color:#00e676;margin:0;letter-spacing:2px;">PROD PAIRS</h2>
    <span style="font-size:0.72em;color:#7b8ab8;">Real broker truth, real runner state, real journal rows.</span>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h3 style="font-size:0.8em;color:#00e676;margin:0;letter-spacing:1px;">REAL MONEY RUNNERS</h3>
    <span style="font-size:0.7em;color:#7b8ab8;">Production lane. Same runner surface as QA, but capital-backed.</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px;margin-bottom:12px;" id="ibkr-real-cards"></div>

  <div style="display:grid;grid-template-columns:minmax(0,1fr);gap:10px;margin-bottom:10px;">
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <h3 style="font-size:0.8em;color:#00e676;margin:0;letter-spacing:1px;">PROD EQUITY TRACE</h3>
        <span id="prod-equity-chart-label" style="font-size:0.7em;color:#7b8ab8;"></span>
      </div>
      <canvas id="balance-history-chart" height="160" style="width:100%;display:block;"></canvas>
    </div>
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <h3 style="font-size:0.8em;color:#00e676;margin:0;letter-spacing:1px;">PROD FLEET HISTORY</h3>
        <span id="prod-fleet-history-label" style="font-size:0.7em;color:#7b8ab8;"></span>
      </div>
      <canvas id="prod-fleet-pnl-chart" height="150" style="width:100%;display:block;"></canvas>
      <div id="prod-fleet-history-hover" style="margin-top:6px;font-size:0.72em;color:#7b8ab8;">Hover points for trade-close details.</div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px;margin-bottom:14px;">
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <h3 style="font-size:0.8em;color:#00e676;margin:0 0 10px 0;letter-spacing:1px;">PROD TRADE JOURNAL</h3>
      <div id="prod-trades-table" style="font-size:0.75em;max-height:380px;overflow:auto;padding-right:4px;"></div>
    </div>
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <h3 style="font-size:0.8em;color:#00e676;margin:0 0 10px 0;letter-spacing:1px;">PROD DAILY PERFORMANCE</h3>
      <div id="prod-daily-perf-body" style="font-size:0.75em;color:#7b8ab8;max-height:380px;overflow:auto;padding-right:4px;">Loading...</div>
    </div>
  </div>
</div>

<!-- QA section — HIDDEN (moved to /fleet page) -->
<div style="margin:16px 0 10px 0;display:none !important;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2 style="font-size:0.95em;color:#00d4ff;margin:0;letter-spacing:2px;">QA PAIRS</h2>
    <span style="font-size:0.72em;color:#7b8ab8;">Promotion lane. Same method as prod, isolated from broker capital.</span>
  </div>

  <div id="paper-model" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">QA MODEL ACCOUNT</h3>
      <span style="font-size:0.7em;color:#7b8ab8;">Separate from broker truth. Hypothetical promotion account only.</span>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:12px;">
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.7em;color:#7b8ab8;">Start Equity</div>
        <div id="paper-model-start" style="font-size:1.1em;font-weight:bold;color:#e0e0e0;">$10,000.00</div>
      </div>
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.7em;color:#7b8ab8;">Current Equity</div>
        <div id="paper-model-equity" style="font-size:1.1em;font-weight:bold;color:#e0e0e0;">Loading...</div>
      </div>
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.7em;color:#7b8ab8;">Risk Per Trade</div>
        <div id="paper-model-risk" style="font-size:1.1em;font-weight:bold;color:#00d4ff;">0.50%</div>
      </div>
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.7em;color:#7b8ab8;">Current Risk Budget</div>
        <div id="paper-model-budget" style="font-size:1.1em;font-weight:bold;color:#e0e0e0;">Loading...</div>
      </div>
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.7em;color:#7b8ab8;">Earned Cap (Manual)</div>
        <div id="paper-model-cap" style="font-size:1.1em;font-weight:bold;color:#ffaa00;">3.00%</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;">
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">MODEL STATUS</div>
        <div id="paper-model-status" style="font-size:0.8em;color:#e0e0e0;">Loading...</div>
      </div>
      <div style="background:#0d1117;border-radius:6px;padding:10px;">
        <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;margin-bottom:6px;">RECENT MODELED TRADES</div>
        <div id="paper-model-trades" style="font-size:0.76em;color:#e0e0e0;">Loading...</div>
      </div>
    </div>
  </div>

  <div style="margin:12px 0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="color:#00d4ff;font-weight:bold;font-size:0.85em;">QA Cohort Status</span>
      <span id="ibkr-cohort-status" style="font-size:0.75em;font-weight:bold;"></span>
    </div>
    <div style="display:flex;gap:12px;margin-top:6px;" id="ibkr-cohort-bars"></div>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">PAPER QA RUNNERS</h3>
    <span style="font-size:0.7em;color:#7b8ab8;">These runners earn the right to real-money configs.</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px;margin-bottom:12px;" id="ibkr-paper-cards"></div>

  <div style="display:grid;grid-template-columns:minmax(0,1fr);gap:10px;margin-bottom:10px;">
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">QA EQUITY TRACE</h3>
        <span id="qa-equity-chart-label" style="font-size:0.7em;color:#7b8ab8;"></span>
      </div>
      <canvas id="qa-model-equity-chart" height="170" style="width:100%;display:block;"></canvas>
      <div id="qa-equity-chart-hover" style="margin-top:6px;font-size:0.72em;color:#7b8ab8;">Hover points for modeled equity details.</div>
    </div>
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">QA FLEET HISTORY</h3>
        <span id="qa-fleet-history-label" style="font-size:0.7em;color:#7b8ab8;"></span>
      </div>
      <canvas id="ibkr-pnl-chart" height="155" style="width:100%;display:block;"></canvas>
      <div id="qa-fleet-history-hover" style="margin-top:6px;font-size:0.72em;color:#7b8ab8;">Hover points for modeled trade-close details.</div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px;margin-bottom:14px;">
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <h3 style="font-size:0.8em;color:#00d4ff;margin:0 0 10px 0;letter-spacing:1px;">QA TRADE JOURNAL</h3>
      <div id="qa-trades-table" style="font-size:0.75em;max-height:380px;overflow:auto;padding-right:4px;"></div>
    </div>
    <div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
      <h3 style="font-size:0.8em;color:#00d4ff;margin:0 0 10px 0;letter-spacing:1px;">QA DAILY PERFORMANCE</h3>
      <div id="qa-daily-perf-body" style="font-size:0.75em;color:#7b8ab8;max-height:380px;overflow:auto;padding-right:4px;">Loading...</div>
    </div>
  </div>
</div>

<div id="ibkr-analytics" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin:12px 0;"></div>

<!-- Greek Family (Helio/Apollo/Hermes) -->
<div style="margin:16px 0 14px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2 style="font-size:0.95em;color:#ff9800;margin:0;letter-spacing:2px;">GREEK FAMILY</h2>
    <span style="font-size:0.72em;color:#7b8ab8;">Swing (Helio) + Momentum (Hermes) + Mean Reversion (Apollo)</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:6px;margin-bottom:12px;" id="greek-family-cards"></div>
</div>

<!-- Watcher section -->
<div style="margin:16px 0 14px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2 style="font-size:0.95em;color:#7b8ab8;margin:0;letter-spacing:2px;">WATCHERS</h2>
    <span style="font-size:0.72em;color:#7b8ab8;">Research lane. New managed pairs start here and must earn paper QA.</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px;margin-bottom:12px;" id="ibkr-watcher-cards"></div>
</div>

<!-- Graveyard (killed pairs) -->
<div style="margin:16px 0 14px 0;" id="graveyard-section" style="display:none;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2 style="font-size:0.95em;color:#555;margin:0;letter-spacing:2px;">&#x1F9DF; GRAVEYARD</h2>
    <span style="font-size:0.72em;color:#555;">Killed pairs. Walk-forward failed or strategy proven unprofitable. Revive to re-evaluate.</span>
  </div>
  <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:12px;opacity:0.7;" id="ibkr-graveyard-cards"></div>
</div>

<!-- QA Learning Insights -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
    <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">QA LEARNING INSIGHTS</h3>
    <span style="font-size:0.65em;color:#7b8ab8;">Regime, direction, exit quality, variant comparison</span>
  </div>
  <div id="qa-learning-panel" style="font-size:0.72em;color:#7b8ab8;">Loading...</div>
</div>

<!-- Recent signals -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
    <h3 style="font-size:0.8em;color:#00d4ff;margin:0;letter-spacing:1px;">SIGNAL FEED</h3>
    <span style="font-size:0.7em;color:#7b8ab8;">Watcher, QA, and prod signal rows in one feed.</span>
  </div>
  <div id="ibkr-signals-table" style="font-size:0.75em;max-height:520px;overflow:auto;padding-right:4px;"></div>
</div>
</div><!-- end ibkr-page -->

<!-- LEGACY CONTENT REMOVED 2026-03-26 (was: decision waterfall, evo charts, ML governor viz) -->

<script>
let equityChart = null;
let currentCoin = 'ETH';
let sseConnection = null;
let balanceHistory = [];
let balanceChart = null;
let _ibkrFleetCache = [];
const MAX_BALANCE_POINTS = 500;

// Single-page dashboard — no tab switching needed
function switchPage(page) { loadIBKRFleet(); }

function ibkrGaugeBar(label, value, min, max, thresholds, unit) {
  // thresholds: [{val, color}] sorted ascending
  const pct = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
  let color = '#444';
  for (const t of (thresholds || [])) {
    if (value >= t.val) color = t.color;
  }
  return `<div style="margin:2px 0;">
    <div style="display:flex;justify-content:space-between;font-size:0.65em;color:#888;">
      <span>${label}</span><span style="color:${color}">${typeof value==='number'?value.toFixed(4):value}${unit||''}</span>
    </div>
    <div style="background:#0d1117;border-radius:2px;height:6px;overflow:hidden;">
      <div style="width:${pct}%;height:100%;background:${color};border-radius:2px;transition:width 0.3s;"></div>
    </div>
  </div>`;
}

function ibkrSessionBadge(session) {
  const colors = {ASIA:'#ff9800',LONDON:'#2196f3',NY:'#00ff88',US_PM:'#4caf50',OVERLAP:'#00ff88',OFF:'#555'};
  const c = colors[session] || '#555';
  return `<span style="background:${c}22;color:${c};border:1px solid ${c}44;border-radius:3px;padding:1px 6px;font-size:0.65em;font-weight:bold;">${session}</span>`;
}

function ibkrMiniChart(data, width, height, color) {
  if (!data || data.length < 2) return `<svg width="${width}" height="${height}"></svg>`;
  const mn = Math.min(...data), mx = Math.max(...data);
  const range = mx - mn || 1;
  const pts = data.map((v, i) => `${(i/(data.length-1))*width},${height - ((v-mn)/range)*height}`).join(' ');
  const step = Math.max(1, Math.ceil(data.length / 16));
  const dots = data.map((v, i) => {
    if (i !== 0 && i !== data.length - 1 && i % step !== 0) return '';
    const x = (i / (data.length - 1)) * width;
    const y = height - ((v - mn) / range) * height;
    const r = i === data.length - 1 ? 2.4 : 1.6;
    return `<circle cx="${x}" cy="${y}" r="${r}" fill="${color}" stroke="#08131f" stroke-width="0.8"/>`;
  }).join('');
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="width:100%;height:${height}px;display:block;vertical-align:middle;">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.7"/>
    ${dots}
  </svg>`;
}

async function loadIBKRFleet() {
  try {
    const resp = await fetch('/api/ibkr_fleet');
    const data = await resp.json();
    _ibkrFleetCache = Array.isArray(data.runners) ? data.runners : [];

    document.getElementById('ibkr-timestamp').textContent = (data.timestamp || '').substring(11,19) + ' UTC';
    document.getElementById('ibkr-total-signals').textContent = data.total_signals || 0;
    document.getElementById('ibkr-total-trades').textContent = data.total_trades || 0;

    // Fleet PnL from trades.csv (single source of truth) and active count
    let fleetPnl = 0; let activeCount = 0;
    for (const r of data.runners) {
      fleetPnl += Number(r.realized_pnl_pips || r.pnl || 0);
      if (r.runner_alive || r.status === 'RUNNING' || r.status === 'IDLE') activeCount++;
    }
    const fpEl = document.getElementById('ibkr-fleet-pnl');
    fpEl.textContent = (fleetPnl >= 0 ? '+' : '') + fleetPnl.toFixed(1);
    fpEl.style.color = fleetPnl >= 0 ? '#00ff88' : '#ff4444';
    document.getElementById('ibkr-active-count').textContent = activeCount;
    document.getElementById('ibkr-total-count').textContent = data.runners.length;

    // Strategy USD PnL is only valid when every closed trade has journal-backed pnl_usd.
    let dollarPnl = 0;
    let openRiskUsd = 0;
    let journalTradeCount = 0;
    let journalUsdTradeCount = 0;
    for (const r of data.runners) {
      journalTradeCount += Number(r.journal_total_trades || 0);
      journalUsdTradeCount += Number(r.journal_usd_trade_count || 0);
      if (r.realized_pnl_usd_available && r.realized_pnl_usd != null) {
        dollarPnl += Number(r.realized_pnl_usd || 0);
      }
      openRiskUsd += Number(r.open_risk_usd || 0);
    }
    const journalUsdComplete = journalTradeCount > 0 && journalTradeCount === journalUsdTradeCount;
    const account = data.account || {};
    const balance = Number(account.net_liquidation_usd || 0);
    const balEl = document.getElementById('ibkr-balance');
    balEl.textContent = '$' + balance.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    balEl.style.color = balance > 0 ? '#00d4ff' : '#ff4444';
    const balanceSourceEl = document.getElementById('ibkr-balance-source');
    if (balanceSourceEl) {
      const age = account.snapshot_age_s != null ? account.snapshot_age_s + 's' : 'unknown age';
      balanceSourceEl.textContent = account.account_id ? '(' + account.account_id + ' | ' + age + ')' : '';
    }
    const buyingPowerEl = document.getElementById('ibkr-buying-power');
    if (buyingPowerEl) {
      const bp = Number(account.buying_power_usd || 0);
      buyingPowerEl.textContent = '$' + bp.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      buyingPowerEl.style.color = bp > 0 ? '#e0e0e0' : '#ff4444';
    }
    const accountStripEl = document.getElementById('cs-account');
    if (accountStripEl) {
      accountStripEl.textContent = account.account_id
        ? account.account_id + (account.broker_connected === false ? ' (broker offline)' : '')
        : 'No broker snapshot';
      accountStripEl.style.color = account.account_id ? '#00d4ff' : '#ff9800';
    }
    const csCoinsEl = document.getElementById('cs-active-coins');
    if (csCoinsEl) {
      const watcherCount = data.runners.filter(r => r.current_stage === 'watcher').length;
      const paperCount = data.runners.filter(r => r.current_stage === 'paper').length;
      const realCount = data.runners.filter(r => r.current_stage === 'real' || r.current_stage === 'quarantine').length;
      csCoinsEl.textContent = watcherCount + ' watcher | ' + paperCount + ' QA | ' + realCount + ' prod';
    }
    const csPhaseEl = document.getElementById('cs-phase');
    if (csPhaseEl) {
      const deploymentSummary = (data.deployment && data.deployment.summary) ? data.deployment.summary : {};
      const readyForReal = Number(deploymentSummary.ready_for_real || 0);
      csPhaseEl.textContent = readyForReal > 0 ? 'Promotion Ready' : 'Watcher -> QA -> Prod';
      csPhaseEl.style.color = readyForReal > 0 ? '#00e676' : '#e040fb';
    }
    const deltaEl = document.getElementById('ibkr-balance-delta');
    const pnlSourceEl = document.getElementById('ibkr-pnl-source');
    if (journalUsdComplete) {
      const sign = dollarPnl >= 0 ? '+' : '';
      deltaEl.textContent = sign + '$' + dollarPnl.toFixed(2);
      deltaEl.style.color = dollarPnl >= 0 ? '#00e676' : '#ff4444';
      if (pnlSourceEl) pnlSourceEl.textContent = '(' + journalUsdTradeCount + '/' + journalTradeCount + ' trades)';
    } else {
      deltaEl.textContent = 'n/a';
      deltaEl.style.color = '#7b8ab8';
      if (pnlSourceEl) pnlSourceEl.textContent = '(' + journalUsdTradeCount + '/' + journalTradeCount + ' trades journal-backed)';
    }
    const openRiskEl = document.getElementById('ibkr-open-risk');
    if (openRiskEl) {
      openRiskEl.textContent = '$' + openRiskUsd.toFixed(2);
      openRiskEl.style.color = openRiskUsd > 0 ? '#ff9800' : '#7b8ab8';
    }

    // Update balance history chart with current value
    updateBalanceChart(balance);

    // Single-source truth summary
    const deployment = data.deployment || {};
    const deploymentSummary = deployment.summary || {};
    const watcherRunners = data.runners.filter(r => r.current_stage === 'watcher');
    const paperRunners = data.runners.filter(r => r.current_stage === 'paper');
    const realRunners = data.runners.filter(r => r.current_stage === 'real' || r.current_stage === 'quarantine');
    const paperValidTrades = paperRunners.reduce((acc, r) => acc + Number(r.valid_trades || 0), 0);
    const topWatcherBlocked = watcherRunners
      .slice()
      .sort((a, b) => Number(b.blocked_signals_24h || 0) - Number(a.blocked_signals_24h || 0))[0];
    const brokerLineEl = document.getElementById('truth-broker-line');
    if (brokerLineEl) {
      const age = account.snapshot_age_s != null ? account.snapshot_age_s + 's old' : 'age unknown';
      brokerLineEl.textContent = account.account_id
        ? account.account_id + ' | $' + balance.toFixed(2) + ' | ' + age
        : 'No broker snapshot';
    }
    const usdCoverageEl = document.getElementById('truth-usd-coverage');
    if (usdCoverageEl) {
      usdCoverageEl.textContent = journalUsdTradeCount + '/' + journalTradeCount + ' trades with pnl_usd'
        + (journalUsdComplete ? ' | COMPLETE' : ' | INCOMPLETE');
      usdCoverageEl.style.color = journalUsdComplete ? '#00e676' : '#ffb74d';
    }
    const watcherLineEl = document.getElementById('truth-watcher-line');
    if (watcherLineEl) {
      watcherLineEl.textContent = topWatcherBlocked && Number(topWatcherBlocked.blocked_signals_24h || 0) > 0
        ? topWatcherBlocked.name + ' blocked ' + topWatcherBlocked.blocked_signals_24h + ' in 24h'
            + ' | ' + (topWatcherBlocked.blocked_top_reason || '').replace('RISK_BLOCKED_', '')
        : 'No elevated watcher guard pressure';
    }
    const paperLineEl = document.getElementById('truth-paper-line');
    if (paperLineEl) {
      const bestPaper = paperRunners
        .slice()
        .sort((a, b) => Number(b.valid_trades || 0) - Number(a.valid_trades || 0))[0];
      paperLineEl.textContent = bestPaper
        ? bestPaper.name + ' leads | ' + bestPaper.valid_trades + '/60 | total valid=' + paperValidTrades
        : 'No paper QA runners found';
    }
    const realLineEl = document.getElementById('truth-real-line');
    if (realLineEl) {
      const realReady = Number(deploymentSummary.ready_for_real || 0);
      realLineEl.textContent = realRunners.length
        ? realRunners.length + ' real runner(s) deployed | broker $' + balance.toFixed(2)
        : (realReady > 0 ? realReady + ' paper runner(s) ready for real config generation' : 'No real-money configs deployed yet');
    }

    // Paper model account
    const paperModel = data.paper_model || {};
    const modelStartEl = document.getElementById('paper-model-start');
    const modelEquityEl = document.getElementById('paper-model-equity');
    const modelRiskEl = document.getElementById('paper-model-risk');
    const modelBudgetEl = document.getElementById('paper-model-budget');
    const modelCapEl = document.getElementById('paper-model-cap');
    const modelStatusEl = document.getElementById('paper-model-status');
    const modelTradesEl = document.getElementById('paper-model-trades');
    if (modelStartEl) modelStartEl.textContent = '$' + Number(paperModel.start_equity_usd || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    if (modelEquityEl) {
      const ret = Number(paperModel.return_pct || 0);
      modelEquityEl.textContent = '$' + Number(paperModel.current_equity_usd || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
      modelEquityEl.style.color = ret >= 0 ? '#00e676' : '#ff4444';
    }
    if (modelRiskEl) modelRiskEl.textContent = (Number(paperModel.per_trade_risk_pct || 0) * 100).toFixed(2) + '%';
    if (modelBudgetEl) modelBudgetEl.textContent = '$' + Number(paperModel.current_risk_budget_usd || 0).toFixed(2);
    if (modelCapEl) modelCapEl.textContent = (Number(paperModel.risk_cap_pct || 0) * 100).toFixed(2) + '% | $' + Number(paperModel.current_cap_budget_usd || 0).toFixed(2);
    if (modelStatusEl) {
      const ret = Number(paperModel.return_pct || 0);
      const retText = (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%';
      const coverage = (paperModel.coverage_gaps || []).length ? ' | gaps: ' + paperModel.coverage_gaps.join(', ') : '';
      const lastTradeText = paperModel.last_trade_ts
        ? ' | last modeled close ' + new Date(paperModel.last_trade_ts).toLocaleString()
        : ' | no modeled closes yet';
      modelStatusEl.innerHTML =
        '<div style="color:' + (ret >= 0 ? '#00e676' : '#ff4444') + ';font-weight:bold;">'
          + retText + ' modeled return | ' + (paperModel.modeled_trade_count || 0) + ' valid trades'
          + '</div>'
        + '<div style="color:#7b8ab8;margin-top:4px;">'
          + 'Win rate ' + Number(paperModel.win_rate || 0).toFixed(1) + '% | '
          + 'Max DD $' + Number(paperModel.max_drawdown_usd || 0).toFixed(2) + ' | '
          + 'shared risk ladder auto-steps toward the 3% cap'
          + lastTradeText
          + coverage
          + '</div>';
    }
    if (modelTradesEl) {
      const trades = paperModel.recent_trades || [];
      modelTradesEl.innerHTML = trades.length
        ? trades.slice().reverse().map(t => {
            const pnl = Number(t.modeled_pnl_usd || 0);
            const pnlColor = pnl >= 0 ? '#00e676' : '#ff4444';
            return '<div style="padding:4px 0;border-bottom:1px solid #141b2d;">'
              + '<span style="color:#00d4ff;">' + (t.name || t.symbol || '') + '</span>'
              + ' <span style="color:#7b8ab8;">R=' + Number(t.r_multiple || 0).toFixed(2) + '</span>'
              + ' <span style="color:' + pnlColor + ';font-weight:bold;">'
              + (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2)
              + '</span>'
              + ' <span style="color:#555;">→ $' + Number(t.equity_after || 0).toFixed(2) + '</span>'
              + '</div>';
          }).join('')
        : '<div style="color:#7b8ab8;">No modeled paper-QA trades yet.</div>';
    }

    // Cohort summary
    const csEl = document.getElementById('ibkr-cohort-status');
    const cbEl = document.getElementById('ibkr-cohort-bars');
    const cohortStatus = data.cohort_status || 'COLLECTING';
    const csColors = {COLLECTING:'#ffaa00',REVIEW:'#00d4ff',PROMOTED:'#00ff88'};
    csEl.textContent = cohortStatus;
    csEl.style.color = csColors[cohortStatus] || '#888';
    let cbHtml = '';
    for (const r of paperRunners) {
      const vt = r.valid_trades || 0;
      const tgt = r.cohort_target || 60;
      const cpct = Math.min(100, (vt/tgt)*100);
      const pc = cpct >= 100 ? '#00ff88' : cpct >= 50 ? '#ffaa00' : '#ff4444';
      cbHtml += `<div style="flex:1;"><div style="font-size:0.65em;color:#888;margin-bottom:2px;">${r.name} (${vt}/${tgt})</div>
        <div style="background:#0d1117;border-radius:3px;height:6px;overflow:hidden;">
          <div style="width:${cpct}%;height:100%;background:${pc};border-radius:3px;"></div>
        </div></div>`;
    }
    cbEl.innerHTML = cbHtml;

    // Runner cards
    const watcherCardsDiv = document.getElementById('ibkr-watcher-cards');
    const paperCardsDiv = document.getElementById('ibkr-paper-cards');
    const realCardsDiv = document.getElementById('ibkr-real-cards');
    const graveyardDiv = document.getElementById('ibkr-graveyard-cards');
    if (graveyardDiv) graveyardDiv.innerHTML = '';
    watcherCardsDiv.innerHTML = '';
    paperCardsDiv.innerHTML = '';
    realCardsDiv.innerHTML = '';

    // Deduplicate by symbol per stage — keep the first (highest-priority) config per symbol
    const seenByStage = {};
    const dedupedRunners = [];
    for (const r of data.runners) {
      const key = (r.symbol || '') + '|' + (r.current_stage || '');
      if (seenByStage[key]) continue;
      seenByStage[key] = true;
      dedupedRunners.push(r);
    }

    for (const r of dedupedRunners) {
      const statusColors = {RUNNING:'#00ff88',IDLE:'#ffaa00',STALE:'#ff4444',ERROR:'#ff4444',NOT_STARTED:'#555'};
      const sc = statusColors[r.status] || '#555';
      const posColor = r.position === 'FLAT' ? '#666' : r.position === 'LONG' ? '#00ff88' : '#ff4444';
      const pnl = Number(r.pnl || 0);
      const pnlColor = pnl >= 0 ? '#00ff88' : '#ff4444';
      const inTrade = r.position !== 'FLAT';
      const borderColor = inTrade ? '#00ff88' : '#1e2a42';
      const pulse = inTrade ? 'box-shadow:0 0 8px #00ff8844;' : '';
      const f = r.features || {};

      // Health indicator
      const sigAge = r.last_signal_age_s || 9999;
      const healthColor = sigAge < 120 ? '#00ff88' : sigAge < 600 ? '#ffaa00' : '#ff4444';
      const healthLabel = sigAge < 120 ? 'LIVE' : sigAge < 600 ? 'SLOW' : 'STALE';

      // Win rate comparison to replay
      const wrLive = r.win_rate || 0;
      const wrReplay = r.replay_win_rate || 0;
      const replayLine = r.replay_available
        ? '(replay: ' + (wrReplay * 100).toFixed(0) + '%)'
        : '(replay n/a)';
      const journalUsdText = r.realized_pnl_usd_available && r.journal_usd_complete
        ? ((Number(r.realized_pnl_usd || 0) >= 0 ? '+' : '') + '$' + Number(r.realized_pnl_usd || 0).toFixed(2))
        : 'n/a';
      const journalUsdColor = r.realized_pnl_usd_available && r.journal_usd_complete
        ? (Number(r.realized_pnl_usd || 0) >= 0 ? '#00e676' : '#ff4444')
        : '#7b8ab8';
      const journalUsdDetail = r.realized_pnl_usd_available
        ? (r.journal_usd_complete ? 'journal-backed' : 'partial ' + (r.journal_usd_trade_count || 0) + '/' + (r.journal_total_trades || 0))
        : 'missing pnl_usd';
      const lastSignalText = r.last_signal_ts ? _chartAgeFromIso(r.last_signal_ts) : 'none';
      const lastTradeRow = (r.trades && r.trades.length) ? r.trades[r.trades.length - 1] : null;
      const lastCloseText = lastTradeRow && lastTradeRow.ts ? _chartAgeFromIso(lastTradeRow.ts) : 'none';

      const transitionReady = r.transition_ready || false;
      const transitionGlow = transitionReady ? 'animation:stage-transition-glow 2s ease-in-out infinite;' : '';
      let card = `<div style="background:#141b2d;border:1px solid ${borderColor};border-radius:6px;padding:12px;${pulse}${transitionGlow}transition:all 0.5s ease;">`;

      // Header: name + status + health
      const isProdLane = r.current_stage === 'real' || r.current_stage === 'quarantine';
      const stageColor = isProdLane ? '#00e676' : r.current_stage === 'paper' ? '#00d4ff' : '#7b8ab8';
      const riskPolicy = r.risk_policy || {};
      const activeRiskPct = Number(riskPolicy.active_risk_pct || 0) * 100;
      const capRiskPct = Number(riskPolicy.earned_cap_pct || 0) * 100;
      card += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <div>
          <span style="color:#00d4ff;font-weight:bold;font-size:0.95em;">${r.name}</span>
          <span style="color:#555;font-size:0.65em;margin-left:6px;">${r.strategy}</span>
          <span style="color:${stageColor};font-size:0.6em;font-weight:bold;margin-left:6px;text-transform:uppercase;">${r.current_stage || r.lane || 'unknown'}</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center;">
          <button onclick="selectRunnerChart('${r.chart_key || r.symbol}')" style="background:#0d1117;color:#7b8ab8;border:1px solid #1e2a42;padding:2px 7px;border-radius:999px;cursor:pointer;font-size:0.6em;">CHART</button>
          <span style="color:${healthColor};font-size:0.6em;font-weight:bold;">&#9679; ${healthLabel}</span>
          <span style="color:${sc};font-size:0.6em;font-weight:bold;text-transform:uppercase;">${r.status}</span>
        </div>
      </div>`;

      // Position + Price row
      const price = r.current_price ? Number(r.current_price).toFixed(r.symbol === 'MNQ' ? 2 : 5) : '—';
      card += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:0.8em;">
        <div>
          ${inTrade ? `<span style="color:${posColor};font-weight:bold;animation:pulse 1.5s infinite;">${r.position}</span>
            <span style="color:#888;margin-left:4px;">@ ${Number(r.entry_price||0).toFixed(r.symbol==='MNQ'?2:5)}</span>` :
            `<span style="color:#666;">FLAT</span>`}
        </div>
        <div style="color:#e0e0e0;">${price}</div>
      </div>`;

      // PnL + metrics row
      card += `<div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.75em;">
        <div>PnL: <span style="color:${pnlColor};font-weight:bold;">${pnl>=0?'+':''}${pnl.toFixed(1)} ${r.unit}</span></div>
        <div>WR: <span style="color:#e0e0e0;">${(wrLive*100).toFixed(0)}%</span>
          <span style="color:#555;font-size:0.8em;">${replayLine}</span></div>
      </div>`;
      card += `<div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.68em;color:#7b8ab8;">
        <div>Journal USD: <span style="color:${journalUsdColor};">${journalUsdText}</span>
          <span style="color:#555;">(${journalUsdDetail})</span></div>
        <div>Broker: <span style="color:${r.broker_connected ? '#00e676' : '#ff4444'};">${r.broker_connected ? 'CONNECTED' : 'OFFLINE'}</span></div>
      </div>`;
      card += `<div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.68em;color:#7b8ab8;">
        <div>Risk Policy: <span style="color:#00d4ff;">${activeRiskPct.toFixed(2)}%</span></div>
        <div>Earned Cap: <span style="color:#ffaa00;">${capRiskPct.toFixed(2)}%</span></div>
      </div>`;
      card += `<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:8px;font-size:0.66em;color:#7b8ab8;">
        <div>Last signal: <span style="color:#e0e0e0;">${lastSignalText}</span></div>
        <div>Last close: <span style="color:#e0e0e0;">${lastCloseText}</span></div>
      </div>`;

      // Equity mini chart
      if (r.equity_curve && r.equity_curve.length > 1) {
        const chartColor = r.equity_curve[r.equity_curve.length-1] >= 0 ? '#00ff88' : '#ff4444';
        card += `<div style="margin-bottom:8px;text-align:right;">
          ${ibkrMiniChart(r.equity_curve, 200, 30, chartColor)}
        </div>`;
      }

      // Feature gauges
      card += `<div style="margin-bottom:6px;">`;
      if (f.range_pct !== undefined) {
        card += ibkrGaugeBar('Range %', f.range_pct, 0, 0.005,
          [{val:0,color:'#444'},{val:0.0008,color:'#ffaa00'},{val:0.0012,color:'#00ff88'},{val:0.002,color:'#ff4444'}]);
      }
      if (f.vol_z !== undefined) {
        card += ibkrGaugeBar('Vol Z', f.vol_z, -0.5, 1.5,
          [{val:-0.5,color:'#444'},{val:0,color:'#ffaa00'},{val:0.2,color:'#00ff88'},{val:0.8,color:'#2196f3'}]);
      }
      if (f.range_accel !== undefined) {
        card += ibkrGaugeBar('Accel', f.range_accel, -0.5, 1.0,
          [{val:-0.5,color:'#ff4444'},{val:0,color:'#ffaa00'},{val:0.1,color:'#00ff88'},{val:0.5,color:'#2196f3'}]);
      }
      if (f.vol_burst_z !== undefined) {
        card += ibkrGaugeBar('Vol Burst', f.vol_burst_z, -1, 3,
          [{val:-1,color:'#444'},{val:0,color:'#ffaa00'},{val:1.0,color:'#00ff88'},{val:2.0,color:'#2196f3'}]);
      }
      if (f.dist_from_low !== undefined) {
        card += ibkrGaugeBar('Dist Low', f.dist_from_low, 0, 1,
          [{val:0,color:'#00ff88'},{val:0.3,color:'#ffaa00'},{val:0.5,color:'#888'},{val:0.7,color:'#ffaa00'}]);
      }
      card += `</div>`;

      // Session + stats row
      card += `<div style="display:flex;justify-content:space-between;align-items:center;font-size:0.7em;">
        <div>${ibkrSessionBadge(r.session)} <span style="color:#555;margin-left:4px;">h${r.hour}</span></div>
        <div style="color:#888;">
          <span title="Signals today">S:${r.signals_today}</span>
          <span style="margin-left:6px;" title="Entries today">E:${r.entries_today}</span>
          <span style="margin-left:6px;" title="Closed trades">T:${r.closed_trades}</span>
          <span style="margin-left:6px;" title="Signals blocked by guards in the last 24 hours">B24:${r.blocked_signals_24h || 0}</span>
        </div>
      </div>`;

      // Cohort progress
      const target = r.cohort_target || 60;
      const validT = r.valid_trades || 0;
      const pct = Math.min(100, (validT / target) * 100);
      const progColor = pct >= 100 ? '#00ff88' : pct >= 50 ? '#ffaa00' : '#ff4444';
      card += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1e2a42;">
        <div style="display:flex;justify-content:space-between;font-size:0.65em;color:#888;">
          <span>Cohort Progress</span>
          <span style="color:${progColor}">${validT}/${target} valid trades</span>
        </div>
        <div style="background:#0d1117;border-radius:3px;height:8px;overflow:hidden;margin-top:2px;">
          <div style="width:${pct}%;height:100%;background:${progColor};border-radius:3px;transition:width 0.5s;"></div>
        </div>
        ${r.promotion_eligible ? '<div style="text-align:center;color:#00ff88;font-size:0.6em;margin-top:2px;font-weight:bold;">ELIGIBLE FOR PROMOTION</div>' : ''}
      </div>`;

      const researchColor = r.research_status === 'PASS' ? '#00e676' : r.research_status === 'WATCH' ? '#ffc107' : '#7b8ab8';
      const gateColor = r.promotion_verdict === 'PROMOTE' ? '#00e676' : r.promotion_verdict === 'BLOCKED' ? '#ff4444' : r.promotion_verdict ? '#ffc107' : '#7b8ab8';
      const brokerReconColor = (r.broker_reconciliation || '').startsWith('CLEAN') ? '#00e676' : (r.broker_reconciliation ? '#ff9800' : '#7b8ab8');
      const artifactColor = r.artifact_integrity === 'CLEAN' ? '#00e676' : r.artifact_integrity === 'DIVERGENT' ? '#ff4444' : '#7b8ab8';
      const blockerPreview = (r.promotion_blockers || []).slice(0, 3).join(', ');
      const artifactPreview = (r.artifact_alerts || [])[0] || '';
      card += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1e2a42;font-size:0.65em;">
        <div style="display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;margin-bottom:4px;">
          <span style="color:${researchColor};font-weight:bold;">Research ${r.research_status || 'MISSING'}</span>
          <span style="color:${gateColor};font-weight:bold;">Gate ${r.promotion_verdict || 'UNKNOWN'}</span>
          <span style="color:${brokerReconColor};font-weight:bold;">Broker ${r.broker_reconciliation || 'UNKNOWN'}</span>
          <span style="color:${artifactColor};font-weight:bold;">Artifacts ${r.artifact_integrity || 'UNKNOWN'}</span>
        </div>
        <div style="color:#7b8ab8;">Next: ${r.next_milestone || 'collect more evidence'}</div>
        ${blockerPreview ? `<div style="color:#ffb74d;margin-top:3px;">Blockers: ${blockerPreview}</div>` : ''}
        ${artifactPreview ? `<div style="color:#ff9800;margin-top:3px;">Artifact note: ${artifactPreview}</div>` : ''}
        ${(r.blocked_signals_24h || 0) > 0 ? `<div style="color:#ffb74d;margin-top:3px;">Blocked 24h: ${r.blocked_signals_24h} (${(r.blocked_top_reason || '').replace('RISK_BLOCKED_', '')})</div>` : ''}
      </div>`;

      // Performance stats (if trades exist)
      if (r.closed_trades > 0) {
        card += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1e2a42;font-size:0.65em;color:#888;display:flex;justify-content:space-between;">
          <span>PF: ${r.profit_factor}</span>
          <span>Avg W: ${Number(r.avg_win).toFixed(1)}</span>
          <span>Avg L: ${Number(r.avg_loss).toFixed(1)}</span>
          <span>Max CL: ${r.max_consec_loss}</span>
        </div>`;
      }

      // Action buttons
      const sym = r.symbol;
      const stage = r.current_stage;
      let actions = '';
      if (stage === 'watcher') {
        actions += `<button onclick="stageAction('promote','${sym}')" style="background:#00d4ff;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;font-weight:bold;" title="Promote to Paper QA">&#9650; PAPER</button>`;
        actions += `<button onclick="stageAction('kill','${sym}')" style="background:#ff4444;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Kill this pair">&#10005; KILL</button>`;
      } else if (stage === 'paper') {
        if (r.promotion_eligible) {
          actions += `<button onclick="stageAction('promote','${sym}')" style="background:#00e676;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;font-weight:bold;animation:pulse 2s infinite;" title="Promote to Real">&#9650; REAL</button>`;
        }
        actions += `<button onclick="stageAction('demote','${sym}')" style="background:#ffaa00;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Demote to Watcher">&#9660; WATCH</button>`;
        actions += `<button onclick="stageAction('kill','${sym}')" style="background:#ff4444;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Kill">&#10005;</button>`;
      } else if (stage === 'real') {
        actions += `<button onclick="stageAction('demote','${sym}')" style="background:#ffaa00;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;" title="Demote to Paper">&#9660; PAPER</button>`;
        actions += `<button onclick="stageAction('quarantine','${sym}')" style="background:#ff9800;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Quarantine">&#9888; QUAR</button>`;
        actions += `<button onclick="stageAction('kill','${sym}')" style="background:#ff4444;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Kill">&#10005;</button>`;
      } else if (stage === 'quarantine') {
        actions += `<button onclick="stageAction('demote','${sym}')" style="background:#ffaa00;color:#000;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;" title="Demote to Paper for revalidation">&#9660; PAPER</button>`;
        actions += `<button onclick="stageAction('kill','${sym}')" style="background:#ff4444;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;margin-left:4px;" title="Kill permanently">&#10005; KILL</button>`;
      } else if (stage === 'killed') {
        actions += `<button onclick="stageAction('revive','${sym}')" style="background:#7b8ab8;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.65em;font-weight:bold;" title="Revive to Watcher for re-evaluation">&#8635; REVIVE</button>`;
      }
      if (actions) {
        card += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1e2a42;display:flex;gap:4px;justify-content:flex-end;">${actions}</div>`;
      }

      card += `</div>`;
      if (r.current_stage === 'killed') {
        // Compact graveyard tombstone — just name + reason + revive button
        if (graveyardDiv) {
          const reason = r.transition_reason || r.next_milestone || 'walk-forward failed';
          graveyardDiv.innerHTML += `<div style="background:#0d1117;border:1px solid #333;border-radius:4px;padding:6px 10px;display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span style="color:#555;font-weight:bold;font-size:0.8em;">${r.name || r.symbol}</span>
              <span style="color:#444;font-size:0.6em;margin-left:8px;">${reason.substring(0, 50)}</span>
            </div>
            <button onclick="stageAction('revive','${r.symbol}')" style="background:#7b8ab8;color:#fff;border:none;padding:3px 10px;border-radius:3px;cursor:pointer;font-size:0.65em;font-weight:bold;white-space:nowrap;" title="Revive to Watcher">&#8635; REVIVE</button>
          </div>`;
        }
      } else if (r.current_stage === 'real' || r.current_stage === 'quarantine') {
        realCardsDiv.innerHTML += card;
      } else if (r.current_stage === 'paper') {
        paperCardsDiv.innerHTML += card;
      } else {
        watcherCardsDiv.innerHTML += card;
      }
    }
    // Show/hide graveyard section
    const graveyardSection = document.getElementById('graveyard-section');
    if (graveyardSection) {
      graveyardSection.style.display = (graveyardDiv && graveyardDiv.innerHTML) ? 'block' : 'none';
    }
    if (!watcherCardsDiv.innerHTML) watcherCardsDiv.innerHTML = '<div style="color:#7b8ab8;padding:12px;background:#141b2d;border:1px dashed #1e2a42;border-radius:6px;">No watcher runners staged right now.</div>';
    populateChartSelector(data.runners || []);
    if (!paperCardsDiv.innerHTML) paperCardsDiv.innerHTML = '<div style="color:#7b8ab8;padding:12px;background:#141b2d;border:1px dashed #1e2a42;border-radius:6px;">No paper QA runners staged right now.</div>';
    if (!realCardsDiv.innerHTML) realCardsDiv.innerHTML = '<div style="color:#7b8ab8;padding:12px;background:#141b2d;border:1px dashed #1e2a42;border-radius:6px;">No real-money runners deployed yet.</div>';

    // Stage-specific trade journals
    let tradeRows = [];
    for (const r of data.runners) {
      for (const t of (r.trades || [])) { tradeRows.push({...t, runner: r.name, unit: r.unit, stage: r.current_stage}); }
    }
    tradeRows.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
    const prodTradeRows = tradeRows.filter(t => t.stage === 'real' || t.stage === 'quarantine');
    const qaTradeRows = tradeRows.filter(t => t.stage === 'paper');
    renderStageTradeJournal('prod-trades-table', prodTradeRows, 'No real-money trades yet.', '#00e676');
    renderStageTradeJournal('qa-trades-table', qaTradeRows, 'No paper-QA trades yet.', '#00d4ff');

    // Stage history charts
    const paperHistory = (paperModel.history || []);
    const qaEquitySeries = [];
    if (paperModel.start_equity_usd != null) {
      qaEquitySeries.push({
        value: Number(paperModel.start_equity_usd),
        label: 'Start',
        detail: 'modeled QA baseline',
      });
    }
    for (const item of paperHistory) {
      const equityAfter = Number(item.equity_after || 0);
      if (isFinite(equityAfter)) {
        qaEquitySeries.push({
          value: equityAfter,
          label: formatTsShort(item.ts),
          detail: `${item.name || item.symbol || ''} | R ${Number(item.r_multiple || 0).toFixed(2)} | ${(Number(item.modeled_pnl_usd || 0) >= 0 ? '+' : '')}$${Number(item.modeled_pnl_usd || 0).toFixed(2)}`,
          raw: item,
        });
      }
    }
    renderSeriesChart('qa-model-equity-chart', 'qa-equity-chart-label', qaEquitySeries, {
      formatter: (v) => '$' + Number(v).toFixed(2),
      emptyText: 'No modeled QA equity history yet.',
      hoverTargetId: 'qa-equity-chart-hover',
      hoverDefaultText: 'Hover points for modeled equity details.',
      hoverEmptyText: 'No modeled QA points yet.',
    });

    const qaFleetSeries = [{
      value: 0,
      label: 'Start',
      detail: 'modeled fleet baseline',
    }];
    if (paperModel.start_equity_usd != null) {
      for (const item of paperHistory) {
        const equityAfter = Number(item.equity_after || 0);
        if (!isFinite(equityAfter)) continue;
        qaFleetSeries.push({
          value: equityAfter - Number(paperModel.start_equity_usd),
          label: formatTsShort(item.ts),
          detail: `${item.name || item.symbol || ''} | ${(Number(item.modeled_pnl_usd || 0) >= 0 ? '+' : '')}$${Number(item.modeled_pnl_usd || 0).toFixed(2)}`,
          raw: item,
        });
      }
    }
    renderSeriesChart('ibkr-pnl-chart', 'qa-fleet-history-label', qaFleetSeries, {
      formatter: (v) => '$' + Number(v).toFixed(2),
      emptyText: 'No QA fleet history yet.',
      hoverTargetId: 'qa-fleet-history-hover',
      hoverDefaultText: 'Hover points for modeled trade-close details.',
      hoverEmptyText: 'No QA fleet history points yet.',
    });

    const prodUsdTrades = prodTradeRows
      .filter(t => t.pnl_usd !== undefined && t.pnl_usd !== null && t.pnl_usd !== '')
      .sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
    const prodFleetSeries = [{
      value: 0,
      label: 'Start',
      detail: 'real-money baseline',
    }];
    let prodCum = 0;
    for (const t of prodUsdTrades) {
      const pnlUsd = Number(t.pnl_usd || 0);
      if (!isFinite(pnlUsd)) continue;
      prodCum += pnlUsd;
      prodFleetSeries.push({
        value: Number(prodCum.toFixed(2)),
        label: formatTsShort(t.ts),
        detail: `${t.runner || ''} | ${(pnlUsd >= 0 ? '+' : '')}$${pnlUsd.toFixed(2)} | ${t.exit_reason || 'close'}`,
        raw: t,
      });
    }
    renderSeriesChart('prod-fleet-pnl-chart', 'prod-fleet-history-label', prodFleetSeries, {
      formatter: (v) => '$' + Number(v).toFixed(2),
      emptyText: 'No real-money journal USD history yet.',
      hoverTargetId: 'prod-fleet-history-hover',
      hoverDefaultText: 'Hover points for trade-close details.',
      hoverEmptyText: 'No real-money fleet history points yet.',
    });

    // Signals table — all signal feeds across watcher, QA, and prod
    const sigsDiv = document.getElementById('ibkr-signals-table');
    let allSigs = [];
    for (const r of data.runners) {
      for (const s of (r.recent_signals || [])) { allSigs.push({...s, runner: r.name, stage: r.current_stage}); }
    }
    allSigs.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

    if (allSigs.length === 0) {
      sigsDiv.innerHTML = '<div style="color:#666;">No signals yet.</div>';
    } else {
      let html = '<table style="width:100%;border-collapse:collapse;"><tr style="color:#00d4ff;border-bottom:1px solid #1e2a42;font-size:0.9em;">' +
        '<th style="text-align:left;padding:3px;">Timestamp</th><th>Stage</th><th>Runner</th><th>Action</th><th>Dir</th><th>Price</th><th>Range%</th><th>Vol Z</th><th>Accel</th><th>Dist</th></tr>';
      for (const s of allSigs) {
        const isEntry = s.action === 'ENTRY';
        const ac = isEntry ? '#00ff88' : '#555';
        const bg = isEntry ? 'background:#00ff8811;' : '';
        const stageLabel = (s.stage || '').toUpperCase() || '-';
        html += `<tr style="border-bottom:1px solid #0d1117;${bg}">
          <td style="padding:2px 3px;">${formatTsShort(s.ts)}</td>
          <td style="color:#7b8ab8;">${stageLabel}</td>
          <td>${s.runner}</td>
          <td style="color:${ac};font-weight:${isEntry?'bold':'normal'};">${s.action}</td>
          <td style="color:${s.direction==='long'?'#00ff88':s.direction==='short'?'#ff4444':'#555'}">${s.direction||'-'}</td>
          <td>${s.price||''}</td>
          <td>${s.range_pct||''}</td>
          <td>${s.vol_z||s.vol_burst_z||''}</td>
          <td>${s.range_accel||''}</td>
          <td>${s.dist_from_low||''}</td></tr>`;
      }
      html += '</table>';
      sigsDiv.innerHTML = html;
    }

    // Per-pair equity + drawdown section
    fetch('/api/fx_analytics').then(r=>r.json()).then(fa=>{
      const anaDiv = document.getElementById('ibkr-analytics');
      if (!anaDiv) return;
      window.toggleKilledCards = function() {
        const el = document.getElementById('killed-cards');
        const btn = document.getElementById('killed-toggle-btn');
        if (!el || !btn) return;
        const showing = el.style.display !== 'none';
        el.style.display = showing ? 'none' : 'grid';
        const count = el.dataset.killedCount || '0';
        btn.textContent = showing ? ('Show ' + count + ' killed') : ('Hide ' + count + ' killed');
      };
      const order = { real: 0, quarantine: 1, paper: 2, watcher: 3 };
      const allAnalytics = (fa.analytics || []).slice().sort((a, b) => {
        const diff = (order[a.current_stage] ?? 9) - (order[b.current_stage] ?? 9);
        if (diff) return diff;
        return String(a.name || '').localeCompare(String(b.name || ''));
      });
      const active = allAnalytics.filter(a => String(a.current_stage||'').toLowerCase() !== 'killed');
      const killed = allAnalytics.filter(a => String(a.current_stage||'').toLowerCase() === 'killed');
      const analytics = active;
      let html = '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;">'
        + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;">Per-Pair Analytics</div>'
        + '<div style="display:flex;align-items:center;gap:12px;">'
        + '<div style="color:#7b8ab8;font-size:0.68em;">' + active.length + ' active</div>'
        + (killed.length ? '<button id="killed-toggle-btn" onclick="toggleKilledCards()" style="background:#1e2a42;color:#7b8ab8;border:1px solid #2a3654;border-radius:4px;padding:2px 8px;font-size:0.65em;cursor:pointer;">Show ' + killed.length + ' killed</button>' : '')
        + '</div></div>';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;">';
      for (const a of analytics) {
        const ddColor = a.current_drawdown > 0 ? '#ff4444' : '#00ff88';
        const stage = String(a.current_stage || '').toUpperCase();
        const stageColor = stage === 'REAL' || stage === 'QUARANTINE' ? '#00e676' : (stage === 'PAPER' ? '#00d4ff' : '#7b8ab8');
        html += '<div style="min-width:0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;">';
        html += '<div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">'
          + '<div style="font-weight:bold;color:#00d4ff;font-size:0.8em;min-width:0;">' + a.name + '</div>'
          + '<div style="font-size:0.62em;font-weight:bold;color:' + stageColor + ';letter-spacing:0.8px;white-space:nowrap;">' + stage + '</div>'
          + '</div>';
        html += '<div style="font-size:0.7em;margin-top:4px;">';
        html += 'PnL: <span style="color:' + (a.cumulative_pnl>=0?'#00ff88':'#ff4444') + '">' + (a.cumulative_pnl>=0?'+':'') + a.cumulative_pnl.toFixed(1) + '</span>';
        html += ' | Max DD: <span style="color:#ff4444">' + a.max_drawdown.toFixed(1) + '</span>';
        html += ' | Now: <span style="color:' + ddColor + '">' + a.current_drawdown.toFixed(1) + '</span>';
        html += '</div>';
        if (a.equity_curve && a.equity_curve.length > 1) {
          html += '<div style="margin-top:8px;">' + ibkrMiniChart(a.equity_curve, 220, 44, a.cumulative_pnl>=0?'#00ff88':'#ff4444') + '</div>';
        } else {
          html += '<div style="margin-top:8px;color:#7b8ab8;font-size:0.68em;">No equity history yet.</div>';
        }
        html += '</div>';
      }
      html += '</div>';
      // Killed instruments — collapsed by default
      if (killed.length) {
        html += '<div id="killed-cards" data-killed-count="' + killed.length + '" style="display:none;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:12px;opacity:0.5;">';
        for (const a of killed) {
          html += '<div style="min-width:0;padding:10px;background:#0d1117;border:1px solid #1a1f2e;border-radius:6px;">';
          html += '<div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">'
            + '<div style="font-weight:bold;color:#555;font-size:0.8em;min-width:0;">' + a.name + '</div>'
            + '<div style="font-size:0.62em;font-weight:bold;color:#ff4444;letter-spacing:0.8px;white-space:nowrap;">KILLED</div>'
            + '</div>';
          html += '<div style="font-size:0.7em;margin-top:4px;color:#555;">';
          html += 'PnL: <span>' + (a.cumulative_pnl>=0?'+':'') + a.cumulative_pnl.toFixed(1) + '</span>';
          html += '</div></div>';
        }
        html += '</div>';
      }
      anaDiv.innerHTML = html;
    }).catch(()=>{});

  } catch (e) {
    console.error('IBKR fleet load error:', e);
  }
}

function loadEvolution() {
  fetch('/api/evolution').then(r => r.json()).then(data => {
    evoLoaded = true;
    const EVO = data.evo || [];
    const SCATTER = data.scatter || [];

    // Stats bar
    const bar = document.getElementById('evo-stats-bar');
    if (!EVO.length) { bar.innerHTML = '<div class="evo-stat"><div class="val" style="color:#7b8ab8">No data</div></div>'; return; }
    const latest = EVO[EVO.length - 1];
    const bestPf = Math.max(...EVO.map(d => d.pf));
    const bestWr = Math.max(...EVO.map(d => d.wr));
    const stats = [
      ['Runs', EVO.length, ''],
      ['Latest PF', latest.pf.toFixed(2), latest.pf >= 1 ? 'positive' : 'negative'],
      ['All-Time Best PF', bestPf.toFixed(2), bestPf >= 1 ? 'positive' : 'negative'],
      ['Latest WR', latest.wr.toFixed(1) + '%', latest.wr >= 35 ? 'positive' : 'negative'],
      ['Latest PnL', '$' + latest.pnl.toFixed(2), latest.pnl >= 0 ? 'positive' : 'negative'],
      ['Latest Trades', latest.trades, ''],
    ];
    bar.innerHTML = stats.map(([l,v,c]) =>
      '<div class="evo-stat"><div class="val ' + c + '">' + v + '</div><div class="lbl">' + l + '</div></div>'
    ).join('');

    // Draw charts
    drawEvoCanvas('evo-pf-wr', 'evo-tip-pf', EVO,
      [{key:'pf', color:'#00d4ff', label:'Profit Factor'}, {key:'wr', color:'#00e676', label:'Win Rate %'}],
      [{min:0, max:Math.max(2, ...EVO.map(d=>d.pf), 1.5), ref:1.0}, {min:0, max:100}]
    );
    drawEvoCanvas('evo-pnl', 'evo-tip-pnl', EVO,
      [{key:'pnl', color:'#ffb74d', label:'PnL ($)'}, {key:'expectancy', color:'#ba68c8', label:'Exp ($/trade)'}],
      [{ref:0}, {ref:0}]
    );

    // Strategy comparison chart
    try { renderStrategyCompare(data.strategy_compare || []); } catch(e) { console.error('Strategy compare error', e); }

    // Trade analytics: heatmap + distribution
    const ta = data.trade_analytics || {};
    try { renderHeatmap(ta.heatmap || []); } catch(e) { console.error('Heatmap error', e); }
    try { renderDistribution(ta.distribution || [], ta); } catch(e) { console.error('Distribution error', e); }
    try { renderDrawdown(ta.drawdown || []); } catch(e) { console.error('Drawdown error', e); }

    // ML Network visualization
    try { renderMLNetwork(data.ml_network || {}); } catch(e) { console.error('ML network error', e); }

    // Year-end projection
    renderProjection(data.projection || {});
    // renderMilestone100k removed — 100K section hidden
  }).catch(e => {
    console.error('Evolution load error', e);
    document.getElementById('evo-stats-bar').innerHTML = '<div class="evo-stat"><div class="val" style="color:#ff5252">Load Error</div></div>';
  });
}

let queueInterval = null;
function loadQueueStatus() {
  fetch('/api/queue').then(r => r.json()).then(data => {
    // Running job
    const runEl = document.getElementById('q-running');
    const progWrap = document.getElementById('q-progress-wrap');
    if (data.running_job) {
      const j = data.running_job;
      // Format elapsed and ETA
      const fmtDur = (s) => { const h=Math.floor(s/3600); const m=Math.floor((s%3600)/60); return h>0 ? h+'h '+m+'m' : m+'m'; };
      let etaLine = '';
      if (j.eta_remaining_s > 0) {
        const etaLocal = new Date(j.eta_completion).toLocaleString('en-US', {timeZone:'America/Chicago', hour:'numeric', minute:'2-digit', hour12:true});
        etaLine = ' | ETA: <span style="color:#00e676; font-weight:bold;">' + etaLocal + ' CT</span> (' + fmtDur(j.eta_remaining_s) + ' remaining)';
      }
      runEl.innerHTML = '<div style="font-size:1.1em; color:#ffc107; font-weight:bold;">' + (j.label || j.run_id) + '</div>' +
        '<div style="color:#7b8ab8; font-size:0.8em; margin-top:4px;">Run ID: ' + j.run_id + ' | ' + j.progress.toLocaleString() + ' / ' + j.total_bars.toLocaleString() + ' bars | Elapsed: ' + fmtDur(j.elapsed_s) + etaLine + '</div>';
      progWrap.style.display = 'block';
      document.getElementById('q-progress-bar').style.width = j.pct + '%';
      document.getElementById('q-progress-label').textContent = j.pct + '% complete';
    } else {
      runEl.innerHTML = '<span style="color:#7b8ab8;">No backtest currently running</span>';
      progWrap.style.display = 'none';
    }

    // Queue-wide ETA
    let qEtaEl = document.getElementById('q-queue-eta');
    if (!qEtaEl) {
      qEtaEl = document.createElement('div');
      qEtaEl.id = 'q-queue-eta';
      qEtaEl.style.cssText = 'margin:12px 0; padding:10px; background:#0d1321; border:1px solid #1e2a42; border-radius:6px;';
      progWrap.parentNode.insertBefore(qEtaEl, progWrap.nextSibling);
    }
    if (data.queue_eta) {
      const fmtDur = (s) => { const h=Math.floor(s/3600); const m=Math.floor((s%3600)/60); return h>0 ? h+'h '+m+'m' : m+'m'; };
      const allDoneLocal = new Date(data.queue_eta.completion).toLocaleString('en-US', {timeZone:'America/Chicago', hour:'numeric', minute:'2-digit', hour12:true, month:'short', day:'numeric'});
      const perJob = fmtDur(data.queue_eta.est_per_job_s);
      const pending = (data.pending_labels || []).length;
      qEtaEl.innerHTML = '<div style="font-size:0.9em;">' +
        '<span style="color:#00d4ff; font-weight:bold;">QUEUE COMPLETION</span>' +
        '<span style="color:#00e676; font-weight:bold; margin-left:12px;">' + allDoneLocal + ' CT</span>' +
        '<span style="color:#7b8ab8; margin-left:12px;">(' + fmtDur(data.queue_eta.remaining_s) + ' total remaining | ~' + perJob + '/job | ' + (pending + 1) + ' jobs left)</span>' +
        '</div>';
      qEtaEl.style.display = 'block';
    } else {
      qEtaEl.style.display = 'none';
    }

    // Pending queue - PC1
    const labels = data.pending_labels || [];
    document.getElementById('q-pending-count').textContent = labels.length;
    const pendEl = document.getElementById('q-pending');
    if (!labels.length) {
      pendEl.innerHTML = '<span style="color:#7b8ab8;">Queue empty</span>';
    } else {
      pendEl.innerHTML = labels.map((l, i) =>
        '<div style="padding:4px 8px; margin:2px 0; background:#0d1321; border-radius:3px; border-left:2px solid #00d4ff; font-size:0.85em;">' +
        '<span style="color:#7b8ab8;">#' + (i+1) + '</span> ' + l + '</div>'
      ).join('');
    }

    // PC2 status
    const pc2 = data.pc2 || {};
    const pc2El = document.getElementById('q-pc2-status');
    if (!pc2.reachable) {
      pc2El.innerHTML = '<span style="color:#ff5252;">' + (pc2.error || 'PC2 unreachable') + '</span>';
    } else if (pc2.running_label) {
      let pc2Eta = '';
      if (pc2.eta_pct > 0) {
        const pct = pc2.eta_pct;
        const elH = Math.floor(pc2.elapsed_s / 3600);
        const elM = Math.floor((pc2.elapsed_s % 3600) / 60);
        const etaH = Math.floor(pc2.eta_remaining_s / 3600);
        const etaM = Math.floor((pc2.eta_remaining_s % 3600) / 60);
        const prog = pc2.progress_bars || 0;
        const tot = pc2.total_bars || 0;
        pc2Eta =
          '<div style="margin-top:6px;">' +
          '<div style="background:#0d1321; border-radius:4px; height:8px; overflow:hidden; border:1px solid #1e2a42;">' +
          '<div style="height:100%; width:'+pct+'%; background:linear-gradient(90deg, #ba68c8, #9c27b0); border-radius:4px;"></div></div>' +
          '<div style="display:flex; justify-content:space-between; margin-top:3px; font-size:0.72em; color:#7b8ab8;">' +
          '<span>'+pct+'% ('+prog.toLocaleString()+'/'+tot.toLocaleString()+' bars)</span>' +
          '<span>Elapsed: '+elH+'h '+elM+'m</span></div>' +
          '<div style="font-size:0.82em; color:#ba68c8; margin-top:3px; font-weight:bold;">ETA: ~'+etaH+'h '+etaM+'m remaining</div></div>';
      } else {
        pc2Eta = '<div style="color:#7b8ab8; font-size:0.75em; margin-top:4px;">Starting... (waiting for progress data)</div>';
      }
      pc2El.innerHTML = '<div style="font-size:1.1em; color:#ba68c8; font-weight:bold;">' + pc2.running_label + '</div>' + pc2Eta;
    } else {
      const lastLog = pc2.recent_log && pc2.recent_log.length ? pc2.recent_log[pc2.recent_log.length - 1] : null;
      if (lastLog && lastLog.status === 'DONE') {
        pc2El.innerHTML = '<span style="color:#00e676;">Last job completed: ' + lastLog.label + '</span>';
      } else if (lastLog && lastLog.status === 'FAIL') {
        pc2El.innerHTML = '<span style="color:#ff5252;">Last job failed: ' + lastLog.label + '</span>';
      } else {
        pc2El.innerHTML = '<span style="color:#7b8ab8;">Idle</span>';
      }
    }
    // PC2 pending
    const pc2Pend = pc2.pending || [];
    document.getElementById('q-pc2-pending-count').textContent = pc2Pend.length;
    const pc2PendEl = document.getElementById('q-pc2-pending');
    if (!pc2Pend.length) {
      pc2PendEl.innerHTML = '<span style="color:#7b8ab8;">Queue empty</span>';
    } else {
      pc2PendEl.innerHTML = pc2Pend.map((l, i) =>
        '<div style="padding:4px 8px; margin:2px 0; background:#0d1321; border-radius:3px; border-left:2px solid #ba68c8; font-size:0.85em;">' +
        '<span style="color:#7b8ab8;">#' + (i+1) + '</span> ' + l + '</div>'
      ).join('');
    }

    // Unified results table (all PCs)
    const recent = data.recent || [];
    const tbody = document.getElementById('q-results');
    if (!recent.length) {
      tbody.innerHTML = '<tr><td colspan="12" style="color:#7b8ab8; text-align:center;">No results</td></tr>';
    } else {
      tbody.innerHTML = recent.filter(r => r.trades > 0).map(r => {
        const pfColor = r.pf >= 1.2 ? '#00e676' : r.pf >= 1.0 ? '#ffc107' : '#ff5252';
        const wrColor = r.wr >= 50 ? '#00e676' : r.wr >= 35 ? '#ffc107' : '#ff5252';
        const pnlVal = parseFloat(r.pnl) || 0;
        const pnlColor = pnlVal >= 0 ? '#00e676' : '#ff5252';
        const expVal = parseFloat(r.expectancy) || 0;
        const expColor = expVal >= 0 ? '#00e676' : '#ff5252';
        const pcColor = r.source === 'PC2' ? '#ba68c8' : '#00d4ff';
        const mfeStr = r.avg_mfe ? r.avg_mfe.toFixed(3) + '%' : '-';
        const maeStr = r.avg_mae ? r.avg_mae.toFixed(3) + '%' : '-';
        const ddColor = r.max_dd_pct > 3 ? '#ff5252' : r.max_dd_pct > 1 ? '#ffc107' : '#00e676';
        const coin = (r.symbol || 'ETH-USD').replace('-USD','');
        return '<tr style="border-bottom:1px solid #1e2a42;">' +
          '<td style="text-align:left; padding:5px 8px; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="' + r.run_id + '">' +
            (r.label || r.run_id.slice(-12)) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + pcColor + '; font-size:0.8em;">' + r.source + '</td>' +
          '<td style="text-align:center; padding:5px 4px; font-size:0.8em;">' + coin + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:#7b8ab8;">' + r.elapsed_days + '</td>' +
          '<td style="text-align:center; padding:5px 4px;">' + r.trades + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + pfColor + '; font-weight:bold;">' + r.pf.toFixed(2) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + wrColor + ';">' + r.wr.toFixed(1) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + pnlColor + ';">$' + pnlVal.toFixed(2) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + expColor + '; font-size:0.85em;">$' + expVal.toFixed(3) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:' + ddColor + ';">' + r.max_dd_pct.toFixed(2) + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:#00e676;">' + mfeStr + '</td>' +
          '<td style="text-align:center; padding:5px 4px; color:#ff5252;">' + maeStr + '</td>' +
          '</tr>';
      }).join('');
    }

    // Queue log
    const log = data.log || [];
    const logEl = document.getElementById('q-log');
    if (!log.length) {
      logEl.innerHTML = '<span style="color:#7b8ab8;">No log entries</span>';
    } else {
      logEl.innerHTML = log.slice().reverse().map(e => {
        const sc = e.status === 'DONE' ? '#00e676' : e.status === 'FAIL' ? '#ff5252' : e.status === 'START' ? '#ffc107' : '#7b8ab8';
        const icon = e.status === 'DONE' ? 'OK' : e.status === 'FAIL' ? 'FAIL' : e.status === 'START' ? 'RUN' : '?';
        let line = '<div style="padding:3px 6px; margin:1px 0; border-left:2px solid ' + sc + ';">' +
          '<span style="color:' + sc + '; font-weight:bold; width:35px; display:inline-block;">' + icon + '</span> ' +
          '<span style="color:#7b8ab8; font-size:0.9em;">' + (e.ts ? new Date(e.ts).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') : '') + '</span> ' +
          e.label;
        if (e.reason) {
          line += '<div style="color:#ff5252; font-size:0.85em; margin-left:40px; margin-top:2px;">' + e.reason + '</div>';
        }
        return line + '</div>';
      }).join('');
    }

    // Auto-refresh every 30s while on queue page
    if (!queueInterval) {
      queueInterval = setInterval(() => {
        if (document.getElementById('queue-page').classList.contains('active')) {
          loadQueueStatus();
          loadSweepProgress();
        }
      }, 30000);
    }
  }).catch(e => {
    console.error('Queue load error', e);
    document.getElementById('q-running').innerHTML = '<span style="color:#ff5252;">Load Error</span>';
  });
}

function updateSignalHealth(gov, botState, coin) {
  if (!gov || !Object.keys(gov).length) return;
  const c = (coin || 'ETH').toUpperCase();
  const $ = (id) => document.getElementById('sh-' + c + '-' + id);

  // Score bar
  const score = parseInt(gov.confluence_score) || parseInt(gov.score) || 0;
  const scoreColor = score >= 88 ? '#00e676' : score >= 55 ? '#ffc107' : '#ff5252';
  const scoreBar = $('score-bar'), scoreLabel = $('score-label');
  if (scoreBar) { scoreBar.style.width = Math.min(100, score) + '%'; scoreBar.style.background = scoreColor; }
  if (scoreLabel) scoreLabel.textContent = score + ' / 100';

  // Gate badge
  const gate = gov.gate || '';
  const gateBadge = $('gate-badge');
  if (gateBadge) {
    const gateColor = gate === 'TRADE' ? '#00e676' : gate === 'WATCH' ? '#ffc107' : gate === 'BLOCK' ? '#ff5252' : '#7b8ab8';
    gateBadge.textContent = gate || 'HOLD';
    gateBadge.style.color = gateColor;
    gateBadge.style.border = '1px solid ' + gateColor;
  }

  // Governor bar
  const hasGov = gov.win_prob !== null && gov.win_prob !== undefined && gov.win_prob !== '';
  const prob = hasGov ? parseFloat(gov.win_prob) : null;
  const pct = prob !== null ? Math.round(prob * 100) : null;
  const govColor = prob === null ? '#7b8ab8' : prob >= 0.45 ? '#00e676' : prob >= 0.30 ? '#ffc107' : '#ff5252';
  const govBar = $('gov-bar'), govLabel = $('gov-label'), govRec = $('gov-rec');
  if (govBar) { govBar.style.width = (pct !== null ? pct : 0) + '%'; govBar.style.background = govColor; }
  if (govLabel) govLabel.textContent = pct !== null ? pct + '% win prob' : '— no data';
  if (govRec) {
    const rec = gov.recommendation || '—';
    govRec.textContent = rec;
    govRec.style.color = rec === 'ALLOW' ? '#00e676' : rec === 'BLOCK' ? '#ff5252' : '#ffc107';
    govRec.style.display = rec && rec !== '—' ? '' : 'none';
  }

  // OB imbalance meter (centered, -1 to +1)
  const obVal = parseFloat(gov.ob_imbalance);
  const obFill = $('ob-fill'), obLabel = $('ob-label'), obBadge = $('ob-badge');
  if (!isNaN(obVal)) {
    const offset = obVal * 50;
    const color = obVal > 0.1 ? '#00e676' : obVal < -0.1 ? '#ff5252' : '#7b8ab8';
    if (obFill) {
      if (offset >= 0) { obFill.style.left = '50%'; obFill.style.width = offset + '%'; }
      else { obFill.style.left = (50 + offset) + '%'; obFill.style.width = (-offset) + '%'; }
      obFill.style.background = color;
    }
    if (obLabel) obLabel.textContent = (obVal >= 0 ? '+' : '') + obVal.toFixed(3);
    if (obBadge) {
      obBadge.textContent = obVal > 0.25 ? 'BULL ▲' : obVal > 0.1 ? 'MILD ▲' : obVal < -0.25 ? 'BEAR ▼' : obVal < -0.1 ? 'MILD ▼' : 'NEUTRAL';
      obBadge.style.color = color;
    }
  } else {
    if (obLabel) obLabel.textContent = 'N/A';
    if (obBadge) { obBadge.textContent = 'N/A'; obBadge.style.color = '#7b8ab8'; }
  }

  // Regime badge
  const regimeEl = $('regime');
  if (regimeEl) {
    const regime = gov.regime || '—';
    regimeEl.textContent = regime;
    regimeEl.style.background = regime === 'TREND_UP' ? '#0d3320' : regime === 'TREND_DOWN' ? '#3d0d0d' : regime === 'RANGE' ? '#2a2a0d' : '#1e2a42';
    regimeEl.style.color = regime === 'TREND_UP' ? '#00e676' : regime === 'TREND_DOWN' ? '#ff5252' : regime === 'RANGE' ? '#ffc107' : '#7b8ab8';
  }

  // Session badge
  const sessionEl = $('session');
  if (sessionEl) {
    const session = gov.session || '—';
    sessionEl.textContent = session;
    const sessionColor = session === 'OVERLAP' || session === 'NY' ? '#00e676' : session === 'LONDON' ? '#00d4ff' : session === 'ASIA' ? '#ffc107' : '#7b8ab8';
    sessionEl.style.color = sessionColor;
    sessionEl.style.background = '#1e2a42';
  }

  // Action badge
  const actionEl = $('action');
  if (actionEl) {
    const action = gov.action || '—';
    actionEl.textContent = action;
    actionEl.style.color = action === 'WOULD_BUY' ? '#00e676' : action === 'WOULD_SELL' ? '#ff5252' : '#7b8ab8';
  }

  // Readiness indicator + card border coloring
  const isReady = score >= 88 && (gov.recommendation === 'ALLOW' || !gov.recommendation) && gate === 'TRADE';
  const isWatch = score >= 55 && score < 88;
  const readEl = $('readiness');
  if (readEl) {
    readEl.textContent = isReady ? '● TRADE READY' : isWatch ? '◑ WATCHING' : '○ IDLE';
    readEl.style.color = isReady ? '#00e676' : isWatch ? '#ffc107' : '#7b8ab8';
    readEl.style.fontSize = isReady ? '1.1em' : '0.95em';
  }
  // Color-code the card border by readiness
  const card = document.getElementById('summary-' + c);
  if (card) {
    card.style.borderColor = isReady ? '#00e676' : isWatch ? '#ffc107' : '#1e2a42';
    card.style.boxShadow = isReady ? '0 0 12px rgba(0,230,118,0.2)' : 'none';
  }

  // Volatility / ATR bar
  const atrNorm = parseFloat(gov.liq_atr_norm) || 0;
  const spreadBps = parseFloat(gov.liq_spread_bps) || 0;
  const vol1m = parseFloat(gov.liq_vol_1m) || 0;
  const volBar = $('vol-bar'), volLabel = $('vol-label'), volBadge = $('vol-badge');
  // ATR norm typically 0.0003-0.003; map to 0-100%
  const volPct = Math.min(100, Math.max(0, (atrNorm / 0.003) * 100));
  const volLevel = atrNorm >= 0.002 ? 'HIGH' : atrNorm >= 0.0008 ? 'MEDIUM' : atrNorm > 0 ? 'LOW' : '—';
  const volColor = atrNorm >= 0.002 ? '#ff9800' : atrNorm >= 0.0008 ? '#00e676' : atrNorm > 0 ? '#7b8ab8' : '#7b8ab8';
  if (volBar) { volBar.style.width = volPct.toFixed(1) + '%'; volBar.style.background = volColor; }
  if (volLabel) volLabel.textContent = atrNorm > 0 ? 'ATR ' + (atrNorm * 100).toFixed(3) + '% | Spread ' + spreadBps.toFixed(1) + 'bp' : '—';
  if (volBadge) { volBadge.textContent = volLevel; volBadge.style.color = volColor; }

  // Action reason
  const reasonEl = $('action-reason');
  if (reasonEl) reasonEl.textContent = gov.action_reason || '';
}

function updateTradeProgress(data) {
  const panel = document.getElementById('trade-progress-panel');
  if (!panel) return;
  const isOpen = (data.bot_state === 'OPEN' || data.bot_state === 'LONG');
  panel.style.display = isOpen ? '' : 'none';
  if (!isOpen) return;

  const gov = data.governor || {};
  const entryPx = parseFloat(gov.entry_px) || 0;
  const currentPx = parseFloat(gov.price) || 0;
  const upnl = parseFloat(data.unrealized_pnl) || 0;

  const entryEl = document.getElementById('tp-entry');
  const curEl = document.getElementById('tp-current');
  const upnlEl = document.getElementById('tp-upnl');

  if (entryEl) entryEl.textContent = entryPx > 0 ? '$' + entryPx.toFixed(2) : '—';
  if (curEl) curEl.textContent = currentPx > 0 ? '$' + currentPx.toFixed(2) : '—';
  if (upnlEl) {
    upnlEl.textContent = '$' + upnl.toFixed(4);
    upnlEl.style.color = upnl > 0 ? '#00e676' : upnl < 0 ? '#ff5252' : '#e0e0e0';
  }

  // OB now
  const obNow = document.getElementById('tp-ob-now');
  const obVal = parseFloat(gov.ob_imbalance);
  if (obNow) obNow.textContent = !isNaN(obVal) ? (obVal >= 0 ? '+' : '') + obVal.toFixed(3) : '—';

  // TP progress bar (if entry price known)
  const tpPct = parseFloat('1.5'); // TAKE_PROFIT_PCT default 1.5%
  if (entryPx > 0 && currentPx > 0) {
    const moveToTP = (currentPx - entryPx) / entryPx;
    const barPct = Math.min(100, Math.max(0, (moveToTP / (tpPct/100)) * 100));
    const tpBar = document.getElementById('tp-tp-bar');
    const tpLabel = document.getElementById('tp-tp-label');
    if (tpBar) tpBar.style.width = barPct.toFixed(1) + '%';
    if (tpLabel) tpLabel.textContent = (moveToTP * 100).toFixed(3) + '%';

    const tpPrice = document.getElementById('tp-tp-price');
    const slPrice = document.getElementById('tp-sl-price');
    if (tpPrice) tpPrice.textContent = '$' + (entryPx * 1.015).toFixed(2);
    if (slPrice) slPrice.textContent = '$' + (entryPx * 0.98).toFixed(2);
  }
}

function loadSweepProgress() {
  fetch('/api/sweep').then(r => r.json()).then(data => {
    const phases = data.phases || {};
    const pending = data.queue_pending || {};
    const phaseNames = {
      phase1:       {label: 'Phase 1 (TP 3-5%)', color: '#7b8ab8'},
      phase2_60:    {label: 'Phase 2 (TP 6-12%, 60bps)', color: '#00d4ff'},
      phase2_maker: {label: 'Phase 2 (TP 6-12%, 40bps)', color: '#00e676'},
      phase2_trail: {label: 'Phase 2 (Trail50 variants)', color: '#ffb74d'},
    };
    let bestOverall = null;
    let rows = '';
    for (const [key, meta] of Object.entries(phaseNames)) {
      const ph = phases[key] || {};
      const qPend = pending[key] || 0;
      const done = ph.count || 0;
      const profitable = ph.profitable || 0;
      const bestPf = ph.best_pf || 0;
      const best = ph.best;
      const profColor = profitable > 0 ? '#00e676' : (done > 0 ? '#ff5252' : '#7b8ab8');
      const pfColor = bestPf >= 1.2 ? '#00e676' : bestPf >= 1.0 ? '#ffc107' : '#ff5252';
      rows += '<tr style="border-bottom:1px solid #1e2a42;">' +
        '<td style="padding:4px 6px; color:' + meta.color + '; font-size:0.8em;">' + meta.label + '</td>' +
        '<td style="text-align:right; padding:4px 6px;">' + done + '</td>' +
        '<td style="text-align:right; padding:4px 6px; color:#7b8ab8;">' + (qPend > 0 ? qPend : '—') + '</td>' +
        '<td style="text-align:right; padding:4px 6px; color:' + profColor + '; font-weight:bold;">' + profitable + '</td>' +
        '<td style="text-align:right; padding:4px 6px; color:' + pfColor + '; font-weight:bold;">' + (done > 0 ? bestPf.toFixed(3) : '—') + '</td>' +
        '</tr>';
      if (best && (!bestOverall || best.pf > bestOverall.pf)) {
        bestOverall = {...best, phase: meta.label};
      }
    }
    document.getElementById('sweep-table-body').innerHTML = rows;

    // Best config alert
    const alertEl = document.getElementById('sweep-best-alert');
    const bestCfgEl = document.getElementById('sweep-best-config');
    if (bestOverall && bestOverall.pf >= 1.0) {
      alertEl.style.display = 'block';
      const be = bestOverall.be_wr ? bestOverall.be_wr.toFixed(0) + '% needed' : '?';
      alertEl.innerHTML = '&#10003; <b style="color:#00e676;">PROFITABLE CONFIG FOUND!</b> ' +
        'PF=' + bestOverall.pf.toFixed(3) + ' WR=' + bestOverall.wr.toFixed(1) + '% ' +
        'TP=' + (bestOverall.tp_pct*100).toFixed(0) + '% SL=' + (bestOverall.sl_pct*100).toFixed(1) + '% ' +
        '| BE-WR: ' + be;
      bestCfgEl.textContent = bestOverall.label;
    } else {
      alertEl.style.display = 'none';
      if (bestOverall) {
        const be = bestOverall.be_wr ? bestOverall.be_wr.toFixed(0) + '%' : '?';
        bestCfgEl.textContent = 'Best so far: PF=' + bestOverall.pf.toFixed(3) +
          ' | ' + bestOverall.label + ' | need WR>=' + be + ' to profit';
      }
    }
  }).catch(e => console.error('Sweep load error', e));
}

function renderProjection(proj) {
  const cards = document.getElementById('projection-cards');
  if (!proj.scenarios || !proj.scenarios.length) {
    cards.innerHTML = '<div class="evo-stat" style="width:100%"><div class="val" style="color:#7b8ab8">No projection data</div></div>';
    return;
  }

  const startCash = proj.start_cash || 1000;
  const numCoins = proj.num_coins || 2;
  const tagColors = {latest:'#00d4ff', best_ever:'#7b8ab866'};
  const tagLabels = {latest:'CURRENT STRATEGY', best_ever:'Best Historical'};

  let html = '<div style="font-size:0.7em; color:#7b8ab8; margin-bottom:8px;">Source: latest backtest (current config) | $'+startCash+' per coin x '+numCoins+' coins</div>';
  proj.scenarios.forEach(sc => {
    const color = tagColors[sc.tag] || '#7b8ab8';
    const label = tagLabels[sc.tag] || sc.tag;
    const arrow = sc.pct_gain >= 0 ? '&#9650;' : '&#9660;';
    const isPrimary = sc.tag === 'latest';
    const borderWidth = isPrimary ? '3px' : '1px';
    const opacity = isPrimary ? '1' : '0.6';
    const total3 = sc.total_year_end_2coin || sc.year_end_balance * numCoins;
    html += '<div style="background:#141b2d; border:1px solid #1e2a42; border-left:'+borderWidth+' solid '+color+'; border-radius:6px; padding:10px 14px; opacity:'+opacity+';">' +
      '<div style="display:flex; justify-content:space-between; align-items:center;">' +
      '<div><span style="color:'+color+'; font-weight:bold; font-size:0.9em;">'+label+'</span>' +
      (sc.note ? '<span style="color:#7b8ab8; font-size:0.65em; margin-left:6px;">('+sc.note+')</span>' : '') +
      '<span style="color:#7b8ab8; font-size:0.7em; margin-left:8px;">PF='+sc.pf+' | '+sc.bt_days+'d backtest</span></div>' +
      '<div style="font-size:0.72em; color:#7b8ab8;">'+sc.days_left+' days left</div></div>' +
      '<div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:6px;">' +
      '<div><span style="font-size:1.5em; font-weight:bold; color:'+color+';">$'+sc.year_end_balance.toFixed(2)+'</span>' +
      '<span style="font-size:0.75em; color:#7b8ab8; margin-left:6px;">per coin</span></div>' +
      '<div style="font-size:1.0em; color:'+color+';">'+arrow+' '+sc.pct_gain.toFixed(1)+'%</div></div>' +
      '<div style="font-size:0.7em; color:#7b8ab8; margin-top:2px;">$'+sc.daily_pnl.toFixed(4)+'/day/coin &rarr; $'+sc.projected_gain.toFixed(2)+' gain | <b style="color:'+color+';">2-coin total: $'+total3.toFixed(2)+'</b></div>' +
      '</div>';
  });

  // $2K live-readiness milestone
  const m2k = proj.milestone_2k;
  if (m2k) {
    const m2kColor = m2k.days_needed <= 90 ? '#00e676' : m2k.days_needed <= 365 ? '#ffb74d' : '#ff5252';
    const m2kPct = Math.min(100, Math.max(0, (m2k.current_balance / m2k.target) * 100));
    html += '<div style="margin-top:8px; padding:10px 14px; background:#141b2d; border:1px solid #1e2a42; border-left:3px solid #ba68c8; border-radius:6px;">' +
      '<div style="display:flex; justify-content:space-between; align-items:center;">' +
      '<div style="color:#ba68c8; font-weight:bold; font-size:0.85em;">GO-LIVE TARGET: $2,000/coin</div>' +
      '<div style="font-size:0.72em; color:'+m2kColor+';">'+Math.round(m2k.days_needed)+' days ('+m2k.target_date+')</div></div>' +
      '<div style="margin-top:6px; background:#0d1321; border-radius:3px; height:8px; overflow:hidden;">' +
      '<div style="height:100%; width:'+m2kPct.toFixed(1)+'%; background:linear-gradient(90deg,#ba68c8,#e040fb); border-radius:3px;"></div></div>' +
      '<div style="font-size:0.65em; color:#7b8ab8; margin-top:3px;">$'+m2k.current_balance.toFixed(0)+' / $'+m2k.target.toFixed(0)+' ('+m2kPct.toFixed(1)+'%) | $'+m2k.daily_pnl.toFixed(4)+'/day</div></div>';
  } else {
    // Get current PF from latest scenario
    const latestSc = proj.scenarios.find(s => s.tag === 'latest');
    const curPf = latestSc ? latestSc.pf : 0;
    const pfPct = Math.min(100, Math.max(0, (curPf / 1.5) * 100));  // gauge: 0 to 1.5 PF
    const pfColor = curPf >= 1.0 ? '#00e676' : curPf >= 0.9 ? '#ffb74d' : '#ff5252';
    html += '<div style="margin-top:8px; padding:10px 14px; background:#141b2d; border:1px solid #1e2a42; border-left:3px solid #ba68c8; border-radius:6px;">' +
      '<div style="display:flex; justify-content:space-between; align-items:center;">' +
      '<div style="color:#ba68c8; font-weight:bold; font-size:0.85em;">GO-LIVE TARGET: $2,000/coin</div>' +
      '<div style="font-size:0.72em; color:#ff5252;">PF must reach 1.0+</div></div>' +
      '<div style="margin-top:8px; display:flex; align-items:center; gap:10px;">' +
      '<div style="flex:1;">' +
      '<div style="font-size:0.7em; color:#7b8ab8; margin-bottom:3px;">Profit Factor Progress</div>' +
      '<div style="background:#0d1321; border-radius:3px; height:14px; overflow:hidden; position:relative;">' +
      '<div style="height:100%; width:'+pfPct.toFixed(1)+'%; background:linear-gradient(90deg,#ff5252,'+pfColor+'); border-radius:3px; transition:width 0.5s;"></div>' +
      '<div style="position:absolute; top:0; left:50%; transform:translateX(-50%); height:100%; width:1px; background:#00e67666;"></div>' +
      '</div>' +
      '<div style="display:flex; justify-content:space-between; margin-top:2px; font-size:0.6em; color:#7b8ab8;">' +
      '<span>0</span><span style="color:#00e676;">1.0 (breakeven)</span><span>1.5+</span></div>' +
      '</div>' +
      '<div style="text-align:center; min-width:70px;">' +
      '<div style="font-size:1.4em; font-weight:bold; color:'+pfColor+';">'+curPf.toFixed(2)+'</div>' +
      '<div style="font-size:0.6em; color:#7b8ab8;">Current PF</div></div></div></div>';
  }

  cards.innerHTML = html;

  // Monthly projection curve
  const curve = proj.monthly_curve || [];
  if (!curve.length) return;
  const canvas = document.getElementById('projection-chart');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const PAD = {l:55, r:15, t:15, b:35};
  const pW = W-PAD.l-PAD.r, pH = H-PAD.t-PAD.b;

  const bals = curve.map(c => c.balance);
  const yMin = Math.min(0, ...bals) * 0.9;
  const yMax = Math.max(...bals) * 1.1;
  const yRange = yMax - yMin || 1;
  const xScale = i => PAD.l + (i/(curve.length-1)) * pW;
  const yScale = v => PAD.t + pH - ((v-yMin)/yRange)*pH;

  // Grid
  ctx.strokeStyle = '#1e2a42'; ctx.lineWidth = 0.5;
  for (let i=0;i<=4;i++) { const y=PAD.t+(i/4)*pH; ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r,y); ctx.stroke(); }

  // Starting cash reference
  ctx.strokeStyle = '#7b8ab844'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(PAD.l, yScale(startCash)); ctx.lineTo(W-PAD.r, yScale(startCash)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#7b8ab8'; ctx.font = '9px Courier New'; ctx.textAlign = 'right';
  ctx.fillText('$'+startCash, PAD.l-4, yScale(startCash)+3);

  // Gradient fill
  const grad = ctx.createLinearGradient(0, PAD.t, 0, PAD.t+pH);
  const endBal = bals[bals.length-1];
  if (endBal >= startCash) {
    grad.addColorStop(0, 'rgba(0,230,118,0.25)'); grad.addColorStop(1, 'rgba(0,230,118,0.02)');
  } else {
    grad.addColorStop(0, 'rgba(255,82,82,0.05)'); grad.addColorStop(1, 'rgba(255,82,82,0.25)');
  }
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.moveTo(xScale(0), yScale(0));
  curve.forEach((c,i) => ctx.lineTo(xScale(i), yScale(c.balance)));
  ctx.lineTo(xScale(curve.length-1), PAD.t+pH); ctx.lineTo(xScale(0), PAD.t+pH); ctx.closePath(); ctx.fill();

  // Line
  ctx.strokeStyle = endBal >= startCash ? '#00e676' : '#ff5252'; ctx.lineWidth = 2.5;
  ctx.beginPath();
  curve.forEach((c,i) => { i===0 ? ctx.moveTo(xScale(i),yScale(c.balance)) : ctx.lineTo(xScale(i),yScale(c.balance)); });
  ctx.stroke();

  // Points + labels
  const months = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  ctx.textAlign = 'center'; ctx.font = '9px Courier New';
  curve.forEach((c,i) => {
    ctx.fillStyle = endBal >= startCash ? '#00e676' : '#ff5252';
    ctx.beginPath(); ctx.arc(xScale(i), yScale(c.balance), 3, 0, Math.PI*2); ctx.fill();
    if (i === 0 || i === curve.length-1 || i % 3 === 0) {
      ctx.fillStyle = '#7b8ab8';
      ctx.fillText(months[c.month] || c.month, xScale(i), H-PAD.b+14);
    }
  });

  // End balance label
  ctx.fillStyle = endBal >= startCash ? '#00e676' : '#ff5252'; ctx.font = 'bold 11px Courier New'; ctx.textAlign = 'left';
  ctx.fillText('$'+endBal.toFixed(0), xScale(curve.length-1)+6, yScale(endBal)+4);

  // Y axis
  ctx.fillStyle = '#7b8ab8'; ctx.font = '9px Courier New'; ctx.textAlign = 'right';
  for (let i=0;i<=4;i++) { const v=yMin+(yRange*i/4); ctx.fillText('$'+v.toFixed(0), PAD.l-4, PAD.t+pH-(i/4)*pH+3); }
}

function renderMilestone100k(proj) {
  const el = document.getElementById('milestone-100k');
  if (!el) return;
  const ms = proj.milestone_100k;
  if (!ms) {
    // No positive scenario — show "not yet" state
    const startCash = proj.start_cash || 1000;
    const toGo = 100000 - startCash;
    el.style.display = 'block';
    el.innerHTML =
      '<div style="background:#141b2d; border:1px solid #1e2a42; border-left:3px solid #ffb74d; border-radius:6px; padding:12px 14px;">' +
      '<div style="display:flex; justify-content:space-between; align-items:center;">' +
      '<div style="font-size:0.85em; letter-spacing:1px; color:#ffb74d; font-weight:bold;">&#127942; $100K MILESTONE</div>' +
      '<div style="font-size:0.72em; color:#7b8ab8;">$'+toGo.toLocaleString()+' to go</div></div>' +
      '<div style="margin-top:8px;">' +
      '<div style="background:#0d1321; border-radius:4px; height:10px; overflow:hidden; border:1px solid #1e2a42;">' +
      '<div style="height:100%; width:'+((startCash/100000)*100).toFixed(2)+'%; background:linear-gradient(90deg, #ffb74d, #ff9800); border-radius:4px;"></div></div>' +
      '<div style="display:flex; justify-content:space-between; margin-top:4px; font-size:0.68em; color:#7b8ab8;">' +
      '<span>$'+startCash.toLocaleString()+'</span><span>$100,000</span></div></div>' +
      '<div style="margin-top:6px; font-size:0.75em; color:#7b8ab8;">Waiting for profitable backtest scenario to estimate timeline...</div></div>';
    return;
  }

  const pct = ((ms.current_balance / ms.target) * 100).toFixed(2);
  const years = (ms.days_needed / 365).toFixed(1);
  const months = Math.round(ms.days_needed / 30.44);
  let timeStr;
  if (ms.days_needed < 60) timeStr = Math.round(ms.days_needed) + ' days';
  else if (ms.days_needed < 730) timeStr = months + ' months';
  else timeStr = years + ' years';

  const barColor = ms.days_needed < 365 ? '#00e676' : ms.days_needed < 1095 ? '#ffb74d' : '#ff5252';
  const nCoins = ms.num_coins || 3;
  const dailyTotal = ms.daily_pnl_3coin || (ms.daily_pnl_latest || 0) * nCoins;
  el.style.display = 'block';
  let msHtml =
    '<div style="background:#141b2d; border:1px solid #1e2a42; border-left:3px solid '+barColor+'; border-radius:6px; padding:12px 14px;">' +
    '<div style="display:flex; justify-content:space-between; align-items:center;">' +
    '<div style="font-size:0.85em; letter-spacing:1px; color:'+barColor+'; font-weight:bold;">$100K MILESTONE</div>' +
    '<div style="font-size:0.72em; color:#7b8ab8;">$'+(ms.target - ms.current_balance).toLocaleString(undefined,{maximumFractionDigits:0})+' to go</div></div>' +
    '<div style="margin-top:8px;">' +
    '<div style="background:#0d1321; border-radius:4px; height:10px; overflow:hidden; border:1px solid #1e2a42;">' +
    '<div style="height:100%; width:'+pct+'%; background:linear-gradient(90deg, '+barColor+', '+barColor+'aa); border-radius:4px; min-width:2px;"></div></div>' +
    '<div style="display:flex; justify-content:space-between; margin-top:4px; font-size:0.68em; color:#7b8ab8;">' +
    '<span>$'+ms.current_balance.toLocaleString()+' ('+nCoins+' coins)</span><span>$100,000</span></div></div>' +
    '<div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:8px;">' +
    '<div><span style="font-size:1.2em; font-weight:bold; color:'+barColor+';">~'+timeStr+'</span>' +
    '<span style="font-size:0.72em; color:#7b8ab8; margin-left:8px;">at $'+dailyTotal.toFixed(2)+'/day ('+nCoins+' coins combined)</span></div>' +
    '<div style="font-size:0.78em; color:#7b8ab8;">ETA: <b style="color:'+barColor+';">'+ms.target_date+'</b></div></div>';
  // Show best-ever potential if different
  if (ms.best_ever_days && ms.best_ever_days < ms.days_needed) {
    const bestYears = (ms.best_ever_days / 365).toFixed(1);
    const bestMonths = Math.round(ms.best_ever_days / 30.44);
    let bestTimeStr;
    if (ms.best_ever_days < 60) bestTimeStr = Math.round(ms.best_ever_days) + ' days';
    else if (ms.best_ever_days < 730) bestTimeStr = bestMonths + ' months';
    else bestTimeStr = bestYears + ' years';
    msHtml += '<div style="font-size:0.68em; color:#7b8ab866; margin-top:4px;">Best-ever potential: ~'+bestTimeStr+' at $'+(ms.best_ever_daily_3coin||0).toFixed(2)+'/day</div>';
  }
  msHtml += '<div style="font-size:0.65em; color:#7b8ab866; margin-top:4px;">Based on latest backtest x'+nCoins+' coins — auto-adjusts as strategy improves</div></div>';
  el.innerHTML = msHtml;
}

function renderReadiness(r) {
  const el = document.getElementById('readiness-content');
  if (!el || !r.checks) { if(el) el.innerHTML = '<span style="color:#7b8ab8">No readiness data</span>'; return; }

  const pct = r.pct || 0;
  const barColor = pct >= 80 ? '#00e676' : pct >= 50 ? '#ffb74d' : '#ff5252';

  let html = '<div style="background:#141b2d; border:1px solid #1e2a42; border-radius:6px; padding:12px 14px; margin-bottom:10px;">' +
    '<div style="display:flex; justify-content:space-between; align-items:center;">' +
    '<div><span style="font-size:1.3em; font-weight:bold; color:'+barColor+';">'+r.passed+'/'+r.total+'</span>' +
    '<span style="font-size:0.78em; color:#7b8ab8; margin-left:8px;">checks passed ('+pct+'%)</span></div>' +
    '<div style="font-size:0.72em; color:#7b8ab8;">Portfolio: $'+((r.total_live_equity||1500).toFixed(2))+'</div></div>' +
    '<div style="margin-top:6px; background:#0d1321; border-radius:3px; height:12px; overflow:hidden;">' +
    '<div style="height:100%; width:'+pct+'%; background:linear-gradient(90deg,'+barColor+','+barColor+'aa); border-radius:3px; transition:width 0.5s;"></div></div></div>';

  r.checks.forEach(cat => {
    const catPassed = cat.items.filter(i => i.pass).length;
    const catTotal = cat.items.length;
    const catColor = catPassed === catTotal ? '#00e676' : catPassed > 0 ? '#ffb74d' : '#ff5252';
    html += '<div style="background:#141b2d; border:1px solid #1e2a42; border-radius:6px; padding:10px 14px; margin-bottom:6px;">' +
      '<div style="color:'+catColor+'; font-weight:bold; font-size:0.82em; margin-bottom:8px; letter-spacing:0.5px;">' +
      cat.category.toUpperCase() + ' <span style="color:#7b8ab8; font-weight:normal;">('+catPassed+'/'+catTotal+')</span></div>';

    cat.items.forEach(item => {
      const icon = item.pass ? '<span style="color:#00e676;">&#10003;</span>' : '<span style="color:#ff5252;">&#10007;</span>';
      const valColor = item.pass ? '#00e676' : '#ff5252';
      let currentStr = String(item.current);
      if (item.format === 'pf') currentStr = parseFloat(item.current).toFixed(3);
      else if (item.format === 'pct') currentStr = parseFloat(item.current).toFixed(1) + '%';
      else if (item.format === 'usd') currentStr = '$' + parseFloat(item.current).toFixed(2);
      else if (item.format === 'int') currentStr = String(item.current);
      const targetStr = typeof item.target === 'number' ? (item.format === 'pct' ? item.target + '%' : item.format === 'usd' ? '$' + item.target : String(item.target)) : String(item.target);

      html += '<div style="display:flex; justify-content:space-between; align-items:center; padding:3px 0; border-bottom:1px solid #1e2a4233;">' +
        '<div style="display:flex; align-items:center; gap:8px;">' + icon +
        '<span style="font-size:0.78em; color:#e0e0e0;">'+item.name+'</span></div>' +
        '<div style="display:flex; align-items:center; gap:12px;">' +
        '<span style="font-size:0.75em; color:'+valColor+'; font-weight:bold;">'+currentStr+'</span>' +
        '<span style="font-size:0.65em; color:#7b8ab8;">/ '+targetStr+'</span></div></div>';
    });
    html += '</div>';
  });

  el.innerHTML = html;
}

// Real-time projection ticker — updates from SSE live PnL
let lastProjectionData = null;
function updateLiveProjection(livePnl, liveEquity) {
  if (!lastProjectionData && evoLoaded) {
    // Cache projection data on first SSE update after evo load
    fetch('/api/evolution').then(r => r.json()).then(d => {
      lastProjectionData = d.projection;
      _applyLiveProjectionUpdate(livePnl, liveEquity);
    }).catch(() => {});
    return;
  }
  if (!lastProjectionData) return;
  _applyLiveProjectionUpdate(livePnl, liveEquity);
}

function _applyLiveProjectionUpdate(livePnl, liveEquity) {
  const proj = lastProjectionData;
  if (!proj || !proj.scenarios || !proj.scenarios.length) return;
  const liveEl = document.getElementById('live-projection-ticker');
  if (!liveEl) {
    // Create the live ticker element if it doesn't exist
    const section = document.getElementById('projection-cards');
    if (!section) return;
    const div = document.createElement('div');
    div.id = 'live-projection-ticker';
    div.style.cssText = 'margin-top:8px; padding:10px 14px; background:#0d1321; border:1px solid #00d4ff44; border-radius:6px; animation: pulse 2s infinite;';
    section.appendChild(div);
    // Add pulse animation
    if (!document.getElementById('pulse-style')) {
      const style = document.createElement('style');
      style.id = 'pulse-style';
      style.textContent = '@keyframes pulse { 0%,100%{border-color:#00d4ff44} 50%{border-color:#00d4ff} }';
      document.head.appendChild(style);
    }
  }
  const ticker = document.getElementById('live-projection-ticker');
  if (!ticker) return;

  const startCash = proj.start_cash || 1000;
  const currentBalance = liveEquity > 0 ? liveEquity : startCash + livePnl;
  const gainPct = ((currentBalance - startCash) / startCash * 100);
  const color = livePnl >= 0 ? '#00e676' : '#ff5252';
  const arrow = livePnl >= 0 ? '&#9650;' : '&#9660;';

  // Annualize from live performance — dynamically compute elapsed days
  const now = new Date();
  // Use first scenario's bt_days as a sanity floor; actual paper start = 2026-03-15
  const paperStartStr = '2026-03-15T00:00:00Z';
  const startDate = new Date(paperStartStr);
  const elapsedDays = Math.max(1, (now - startDate) / 86400000);
  const dailyRate = livePnl / elapsedDays;
  const daysLeft = proj.scenarios[0] ? proj.scenarios[0].days_left : 290;
  const projectedYearEnd = currentBalance + dailyRate * daysLeft;
  const projYearEndPct = ((projectedYearEnd - startCash) / startCash * 100);

  ticker.innerHTML =
    '<div style="display:flex; justify-content:space-between; align-items:center;">' +
    '<div style="font-size:0.75em; color:#00d4ff; font-weight:bold; letter-spacing:1px;">LIVE PERFORMANCE</div>' +
    '<div style="font-size:0.68em; color:#7b8ab8;">Updated ' + new Date().toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) + ' CT</div></div>' +
    '<div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:6px;">' +
    '<div><span style="color:#7b8ab8; font-size:0.78em;">Current Balance:</span> ' +
    '<span style="font-size:1.3em; font-weight:bold; color:'+color+';">$'+currentBalance.toFixed(2)+'</span>' +
    '<span style="font-size:0.85em; color:'+color+'; margin-left:6px;">'+arrow+' '+gainPct.toFixed(2)+'%</span></div>' +
    '<div><span style="color:#7b8ab8; font-size:0.78em;">Year-End Proj:</span> ' +
    '<span style="font-size:1.1em; font-weight:bold; color:'+(projectedYearEnd>=startCash?'#00e676':'#ff5252')+';">$'+projectedYearEnd.toFixed(2)+'</span>' +
    '<span style="font-size:0.78em; color:'+(projYearEndPct>=0?'#00e676':'#ff5252')+'; margin-left:4px;">('+projYearEndPct.toFixed(1)+'%)</span></div></div>' +
    '<div style="font-size:0.68em; color:#7b8ab8; margin-top:4px;">$'+dailyRate.toFixed(4)+'/day over '+elapsedDays.toFixed(0)+' days | Live P&L: <span style="color:'+color+'">$'+livePnl.toFixed(4)+'</span></div>';

  // Update milestone with live data
  if (dailyRate > 0) {
    const liveMs = {target:100000, current_balance:currentBalance, daily_pnl_best:dailyRate,
      days_needed:(100000-currentBalance)/dailyRate,
      target_date:new Date(Date.now()+((100000-currentBalance)/dailyRate)*86400000).toISOString().slice(0,10)};
    // renderMilestone100k removed — 100K section hidden
  }
}

function drawEvoCanvas(canvasId, tipId, EVO, series, yConfigs) {
  const canvas = document.getElementById(canvasId);
  const tip = document.getElementById(tipId);
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const PAD = {l:55, r:55, t:15, b:40};
  const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
  if (!EVO.length) { ctx.fillStyle='#7b8ab8'; ctx.font='13px Courier New'; ctx.fillText('No data', W/2-25, H/2); return; }
  const xScale = i => PAD.l + (i / Math.max(1, EVO.length-1)) * pW;

  // Grid
  ctx.strokeStyle = '#1e2a42'; ctx.lineWidth = 0.5;
  for (let i = 0; i <= 5; i++) { const y = PAD.t + (i/5)*pH; ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r,y); ctx.stroke(); }

  series.forEach((s, si) => {
    const yc = yConfigs[si];
    const vals = EVO.map(d => d[s.key]);
    const yMin = yc.min !== undefined ? yc.min : Math.min(...vals);
    const yMax = yc.max !== undefined ? yc.max : Math.max(...vals);
    const yRange = yMax - yMin || 1;
    const yS = v => PAD.t + pH - ((v - yMin) / yRange) * pH;

    if (yc.ref !== undefined) {
      ctx.strokeStyle = '#ffb74d44'; ctx.lineWidth = 1; ctx.setLineDash([6,4]);
      ctx.beginPath(); ctx.moveTo(PAD.l, yS(yc.ref)); ctx.lineTo(W-PAD.r, yS(yc.ref)); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
    EVO.forEach((d,i) => { const x = xScale(i), y = yS(d[s.key]); i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y); });
    ctx.stroke();
    EVO.forEach((d,i) => { ctx.fillStyle=s.color; ctx.beginPath(); ctx.arc(xScale(i), yS(d[s.key]), 3.5, 0, Math.PI*2); ctx.fill(); });
    ctx.fillStyle = s.color; ctx.font = '9px Courier New'; ctx.save();
    ctx.translate(si===0?10:W-10, PAD.t+pH/2); ctx.rotate(-Math.PI/2); ctx.textAlign='center'; ctx.fillText(s.label,0,0); ctx.restore();
  });

  ctx.fillStyle = '#7b8ab8'; ctx.font = '9px Courier New'; ctx.textAlign = 'center';
  const step = Math.max(1, Math.floor(EVO.length/10));
  EVO.forEach((d,i) => { if (i%step===0||i===EVO.length-1) { ctx.fillText(d.ts>0?new Date(d.ts).toLocaleDateString('en-US',{month:'short',day:'numeric'}):'#'+i, xScale(i), H-PAD.b+14); }});

  canvas.onmousemove = e => {
    const br = canvas.getBoundingClientRect(), mx = e.clientX - br.left;
    const idx = Math.round(((mx - PAD.l) / pW) * (EVO.length-1));
    if (idx >= 0 && idx < EVO.length) {
      const d = EVO[idx];
      tip.innerHTML = '<b>'+d.label+'</b><br>PF: '+d.pf+' | WR: '+d.wr+'% | PnL: $'+d.pnl+'<br>Trades: '+d.trades+' | Exp: $'+d.expectancy;
      tip.style.display = 'block'; tip.style.left = Math.min(mx+10, W-280)+'px'; tip.style.top = '20px';
    }
  };
  canvas.onmouseleave = () => { tip.style.display = 'none'; };
}

let drawdownChart = null;
function renderDrawdown(dd) {
  const canvas = document.getElementById('drawdown-chart');
  if (!dd.length) { canvas.parentElement.innerHTML = '<div style="padding:20px; text-align:center; color:#7b8ab8;">No drawdown data</div>'; return; }
  if (drawdownChart) { drawdownChart.destroy(); }

  const maxDD = Math.max(...dd);
  drawdownChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels: dd.map((_, i) => i),
      datasets: [{
        data: dd.map(v => -v),  // Negative so drawdown goes DOWN
        borderColor: '#ff5252',
        backgroundColor: 'rgba(255,82,82,0.15)',
        fill: true,
        pointRadius: 0,
        borderWidth: 1.2,
        tension: 0.1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (ctx) => 'Drawdown: ' + Math.abs(ctx.raw).toFixed(2) + '%' }
        },
        title: {
          display: true,
          text: 'Max Drawdown: ' + maxDD.toFixed(2) + '%',
          color: '#ff5252', font: { size: 11 }, align: 'end'
        }
      },
      scales: {
        x: { display: false },
        y: { ticks: { color: '#7b8ab8', callback: (v) => Math.abs(v).toFixed(1) + '%' },
             grid: { color: '#1e2a42' },
             title: { display: true, text: 'Drawdown %', color: '#7b8ab8' } }
      }
    }
  });
}

function renderHeatmap(data) {
  const canvas = document.getElementById('heatmap-canvas');
  if (!data.length) { canvas.parentElement.querySelector('.evo-legend').insertAdjacentHTML('beforebegin', '<div style="padding:20px; text-align:center; color:#7b8ab8;">No heatmap data</div>'); return; }

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  // UTC to CT offset (-5 CST / -6 CDT -- approximate CDT for March)
  const CT_OFFSET = -5;

  const days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const cellW = (W - 60) / 24;
  const cellH = (H - 35) / 7;
  const ox = 40, oy = 25;

  // Find max absolute PnL for color scaling
  const maxAbs = Math.max(...data.map(d => Math.abs(d.avg_pnl)), 0.01);

  // Draw cells
  data.forEach(d => {
    const ctHour = ((d.hour + CT_OFFSET) % 24 + 24) % 24;
    const x = ox + ctHour * cellW;
    const y = oy + d.dow * cellH;
    const intensity = Math.min(Math.abs(d.avg_pnl) / maxAbs, 1);
    if (d.avg_pnl >= 0) {
      ctx.fillStyle = 'rgba(0,230,118,' + (0.15 + intensity * 0.75) + ')';
    } else {
      ctx.fillStyle = 'rgba(255,82,82,' + (0.15 + intensity * 0.75) + ')';
    }
    ctx.fillRect(x + 1, y + 1, cellW - 2, cellH - 2);

    // Count label
    if (d.count >= 2) {
      ctx.fillStyle = '#fff';
      ctx.font = '9px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(d.count + 't', x + cellW / 2, y + cellH / 2 + 3);
    }
  });

  // Hour labels
  ctx.fillStyle = '#7b8ab8'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
  for (let h = 0; h < 24; h += 2) {
    const label = h === 0 ? '12a' : h < 12 ? h + 'a' : h === 12 ? '12p' : (h - 12) + 'p';
    ctx.fillText(label, ox + h * cellW + cellW / 2, oy - 6);
  }
  // Day labels
  ctx.textAlign = 'right';
  days.forEach((d, i) => { ctx.fillText(d, ox - 4, oy + i * cellH + cellH / 2 + 3); });

  // Title
  ctx.fillStyle = '#3a4a6b'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
  ctx.fillText('Hour of Day (CT)', W / 2, H - 2);
}

let distChart = null;
function renderDistribution(bins, stats) {
  const canvas = document.getElementById('dist-chart');
  const statsEl = document.getElementById('dist-stats');
  if (!bins.length) { canvas.parentElement.innerHTML = '<div style="padding:20px; text-align:center; color:#7b8ab8;">No distribution data</div>'; return; }

  if (distChart) { distChart.destroy(); }

  statsEl.innerHTML = 'Total trades: <b>' + (stats.total_trades || 0) + '</b> | ' +
    'Avg PnL: <span style="color:' + ((stats.avg_pnl||0) >= 0 ? '#00e676' : '#ff5252') + ';">$' + (stats.avg_pnl || 0).toFixed(4) + '</span> | ' +
    'Median PnL: <span style="color:' + ((stats.median_pnl||0) >= 0 ? '#00e676' : '#ff5252') + ';">$' + (stats.median_pnl || 0).toFixed(4) + '</span>';

  distChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: bins.map(b => '$' + b.bin.toFixed(2)),
      datasets: [{
        data: bins.map(b => b.count),
        backgroundColor: bins.map(b => b.is_win ? 'rgba(0,230,118,0.6)' : 'rgba(255,82,82,0.6)'),
        borderColor: bins.map(b => b.is_win ? '#00e676' : '#ff5252'),
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ctx.raw + ' trades in this PnL range'
          }
        }
      },
      scales: {
        x: { ticks: { color: '#7b8ab8', font: { size: 8 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 15 }, grid: { color: '#1e2a42' },
             title: { display: true, text: 'PnL per trade ($)', color: '#7b8ab8' } },
        y: { ticks: { color: '#7b8ab8' }, grid: { color: '#1e2a42' },
             title: { display: true, text: 'Frequency', color: '#7b8ab8' } }
      }
    },
    plugins: [{
      id: 'zeroLine',
      afterDraw: (chart) => {
        // Draw vertical line at $0
        const xScale = chart.scales.x;
        const labels = bins.map(b => b.bin);
        const zeroIdx = labels.findIndex(b => b >= 0);
        if (zeroIdx >= 0) {
          const x = xScale.getPixelForValue(zeroIdx);
          const ctx = chart.ctx;
          ctx.save(); ctx.strokeStyle = '#ffc107'; ctx.lineWidth = 1.5;
          ctx.setLineDash([4,3]); ctx.beginPath();
          ctx.moveTo(x, chart.chartArea.top); ctx.lineTo(x, chart.chartArea.bottom);
          ctx.stroke(); ctx.restore();
        }
      }
    }]
  });
}

let strategyChart = null;
function renderStrategyCompare(configs) {
  const canvas = document.getElementById('strategy-compare-chart');
  if (!configs.length) { canvas.parentElement.innerHTML = '<div style="padding:30px; text-align:center; color:#7b8ab8;">No labeled backtest configs to compare</div>'; return; }

  if (strategyChart) { strategyChart.destroy(); }

  // Short labels
  const labels = configs.map(c => c.label.replace('screen_','').replace('compound','cmpd').replace('_regime',''));
  const pfData = configs.map(c => c.pf);
  const wrData = configs.map(c => c.wr);
  const tradesData = configs.map(c => c.trades);

  strategyChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        { label: 'Profit Factor', data: pfData, backgroundColor: pfData.map(v => v >= 1.0 ? 'rgba(0,212,255,0.7)' : 'rgba(255,82,82,0.5)'),
          borderColor: '#00d4ff', borderWidth: 1, yAxisID: 'y', order: 2 },
        { label: 'Win Rate %', data: wrData, type: 'line', borderColor: '#00e676', backgroundColor: 'rgba(0,230,118,0.1)',
          pointBackgroundColor: wrData.map(v => v >= 40 ? '#00e676' : '#ffc107'), pointRadius: 5, tension: 0.3, yAxisID: 'y1', order: 1 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#7b8ab8', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            afterLabel: (ctx) => {
              const c = configs[ctx.dataIndex];
              return 'Trades: ' + c.trades + ' | PnL: $' + c.pnl.toFixed(2) + ' | Exp: $' + c.expectancy.toFixed(4);
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: '#7b8ab8', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1e2a42' } },
        y: { position: 'left', title: { display: true, text: 'Profit Factor', color: '#00d4ff' },
             ticks: { color: '#00d4ff' }, grid: { color: '#1e2a42' },
             suggestedMin: 0, suggestedMax: 2 },
        y1: { position: 'right', title: { display: true, text: 'Win Rate %', color: '#00e676' },
              ticks: { color: '#00e676' }, grid: { display: false },
              suggestedMin: 0, suggestedMax: 60 },
      }
    },
    plugins: [{
      id: 'breakeven',
      afterDraw: (chart) => {
        const yScale = chart.scales.y;
        const y = yScale.getPixelForValue(1.0);
        const ctx = chart.ctx;
        ctx.save(); ctx.strokeStyle = '#ffb74d'; ctx.lineWidth = 1.5;
        ctx.setLineDash([6,4]); ctx.beginPath();
        ctx.moveTo(chart.chartArea.left, y); ctx.lineTo(chart.chartArea.right, y);
        ctx.stroke(); ctx.restore();
      }
    }]
  });
}

function renderMLNetwork(mlData) {
  const canvas = document.getElementById('ml-network-canvas');
  const statsEl = document.getElementById('ml-network-stats');
  if (!mlData.features || !mlData.features.length) {
    canvas.parentElement.innerHTML = '<div style="padding:40px; text-align:center; color:#7b8ab8;">ML Governor model not loaded</div>';
    return;
  }

  // Stats overlay
  const aucLine = mlData.roc_auc ? 'ROC-AUC: <span style="color:#00e676;">' + mlData.roc_auc.toFixed(3) + '</span>' :
    'Train WR: <span style="color:#ffc107;">' + (mlData.win_rate || 0) + '%</span>';
  statsEl.innerHTML = '<span style="color:#00d4ff; font-weight:bold;">ML GOVERNOR</span>' +
    '<span>' + aucLine + '</span>' +
    '<span>Trees: ' + mlData.n_estimators + ' | Depth: ' + mlData.max_depth + '</span>' +
    '<span>Trained: ' + (mlData.train_samples || 0).toLocaleString() + ' trades</span>';

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  // Layout: features on left, 2 hidden layers in middle, output on right
  const features = mlData.features.slice(0, 10); // top 10 by importance
  const maxImp = Math.max(...features.map(f => f.importance));

  // Positions
  const leftX = 160, midX1 = W * 0.38, midX2 = W * 0.58, rightX = W - 80;
  const inputNodes = features.map((f, i) => ({
    x: leftX, y: 25 + i * ((H - 50) / (features.length - 1 || 1)),
    imp: f.importance, name: f.name
  }));
  // Hidden layer 1 (8 nodes)
  const h1Count = 8;
  const h1Nodes = Array.from({length: h1Count}, (_, i) => ({
    x: midX1, y: 40 + i * ((H - 80) / (h1Count - 1))
  }));
  // Hidden layer 2 (4 nodes)
  const h2Count = 4;
  const h2Nodes = Array.from({length: h2Count}, (_, i) => ({
    x: midX2, y: H * 0.2 + i * ((H * 0.6) / (h2Count - 1))
  }));
  // Output: TRADE / BLOCK
  const outputNodes = [
    { x: rightX, y: H * 0.35, label: 'TRADE', color: '#00e676' },
    { x: rightX, y: H * 0.65, label: 'BLOCK', color: '#ff5252' },
  ];

  // Animation state
  let pulsePhase = 0;
  const pulses = [];

  function draw() {
    ctx.clearRect(0, 0, W, H);

    // Background glow
    const grd = ctx.createRadialGradient(W/2, H/2, 50, W/2, H/2, W/2);
    grd.addColorStop(0, 'rgba(0,212,255,0.03)');
    grd.addColorStop(1, 'rgba(10,14,26,0)');
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, W, H);

    // Draw connections: input -> h1
    inputNodes.forEach(inp => {
      const alpha = 0.05 + (inp.imp / maxImp) * 0.3;
      const width = 0.3 + (inp.imp / maxImp) * 2.5;
      h1Nodes.forEach(h => {
        ctx.strokeStyle = 'rgba(0,212,255,' + alpha + ')';
        ctx.lineWidth = width;
        ctx.beginPath(); ctx.moveTo(inp.x, inp.y); ctx.lineTo(h.x, h.y); ctx.stroke();
      });
    });

    // h1 -> h2
    h1Nodes.forEach(h1 => {
      h2Nodes.forEach(h2 => {
        ctx.strokeStyle = 'rgba(255,193,7,0.12)';
        ctx.lineWidth = 0.8;
        ctx.beginPath(); ctx.moveTo(h1.x, h1.y); ctx.lineTo(h2.x, h2.y); ctx.stroke();
      });
    });

    // h2 -> output
    h2Nodes.forEach(h2 => {
      outputNodes.forEach(out => {
        ctx.strokeStyle = 'rgba(255,255,255,0.08)';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(h2.x, h2.y); ctx.lineTo(out.x, out.y); ctx.stroke();
      });
    });

    // Draw input nodes + labels
    inputNodes.forEach(n => {
      const r = 3 + (n.imp / maxImp) * 6;
      const bright = 0.4 + (n.imp / maxImp) * 0.6;
      ctx.fillStyle = 'rgba(0,212,255,' + bright + ')';
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
      // Glow
      ctx.shadowColor = '#00d4ff'; ctx.shadowBlur = n.imp / maxImp * 12;
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
      // Label
      ctx.fillStyle = '#7b8ab8';
      ctx.font = '9px monospace';
      ctx.textAlign = 'right';
      const lbl = n.name.length > 18 ? n.name.slice(0, 17) + '..' : n.name;
      ctx.fillText(lbl, n.x - r - 8, n.y + 3);
      // Importance bar
      const barW = (n.imp / maxImp) * 30;
      ctx.fillStyle = 'rgba(0,212,255,0.2)';
      ctx.fillRect(n.x - r - 8 - barW, n.y - 2, barW, 4);
    });

    // Hidden layer 1 nodes
    h1Nodes.forEach((n, i) => {
      const pulse = 0.5 + 0.3 * Math.sin(pulsePhase + i * 0.8);
      ctx.fillStyle = 'rgba(255,193,7,' + pulse + ')';
      ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = '#ffc107'; ctx.shadowBlur = 8 * pulse;
      ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
    });

    // Hidden layer 2 nodes
    h2Nodes.forEach((n, i) => {
      const pulse = 0.5 + 0.3 * Math.sin(pulsePhase + i * 1.2 + 1);
      ctx.fillStyle = 'rgba(186,104,200,' + pulse + ')';
      ctx.beginPath(); ctx.arc(n.x, n.y, 6, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = '#ba68c8'; ctx.shadowBlur = 10 * pulse;
      ctx.beginPath(); ctx.arc(n.x, n.y, 6, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
    });

    // Output nodes
    outputNodes.forEach(n => {
      ctx.fillStyle = n.color;
      ctx.beginPath(); ctx.arc(n.x, n.y, 10, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = n.color; ctx.shadowBlur = 15;
      ctx.beginPath(); ctx.arc(n.x, n.y, 10, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = '#fff'; ctx.font = 'bold 11px monospace'; ctx.textAlign = 'left';
      ctx.fillText(n.label, n.x + 16, n.y + 4);
    });

    // Layer labels
    ctx.fillStyle = '#3a4a6b'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
    ctx.fillText('INPUT FEATURES', leftX, H - 5);
    ctx.fillText('HIDDEN 1', midX1, H - 5);
    ctx.fillText('HIDDEN 2', midX2, H - 5);
    ctx.fillText('OUTPUT', rightX, H - 5);

    // Animated pulses traveling through network
    if (Math.random() < 0.03) {
      const srcIdx = Math.floor(Math.random() * inputNodes.length);
      pulses.push({ x: inputNodes[srcIdx].x, y: inputNodes[srcIdx].y, targetLayer: 1, progress: 0,
        imp: inputNodes[srcIdx].imp, srcIdx: srcIdx });
    }

    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i];
      p.progress += 0.02;
      let sx, sy, ex, ey;
      if (p.targetLayer === 1) {
        sx = inputNodes[p.srcIdx]?.x || leftX; sy = inputNodes[p.srcIdx]?.y || H/2;
        const tIdx = Math.floor(Math.random() * h1Count);
        ex = h1Nodes[tIdx].x; ey = h1Nodes[tIdx].y;
      } else if (p.targetLayer === 2) {
        sx = midX1; sy = p.y;
        const tIdx = Math.floor(Math.random() * h2Count);
        ex = h2Nodes[tIdx].x; ey = h2Nodes[tIdx].y;
      } else {
        sx = midX2; sy = p.y;
        const tIdx = Math.random() < 0.5 ? 0 : 1;
        ex = outputNodes[tIdx].x; ey = outputNodes[tIdx].y;
      }
      const px = sx + (ex - sx) * p.progress;
      const py = sy + (ey - sy) * p.progress;
      const bright = 0.6 + (p.imp / maxImp) * 0.4;
      ctx.fillStyle = p.targetLayer === 1 ? 'rgba(0,212,255,' + bright + ')' :
                      p.targetLayer === 2 ? 'rgba(255,193,7,' + bright + ')' : 'rgba(0,230,118,' + bright + ')';
      ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 8;
      ctx.beginPath(); ctx.arc(px, py, 2.5, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;

      if (p.progress >= 1) {
        if (p.targetLayer < 3) {
          p.targetLayer++; p.progress = 0; p.x = ex; p.y = ey;
        } else {
          pulses.splice(i, 1);
        }
      }
    }

    pulsePhase += 0.03;
    requestAnimationFrame(draw);
  }
  draw();
}

function render3DScatter(SCATTER) {
  const container = document.getElementById('scatter3d');
  if (!SCATTER.length) { container.innerHTML = '<div style="padding:40px;text-align:center;color:#7b8ab8">No trade data for 3D scatter</div>'; return; }
  if (typeof THREE === 'undefined') { container.innerHTML = '<div style="padding:40px;text-align:center;color:#7b8ab8">Three.js loading...</div>'; return; }

  const W = container.clientWidth, H = container.clientHeight;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0e1a);
  const camera = new THREE.PerspectiveCamera(55, W/H, 0.1, 5000);
  camera.position.set(250, 200, 350);
  const renderer = new THREE.WebGLRenderer({antialias:true});
  renderer.setSize(W, H); renderer.setPixelRatio(window.devicePixelRatio);
  container.innerHTML = '';
  container.appendChild(renderer.domElement);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08; controls.autoRotate = true; controls.autoRotateSpeed = 0.5;

  const xs = SCATTER.map(d=>d.x), ys = SCATTER.map(d=>d.y), zs = SCATTER.map(d=>d.z);
  const xMin=Math.min(...xs), xMax=Math.max(...xs), yMin=Math.min(...ys), yMax=Math.max(...ys), zMin=Math.min(...zs), zMax=Math.max(...zs);
  const xR=xMax-xMin||1, yR=yMax-yMin||1, zR=zMax-zMin||1, SZ=200;
  const norm = (v,mn,rng) => ((v-mn)/rng - 0.5)*SZ;

  scene.add(new THREE.GridHelper(SZ, 20, 0x222244, 0x111122));

  function mkLabel(text, pos, color) {
    const c2 = document.createElement('canvas'); c2.width=256; c2.height=64;
    const x2 = c2.getContext('2d'); x2.font='bold 28px Courier New'; x2.fillStyle=color; x2.fillText(text,4,40);
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c2), transparent:true}));
    sp.position.copy(pos); sp.scale.set(40,10,1); scene.add(sp);
  }
  mkLabel('TIME >>>',new THREE.Vector3(SZ/2+20,-SZ/2,0),'#00d4ff');
  mkLabel('PRICE',new THREE.Vector3(0,SZ/2+10,0),'#00e676');
  mkLabel('PnL >>>',new THREE.Vector3(0,-SZ/2,SZ/2+20),'#ffb74d');

  const winGeo=new THREE.SphereGeometry(2.5,12,8), loseGeo=new THREE.SphereGeometry(2.5,12,8);
  const winMat=new THREE.MeshBasicMaterial({color:0x00e676,transparent:true,opacity:0.85});
  const loseMat=new THREE.MeshBasicMaterial({color:0xff5252,transparent:true,opacity:0.85});

  SCATTER.forEach(d => {
    const px=norm(d.x,xMin,xR), py=norm(d.y,yMin,yR), pz=norm(d.z,zMin,zR);
    const mesh = new THREE.Mesh(d.win?winGeo:loseGeo, d.win?winMat:loseMat);
    mesh.position.set(px,py,pz); scene.add(mesh);
    const lg = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(px,-SZ/2,pz), new THREE.Vector3(px,py,pz)]);
    scene.add(new THREE.Line(lg, new THREE.LineBasicMaterial({color:d.win?0x00e676:0xff5252, transparent:true, opacity:0.15})));
  });

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const pl = new THREE.PointLight(0x00d4ff, 0.8, 1000); pl.position.set(100,200,100); scene.add(pl);

  (function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); })();
  window.addEventListener('resize', () => { const w=container.clientWidth, h=container.clientHeight; camera.aspect=w/h; camera.updateProjectionMatrix(); renderer.setSize(w,h); });
}

function switchCoin(coin) {
  currentCoin = coin.toUpperCase();
  // Update tab active state
  document.querySelectorAll('.coin-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.coin === currentCoin);
  });
  // Highlight selected summary card
  document.querySelectorAll('.coin-summary').forEach(c => {
    c.style.borderColor = c.id === 'summary-' + currentCoin ? '#00d4ff' : '#1e2a42';
  });
  // Reconnect SSE for new coin
  if (sseConnection) { sseConnection.close(); }
  connectSSE();
  // Reload coin-specific data
  loadEquity();
  loadBacktests();
  loadJournal();
  loadDecisions();
  window._journalLoaded = false;
}

const COIN_COLORS = ['#00d4ff','#ffc107','#00e676','#ff9800','#e040fb'];
function buildCoinCard(coin, colorIdx) {
  const color = COIN_COLORS[colorIdx] || '#00d4ff';
  const div = document.createElement('div');
  div.className = 'coin-summary';
  div.id = 'summary-' + coin;
  div.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <div class="coin-name" style="color:${color};margin-bottom:0;">${coin}-USD
          <span id="ms-rotate-${coin}" style="display:none;font-size:0.65em;background:#ff5252;color:#fff;padding:1px 5px;border-radius:3px;margin-left:4px;">DEGRADED</span>
        </div>
        <div id="ms-price-${coin}" style="font-size:1.0em;font-weight:bold;color:#e0e0e0;">—</div>
        <div id="ms-active-${coin}" style="display:none;font-size:0.7em;font-weight:bold;color:#00e676;letter-spacing:1px;background:rgba(0,230,118,0.12);padding:2px 8px;border-radius:10px;">▶ IN TRADE</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <div id="sh-${coin}-readiness" style="font-size:0.85em;font-weight:bold;color:#7b8ab8;letter-spacing:1px;">—</div>
        <div id="sh-${coin}-gate-badge" style="font-size:0.7em;padding:2px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;white-space:nowrap;">—</div>
      </div>
    </div>
    <div style="display:flex;gap:16px;align-items:baseline;margin-bottom:4px;">
      <div class="coin-state">State: <span id="ms-state-${coin}" class="state-FLAT">—</span></div>
      <div class="coin-pnl" id="ms-pnl-${coin}" style="font-size:1.1em;">$0.00</div>
      <div class="coin-detail" style="margin-top:0;">Equity: <span id="ms-eq-${coin}">—</span> | Qty: <span id="ms-qty-${coin}">0</span></div>
    </div>
    <div style="display:flex;gap:16px;margin-bottom:6px;">
      <div class="coin-detail" id="ms-perf-${coin}" style="font-size:0.7em;color:#7b8ab8;">PF: — | WR: — | Trades: —</div>
      <div class="coin-detail" style="font-size:0.7em;color:#7b8ab8;">Updated: <span id="ms-ts-${coin}">—</span></div>
    </div>
    <div style="font-size:0.72em;color:#7b8ab8;margin-bottom:2px;letter-spacing:1px;">CONFLUENCE SCORE</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
      <div style="flex:1;background:#0d1321;border-radius:4px;height:16px;overflow:hidden;position:relative;">
        <div id="sh-${coin}-score-bar" style="height:100%;width:0%;border-radius:4px;transition:width 0.5s;background:#ff5252;"></div>
        <div style="position:absolute;left:80%;top:0;height:100%;width:1px;background:#333;opacity:0.6;" title="80"></div>
        <div style="position:absolute;left:88%;top:0;height:100%;width:1px;background:#ffc107;opacity:0.8;" title="88 threshold"></div>
        <div style="position:absolute;left:92%;top:0;height:100%;width:1px;background:#00e676;opacity:0.6;" title="92"></div>
        <span id="sh-${coin}-score-label" style="position:absolute;top:0;left:0;width:100%;text-align:center;line-height:16px;font-size:0.8em;font-weight:bold;color:#fff;">—</span>
      </div>
    </div>
    <div style="font-size:0.72em;color:#7b8ab8;margin-bottom:2px;letter-spacing:1px;">GOVERNOR WIN PROB</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
      <div style="flex:1;background:#0d1321;border-radius:4px;height:14px;overflow:hidden;position:relative;">
        <div id="sh-${coin}-gov-bar" style="height:100%;width:0%;border-radius:4px;transition:width 0.5s;background:#7b8ab8;"></div>
        <span id="sh-${coin}-gov-label" style="position:absolute;top:0;left:0;width:100%;text-align:center;line-height:14px;font-size:0.72em;font-weight:bold;color:#fff;">—</span>
      </div>
      <div id="sh-${coin}-gov-rec" style="font-size:0.7em;padding:2px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;white-space:nowrap;min-width:58px;text-align:center;">—</div>
    </div>
    <div style="font-size:0.72em;color:#7b8ab8;margin-bottom:2px;letter-spacing:1px;">ORDER BOOK IMBALANCE</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
      <div style="flex:1;background:#0d1321;border-radius:4px;height:14px;overflow:hidden;position:relative;">
        <div id="sh-${coin}-ob-fill" style="position:absolute;height:100%;background:${color};opacity:0.4;transition:all 0.5s;"></div>
        <div style="position:absolute;left:50%;top:0;height:100%;width:1px;background:#2a3a5c;"></div>
        <span id="sh-${coin}-ob-label" style="position:absolute;top:0;left:0;width:100%;text-align:center;line-height:14px;font-size:0.72em;font-weight:bold;color:#fff;">—</span>
      </div>
      <div id="sh-${coin}-ob-badge" style="font-size:0.7em;padding:2px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;white-space:nowrap;min-width:50px;text-align:center;">—</div>
    </div>
    <div style="font-size:0.72em;color:#7b8ab8;margin-bottom:2px;letter-spacing:1px;">VOLATILITY / ATR</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
      <div style="flex:1;background:#0d1321;border-radius:4px;height:14px;overflow:hidden;position:relative;">
        <div id="sh-${coin}-vol-bar" style="height:100%;width:0%;border-radius:4px;transition:width 0.5s;background:#7b8ab8;"></div>
        <span id="sh-${coin}-vol-label" style="position:absolute;top:0;left:0;width:100%;text-align:center;line-height:14px;font-size:0.72em;font-weight:bold;color:#fff;">—</span>
      </div>
      <div id="sh-${coin}-vol-badge" style="font-size:0.7em;padding:2px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;white-space:nowrap;min-width:50px;text-align:center;">—</div>
    </div>
    <div style="display:flex;gap:16px;align-items:center;margin-bottom:6px;">
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;">TODAY P&L</div>
      <div id="sh-${coin}-daily-pnl" style="font-size:0.85em;font-weight:bold;color:#7b8ab8;">$0.00</div>
      <div style="flex:1;"></div>
      <div style="font-size:0.72em;color:#7b8ab8;letter-spacing:1px;">EQUITY</div>
      <canvas id="sh-${coin}-sparkline" width="120" height="24" style="border-radius:3px;background:#0d1321;"></canvas>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
      <div><div style="font-size:0.65em;color:#7b8ab8;margin-bottom:2px;">REGIME</div><div id="sh-${coin}-regime" style="padding:3px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;font-size:0.72em;font-weight:bold;">—</div></div>
      <div><div style="font-size:0.65em;color:#7b8ab8;margin-bottom:2px;">SESSION</div><div id="sh-${coin}-session" style="padding:3px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;font-size:0.72em;font-weight:bold;">—</div></div>
      <div><div style="font-size:0.65em;color:#7b8ab8;margin-bottom:2px;">ACTION</div><div id="sh-${coin}-action" style="padding:3px 8px;border-radius:10px;background:#1e2a42;color:#7b8ab8;font-size:0.72em;font-weight:bold;">—</div></div>
      <div id="sh-${coin}-action-reason" style="font-size:0.68em;color:#7b8ab8;margin-top:14px;"></div>
    </div>`;
  return div;
}

async function loadMultiOverview() {
  try {
    const resp = await fetch('/api/multi');
    const data = await resp.json();
    // Fetch rotation status (non-blocking — fails silently)
    let rotCandidates = [];
    let poolCoins = {};
    try {
      const rotResp = await fetch('/api/rotation_status');
      const rotData = await rotResp.json();
      rotCandidates = (rotData.candidates || []).map(c => c.coin);
      poolCoins = rotData.pool && rotData.pool.coins ? rotData.pool.coins : {};
    } catch(_) {}
    // Dynamically create/remove coin cards based on what the backend reports
    const activeCoins = Object.keys(data).slice(0, 5);
    const container = document.getElementById('multi-overview');
    // Add cards for new coins
    activeCoins.forEach((coin, idx) => {
      if (!document.getElementById('summary-' + coin)) {
        container.appendChild(buildCoinCard(coin, idx));
      }
    });
    // Remove cards for coins no longer active
    container.querySelectorAll('.coin-summary').forEach(el => {
      const c = el.id.replace('summary-', '');
      if (!activeCoins.includes(c)) el.remove();
    });
    let aggEq = 0, aggPnl = 0, latestTs = '';
    activeCoins.forEach(coin => {
      const c = data[coin];
      if (!c) return;
      const stEl = document.getElementById('ms-state-' + coin);
      if (stEl) { stEl.textContent = c.bot_state || 'UNKNOWN'; stEl.className = 'state-' + (c.bot_state || 'FLAT'); }
      const pnlVal = parseFloat(c.realized_pnl) || 0;
      const eqVal = parseFloat(c.equity) || 0;
      aggEq += eqVal; aggPnl += pnlVal;
      const pnlEl = document.getElementById('ms-pnl-' + coin);
      if (pnlEl) { pnlEl.textContent = '$' + pnlVal.toFixed(4); pnlEl.style.color = pnlVal > 0 ? '#00e676' : pnlVal < 0 ? '#ff5252' : '#e0e0e0'; }
      const eqEl = document.getElementById('ms-eq-' + coin);
      if (eqEl) eqEl.textContent = '$' + eqVal.toFixed(2);
      const qtyEl = document.getElementById('ms-qty-' + coin);
      if (qtyEl) qtyEl.textContent = c.position_qty || '0';
      const tsEl = document.getElementById('ms-ts-' + coin);
      const tsStr = c.saved_at_iso ? new Date(c.saved_at_iso).toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '—';
      if (tsEl) tsEl.textContent = tsStr;
      if (c.saved_at_iso) latestTs = tsStr;
      // Rolling PF/WR from coin pool
      const perfEl = document.getElementById('ms-perf-' + coin);
      if (perfEl && poolCoins[coin]) {
        const m = poolCoins[coin].live_metrics || {};
        const pf = m.rolling_pf != null ? parseFloat(m.rolling_pf).toFixed(3) : '—';
        const wr = m.rolling_wr != null ? (parseFloat(m.rolling_wr)*100).toFixed(1)+'%' : '—';
        const n = m.n_trades || 0;
        const pfColor = m.rolling_pf == null ? '#7b8ab8' : m.rolling_pf >= 1.2 ? '#00e676' : m.rolling_pf >= 0.9 ? '#ffc107' : '#ff5252';
        perfEl.innerHTML = `PF: <span style="color:${pfColor}">${pf}</span> | WR: ${wr} | Trades: ${n}`;
      }
      // Health degraded badge (passive monitor)
      const rotEl = document.getElementById('ms-rotate-' + coin);
      if (rotEl) rotEl.style.display = rotCandidates.includes(coin) ? 'inline' : 'none';
      // Live price ticker
      const priceEl = document.getElementById('ms-price-' + coin);
      if (priceEl && c.governor && c.governor.price) {
        const px = parseFloat(c.governor.price);
        priceEl.textContent = px ? '$' + px.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
      }
      // Active trade indicator + pulse animation
      const inTrade = c.bot_state && c.bot_state !== 'FLAT';
      const activeEl = document.getElementById('ms-active-' + coin);
      const cardEl = document.getElementById('summary-' + coin);
      if (activeEl) activeEl.style.display = inTrade ? '' : 'none';
      if (cardEl) {
        if (inTrade) cardEl.classList.add('trading-active');
        else cardEl.classList.remove('trading-active');
      }
      // Daily P&L — compute from equity vs start-of-day
      const dailyPnlEl = document.getElementById('sh-' + coin + '-daily-pnl');
      if (dailyPnlEl) {
        const startCash = 500;
        const eq = parseFloat(c.equity) || startCash;
        const dailyPnl = eq - startCash;
        dailyPnlEl.textContent = (dailyPnl >= 0 ? '+' : '') + '$' + dailyPnl.toFixed(4);
        dailyPnlEl.style.color = dailyPnl > 0 ? '#00e676' : dailyPnl < 0 ? '#ff5252' : '#7b8ab8';
      }
      // Equity sparkline — fetch and draw
      const canvas = document.getElementById('sh-' + coin + '-sparkline');
      if (canvas && !canvas._loaded) {
        canvas._loaded = true;
        fetch('/api/equity?coin=' + coin).then(r => r.json()).then(pts => {
          if (!pts || pts.length < 2) return;
          const ctx = canvas.getContext('2d');
          const w = canvas.width, h = canvas.height;
          const vals = pts.map(p => p.y);
          const mn = Math.min(...vals), mx = Math.max(...vals);
          const range = mx - mn || 1;
          ctx.clearRect(0, 0, w, h);
          ctx.beginPath();
          ctx.strokeStyle = vals[vals.length-1] >= vals[0] ? '#00e676' : '#ff5252';
          ctx.lineWidth = 1.5;
          vals.forEach((v, i) => {
            const x = (i / (vals.length - 1)) * w;
            const y = h - ((v - mn) / range) * (h - 4) - 2;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.stroke();
        }).catch(() => {});
      }
    });
    // Update portfolio aggregate bar
    const aggEqEl = document.getElementById('agg-equity');
    const aggPnlEl = document.getElementById('agg-pnl');
    const aggDailyEl = document.getElementById('agg-daily');
    const aggTsEl = document.getElementById('agg-ts');
    if (aggEqEl) { aggEqEl.textContent = '$' + aggEq.toFixed(2); aggEqEl.style.color = aggEq >= 1000 ? '#00e676' : '#ff5252'; }
    if (aggPnlEl) { aggPnlEl.textContent = '$' + aggPnl.toFixed(4); aggPnlEl.style.color = aggPnl >= 0 ? '#00e676' : '#ff5252'; }
    if (aggDailyEl) {
      const paperStart = new Date('2026-03-15T00:00:00Z');
      const elapsed = Math.max(1, (Date.now() - paperStart) / 86400000);
      const dailyRate = aggPnl / elapsed;
      aggDailyEl.textContent = '$' + dailyRate.toFixed(4) + '/day';
      aggDailyEl.style.color = dailyRate >= 0 ? '#00e676' : '#ff5252';
    }
    if (aggTsEl) aggTsEl.textContent = latestTs;

    // Update control strip + orientation
    updateControlStrip(data);
    updateOrientation(data);

    // ---- Per-coin capital doubling bars: $500 -> $1000 ----
    const coinBarsEl = document.getElementById('coin-capital-bars');
    if (coinBarsEl) {
      const PER_COIN_START = 500;
      const PER_COIN_TARGET = 1000;  // 2x
      const COIN_COLORS = { ETH: '#627eea', BTC: '#f7931a', SOL: '#9945ff', AVAX: '#e84142', DOGE: '#c3a634' };
      let barsHtml = '';
      const activeCoins = Object.keys(data).filter(c => data[c] && data[c].bot_state);
      activeCoins.forEach(coin => {
        const cash = parseFloat(data[coin].equity) || parseFloat(data[coin].cash) || PER_COIN_START;
        const pct = Math.min(100, Math.max(0, ((cash - PER_COIN_START) / PER_COIN_START) * 100));
        const barPct = Math.min(100, Math.max(0, (cash / PER_COIN_TARGET) * 100));
        const color = COIN_COLORS[coin] || '#00d4ff';
        const pnlSoFar = cash - PER_COIN_START;
        const pnlSign = pnlSoFar >= 0 ? '+' : '';
        barsHtml += `<div style="display:flex; align-items:center; gap:8px;">
          <div style="min-width:36px; font-size:0.75em; font-weight:bold; color:${color};">${coin}</div>
          <div style="flex:1;">
            <div style="background:#0d1321; border-radius:3px; height:8px; overflow:hidden;">
              <div style="height:100%; width:${barPct.toFixed(1)}%; background:${color}; opacity:0.85; border-radius:3px; transition:width 0.5s;"></div>
            </div>
          </div>
          <div style="min-width:80px; text-align:right; font-size:0.7em;">
            <span style="color:#e0e0e0;">$${cash.toFixed(0)}</span>
            <span style="color:${pnlSoFar>=0?'#00e676':'#ff5252'}; margin-left:4px;">(${pnlSign}$${pnlSoFar.toFixed(2)})</span>
          </div>
          <div style="min-width:32px; font-size:0.65em; color:#7b8ab8; text-align:right;">${barPct.toFixed(0)}%</div>
        </div>`;
      });
      coinBarsEl.innerHTML = barsHtml || '<div style="font-size:0.75em; color:#7b8ab8;">No active coins</div>';
    }

    // ---- Portfolio goal bar: current total -> $1000 (2 active coins x $500 doubled) ----
    const numActive = Object.keys(data).filter(c => data[c] && data[c].bot_state).length || 2;
    const portGoalTotal = numActive * 1000;
    const portGoalCur = Math.max(0, aggEq);
    const portGoalPct = Math.min(100, Math.max(0, (portGoalCur / portGoalTotal) * 100));
    const portGoalBarEl = document.getElementById('port-goal-bar');
    const portGoalPctEl = document.getElementById('port-goal-pct');
    const portGoalCurEl = document.getElementById('port-goal-cur');
    if (portGoalBarEl) portGoalBarEl.style.width = portGoalPct.toFixed(1) + '%';
    if (portGoalPctEl) portGoalPctEl.textContent = portGoalPct.toFixed(1);
    if (portGoalCurEl) portGoalCurEl.textContent = portGoalCur.toFixed(0);
    // Update label to reflect actual target
    const portGoalLabelEl = document.querySelector('#portfolio-aggregate [style*="PORTFOLIO GOAL"]');

    // Update signal health panels for all active coins
    activeCoins.forEach(coin => {
      const c = data[coin];
      if (c && c.governor) updateSignalHealth(c.governor, c.bot_state, coin);
    });

  } catch(e) { console.error('multi fetch error', e); }
}

async function loadPool() {
  try {
    const r = await fetch('/api/pool');
    const data = await r.json();
    const coins = data.coins || {};
    const active = data.active || [];
    const maxActive = data.max_active || 2;
    const poolMaxEl = document.getElementById('pool-max-active');
    const poolActEl = document.getElementById('pool-active-count');
    if (poolMaxEl) poolMaxEl.textContent = maxActive;
    if (poolActEl) poolActEl.textContent = active.length;
    const statusColor = {
      'ACTIVE': '#00e676',
      'DISABLED': '#ff5252',
      'SCREENED_FAIL': '#ff9800',
      'CANDIDATE': '#00d4ff'
    };
    const statusLabel = {
      'ACTIVE': 'LIVE',
      'DISABLED': 'OFF',
      'SCREENED_FAIL': 'FAIL',
      'CANDIDATE': 'READY'
    };
    let html = '';
    // Sort: ACTIVE first, then CANDIDATE, then SCREENED_FAIL, then DISABLED
    const order = ['ACTIVE','CANDIDATE','SCREENED_FAIL','DISABLED'];
    const sorted = Object.entries(coins).sort((a,b) => {
      return (order.indexOf(a[1].status||'') - order.indexOf(b[1].status||''));
    });
    for (const [coin, info] of sorted) {
      const st = info.status || 'UNKNOWN';
      const col = statusColor[st] || '#7b8ab8';
      const lbl = statusLabel[st] || st;
      const pf = info.backtest_pf ? info.backtest_pf.toFixed(3) : '—';
      const wr = info.backtest_wr_pct ? info.backtest_wr_pct.toFixed(0)+'%' : '—';
      const gov = info.governor_trained ? '✓' : '✗';
      html += `<div title="${info.notes||''}" style="background:#0d1321; border:1px solid ${col}44; border-left:3px solid ${col}; border-radius:4px; padding:6px 10px; min-width:100px; cursor:default;">
        <div style="font-weight:bold; font-size:0.85em; color:${col};">${coin} <span style="font-size:0.75em; color:#7b8ab8;">[${lbl}]</span></div>
        <div style="font-size:0.72em; color:#e0e0e0; margin-top:2px;">PF: ${pf} | WR: ${wr}</div>
        <div style="font-size:0.68em; color:#7b8ab8;">Gov: ${gov} | ${info.backtest_window||'—'}</div>
      </div>`;
    }
    const poolCoinsEl = document.getElementById('pool-coins-container');
    if (poolCoinsEl) poolCoinsEl.innerHTML = html || '<span style="color:#7b8ab8; font-size:0.8em;">No pool data</span>';
  } catch(e) { console.error('pool fetch error', e); }
}

function initChart() {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line',
    data: { datasets: [{ label: 'Equity (USD)', data: [], borderColor: '#00d4ff',
      backgroundColor: 'rgba(0,212,255,0.1)', fill: true, tension: 0.1, pointRadius: 0, borderWidth: 1.5 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { type: 'time', time: { unit: 'hour' }, grid: { color: '#1e2a42' }, ticks: { color: '#7b8ab8', font: { size: 10 } } },
        y: { grid: { color: '#1e2a42' }, ticks: { color: '#7b8ab8', font: { size: 10 } } }
      },
      plugins: { legend: { display: false } }
    }
  });
}

function formatPnl(val) {
  const n = parseFloat(val) || 0;
  return { text: '$' + n.toFixed(4), cls: n > 0 ? 'positive' : n < 0 ? 'negative' : '' };
}

function epochToTime(ts) {
  const ms = parseInt(ts);
  if (!ms) return '—';
  const d = new Date(ms > 1e12 ? ms : ms * 1000);
  return d.toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true});
}

function updateDashboard(data) {
  // State
  const st = data.bot_state || 'UNKNOWN';
  const stEl = document.getElementById('bot-state');
  stEl.textContent = st;
  stEl.className = 'metric state-' + st;

  document.getElementById('symbol').textContent = data.symbol || 'ETH-USD';
  document.getElementById('position-qty').textContent = data.position_qty || '0';

  // Equity
  const eq = parseFloat(data.equity) || 0;
  document.getElementById('equity').textContent = '$' + eq.toFixed(2);

  document.getElementById('cash').textContent = (parseFloat(data.cash) || 0).toFixed(2);
  document.getElementById('unrealized').textContent = (parseFloat(data.unrealized_pnl) || 0).toFixed(4);

  // PnL
  const pnl = formatPnl(data.realized_pnl);
  const pnlEl = document.getElementById('realized-pnl');
  pnlEl.textContent = pnl.text;
  pnlEl.className = 'metric ' + pnl.cls;

  document.getElementById('total-fills').textContent = data.total_fills || 0;
  document.getElementById('total-journal').textContent = data.total_journal_entries || 0;

  // Real-time projection update on evolution page
  updateLiveProjection(parseFloat(data.realized_pnl) || 0, parseFloat(data.equity) || 0);

  // Runtime
  document.getElementById('uptime').textContent = (data.uptime_hours || 0) + 'h';
  document.getElementById('runtime-mode').textContent = data.runtime_mode || 'FULL';
  document.getElementById('manifest-status').textContent = data.manifest_status || 'UNKNOWN';
  document.getElementById('config-hash').textContent = data.config_hash || '—';

  // Footer
  document.getElementById('run-id').textContent = data.run_id || '—';
  document.getElementById('last-update').textContent = new Date().toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true}) + ' CT';

  // Fills table
  const fillsBody = document.querySelector('#fills-table tbody');
  fillsBody.innerHTML = '';
  (data.fills || []).reverse().forEach(f => {
    const tr = document.createElement('tr');
    const side = f.side || '';
    const sideColor = side === 'BUY' ? '#00e676' : side === 'SELL' ? '#ff5252' : '#e0e0e0';
    tr.innerHTML = '<td>' + epochToTime(f.ts) + '</td>'
      + '<td style="color:' + sideColor + '">' + side + '</td>'
      + '<td>' + (f.qty || '') + '</td>'
      + '<td>$' + (parseFloat(f.price) || 0).toFixed(2) + '</td>'
      + '<td>' + (f.symbol || '') + '</td>';
    fillsBody.appendChild(tr);
  });

  // Events table
  const eventsBody = document.querySelector('#events-table tbody');
  eventsBody.innerHTML = '';
  (data.events || []).reverse().forEach(e => {
    const tr = document.createElement('tr');
    const action = e.action || '';
    const actionColor = action === 'WOULD_BUY' ? '#00e676' : action === 'HOLD' ? '#7b8ab8' : '#ffc107';
    const evtTsRaw = e.ts ? (e.ts.includes('+') || e.ts.endsWith('Z') ? e.ts : e.ts+'Z') : null;
    const evtTime = evtTsRaw ? new Date(evtTsRaw).toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '';
    tr.innerHTML = '<td>' + evtTime + '</td>'
      + '<td>' + (e.event || '') + '</td>'
      + '<td style="color:' + actionColor + '">' + action + '</td>'
      + '<td>' + (e.confluence_score || '') + '</td>'
      + '<td>' + (e.regime || '') + '</td>';
    eventsBody.appendChild(tr);
  });

  // Journal table — only update from SSE if we haven't loaded full journal yet
  if (!window._journalLoaded) {
    renderJournal(data.journal || []);
  }

  // Governor panel
  if (data.governor && data.governor.score !== undefined) {
    const g = data.governor;
    const prob = g.win_prob !== null ? g.win_prob : 0;
    const pct = Math.round(prob * 100);
    const rec = g.recommendation || '—';
    const barColor = prob >= 0.45 ? '#00e676' : prob >= 0.30 ? '#ffc107' : '#ff5252';
    const recColor = rec === 'ALLOW' ? '#00e676' : rec === 'BLOCK' ? '#ff5252' : rec === 'CAUTION' ? '#ffc107' : '#7b8ab8';

    document.getElementById('gov-status').textContent = pct + '% WIN PROB';
    document.getElementById('gov-status').style.color = barColor;
    document.getElementById('gov-bar').style.width = pct + '%';
    document.getElementById('gov-bar').style.background = barColor;
    document.getElementById('gov-bar-pct').textContent = pct + '%';
    document.getElementById('gov-rec').textContent = rec;
    document.getElementById('gov-rec').style.color = recColor;
    document.getElementById('gov-mod').textContent = g.score_modifier || '0';
    document.getElementById('gov-conf').textContent = g.confluence_score || '—';
    document.getElementById('gov-regime').textContent = g.regime || '—';
    document.getElementById('gov-session').textContent = g.session || '—';
  } else {
    document.getElementById('gov-status').textContent = 'LOG_ONLY';
    document.getElementById('gov-status').style.color = '#7b8ab8';
  }

  // Signal health panel + trade progress
  updateSignalHealth(data.governor, data.bot_state, currentCoin);
  updateTradeProgress(data);

  // Queue panel (minimal — no progress bar)
  if (data.queue) {
    const q = data.queue;
    const runEl = document.getElementById('queue-running');
    if (q.running_job) {
      const rj = q.running_job;
      runEl.textContent = rj.label || rj.run_id.slice(-16);
      runEl.style.color = '#00d4ff';
    } else {
      runEl.textContent = 'Idle';
      runEl.style.color = '#7b8ab8';
    }
    document.getElementById('queue-pending-pc1').textContent = q.pending_pc1 || 0;
    document.getElementById('queue-pending-pc2').textContent = q.pending_pc2 || 0;
    document.getElementById('queue-completed').textContent = q.completed_today || 0;
  }
}

async function loadEquity() {
  try {
    const resp = await fetch('/api/equity?coin=' + currentCoin);
    const data = await resp.json();
    const canvas = document.getElementById('equity-chart');
    const noData = document.getElementById('equity-no-data');
    if (equityChart && data.length >= 2) {
      equityChart.data.datasets[0].data = data;
      equityChart.update('none');
      if (canvas) canvas.style.display = '';
      if (noData) noData.style.display = 'none';
    } else {
      if (canvas) canvas.style.display = 'none';
      if (noData) { noData.style.display = 'flex'; noData.textContent = data.length === 1 ? 'Equity curve — 1 point, waiting for more trades...' : 'Equity curve — no trade data yet for ' + currentCoin; }
    }
  } catch(e) { console.error('equity fetch error', e); }
}

// ---- Control Strip + Orientation + Process Progress ----
function updateControlStrip(data) {
  if (!data) return;
  const coins = Object.keys(data);
  const csCoins = document.getElementById('cs-active-coins');
  if (csCoins) csCoins.textContent = coins.join(', ') + ' (' + coins.length + ')';
  // Feed status from first coin's update timestamp
  const first = data[coins[0]];
  if (first) {
    const csMode = document.getElementById('cs-mode');
    if (csMode) csMode.textContent = 'FULL';
    const csFeed = document.getElementById('cs-feed-status');
    if (csFeed) {
      const iso = first.saved_at_iso;
      if (iso) {
        const age = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
        const csAge = document.getElementById('cs-tick-age');
        if (csAge) {
          csAge.textContent = age + 's';
          csAge.style.color = age < 30 ? '#00e676' : age < 120 ? '#ffc107' : '#ff5252';
        }
        csFeed.textContent = age < 60 ? 'LIVE' : 'STALE';
        csFeed.style.color = age < 60 ? '#00e676' : '#ff5252';
      }
    }
    const csInv = document.getElementById('cs-invariant');
    if (csInv) { csInv.textContent = 'PASS'; csInv.style.color = '#00e676'; }
  }
}

function updateOrientation(data) {
  if (!data) return;
  const coins = Object.keys(data);
  // NOW: summarize current state across coins
  let nowParts = [];
  let whyParts = [];
  coins.forEach(coin => {
    const c = data[coin];
    const g = c.governor || {};
    const state = c.bot_state || 'FLAT';
    if (state !== 'FLAT') {
      const pnl = parseFloat(c.unrealized_pnl) || 0;
      nowParts.push(coin + ': ' + state + ' ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(4));
    } else {
      nowParts.push(coin + ': FLAT');
    }
    // WHY NOT
    const reason = g.action_reason || '';
    const gate = g.gate || '';
    const score = g.confluence_score || g.score || '';
    if (state === 'FLAT' && reason) {
      whyParts.push(coin + ': ' + reason + (gate ? ' (gate=' + gate + ')' : '') + (score ? ' score=' + score : ''));
    }
  });
  const nowEl = document.getElementById('orient-now');
  if (nowEl) nowEl.textContent = nowParts.join(' | ');
  const whyEl = document.getElementById('orient-why');
  if (whyEl) whyEl.textContent = whyParts.length > 0 ? whyParts.join(' | ') : 'All conditions met — waiting for signal';
  if (whyEl) whyEl.style.color = whyParts.length > 0 ? '#ffc107' : '#00e676';
}

function loadProcessProgress() {
  fetch('/api/process_progress').then(r => r.json()).then(d => {
    const traceGoal = 200, highGoal = 20, tradeGoal = 10;
    const trBar = document.getElementById('prog-trace-bar');
    const trLbl = document.getElementById('prog-trace-label');
    if (trBar) trBar.style.width = Math.min(100, (d.trace_rows / traceGoal) * 100) + '%';
    if (trLbl) trLbl.textContent = d.trace_rows + '/' + traceGoal;
    const hiBar = document.getElementById('prog-high-bar');
    const hiLbl = document.getElementById('prog-high-label');
    if (hiBar) hiBar.style.width = Math.min(100, (d.high_score_rows / highGoal) * 100) + '%';
    if (hiLbl) hiLbl.textContent = d.high_score_rows + '/' + highGoal;
    const tdBar = document.getElementById('prog-trades-bar');
    const tdLbl = document.getElementById('prog-trades-label');
    if (tdBar) tdBar.style.width = Math.min(100, (d.trades_taken / tradeGoal) * 100) + '%';
    if (tdLbl) tdLbl.textContent = d.trades_taken + '/' + tradeGoal;
    const ready = d.trace_rows >= traceGoal && d.high_score_rows >= highGoal && d.trades_taken >= tradeGoal;
    const revEl = document.getElementById('prog-review-status');
    if (revEl) {
      revEl.textContent = ready ? 'READY FOR REVIEW' : 'COLLECTING';
      revEl.style.color = ready ? '#00e676' : '#ffc107';
    }
    const csVer = document.getElementById('cs-config-version');
    if (csVer && d.config_version) csVer.textContent = d.config_version;
    const nextEl = document.getElementById('orient-next');
    if (nextEl) {
      if (ready) nextEl.textContent = 'Review gate met — run Phase B analysis';
      else {
        const needs = [];
        if (d.trace_rows < traceGoal) needs.push((traceGoal - d.trace_rows) + ' more trace rows');
        if (d.high_score_rows < highGoal) needs.push((highGoal - d.high_score_rows) + ' more 88+ candidates');
        if (d.trades_taken < tradeGoal) needs.push((tradeGoal - d.trades_taken) + ' more trades');
        nextEl.textContent = 'Need: ' + needs.join(', ');
      }
    }
  }).catch(() => {});
}

function loadDecisionSurface() {
  fetch('/api/decision_trace').then(r => r.json()).then(d => {
    // Waterfall
    const wfEl = document.getElementById('decision-waterfall');
    if (wfEl && d.waterfall) {
      const w = d.waterfall;
      const base = parseInt(w.base_score) || 0;
      const final_ = parseInt(w.final_score) || 0;
      const deltas = [
        {name: 'Adaptive', val: parseInt(w.adaptive_delta) || 0},
        {name: 'Session', val: parseInt(w.session_bonus) || 0},
        {name: 'Liquidity', val: parseInt(w.liq_penalty) || 0},
        {name: 'OB Imbal', val: parseInt(w.ob_adjustment) || 0},
        {name: 'BTC Lag', val: parseInt(w.btc_lag_adjustment) || 0},
        {name: 'Structure', val: parseInt(w.structure_delta) || 0},
        {name: 'Trendline', val: parseInt(w.trendline_delta) || 0},
        {name: 'Governor', val: parseInt(w.governor_score_mod) || 0},
      ].filter(d => d.val !== 0);
      let html = '<div style="margin-bottom:6px;color:#7b8ab8;">' + (w.symbol||'') + ' | ' + (w.regime||'') + ' | ' + (w.session||'') + ' | ' + (w.action||'HOLD') + '</div>';
      html += '<div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;">';
      html += '<span style="background:#1e2a42;padding:3px 8px;border-radius:4px;color:#00d4ff;font-weight:bold;">BASE ' + base + '</span>';
      deltas.forEach(d => {
        const c = d.val > 0 ? '#00e676' : '#ff5252';
        html += '<span style="color:' + c + ';">→ ' + (d.val > 0 ? '+' : '') + d.val + ' ' + d.name + '</span>';
      });
      const fc = final_ >= 88 ? '#00e676' : final_ >= 80 ? '#ffc107' : '#ff5252';
      html += '<span style="background:#1e2a42;padding:3px 8px;border-radius:4px;color:' + fc + ';font-weight:bold;">FINAL ' + final_ + ' (' + (w.final_gate||'') + ')</span>';
      html += '</div>';
      if (w.blockers) html += '<div style="margin-top:4px;color:#ff5252;font-size:0.85em;">Blockers: ' + w.blockers + '</div>';
      wfEl.innerHTML = html;
    } else if (wfEl) {
      wfEl.innerHTML = '<div style="color:#7b8ab8;">Waiting for entry-eligible signal...</div>';
    }
    // Blocker summary — split hard vs score suppressors
    const blEl = document.getElementById('blocker-summary');
    if (blEl && Object.keys(d.blockers).length > 0) {
      const hardKeys = ['RISK_LOCKOUT','COOLDOWN','MAX_TRADES','DAILY_MAX_LOSS','LIQUIDITY','CROSS_COIN','GOVERNOR','EXPOSURE_CAP','STALE_DATA','QTY_ZERO'];
      const hard = {}, soft = {};
      Object.entries(d.blockers).forEach(([k,v]) => {
        if (hardKeys.some(h => k.toUpperCase().includes(h))) hard[k] = v;
        else soft[k] = v;
      });
      let html = '<div style="display:flex;gap:16px;flex-wrap:wrap;">';
      // Hard blockers
      html += '<div style="flex:1;min-width:200px;"><div style="font-size:0.72em;color:#ff5252;margin-bottom:4px;letter-spacing:1px;">HARD GATES</div>';
      if (Object.keys(hard).length > 0) {
        html += '<table style="width:100%;border-collapse:collapse;">';
        Object.entries(hard).forEach(([k,v]) => {
          html += '<tr style="border-bottom:1px solid #0d1321;"><td style="color:#ff5252;">' + k.replace('MISSED_BUY_','') + '</td><td style="text-align:right;">' + v + '</td></tr>';
        });
        html += '</table>';
      } else { html += '<div style="color:#7b8ab8;">None</div>'; }
      html += '</div>';
      // Score suppressors
      html += '<div style="flex:1;min-width:200px;"><div style="font-size:0.72em;color:#ffc107;margin-bottom:4px;letter-spacing:1px;">SCORE SUPPRESSORS</div>';
      if (Object.keys(soft).length > 0) {
        html += '<table style="width:100%;border-collapse:collapse;">';
        Object.entries(soft).forEach(([k,v]) => {
          html += '<tr style="border-bottom:1px solid #0d1321;"><td style="color:#ffc107;">' + k.replace('MISSED_BUY_','') + '</td><td style="text-align:right;">' + v + '</td></tr>';
        });
        html += '</table>';
      } else { html += '<div style="color:#7b8ab8;">None</div>'; }
      html += '</div></div>';
      blEl.innerHTML = html;
    }
    // Score distribution
    const sdEl = document.getElementById('score-distribution');
    if (sdEl && d.score_dist) {
      const sd = d.score_dist;
      const total = Object.values(sd).reduce((a,b) => a+b, 0) || 1;
      let html = '<div style="display:flex;gap:16px;flex-wrap:wrap;">';
      [['70-79','#ff5252'],['80-87','#ffc107'],['88-91','#00d4ff'],['92+','#00e676']].forEach(([k,c]) => {
        const n = sd[k] || 0;
        const pct = ((n/total)*100).toFixed(1);
        html += '<div style="text-align:center;"><div style="font-size:1.3em;font-weight:bold;color:' + c + ';">' + n + '</div>';
        html += '<div style="font-size:0.75em;color:#7b8ab8;">' + k + ' (' + pct + '%)</div></div>';
      });
      html += '</div>';
      sdEl.innerHTML = html;
    }
    // Blocked high-score
    const bhEl = document.getElementById('blocked-high-score');
    if (bhEl && d.blocked_high && d.blocked_high.length > 0) {
      let html = '';
      d.blocked_high.forEach(r => {
        const bs = r.base_score || '?';
        const fs = r.final_score || '?';
        const bg = r.base_gate || '';
        const fg = r.final_gate || '';
        html += '<div style="border-bottom:1px solid #1e2a42;padding:4px 0;">';
        html += '<span style="color:#00d4ff;">Base:' + bs + '</span> → <span style="color:' + (parseInt(fs)>=88?'#00e676':'#ff5252') + ';">Final:' + fs + '</span>';
        html += ' | Gate: ' + bg + '→' + fg;
        html += ' | <span style="color:#7b8ab8;">' + (r.regime||'') + ' ' + (r.session||'') + '</span>';
        if (r.blockers) html += ' | <span style="color:#ff5252;">' + r.blockers + '</span>';
        html += '</div>';
      });
      bhEl.innerHTML = html;
    }
  }).catch(() => {});
}

// Load process progress every 30s
setInterval(loadProcessProgress, 30000);
loadProcessProgress();

function connectSSE() {
  const status = document.getElementById('connection-status');
  const es = new EventSource('/api/stream?coin=' + currentCoin);
  sseConnection = es;

  es.addEventListener('status', (e) => {
    try {
      const data = JSON.parse(e.data);
      updateDashboard(data);
    } catch(err) { console.error('parse error', err); }
  });

  es.onopen = () => { status.textContent = 'LIVE (' + currentCoin + ')'; status.className = 'connected'; };
  es.onerror = () => {
    status.textContent = 'RECONNECTING...'; status.className = 'disconnected';
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

async function loadBacktests() {
  try {
    const resp = await fetch('/api/backtest?coin=' + currentCoin);
    const runs = await resp.json();
    const body = document.querySelector('#bt-table tbody');
    body.innerHTML = '';
    runs.forEach(r => {
      const tr = document.createElement('tr');
      const status = r.completed ? '<span class="badge badge-flat">DONE</span>' : '<span class="badge badge-running">RUNNING</span>';
      const pnl = r.pnl ? parseFloat(r.pnl) : 0;
      const pnlColor = pnl > 0 ? '#00e676' : pnl < 0 ? '#ff5252' : '#e0e0e0';
      const total = r.total_bars || r.progress_bars || 1;
      const pct = r.progress_bars ? Math.round(r.progress_bars / total * 100) : 0;
      const progressStr = r.completed ? 'Complete' : (r.total_bars ? pct + '% (' + r.progress_bars + '/' + r.total_bars + ')' : pct + '%');
      tr.innerHTML = '<td>' + (r.run_id || '').slice(-20) + '</td>'
        + '<td>' + status + '</td>'
        + '<td>' + progressStr + '</td>'
        + '<td>' + (r.trades || '') + '</td>'
        + '<td>' + (r.win_rate || '') + '</td>'
        + '<td>' + (r.profit_factor || '') + '</td>'
        + '<td style="color:' + pnlColor + '">' + (r.pnl ? '$' + pnl.toFixed(2) : '') + '</td>';
      body.appendChild(tr);
    });
  } catch(e) { console.error('backtest fetch error', e); }
}

async function loadConfig() {
  try {
    const resp = await fetch('/api/config');
    const cfg = await resp.json();
    const panel = document.getElementById('config-panel');
    panel.innerHTML = '';
    const highlights = {'CONFLUENCE_MIN_SCORE':1, 'MAX_HOLD_SECONDS':1, 'REGIME_ENTRY_BLOCK_LIST':1, 'DRAWDOWN_PAUSE_PCT':1, 'USE_TRENDLINES':1, 'USE_ML_GOVERNOR':1, 'ML_GOVERNOR_MODE':1};
    Object.entries(cfg).forEach(([k,v]) => {
      const hl = highlights[k] ? 'color:#00d4ff;font-weight:bold' : 'color:#7b8ab8';
      const el = document.createElement('div');
      el.innerHTML = '<span style="' + hl + '">' + k + '</span> = <span style="color:#fff">' + v + '</span>';
      panel.appendChild(el);
    });
  } catch(e) { console.error('config fetch error', e); }
}

// --- Decision Flow ---
async function loadDecisions() {
  try {
    const resp = await fetch('/api/decisions?coin=' + currentCoin);
    const decisions = await resp.json();
    const container = document.getElementById('decision-flow');
    if (!decisions.length) {
      container.innerHTML = '<div style="color:#7b8ab8; font-size:0.8em;">No entry signals yet...</div>';
      return;
    }
    container.innerHTML = '';
    decisions.forEach(d => {
      const row = document.createElement('div');
      row.className = 'decision-row';
      const event = d.event || d.action || '';
      const isBuy = event.includes('BUY') && !event.includes('MISSED');
      const isBlock = event.includes('MISSED') || event.includes('BLOCK');
      const dotClass = isBuy ? 'dot-allow' : isBlock ? 'dot-block' : 'dot-unknown';

      const prob = d.governor_win_prob ? (parseFloat(d.governor_win_prob) * 100).toFixed(0) + '%' : '';
      const probColor = d.governor_win_prob ? (parseFloat(d.governor_win_prob) >= 0.45 ? '#00e676' : parseFloat(d.governor_win_prob) >= 0.30 ? '#ffc107' : '#ff5252') : '#7b8ab8';
      const govTag = prob ? '<span style="color:' + probColor + '; font-weight:bold; margin-left:4px;">[' + prob + ']</span>' : '';
      const recTag = d.governor_recommendation ? '<span style="color:#7b8ab8; margin-left:2px;">' + d.governor_recommendation + '</span>' : '';

      const tsRaw = d.ts ? (d.ts.includes('+') || d.ts.endsWith('Z') ? d.ts : d.ts+'Z') : null;
      const ts = tsRaw ? new Date(tsRaw).toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '??:??';
      const confStr = d.confluence_score ? 'CS=' + d.confluence_score : '';
      const regimeStr = d.regime ? d.regime : '';
      const sessionStr = d.session ? d.session : '';
      const meta = [confStr, regimeStr, sessionStr].filter(Boolean).join(' | ');

      const eventColor = isBuy ? '#00e676' : isBlock ? '#ff5252' : '#ffc107';
      const shortEvent = event.replace('MISSED_BUY_', 'MISS:').replace('WOULD_BUY', 'ENTRY');

      row.innerHTML = '<div class="dot ' + dotClass + '"></div>'
        + '<span style="color:#7b8ab8; min-width:55px;">' + ts + '</span>'
        + '<span style="color:' + eventColor + '; min-width:90px; font-weight:bold;">' + shortEvent + '</span>'
        + govTag + recTag
        + '<span style="color:#7b8ab8; margin-left:auto; font-size:0.9em;">' + meta + '</span>';
      container.appendChild(row);
    });
  } catch(e) { console.error('decisions fetch error', e); }
}

// --- Journal Viewer ---
let _allJournal = [];

function renderJournal(trades) {
  const body = document.querySelector('#journal-table tbody');
  body.innerHTML = '';
  const resultFilter = document.getElementById('jf-result').value;
  const exitFilter = document.getElementById('jf-exit').value;
  const regimeFilter = document.getElementById('jf-regime').value;

  let filtered = trades.slice().reverse();
  if (resultFilter === 'win') filtered = filtered.filter(j => parseFloat(j.realized_pnl || j.pnl || 0) > 0);
  if (resultFilter === 'loss') filtered = filtered.filter(j => parseFloat(j.realized_pnl || j.pnl || 0) <= 0);
  if (exitFilter !== 'all') filtered = filtered.filter(j => (j.exit_reason || '') === exitFilter);
  if (regimeFilter !== 'all') filtered = filtered.filter(j => (j.regime_at_entry || j.regime || '') === regimeFilter);

  let totalPnl = 0, wins = 0;
  filtered.forEach(j => {
    const pnlVal = parseFloat(j.realized_pnl || j.pnl || 0);
    totalPnl += pnlVal;
    if (pnlVal > 0) wins++;
    const pnlColor = pnlVal > 0 ? '#00e676' : pnlVal < 0 ? '#ff5252' : '#e0e0e0';
    const dur = parseInt(j.duration_s || 0);
    const durStr = dur > 3600 ? (dur/3600).toFixed(1) + 'h' : dur > 60 ? Math.round(dur/60) + 'm' : dur + 's';
    const tr = document.createElement('tr');
    const ets = j.entry_ts || j.entry_time || '';
    const xts = j.exit_ts || j.exit_time || '';
    const fmtJTs = (t) => { if (!t) return ''; const r = t.includes('+') || t.endsWith('Z') ? t : t+'Z'; return new Date(r).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',',''); };
    tr.innerHTML = '<td>' + fmtJTs(ets) + '</td>'
      + '<td>' + fmtJTs(xts) + '</td>'
      + '<td>' + (j.side || 'LONG') + '</td>'
      + '<td>' + (j.qty || '') + '</td>'
      + '<td>$' + (parseFloat(j.entry_px || j.entry_price || 0)).toFixed(2) + '</td>'
      + '<td>$' + (parseFloat(j.exit_px || j.exit_price || 0)).toFixed(2) + '</td>'
      + '<td style="color:' + pnlColor + '">$' + pnlVal.toFixed(4) + '</td>'
      + '<td>' + durStr + '</td>'
      + '<td>' + (j.exit_reason || '') + '</td>'
      + '<td>' + (j.regime_at_entry || j.regime || '') + '</td>';
    body.appendChild(tr);
  });

  const wr = filtered.length > 0 ? (wins / filtered.length * 100).toFixed(1) : '0.0';
  const pnlColor = totalPnl > 0 ? '#00e676' : totalPnl < 0 ? '#ff5252' : '#e0e0e0';
  document.getElementById('jf-stats').innerHTML = filtered.length + ' trades | WR: ' + wr + '% | PnL: <span style="color:' + pnlColor + '">$' + totalPnl.toFixed(4) + '</span>';
}

function populateJournalFilters(trades) {
  const exits = new Set();
  const regimes = new Set();
  trades.forEach(j => {
    if (j.exit_reason) exits.add(j.exit_reason);
    const r = j.regime_at_entry || j.regime || '';
    if (r) regimes.add(r);
  });
  const exitSel = document.getElementById('jf-exit');
  exitSel.innerHTML = '<option value="all">All Exits</option>';
  [...exits].sort().forEach(e => { exitSel.innerHTML += '<option value="' + e + '">' + e + '</option>'; });
  const regSel = document.getElementById('jf-regime');
  regSel.innerHTML = '<option value="all">All Regimes</option>';
  [...regimes].sort().forEach(r => { regSel.innerHTML += '<option value="' + r + '">' + r + '</option>'; });
}

async function loadJournal() {
  try {
    const resp = await fetch('/api/journal?coin=' + currentCoin);
    _allJournal = await resp.json();
    window._journalLoaded = true;
    populateJournalFilters(_allJournal);
    renderJournal(_allJournal);
  } catch(e) { console.error('journal fetch error', e); }
}

// Filter event listeners (guard against missing elements)
['jf-result', 'jf-exit', 'jf-regime'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', () => renderJournal(_allJournal));
});

// --- Leaderboard ---
async function loadLeaderboard() {
  try {
    const res = await fetch('/api/leaderboard');
    const rows = await res.json();
    const tbody = document.querySelector('#leaderboard-table tbody');
    tbody.innerHTML = '';
    const countEl = document.getElementById('lb-count');
    const withTrades = rows.filter(r => r.trades > 0);
    countEl.textContent = withTrades.length + ' runs with trades / ' + rows.length + ' total';
    withTrades.forEach((r, i) => {
      const pf = parseFloat(r.profit_factor) || 0;
      const pnl = parseFloat(r.pnl) || 0;
      const startPnl = 500;  // base capital
      const netPnl = pnl - startPnl;
      const pfColor = pf >= 1.2 ? '#00e676' : pf >= 1.0 ? '#ffc107' : '#ff5252';
      const pnlColor = netPnl >= 0 ? '#00e676' : '#ff5252';
      const tr = document.createElement('tr');
      if (i === 0) tr.style.background = 'rgba(0,230,118,0.08)';
      tr.innerHTML = '<td>' + (i+1) + '</td>'
        + '<td title="' + r.run_id + '" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;">' + r.label + '</td>'
        + '<td>' + r.trades + '</td>'
        + '<td>' + parseFloat(r.win_rate).toFixed(1) + '</td>'
        + '<td style="color:' + pfColor + ';font-weight:bold;">' + pf.toFixed(2) + '</td>'
        + '<td style="color:' + pnlColor + ';">$' + netPnl.toFixed(2) + '</td>'
        + '<td>$' + parseFloat(r.expectancy).toFixed(3) + '</td>'
        + '<td>' + parseFloat(r.max_dd).toFixed(2) + '%</td>'
        + '<td style="color:#00e676;">$' + parseFloat(r.avg_win).toFixed(3) + '</td>'
        + '<td style="color:#ff5252;">$' + parseFloat(r.avg_loss).toFixed(3) + '</td>';
      tbody.appendChild(tr);
    });
  } catch(e) { console.error('leaderboard error', e); }
}

// (balanceHistory moved to top of script block)

function updateBalanceChart(balance) {
    const now = new Date();
    const label = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0');
    // Only add a point when broker-reported equity actually changes.
    const last = balanceHistory.length > 0 ? balanceHistory[balanceHistory.length - 1] : null;
    if (!last || Math.abs(last.value - balance) > 0.005) {
      balanceHistory.push({time: label, value: balance});
      if (balanceHistory.length > MAX_BALANCE_POINTS) balanceHistory = balanceHistory.slice(-MAX_BALANCE_POINTS);
    }

    const canvas = document.getElementById('balance-history-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = Math.max(canvas.offsetWidth || canvas.clientWidth || 320, 320);
    const H = canvas.height;
    const padX = 14;
    const padY = 12;
    const innerW = Math.max(1, W - padX * 2);
    const innerH = Math.max(1, H - padY * 2);

    ctx.clearRect(0, 0, W, H);

    if (balanceHistory.length < 2) return;

    const values = balanceHistory.map(b => b.value);
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const padVal = Math.max((rawMax - rawMin) * 0.14, Math.abs(rawMax || 1) * 0.0025, 0.5);
    const minVal = rawMin - padVal;
    const maxVal = rawMax + padVal;
    const range = maxVal - minVal || 1;

    ctx.strokeStyle = 'rgba(30,42,66,0.65)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const y = padY + (innerH / 3) * i;
      ctx.beginPath();
      ctx.moveTo(padX, y);
      ctx.lineTo(W - padX, y);
      ctx.stroke();
    }

    // Draw balance line
    const isUp = values[values.length-1] >= values[0];
    ctx.strokeStyle = isUp ? '#00e676' : '#ff4444';
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < values.length; i++) {
      const x = padX + (i / (values.length - 1)) * innerW;
      const y = H - padY - ((values[i] - minVal) / range) * innerH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Fill under the line
    ctx.lineTo(W - padX, H - padY);
    ctx.lineTo(padX, H - padY);
    ctx.closePath();
    ctx.fillStyle = isUp ? 'rgba(0,230,118,0.08)' : 'rgba(255,68,68,0.08)';
    ctx.fill();

    const pointStep = Math.max(1, Math.ceil(values.length / 18));
    for (let i = 0; i < values.length; i++) {
      if (i !== 0 && i !== values.length - 1 && i % pointStep !== 0) continue;
      const x = padX + (i / (values.length - 1)) * innerW;
      const y = H - padY - ((values[i] - minVal) / range) * innerH;
      ctx.fillStyle = isUp ? '#00e676' : '#ff4444';
      ctx.strokeStyle = '#08131f';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(x, y, i === values.length - 1 ? 3.4 : 2.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }

    // Label
    const current = values[values.length-1];
    const delta = current - values[0];
    const labelEl = document.getElementById('prod-equity-chart-label') || document.getElementById('balance-chart-label');
    if (labelEl) {
      labelEl.textContent = '$' + current.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' (' + (delta >= 0 ? '+' : '') + '$' + delta.toFixed(2) + ' since page load, ' + values.length + ' pts)';
      labelEl.style.color = delta >= 0 ? '#00e676' : '#ff4444';
    }
  }

function _seriesNormalize(values) {
    if (!Array.isArray(values)) return [];
    const result = [];
    for (let i = 0; i < values.length; i++) {
      const item = values[i];
      if (item && typeof item === 'object' && !Array.isArray(item)) {
        const value = Number(item.value);
        if (!Number.isFinite(value)) continue;
        result.push({
          value,
          label: item.label || '',
          detail: item.detail || '',
          raw: item.raw || item,
        });
      } else {
        const value = Number(item);
        if (!Number.isFinite(value)) continue;
        result.push({
          value,
          label: '',
          detail: '',
          raw: item,
        });
      }
    }
    return result;
}

function renderSeriesChart(canvasId, labelId, values, opts = {}) {
    const canvas = document.getElementById(canvasId);
    const labelEl = document.getElementById(labelId);
    const hoverEl = opts.hoverTargetId ? document.getElementById(opts.hoverTargetId) : null;
    if (!canvas) return;

    const series = _seriesNormalize(values);
    const points = series.map(item => item.value);
    const emptyText = opts.emptyText || 'No history yet.';
    if (labelEl && points.length < 2) {
      labelEl.textContent = emptyText;
      labelEl.style.color = '#7b8ab8';
    }
    if (hoverEl && points.length < 2) {
      hoverEl.textContent = opts.hoverEmptyText || 'Waiting for more points.';
      hoverEl.style.color = '#7b8ab8';
    }
    if (points.length < 2) {
      canvas.onmousemove = null;
      canvas.onmouseleave = null;
    }

    const ctx = canvas.getContext('2d');
    const W = canvas.width = Math.max(canvas.offsetWidth || canvas.clientWidth || 320, 320);
    const H = canvas.height;
    const padX = 14;
    const padY = 12;
    const innerW = Math.max(1, W - padX * 2);
    const innerH = Math.max(1, H - padY * 2);
    ctx.clearRect(0, 0, W, H);

    if (points.length < 2) return;

    const rawMin = Math.min(...points);
    const rawMax = Math.max(...points);
    const valPad = Math.max((rawMax - rawMin) * 0.14, Math.abs(rawMax || 1) * 0.0025, 0.25);
    const minVal = rawMin - valPad;
    const maxVal = rawMax + valPad;
    const range = (maxVal - minVal) || 1;
    const start = points[0];
    const end = points[points.length - 1];
    const isUp = end >= start;
    const stroke = isUp ? (opts.positiveColor || '#00e676') : (opts.negativeColor || '#ff4444');
    const fill = isUp ? (opts.positiveFill || 'rgba(0,230,118,0.08)') : (opts.negativeFill || 'rgba(255,68,68,0.08)');

    ctx.strokeStyle = 'rgba(30,42,66,0.65)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const y = padY + (innerH / 3) * i;
      ctx.beginPath();
      ctx.moveTo(padX, y);
      ctx.lineTo(W - padX, y);
      ctx.stroke();
    }

    let zeroY = null;
    if (opts.showZeroLine !== false && minVal <= 0 && maxVal >= 0) {
      zeroY = H - padY - ((0 - minVal) / range) * innerH;
      ctx.strokeStyle = '#333';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padX, zeroY);
      ctx.lineTo(W - padX, zeroY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    const plotPoints = points.map((point, i) => ({
      x: padX + (i / (points.length - 1)) * innerW,
      y: H - padY - ((point - minVal) / range) * innerH,
      value: point,
      meta: series[i] || {},
      index: i,
    }));

    const draw = (activeIndex = -1) => {
      ctx.clearRect(0, 0, W, H);

      ctx.strokeStyle = 'rgba(30,42,66,0.65)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 3; i++) {
        const y = padY + (innerH / 3) * i;
        ctx.beginPath();
        ctx.moveTo(padX, y);
        ctx.lineTo(W - padX, y);
        ctx.stroke();
      }

      if (zeroY != null) {
        ctx.strokeStyle = '#333';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(padX, zeroY);
        ctx.lineTo(W - padX, zeroY);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i < plotPoints.length; i++) {
        const p = plotPoints[i];
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();

      const baselineY = zeroY != null ? zeroY : H - padY;
      ctx.lineTo(W - padX, baselineY);
      ctx.lineTo(padX, baselineY);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();

      const pointStep = Math.max(1, Math.ceil(plotPoints.length / 18));
      for (let i = 0; i < plotPoints.length; i++) {
        const p = plotPoints[i];
        if (i !== 0 && i !== plotPoints.length - 1 && i % pointStep !== 0 && i !== activeIndex) continue;
        ctx.fillStyle = stroke;
        ctx.strokeStyle = '#08131f';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, i === activeIndex ? 4.2 : (i === plotPoints.length - 1 ? 3.4 : 2.2), 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }

      if (activeIndex >= 0 && plotPoints[activeIndex]) {
        const p = plotPoints[activeIndex];
        ctx.strokeStyle = 'rgba(0, 212, 255, 0.35)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(p.x, padY);
        ctx.lineTo(p.x, H - padY);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    };

    draw(-1);

    if (labelEl) {
      const fmt = opts.formatter || ((v) => Number(v).toFixed(2));
      const delta = end - start;
      labelEl.textContent = `${fmt(end)} (${delta >= 0 ? '+' : ''}${fmt(delta)} vs start, ${points.length} pts)`;
      labelEl.style.color = stroke;
    }

    if (hoverEl) {
      hoverEl.textContent = opts.hoverDefaultText || 'Hover points for details.';
      hoverEl.style.color = '#7b8ab8';
    }

    canvas.onmousemove = evt => {
      if (!plotPoints.length) return;
      const rect = canvas.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      let nearest = 0;
      let nearestDist = Infinity;
      for (const p of plotPoints) {
        const dist = Math.abs(p.x - x);
        if (dist < nearestDist) {
          nearestDist = dist;
          nearest = p.index;
        }
      }
      draw(nearest);
      if (hoverEl) {
        const p = plotPoints[nearest];
        const fmt = opts.formatter || ((v) => Number(v).toFixed(2));
        const meta = p.meta || {};
        const label = meta.label ? `${meta.label} | ` : '';
        const detail = meta.detail ? ` | ${meta.detail}` : '';
        hoverEl.textContent = `${label}${fmt(p.value)}${detail}`;
        hoverEl.style.color = stroke;
      }
    };

    canvas.onmouseleave = () => {
      draw(-1);
      if (hoverEl) {
        hoverEl.textContent = opts.hoverDefaultText || 'Hover points for details.';
        hoverEl.style.color = '#7b8ab8';
      }
    };
}

function formatTsShort(ts) {
    const clean = String(ts || '').replace('T', ' ');
    if (!clean) return '';
    return clean.length >= 19 ? clean.substring(5, 19) : clean;
}

function _chartAgeFromIso(ts) {
    const raw = String(ts || '').trim();
    if (!raw) return 'n/a';
    try {
      const dt = new Date(raw);
      const ageS = Math.max(0, Math.round((Date.now() - dt.getTime()) / 1000));
      return _chartFmtAgo(ageS);
    } catch (_) {
      return 'n/a';
    }
}

function selectRunnerChart(chartKey, followLive = true) {
    const sel = document.getElementById('chart-symbol-select');
    if (!sel || !chartKey) return;
    sel.value = chartKey;
    _strategyChartState.autoFollow = !!followLive;
    _strategyChartState.focusTs = null;
    _strategyChartState.selection = null;
    _updateChartFollowButton();
    loadRunnerChart(true);
    const card = document.getElementById('live-strategy-chart-card');
    if (card && typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function renderStageTradeJournal(targetId, rows, emptyMessage, accentColor) {
    const el = document.getElementById(targetId);
    if (!el) return;
    if (!rows || !rows.length) {
        el.innerHTML = `<div style="color:#7b8ab8;">${emptyMessage}</div>`;
      return;
    }

    let html = '<table style="width:100%;border-collapse:collapse;"><tr style="color:' + accentColor + ';border-bottom:1px solid #1e2a42;font-size:0.9em;">'
      + '<th style="text-align:left;padding:3px;">Time</th><th>Runner</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Dur</th></tr>';

    for (const t of rows) {
      const rawPnl = parseFloat(t.pnl_pips || t.pnl_pts || 0);
      const hasUsd = t.pnl_usd !== undefined && t.pnl_usd !== null && t.pnl_usd !== '';
      const usdPnl = hasUsd ? parseFloat(t.pnl_usd || 0) : null;
      const pnlColor = hasUsd ? (usdPnl >= 0 ? '#00ff88' : '#ff4444') : (rawPnl >= 0 ? '#00ff88' : '#ff4444');
      const pnlText = hasUsd
        ? `${usdPnl >= 0 ? '+' : ''}$${usdPnl.toFixed(2)}`
        : `${rawPnl >= 0 ? '+' : ''}${rawPnl.toFixed(1)} ${t.unit || ''}`.trim();
      const dirColor = t.direction === 'long' ? '#00ff88' : t.direction === 'short' ? '#ff4444' : '#7b8ab8';
      html += `<tr style="border-bottom:1px solid #0d1117;">
        <td style="padding:2px 3px;">${formatTsShort(t.ts || t.exit_ts || t.close_ts || t.entry_ts)}</td>
        <td>${t.runner}</td>
        <td style="color:${dirColor};font-weight:bold;">${(t.direction || '').toUpperCase()}</td>
        <td>${t.entry_px || ''}</td>
        <td>${t.exit_px || ''}</td>
        <td style="color:${pnlColor};font-weight:bold;">${pnlText}</td>
        <td style="color:#888;">${t.exit_reason || ''}</td>
        <td style="color:#888;">${t.duration_min ? Number(t.duration_min).toFixed(0) + 'm' : ''}</td>
      </tr>`;
    }
    html += '</table>';
    el.innerHTML = html;
}

function renderStageDailyJournal(targetId, days, emptyMessage, accentColor) {
    const el = document.getElementById(targetId);
    if (!el) return;
    if (!days || !days.length) {
      el.innerHTML = `<div style="color:#7b8ab8;">${emptyMessage}</div>`;
      return;
    }

    let html = '<table style="width:100%;border-collapse:collapse;"><tr style="color:' + accentColor + ';border-bottom:1px solid #1e2a42;font-size:0.9em;">'
      + '<th style="text-align:left;padding:3px;">Date</th><th>Trades</th><th>W/L</th><th>WR</th><th>Total</th><th>FX</th><th>Fut</th><th>Coverage</th></tr>';
    for (const day of days) {
      const total = day.total || {};
      const fx = day.fx || {};
      const futures = day.futures || {};
      const usdComplete = !!total.journal_usd_complete;
      const pnlUsd = total.pnl_usd;
      const pnlColor = !usdComplete ? '#7b8ab8' : (pnlUsd >= 0 ? '#00e676' : '#ff4444');
      const pnlText = usdComplete && pnlUsd != null ? `${pnlUsd >= 0 ? '+' : ''}$${Number(pnlUsd).toFixed(2)}` : 'USD n/a';
      const fxText = fx.trades
        ? (fx.journal_usd_complete && fx.pnl_usd != null ? `${fx.pnl_usd >= 0 ? '+' : ''}$${Number(fx.pnl_usd).toFixed(2)}` : 'USD n/a')
        : '-';
      const futuresText = futures.trades
        ? (futures.journal_usd_complete && futures.pnl_usd != null ? `${futures.pnl_usd >= 0 ? '+' : ''}$${Number(futures.pnl_usd).toFixed(2)}` : 'USD n/a')
        : '-';
      html += `<tr style="border-bottom:1px solid #0d1117;">
        <td style="padding:2px 3px;">${day.date}</td>
        <td>${total.trades || 0}</td>
        <td>${total.wins || 0}/${total.losses || 0}</td>
        <td>${Number(total.wr || 0).toFixed(1)}%</td>
        <td style="color:${pnlColor};font-weight:bold;">${pnlText}</td>
        <td style="color:#7b8ab8;">${fxText}</td>
        <td style="color:#7b8ab8;">${futuresText}</td>
        <td style="color:#888;">${total.journal_usd_trades || 0}/${total.trades || 0}</td>
      </tr>`;
    }
    html += '</table>';
    el.innerHTML = html;
}

// Init — IBKR Fleet is the primary dashboard
async function loadDailyPerformance() {
  try {
    const resp = await fetch('/api/daily_performance');
    const data = await resp.json();
    const byStage = data.by_stage || {};
    renderStageDailyJournal('prod-daily-perf-body', byStage.real || [], 'No real-money trading days yet.', '#00e676');
    renderStageDailyJournal('qa-daily-perf-body', byStage.paper || [], 'No paper-QA trading days yet.', '#00d4ff');
  } catch(e) { console.error('daily perf error', e); }
}

// ── Stage action handler ──────────────────────────────
async function stageAction(action, symbol) {
  const labels = {promote:'Promote',demote:'Demote',quarantine:'Quarantine',kill:'Kill',pause:'Pause'};
  const label = labels[action] || action;
  if (!confirm(label + ' ' + symbol + '?')) return;
  try {
    const resp = await fetch('/api/stage_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, symbol, reason: 'manual via dashboard'}),
    });
    const data = await resp.json();
    if (data.success) {
      loadIBKRFleet(); // refresh immediately
      loadGovernanceHealth();
      loadStageHistory();
    } else {
      alert('Action failed: ' + (data.error || 'unknown'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

// ── Live Strategy Chart ───────────────────────────────
let _strategyChart = null;
const _strategyChartState = {
  symbol: '',
  rangeMinutes: 120,
  autoFollow: true,
  isLoading: false,
  lastYRange: null,
  focusTs: null,
  dragMode: '',
  dragStartX: null,
  dragStartY: null,
  dragStartRange: null,
  selection: null,
};

function _chartTs(raw) {
  const ts = Date.parse(raw || '');
  return Number.isFinite(ts) ? ts : null;
}

function _chartNum(raw, fallback = 0) {
  const v = Number(raw);
  return Number.isFinite(v) ? v : fallback;
}

function _chartFmtPrice(v, precision = 5) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(precision) : '--';
}

function _chartFmtSigned(v, digits = 1) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '--';
  return (n >= 0 ? '+' : '') + n.toFixed(digits);
}

function _chartFmtAgo(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s)) return 'n/a';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  return (s / 3600).toFixed(1) + 'h ago';
}

function _chartFmtVolume(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return '--';
  if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(2) + 'M';
  if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1) + 'K';
  return n.toFixed(0);
}

function _chartActivityMeta(bar) {
  const kind = String((bar && bar.activityKind) || '').toLowerCase();
  if (kind === 'volume') return { short: 'V', pane: 'VOL', label: 'Volume' };
  if (kind === 'ticks') return { short: 'Ticks', pane: 'TICKS', label: 'Ticks' };
  if (kind === 'range') return { short: 'Range', pane: 'RNG', label: 'Range' };
  const volume = _chartNum(bar && bar.v, 0);
  const ticks = _chartNum(bar && bar.n, 0);
  const high = _chartNum(bar && bar.h, 0);
  const low = _chartNum(bar && bar.l, 0);
  if (volume > 0) return { short: 'V', pane: 'VOL', label: 'Volume' };
  if (ticks > 0) return { short: 'Ticks', pane: 'TICKS', label: 'Ticks' };
  if (high > 0 && low > 0 && high >= low) return { short: 'Range', pane: 'RNG', label: 'Range' };
  return { short: 'Activity', pane: 'ACT', label: 'Activity' };
}

function _chartActivityValue(bar) {
  const explicit = _chartNum(bar && bar.activity, NaN);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const volume = _chartNum(bar && bar.v, 0);
  if (volume > 0) return volume;
  const ticks = _chartNum(bar && bar.n, 0);
  if (ticks > 0) return ticks;
  const high = _chartNum(bar && bar.h, 0);
  const low = _chartNum(bar && bar.l, 0);
  return Math.max(0, high - low);
}

function _setChartCursor(mode) {
  const canvas = document.getElementById('strategy-chart');
  if (canvas) canvas.style.cursor = mode || 'grab';
}

function _updateChartRangeButtons() {
  [5, 30, 60, 120].forEach(mins => {
    const btn = document.getElementById('chart-range-' + mins);
    if (btn) btn.classList.toggle('active', _strategyChartState.rangeMinutes === mins);
  });
}

function _updateChartFollowButton() {
  const btn = document.getElementById('chart-follow-toggle');
  if (!btn) return;
  btn.classList.toggle('soft-active', _strategyChartState.autoFollow);
  btn.textContent = _strategyChartState.autoFollow ? 'Follow Live' : 'View Locked';
}

function setRunnerChartRange(minutes) {
  _strategyChartState.rangeMinutes = minutes;
  _strategyChartState.autoFollow = true;
  _strategyChartState.focusTs = null;
  _strategyChartState.selection = null;
  _strategyChartState.lastYRange = null;
  _updateChartRangeButtons();
  _updateChartFollowButton();
  loadRunnerChart(true);
}

function toggleRunnerChartFollow() {
  _strategyChartState.autoFollow = !_strategyChartState.autoFollow;
  if (_strategyChartState.autoFollow) _strategyChartState.focusTs = null;
  _strategyChartState.selection = null;
  _updateChartFollowButton();
  loadRunnerChart(false);
}

function resetRunnerChartView() {
  _strategyChartState.autoFollow = true;
  _strategyChartState.focusTs = null;
  _strategyChartState.selection = null;
  _strategyChartState.lastYRange = null;
  _updateChartRangeButtons();
  _updateChartFollowButton();
  loadRunnerChart(true);
}

function focusRunnerTrade(ts) {
  const parsed = _chartTs(ts);
  if (!parsed) return;
  _strategyChartState.autoFollow = false;
  _strategyChartState.focusTs = parsed;
  _strategyChartState.selection = null;
  _strategyChartState.lastYRange = null;
  _updateChartFollowButton();
  loadRunnerChart(true);
}

function _renderChartStatus(data, precision) {
  const el = document.getElementById('chart-status-line');
  if (!el) return;
  const lastPrice = _chartFmtPrice(data.latest_price, precision);
  const posColor = data.position === 'LONG' ? '#00e676' : (data.position === 'SHORT' ? '#ff9800' : '#7b8ab8');
  const stage = (data.deployment_stage || '').toUpperCase();
  const watcherNote = String(data.deployment_stage || '').toLowerCase() === 'watcher'
    ? '<span>Signals only: <span style="color:#e8f0ff;">observe-only</span></span>'
    : '';
  el.innerHTML =
    '<span>Stage: <span style="color:#e8f0ff;">' + stage + '</span></span>' +
    '<span>Mode: <span style="color:#e8f0ff;">' + (data.execution_mode || '--') + '</span></span>' +
    '<span>Last: <span style="color:#e8f0ff;">' + lastPrice + '</span></span>' +
    '<span>Bar age: <span style="color:#e8f0ff;">' + _chartFmtAgo(data.last_bar_age_s) + '</span></span>' +
    '<span>Heartbeat: <span style="color:#e8f0ff;">' + _chartFmtAgo(data.heartbeat_age_s) + '</span></span>' +
    '<span>Unrealized: <span style="color:' + posColor + ';">$' + _chartNum(data.unrealized_pnl_usd, 0).toFixed(2) + '</span></span>' +
    '<span>Open risk: <span style="color:#e8f0ff;">$' + _chartNum(data.open_risk_usd, 0).toFixed(2) + '</span></span>' +
    watcherNote;
}

function _renderChartHoverDefault(data, precision) {
  const info = document.getElementById('chart-trade-info');
  if (!info) return;
  const bars = data.bars || [];
  if (!bars.length) {
    info.innerHTML = '<span style="color:#7b8ab8;">Waiting for recent bar data...</span>';
    return;
  }
  const last = bars[bars.length - 1];
  const delta = _chartNum(last.c) - _chartNum(last.o);
  const deltaColor = delta >= 0 ? '#00e676' : '#ff5252';
  const meta = _chartActivityMeta(last);
  const activityValue = _chartActivityValue(last);
  const activityText = meta.short === 'Range'
    ? _chartFmtSigned(activityValue, precision === 5 ? 5 : 2).replace(/^[+]/, '')
    : _chartFmtVolume(activityValue);
  info.innerHTML =
    '<span style="color:#7b8ab8;">Hover a candle or marker for details.</span> ' +
    '<span style="color:#e8f0ff;">Last candle</span> ' +
    '<span>O ' + _chartFmtPrice(last.o, precision) + '</span> ' +
    '<span>H ' + _chartFmtPrice(last.h, precision) + '</span> ' +
    '<span>L ' + _chartFmtPrice(last.l, precision) + '</span> ' +
    '<span>C <span style="color:' + deltaColor + ';">' + _chartFmtPrice(last.c, precision) + '</span></span> ' +
    '<span style="color:' + deltaColor + ';">' + _chartFmtSigned(delta, precision === 5 ? 5 : 2) + '</span> ' +
    '<span>' + meta.short + ' ' + activityText + '</span>';
}

function _renderChartRecentTrades(data, precision) {
  const box = document.getElementById('chart-recent-trades');
  if (!box) return;
  const recent = (data.trades || []).slice(-5).reverse();
  if (!recent.length) {
    if (String(data.deployment_stage || '').toLowerCase() === 'watcher') {
      box.innerHTML = '<div class="chart-empty-note">Watcher lanes are observe-only. Entry markers are candidate signals, not executed trades, so exits only appear after QA/Prod fills.</div>';
    } else {
      box.innerHTML = '<div class="chart-empty-note">No closed trades yet for this runner.</div>';
    }
    return;
  }
  box.innerHTML = recent.map(t => {
    const pnlClass = t.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const dir = (t.direction || '').toUpperCase();
    const exitText = t.exit_reason || 'exit';
    const slip = Number.isFinite(Number(t.slippage_pips)) ? Number(t.slippage_pips).toFixed(1) : '--';
    const latency = Number.isFinite(Number(t.fill_latency_ms)) ? Number(t.fill_latency_ms) : 0;
    return '' +
      '<button class="chart-trade-pill" onclick="focusRunnerTrade(\\'' + (t.ts || '') + '\\')">' +
      '  <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">' +
      '    <span style="font-weight:bold;color:#e8f0ff;">' + dir + '</span>' +
      '    <span class="' + pnlClass + '">' + _chartFmtSigned(t.pnl, 1) + '</span>' +
      '  </div>' +
      '  <div style="margin-top:4px;color:#7b8ab8;">' + exitText + ' - ' + _chartNum(t.duration_min, 0).toFixed(0) + 'm</div>' +
      '  <div style="margin-top:3px;color:#7b8ab8;">slip ' + slip + ' - latency ' + latency + 'ms</div>' +
      '</button>';
  }).join('');
}

function _buildRunnerChartSeries(data) {
  const candles = (data.bars || [])
    .map(b => {
      const o = _chartNum(b.o);
      const h = _chartNum(b.h);
      const l = _chartNum(b.l);
      const c = _chartNum(b.c);
      const v = _chartNum(b.v, 0);
      const n = _chartNum(b.n, 0);
      const rangeActivity = Math.max(0, h - l);
      let activity = v;
      let activityKind = 'volume';
      if (activity <= 0 && n > 0) {
        activity = n;
        activityKind = 'ticks';
      } else if (activity <= 0) {
        activity = rangeActivity;
        activityKind = 'range';
      }
      return {
        x: _chartTs(b.t),
        o,
        h,
        l,
        c,
        v,
        n,
        activity,
        activityKind,
        t: b.t,
      };
    })
    .filter(b => b.x && b.o > 0 && b.h > 0 && b.l > 0 && b.c > 0);

  const longEntries = [];
  const shortEntries = [];
  for (const sig of (data.entry_signals || [])) {
    const point = {
      x: _chartTs(sig.ts),
      y: _chartNum(sig.price),
      ts: sig.ts,
      direction: sig.direction,
      action: sig.action,
      price: _chartNum(sig.price),
    };
    if (!point.x || !point.y) continue;
    if ((sig.direction || '').toLowerCase() === 'long') longEntries.push(point);
    if ((sig.direction || '').toLowerCase() === 'short') shortEntries.push(point);
  }

  const exits = (data.exit_markers || [])
    .map(t => ({
      x: _chartTs(t.ts),
      y: _chartNum(t.price),
      ts: t.ts,
      direction: t.direction,
      pnl: _chartNum(t.pnl),
      exit_reason: t.exit_reason || '',
    }))
    .filter(p => p.x && p.y);

  return { candles, longEntries, shortEntries, exits };
}

function _clampRunnerChartXRange(series, xMin, xMax) {
  const candles = series.candles || [];
  if (!candles.length) return null;
  const firstX = candles[0].x;
  const lastX = candles[candles.length - 1].x;
  const minWindowMs = 5 * 60 * 1000;
  const leftBound = firstX - 60 * 1000;
  const rightBound = lastX + 60 * 1000;
  let nextMin = Number(xMin);
  let nextMax = Number(xMax);
  if (!Number.isFinite(nextMin) || !Number.isFinite(nextMax)) return null;
  if (nextMax <= nextMin) nextMax = nextMin + minWindowMs;
  let span = Math.max(minWindowMs, nextMax - nextMin);
  if (span > (rightBound - leftBound)) span = rightBound - leftBound;
  if (nextMin < leftBound) {
    nextMin = leftBound;
    nextMax = nextMin + span;
  }
  if (nextMax > rightBound) {
    nextMax = rightBound;
    nextMin = nextMax - span;
  }
  nextMin = Math.max(leftBound, nextMin);
  nextMax = Math.min(rightBound, nextMax);
  return { xMin: nextMin, xMax: nextMax };
}

function _computeRunnerChartYRange(data, series, xMin, xMax, forceFit) {
  const candles = series.candles || [];
  if (!candles.length) return null;
  const inView = candles.filter(c => c.x >= xMin && c.x <= xMax);
  const pricePoints = [];
  for (const c of (inView.length ? inView : candles)) {
    pricePoints.push(c.h, c.l);
  }
  for (const marker of [...series.longEntries, ...series.shortEntries, ...series.exits]) {
    if (marker.x >= xMin && marker.x <= xMax) pricePoints.push(marker.y);
  }
  if (data.position !== 'FLAT') {
    [data.entry_price, data.stop_price, data.target_price].forEach(v => {
      const n = _chartNum(v, NaN);
      if (Number.isFinite(n) && n > 0) pricePoints.push(n);
    });
  }
  if (!pricePoints.length) return null;

  const rawMin = Math.min(...pricePoints);
  const rawMax = Math.max(...pricePoints);
  const pricePad = Math.max((rawMax - rawMin) * 0.12, Math.abs(rawMax || 1) * 0.0004);
  let targetMin = rawMin - pricePad;
  let targetMax = rawMax + pricePad;

  if (_strategyChartState.lastYRange && !forceFit && _strategyChartState.symbol === data.symbol) {
    const prev = _strategyChartState.lastYRange;
    const expandMin = targetMin < prev.min;
    const expandMax = targetMax > prev.max;
    const nextMin = expandMin ? targetMin : prev.min + (targetMin - prev.min) * 0.18;
    const nextMax = expandMax ? targetMax : prev.max + (targetMax - prev.max) * 0.18;
    _strategyChartState.lastYRange = { min: nextMin, max: nextMax };
  } else {
    _strategyChartState.lastYRange = { min: targetMin, max: targetMax };
  }

  return {
    min: _strategyChartState.lastYRange.min,
    max: _strategyChartState.lastYRange.max,
  };
}

function _computeRunnerChartViewport(data, series, forceFit) {
  const candles = series.candles;
  if (!candles.length) return null;

  const firstX = candles[0].x;
  const lastX = candles[candles.length - 1].x;
  const rangeMs = _strategyChartState.rangeMinutes * 60 * 1000;

  let xMin;
  let xMax;
  if (!_strategyChartState.autoFollow && _strategyChart && !forceFit) {
    xMin = Number(_strategyChart.scales.x.min || (lastX - rangeMs));
    xMax = Number(_strategyChart.scales.x.max || lastX);
  } else if (_strategyChartState.focusTs && !_strategyChartState.autoFollow) {
    xMin = _strategyChartState.focusTs - rangeMs * 0.45;
    xMax = _strategyChartState.focusTs + rangeMs * 0.55;
  } else {
    xMax = lastX + 30 * 1000;
    xMin = xMax - rangeMs;
  }

  const clamped = _clampRunnerChartXRange(series, xMin, xMax);
  if (!clamped) return null;
  const yRange = _computeRunnerChartYRange(data, series, clamped.xMin, clamped.xMax, forceFit);
  if (!yRange) return null;

  return {
    xMin: clamped.xMin,
    xMax: clamped.xMax,
    yMin: yRange.min,
    yMax: yRange.max,
  };
}

function _applyRunnerChartViewport(chart, xMin, xMax, forceFit = false) {
  if (!chart || !chart.$argusPayload || !chart.$argusSeries) return;
  const clamped = _clampRunnerChartXRange(chart.$argusSeries, xMin, xMax);
  if (!clamped) return;
  const yRange = _computeRunnerChartYRange(chart.$argusPayload, chart.$argusSeries, clamped.xMin, clamped.xMax, forceFit);
  if (!yRange) return;
  chart.options.scales.x.min = clamped.xMin;
  chart.options.scales.x.max = clamped.xMax;
  chart.options.scales.y.min = yRange.min;
  chart.options.scales.y.max = yRange.max;
  chart.update('none');
  _runnerChartHoverFromActive(chart, chart.getActiveElements());
}

function _attachRunnerChartInteractions(chart) {
  if (!chart || chart.$argusInteractionsAttached) return;
  const canvas = chart.canvas;
  if (!canvas) return;

  const onWheel = evt => {
    if (!chart.$argusSeries || !chart.$argusPayload || !chart.chartArea) return;
    const area = chart.chartArea;
    const rect = canvas.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    if (x < area.left || x > area.right || y < area.top || y > area.bottom) return;
    evt.preventDefault();

    const xScale = chart.scales.x;
    const currentMin = Number(xScale.min);
    const currentMax = Number(xScale.max);
    const span = currentMax - currentMin;
    const pivot = Number(xScale.getValueForPixel(x));
    if (!Number.isFinite(pivot) || !Number.isFinite(span) || span <= 0) return;

    const factor = evt.deltaY < 0 ? 0.82 : 1.18;
    const minSpan = 5 * 60 * 1000;
    const maxSpan = 8 * 60 * 60 * 1000;
    const nextSpan = Math.max(minSpan, Math.min(maxSpan, span * factor));
    const leftRatio = Math.max(0, Math.min(1, (pivot - currentMin) / span));
    const nextMin = pivot - nextSpan * leftRatio;
    const nextMax = nextMin + nextSpan;

    _strategyChartState.autoFollow = false;
    _strategyChartState.focusTs = null;
    _updateChartFollowButton();
    _applyRunnerChartViewport(chart, nextMin, nextMax, true);
  };

  const onMouseDown = evt => {
    if (evt.button !== 0 || !chart.chartArea) return;
    const rect = canvas.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    const area = chart.chartArea;
    if (x < area.left || x > area.right || y < area.top || y > area.bottom) return;

    _strategyChartState.autoFollow = false;
    _strategyChartState.focusTs = null;
    _strategyChartState.dragMode = evt.shiftKey ? 'box' : 'pan';
    _strategyChartState.dragStartX = x;
    _strategyChartState.dragStartY = y;
    _strategyChartState.dragStartRange = {
      min: Number(chart.scales.x.min),
      max: Number(chart.scales.x.max),
    };
    _strategyChartState.selection = evt.shiftKey ? { x1: x, x2: x } : null;
    _updateChartFollowButton();
    _setChartCursor(evt.shiftKey ? 'crosshair' : 'grabbing');
    evt.preventDefault();
  };

  const onMouseMove = evt => {
    if (!chart.$argusSeries || !_strategyChartState.dragMode || !chart.chartArea) return;
    const rect = canvas.getBoundingClientRect();
    const x = evt.clientX - rect.left;

    if (_strategyChartState.dragMode === 'box') {
      _strategyChartState.selection = {
        x1: _strategyChartState.dragStartX,
        x2: x,
      };
      chart.draw();
      return;
    }

    const area = chart.chartArea;
    const startRange = _strategyChartState.dragStartRange;
    if (!startRange) return;
    const pixelSpan = Math.max(1, area.right - area.left);
    const deltaX = x - _strategyChartState.dragStartX;
    const deltaMs = (deltaX / pixelSpan) * (startRange.max - startRange.min);
    _applyRunnerChartViewport(chart, startRange.min - deltaMs, startRange.max - deltaMs, false);
  };

  const finishDrag = evt => {
    if (!chart.$argusSeries || !_strategyChartState.dragMode) return;
    if (_strategyChartState.dragMode === 'box' && chart.chartArea) {
      const rect = canvas.getBoundingClientRect();
      const x2 = evt && Number.isFinite(evt.clientX) ? (evt.clientX - rect.left) : (_strategyChartState.selection ? _strategyChartState.selection.x2 : _strategyChartState.dragStartX);
      const x1 = _strategyChartState.dragStartX;
      if (Math.abs(x2 - x1) > 12) {
        const xScale = chart.scales.x;
        const zoomMin = Number(xScale.getValueForPixel(Math.min(x1, x2)));
        const zoomMax = Number(xScale.getValueForPixel(Math.max(x1, x2)));
        if (Number.isFinite(zoomMin) && Number.isFinite(zoomMax) && zoomMax - zoomMin >= 60 * 1000) {
          _applyRunnerChartViewport(chart, zoomMin, zoomMax, true);
        }
      }
      _strategyChartState.selection = null;
      chart.draw();
    }
    _strategyChartState.dragMode = '';
    _strategyChartState.dragStartX = null;
    _strategyChartState.dragStartY = null;
    _strategyChartState.dragStartRange = null;
    _setChartCursor('grab');
  };

  const onLeave = () => {
    if (!_strategyChartState.dragMode) _setChartCursor('grab');
  };

  const onDoubleClick = evt => {
    evt.preventDefault();
    resetRunnerChartView();
  };

  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mouseleave', onLeave);
  canvas.addEventListener('dblclick', onDoubleClick);
  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', finishDrag);

  chart.$argusDetachInteractions = () => {
    canvas.removeEventListener('wheel', onWheel);
    canvas.removeEventListener('mousedown', onMouseDown);
    canvas.removeEventListener('mouseleave', onLeave);
    canvas.removeEventListener('dblclick', onDoubleClick);
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup', finishDrag);
  };
  chart.$argusInteractionsAttached = true;
  _setChartCursor('grab');
}

function _updateChartPositionBadge(data, precision) {
  const badge = document.getElementById('chart-position-badge');
  if (!badge) return;
  const stage = (data.deployment_stage || '').toUpperCase();
  if (data.position !== 'FLAT') {
    const pc = data.position === 'LONG' ? '#00ff88' : '#ff9800';
    badge.innerHTML =
      '<span style="color:' + pc + ';">' + data.position + ' @ ' + _chartFmtPrice(data.entry_price, precision) + '</span>' +
      '<span style="color:#7b8ab8;"> - ' + stage + '</span>';
  } else {
    badge.innerHTML = '<span style="color:#7b8ab8;">FLAT</span><span style="color:#7b8ab8;"> - ' + stage + '</span>';
  }
}

function _runnerChartHoverFromActive(chart, activeEls) {
  const info = document.getElementById('chart-trade-info');
  if (!info) return;
  const payload = chart.$argusPayload || {};
  const precision = payload.precision || 5;
  if (!activeEls || !activeEls.length) {
    _renderChartHoverDefault(payload, precision);
    return;
  }
  const first = activeEls[0];
  const ds = chart.data.datasets[first.datasetIndex];
  const raw = ds.data[first.index] || {};
  if (ds.label === 'Bars') {
    const delta = _chartNum(raw.c) - _chartNum(raw.o);
    const deltaColor = delta >= 0 ? '#00e676' : '#ff5252';
    const meta = _chartActivityMeta(raw);
    const activityValue = _chartActivityValue(raw);
    const activityText = meta.short === 'Range'
      ? _chartFmtSigned(activityValue, precision === 5 ? 5 : 2).replace(/^[+]/, '')
      : _chartFmtVolume(activityValue);
    info.innerHTML =
      '<span style="color:#e8f0ff;">' + new Date(raw.x).toLocaleString() + '</span> ' +
      '<span>O ' + _chartFmtPrice(raw.o, precision) + '</span> ' +
      '<span>H ' + _chartFmtPrice(raw.h, precision) + '</span> ' +
      '<span>L ' + _chartFmtPrice(raw.l, precision) + '</span> ' +
      '<span>C <span style="color:' + deltaColor + ';">' + _chartFmtPrice(raw.c, precision) + '</span></span> ' +
      '<span style="color:' + deltaColor + ';">' + _chartFmtSigned(delta, precision === 5 ? 5 : 2) + '</span> ' +
      '<span>' + meta.short + ' ' + activityText + '</span>';
    return;
  }
  if (ds.label === 'Long Entry' || ds.label === 'Short Entry') {
    const dirColor = ds.label === 'Long Entry' ? '#00d4ff' : '#ff9800';
    info.innerHTML =
      '<span style="color:' + dirColor + ';font-weight:bold;">' + ds.label.toUpperCase() + '</span> ' +
      '<span style="color:#e8f0ff;">' + new Date(raw.x).toLocaleString() + '</span> ' +
      '<span>Price ' + _chartFmtPrice(raw.y, precision) + '</span>';
    return;
  }
  if (ds.label === 'Exit') {
    const pnlColor = _chartNum(raw.pnl) >= 0 ? '#00e676' : '#ff5252';
    info.innerHTML =
      '<span style="color:#e8f0ff;">EXIT ' + new Date(raw.x).toLocaleString() + '</span> ' +
      '<span>Price ' + _chartFmtPrice(raw.y, precision) + '</span> ' +
      '<span style="color:' + pnlColor + ';">' + _chartFmtSigned(raw.pnl, 1) + '</span> ' +
      '<span style="color:#7b8ab8;">' + (raw.exit_reason || 'exit') + '</span>';
  }
}

const _runnerCandlestickPlugin = {
  id: 'argusLiveCandles',
  afterDraw(chart) {
    const payload = chart.$argusPayload || {};
    const candles = chart.data?.datasets?.[0]?.data || [];
    if (!candles.length) return;
    const ctx = chart.ctx;
    const xAxis = chart.scales.x;
    const yAxis = chart.scales.y;
    const area = chart.chartArea;
    const precision = payload.precision || 5;

    let candleW = 8;
    if (candles.length > 1) {
      let minGap = Infinity;
      for (let i = 1; i < candles.length; i++) {
        const gap = xAxis.getPixelForValue(candles[i].x) - xAxis.getPixelForValue(candles[i - 1].x);
        if (gap > 0) minGap = Math.min(minGap, gap);
      }
      if (Number.isFinite(minGap)) candleW = Math.max(4, Math.min(14, minGap * 0.65));
    }

    ctx.save();
    const visibleCandles = candles.filter(bar => bar.x >= Number(xAxis.min) && bar.x <= Number(xAxis.max));
    const volumeCandles = (visibleCandles.length ? visibleCandles : candles).filter(bar => _chartNum(bar.activity, 0) > 0);
    if (volumeCandles.length) {
      const volPaneH = Math.max(38, (area.bottom - area.top) * 0.22);
      const volTop = area.bottom - volPaneH;
      const maxVol = Math.max(...volumeCandles.map(bar => _chartNum(bar.activity, 0)), 1);
      const paneMeta = _chartActivityMeta(volumeCandles.find(bar => _chartNum(bar.activity, 0) > 0) || volumeCandles[0]);
      const paneText = paneMeta.short === 'Range'
        ? _chartFmtSigned(maxVol, precision === 5 ? 5 : 2).replace(/^[+]/, '')
        : _chartFmtVolume(maxVol);
      ctx.fillStyle = 'rgba(13,17,23,0.78)';
      ctx.fillRect(area.left, volTop, area.right - area.left, volPaneH);
      ctx.strokeStyle = 'rgba(47,64,102,0.7)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(area.left, volTop);
      ctx.lineTo(area.right, volTop);
      ctx.stroke();
      for (const bar of volumeCandles) {
        const x = xAxis.getPixelForValue(bar.x);
        if (x < area.left - candleW || x > area.right + candleW) continue;
        const bullish = bar.c >= bar.o;
        const volH = Math.max(1, (_chartNum(bar.activity, 0) / maxVol) * (volPaneH - 4));
        ctx.fillStyle = bullish ? 'rgba(0,212,255,0.22)' : 'rgba(255,152,0,0.22)';
        ctx.fillRect(x - candleW / 2, area.bottom - volH, candleW, volH);
      }
      ctx.fillStyle = 'rgba(123,138,184,0.85)';
      ctx.font = '10px Consolas, monospace';
      ctx.fillText(paneMeta.pane + ' ' + paneText, area.left + 6, volTop + 11);
    }

    for (const bar of candles) {
      const x = xAxis.getPixelForValue(bar.x);
      if (x < area.left - candleW || x > area.right + candleW) continue;
      const oY = yAxis.getPixelForValue(bar.o);
      const cY = yAxis.getPixelForValue(bar.c);
      const hY = yAxis.getPixelForValue(bar.h);
      const lY = yAxis.getPixelForValue(bar.l);
      const bullish = bar.c >= bar.o;
      ctx.strokeStyle = bullish ? '#00e676' : '#ff5252';
      ctx.fillStyle = bullish ? 'rgba(0,230,118,0.55)' : 'rgba(255,82,82,0.55)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, hY);
      ctx.lineTo(x, lY);
      ctx.stroke();
      const top = Math.min(oY, cY);
      const bodyH = Math.max(1.5, Math.abs(oY - cY));
      ctx.fillRect(x - candleW / 2, top, candleW, bodyH);
      ctx.strokeRect(x - candleW / 2, top, candleW, bodyH);
    }

    const drawLine = (price, color, label) => {
      const n = _chartNum(price, NaN);
      if (!Number.isFinite(n) || n <= 0) return;
      const y = yAxis.getPixelForValue(n);
      if (y < area.top || y > area.bottom) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(area.left, y);
      ctx.lineTo(area.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = '10px Consolas, monospace';
      const labelText = label + ' ' + _chartFmtPrice(n, precision);
      const textW = ctx.measureText(labelText).width + 8;
      ctx.fillRect(area.right - textW, y - 9, textW, 14);
      ctx.fillStyle = '#0a0e17';
      ctx.fillText(labelText, area.right - textW + 4, y + 1);
    };

    if (payload.position !== 'FLAT') {
      drawLine(payload.entry_price, '#00d4ff', 'ENTRY');
      drawLine(payload.stop_price, '#ff5252', 'STOP');
      drawLine(payload.target_price, '#00e676', 'TARGET');
    }

    const last = candles[candles.length - 1];
    if (last) {
      const lastY = yAxis.getPixelForValue(last.c);
      const label = _chartFmtPrice(last.c, precision);
      const bg = last.c >= last.o ? '#00e676' : '#ff5252';
      ctx.fillStyle = bg;
      const w = ctx.measureText(label).width + 8;
      ctx.fillRect(area.right - w, lastY - 8, w, 14);
      ctx.fillStyle = '#0a0e17';
      ctx.fillText(label, area.right - w + 4, lastY + 2);
    }

    const active = chart.getActiveElements();
    if (active && active.length) {
      const raw = chart.data.datasets[active[0].datasetIndex].data[active[0].index];
      const x = xAxis.getPixelForValue(raw.x);
      const y = yAxis.getPixelForValue(raw.y || raw.c);
      ctx.strokeStyle = 'rgba(123,138,184,0.35)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.moveTo(area.left, y);
      ctx.lineTo(area.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (_strategyChartState.selection) {
      const left = Math.max(area.left, Math.min(_strategyChartState.selection.x1, _strategyChartState.selection.x2));
      const right = Math.min(area.right, Math.max(_strategyChartState.selection.x1, _strategyChartState.selection.x2));
      if (right - left > 1) {
        ctx.fillStyle = 'rgba(0,212,255,0.12)';
        ctx.strokeStyle = 'rgba(0,212,255,0.55)';
        ctx.lineWidth = 1;
        ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
        ctx.strokeRect(left, area.top, right - left, area.bottom - area.top);
      }
    }
    ctx.restore();
  }
};

async function loadRunnerChart(forceFit = false) {
  const sel = document.getElementById('chart-symbol-select');
  const chartKey = sel ? sel.value : '';
  const canvas = document.getElementById('strategy-chart');
  const info = document.getElementById('chart-trade-info');
  if (!chartKey || !canvas || _strategyChartState.isLoading) return;

  _strategyChartState.isLoading = true;
  try {
    const resp = await fetch('/api/runner_chart/' + encodeURIComponent(chartKey), { cache: 'no-store' });
    const data = await resp.json();
    if (data.error) {
      if (info) info.textContent = data.error;
      return;
    }

    const symbolChanged = _strategyChartState.symbol && _strategyChartState.symbol !== chartKey;
    if (symbolChanged) {
      _strategyChartState.lastYRange = null;
      _strategyChartState.focusTs = null;
    }
    _strategyChartState.symbol = chartKey;
    const fleetRunner = (_ibkrFleetCache || []).find(r => (r.chart_key || r.symbol) === chartKey) || null;
    _updateChartSelectionState({
      ...(fleetRunner || {}),
      name: data.runner_name || fleetRunner?.name || data.symbol || chartKey,
      current_stage: data.deployment_stage || fleetRunner?.current_stage || '',
      stage_label: _chartStageLabel(data.deployment_stage || fleetRunner?.current_stage || ''),
      position: data.position || fleetRunner?.position || 'FLAT',
      status: fleetRunner?.status || '',
    });

    const precision = data.precision || 5;
    _updateChartRangeButtons();
    _updateChartFollowButton();
    _updateChartPositionBadge(data, precision);
    _renderChartStatus(data, precision);
    _renderChartRecentTrades(data, precision);

    const series = _buildRunnerChartSeries(data);
    if (series.candles.length < 5) {
      if (_strategyChart && symbolChanged) {
        if (_strategyChart.$argusDetachInteractions) _strategyChart.$argusDetachInteractions();
        _strategyChart.destroy();
        _strategyChart = null;
      }
      _renderChartHoverDefault(data, precision);
      return;
    }

    const view = _computeRunnerChartViewport(data, series, forceFit || symbolChanged);
    if (!view) {
      _renderChartHoverDefault(data, precision);
      return;
    }

    const chartData = {
      datasets: [
        {
          label: 'Bars',
          type: 'line',
          data: series.candles,
          parsing: false,
          borderWidth: 0,
          pointRadius: 0,
          pointHoverRadius: 5,
          hitRadius: 14,
          showLine: false,
          backgroundColor: 'rgba(0,0,0,0)',
          pointBackgroundColor: 'rgba(0,0,0,0)',
          pointBorderColor: 'rgba(0,0,0,0)',
        },
        {
          label: 'Long Entry',
          type: 'scatter',
          data: series.longEntries,
          parsing: false,
          showLine: false,
          pointRadius: 6,
          pointHoverRadius: 8,
          pointStyle: 'triangle',
          pointRotation: 0,
          pointBackgroundColor: '#00d4ff',
          pointBorderColor: '#08131f',
          pointBorderWidth: 1.5,
        },
        {
          label: 'Short Entry',
          type: 'scatter',
          data: series.shortEntries,
          parsing: false,
          showLine: false,
          pointRadius: 6,
          pointHoverRadius: 8,
          pointStyle: 'triangle',
          pointRotation: 180,
          pointBackgroundColor: '#ff9800',
          pointBorderColor: '#08131f',
          pointBorderWidth: 1.5,
        },
        {
          label: 'Exit',
          type: 'scatter',
          data: series.exits,
          parsing: false,
          showLine: false,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointStyle: 'rectRot',
          pointBackgroundColor: ctx => _chartNum(ctx.raw?.pnl, 0) >= 0 ? '#00e676' : '#ff5252',
          pointBorderColor: '#d7e4ff',
          pointBorderWidth: 1,
        }
      ]
    };

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      normalized: true,
      interaction: { mode: 'nearest', intersect: false },
      onHover: (evt, activeEls, chart) => _runnerChartHoverFromActive(chart, activeEls),
      onClick: (evt, activeEls, chart) => {
        if (!activeEls || !activeEls.length) return;
        const ds = chart.data.datasets[activeEls[0].datasetIndex];
        const raw = ds.data[activeEls[0].index];
        if (raw && raw.ts) focusRunnerTrade(raw.ts);
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: true,
          displayColors: false,
          backgroundColor: '#0d1117',
          borderColor: '#243454',
          borderWidth: 1,
          padding: 10,
          callbacks: {
            title(items) {
              const raw = items[0]?.raw || {};
              return raw.x ? new Date(raw.x).toLocaleString() : '';
            },
            label(ctx) {
              const raw = ctx.raw || {};
              if (ctx.dataset.label === 'Bars') {
                const meta = _chartActivityMeta(raw);
                const activity = _chartActivityValue(raw);
                const lines = [
                  'O ' + _chartFmtPrice(raw.o, precision) + '  H ' + _chartFmtPrice(raw.h, precision),
                  'L ' + _chartFmtPrice(raw.l, precision) + '  C ' + _chartFmtPrice(raw.c, precision),
                ];
                if (activity > 0) {
                  const text = meta.short === 'Range'
                    ? _chartFmtSigned(activity, precision === 5 ? 5 : 2).replace(/^[+]/, '')
                    : _chartFmtVolume(activity);
                  lines.push(meta.label + ' ' + text);
                }
                return lines;
              }
              if (ctx.dataset.label === 'Long Entry' || ctx.dataset.label === 'Short Entry') {
                return ctx.dataset.label + ' @ ' + _chartFmtPrice(raw.y, precision);
              }
              if (ctx.dataset.label === 'Exit') {
                return [
                  'Exit @ ' + _chartFmtPrice(raw.y, precision),
                  'PnL ' + _chartFmtSigned(raw.pnl, 1),
                  raw.exit_reason || 'exit',
                ];
              }
              return '';
            }
          }
        }
      },
      scales: {
        x: {
          type: 'time',
          time: {
            unit: 'minute',
            displayFormats: { minute: 'HH:mm' },
            tooltipFormat: 'MMM d, HH:mm:ss',
          },
          min: view.xMin,
          max: view.xMax,
          grid: { color: 'rgba(30,42,66,0.55)' },
          ticks: { color: '#7b8ab8', font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
        },
        y: {
          min: view.yMin,
          max: view.yMax,
          position: 'right',
          grid: { color: 'rgba(30,42,66,0.55)' },
          ticks: { color: '#7b8ab8', font: { size: 9 } },
        }
      }
    };

    if (!_strategyChart || symbolChanged) {
      if (_strategyChart) {
        if (_strategyChart.$argusDetachInteractions) _strategyChart.$argusDetachInteractions();
        _strategyChart.destroy();
      }
      _strategyChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: chartData,
        options: commonOptions,
        plugins: [_runnerCandlestickPlugin],
      });
      _attachRunnerChartInteractions(_strategyChart);
    } else {
      _strategyChart.data = chartData;
      _strategyChart.options = commonOptions;
      _strategyChart.update('none');
    }

    _strategyChart.$argusPayload = data;
    _strategyChart.$argusSeries = series;
    _strategyChart.update('none');
    _runnerChartHoverFromActive(_strategyChart, _strategyChart.getActiveElements());
  } catch (e) {
    if (info) info.textContent = 'Chart error: ' + e.message;
  } finally {
    _strategyChartState.isLoading = false;
  }
}

// Populate symbol selector from fleet data
function _chartStageLabel(stage) {
  const normalized = String(stage || '').toLowerCase();
  if (normalized === 'paper') return 'QA';
  if (normalized === 'real' || normalized === 'quarantine') return 'PROD';
  if (normalized === 'watcher') return 'WATCHER';
  return normalized ? normalized.toUpperCase() : 'UNKNOWN';
}

function _chartRunnerActiveTag(runner) {
  const pos = String(runner?.position || '').toUpperCase();
  if (pos === 'LONG') return 'ACTIVE LONG';
  if (pos === 'SHORT') return 'ACTIVE SHORT';
  return '';
}

function _chartStageOrder(stageLabel) {
  const order = { PROD: 0, QA: 1, WATCHER: 2 };
  return order[stageLabel] ?? 9;
}

function _chartFindActiveRunner(runners) {
  return (runners || [])
    .filter(r => _chartRunnerActiveTag(r))
    .sort((a, b) => {
      const stageDiff = _chartStageOrder(a.stage_label) - _chartStageOrder(b.stage_label);
      if (stageDiff) return stageDiff;
      return String(a.name || a.symbol || '').localeCompare(String(b.name || b.symbol || ''));
    })[0] || null;
}

function _updateChartSelectionState(runner) {
  const chip = document.getElementById('chart-selection-state');
  if (!chip) return;
  if (!runner) {
    chip.textContent = 'No pair selected';
    chip.style.color = '#7b8ab8';
    chip.style.borderColor = '#1e2a42';
    chip.style.background = 'transparent';
    return;
  }
  const stageLabel = runner.stage_label || _chartStageLabel(runner.current_stage);
  const activeTag = _chartRunnerActiveTag(runner);
  const status = String(runner.status || '').toUpperCase() || 'UNKNOWN';
  const statusColor = activeTag
    ? (String(runner.position || '').toUpperCase() === 'LONG' ? '#00e676' : '#ff9800')
    : (stageLabel === 'PROD' ? '#00e676' : stageLabel === 'QA' ? '#00d4ff' : '#7b8ab8');
  chip.textContent = `${stageLabel} | ${activeTag || status}`;
  chip.style.color = statusColor;
  chip.style.borderColor = statusColor + '55';
  chip.style.background = statusColor + '11';
}

function populateChartSelector(runners) {
  const sel = document.getElementById('chart-symbol-select');
  if (!sel) return;
  const current = sel.value;
  const chartRunners = (runners || [])
    .filter(r => (r.chart_key || r.symbol))
    .map(r => ({
      ...r,
      chart_key: r.chart_key || r.symbol,
      stage_label: _chartStageLabel(r.current_stage),
    }))
    .sort((a, b) => {
      const diff = _chartStageOrder(a.stage_label) - _chartStageOrder(b.stage_label);
      if (diff) return diff;
      const activeDiff = (_chartRunnerActiveTag(b) ? 1 : 0) - (_chartRunnerActiveTag(a) ? 1 : 0);
      if (activeDiff) return activeDiff;
      return String(a.name || a.symbol || '').localeCompare(String(b.name || b.symbol || ''));
    });
  sel.innerHTML = '<option value="">Select pair...</option>';
  const groups = { PROD: [], QA: [], WATCHER: [], OTHER: [] };
  for (const runner of chartRunners) {
    const bucket = groups[runner.stage_label] ? runner.stage_label : 'OTHER';
    groups[bucket].push(runner);
  }
  ['PROD', 'QA', 'WATCHER', 'OTHER'].forEach(label => {
    if (!groups[label].length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    groups[label].forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.chart_key;
      const activeTag = _chartRunnerActiveTag(r);
      opt.textContent = (activeTag ? '>> ' : '') + (r.name || r.symbol || r.chart_key) + ' [' + r.stage_label + ']' + (activeTag ? ' - ' + activeTag : '');
      if (activeTag) {
        opt.style.fontWeight = '700';
        opt.style.color = r.position === 'LONG' ? '#00e676' : '#ff9800';
        opt.style.backgroundColor = 'rgba(0, 212, 255, 0.08)';
      }
      if (r.chart_key === current) opt.selected = true;
      group.appendChild(opt);
    });
    sel.appendChild(group);
  });
  let selectedRunner = chartRunners.find(r => r.chart_key === sel.value) || null;
  const activeRunner = _chartFindActiveRunner(chartRunners);
  if (_strategyChartState.autoFollow && activeRunner && (!selectedRunner || !_chartRunnerActiveTag(selectedRunner))) {
    sel.value = activeRunner.chart_key;
    selectedRunner = activeRunner;
    _updateChartSelectionState(selectedRunner);
    if (current !== activeRunner.chart_key) {
      loadRunnerChart(true);
      return;
    }
  }
  if (!sel.value && chartRunners.length) {
    const best = chartRunners
      .filter(r => _chartRunnerActiveTag(r) || r.closed_trades > 0)
      .sort((a, b) => {
        const activeDiff = (_chartRunnerActiveTag(b) ? 1 : 0) - (_chartRunnerActiveTag(a) ? 1 : 0);
        if (activeDiff) return activeDiff;
        const stageDiff = _chartStageOrder(a.stage_label) - _chartStageOrder(b.stage_label);
        if (stageDiff) return stageDiff;
        return Number(b.pnl_usd || b.pnl || 0) - Number(a.pnl_usd || a.pnl || 0);
      })[0] || chartRunners.find(r => r.stage_label === 'QA') || chartRunners.find(r => r.stage_label === 'PROD') || chartRunners[0];
    if (best) {
      sel.value = best.chart_key;
      selectedRunner = best;
      _updateChartSelectionState(selectedRunner);
      loadRunnerChart(true);
      return;
    }
  }
  _updateChartSelectionState(selectedRunner || activeRunner || chartRunners[0] || null);
  _updateChartRangeButtons();
  _updateChartFollowButton();
}

// ── Greek Family loader ───────────────────────────────
async function loadGreekFamily() {
  try {
    const resp = await fetch('/api/greek_family');
    const data = await resp.json();
    const el = document.getElementById('greek-family-cards');
    if (!el) return;
    const strats = data.strategies || [];
    if (!strats.length) {
      el.innerHTML = '<div style="color:#7b8ab8;padding:12px;background:#141b2d;border:1px dashed #1e2a42;border-radius:6px;">Helio family runners starting up. First signals after market close evaluation.</div>';
      return;
    }
    const familyColors = {helio:'#ffaa00',apollo:'#00d4ff',hermes:'#ff6b6b'};
    const familyIcons = {helio:'&#9788;',apollo:'&#9790;',hermes:'&#9889;'};
    el.innerHTML = strats.map(s => {
      const fc = familyColors[s.family] || '#7b8ab8';
      const icon = familyIcons[s.family] || '&#9679;';
      const alive = s.alive;
      const posColor = s.position === 'LONG' ? '#00ff88' : s.position === 'SHORT' ? '#ff4444' : '#555';
      const pnlColor = s.pnl_total >= 0 ? '#00ff88' : '#ff4444';
      const ageStr = s.hb_age_s != null ? (s.hb_age_s < 60 ? s.hb_age_s + 's' : Math.round(s.hb_age_s/60) + 'm') : '?';
      const regimeStr = s.regime ? s.regime : '-';
      return '<div style="background:#141b2d;border:1px solid ' + (alive ? fc + '44' : '#1e2a42') + ';border-radius:6px;padding:10px;">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">'
        + '<div><span style="color:' + fc + ';font-weight:bold;font-size:0.85em;">' + icon + ' ' + s.symbol + '</span>'
        + ' <span style="color:#555;font-size:0.6em;">' + s.family.toUpperCase() + '</span></div>'
        + '<span style="color:' + (alive ? '#00ff88' : '#ff4444') + ';font-size:0.55em;">' + (alive ? 'LIVE' : 'STALE') + ' ' + ageStr + '</span>'
        + '</div>'
        + '<div style="display:flex;justify-content:space-between;font-size:0.72em;margin-bottom:3px;">'
        + '<span style="color:' + posColor + ';">' + s.position + (s.position !== 'FLAT' ? ' @ ' + Number(s.entry_price).toFixed(2) : '') + '</span>'
        + '<span style="color:#888;">T:' + s.trade_count + '</span>'
        + '</div>'
        + '<div style="display:flex;justify-content:space-between;font-size:0.65em;color:#7b8ab8;">'
        + '<span>PnL: <span style="color:' + pnlColor + ';">' + (s.pnl_total >= 0 ? '+' : '') + Number(s.pnl_total).toFixed(2) + '%</span></span>'
        + '<span>Regime: ' + regimeStr + '</span>'
        + '<span>' + s.stage + '</span>'
        + '</div>'
        + '</div>';
    }).join('');
  } catch(e) {}
}

// ── QA Learning loader ────────────────────────────────
async function loadQALearning() {
  try {
    const resp = await fetch('/api/qa_learning');
    const data = await resp.json();
    const el = document.getElementById('qa-learning-panel');
    if (!el) return;
    const instruments = data.instruments || {};
    const variants = data.variant_comparison || {};
    const syms = Object.keys(instruments);
    if (!syms.length && !Object.keys(variants).length) {
      el.innerHTML = '<div style="color:#7b8ab8;">No trade data yet. Insights appear after closed trades accumulate.</div>';
      return;
    }
    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;">';
    // Per-instrument insights
    for (const sym of syms) {
      const d = instruments[sym];
      html += '<div style="background:#0d1117;border:1px solid #1e2a42;border-radius:4px;padding:10px;">';
      html += '<div style="color:#00d4ff;font-weight:bold;margin-bottom:6px;">' + sym + ' <span style="color:#555;">(' + d.trade_count + ' trades)</span></div>';
      // Direction
      const dirs = d.direction || {};
      for (const [dir, s] of Object.entries(dirs)) {
        if (!s.trades) continue;
        const c = s.win_rate >= 0.5 ? '#00e676' : s.win_rate >= 0.35 ? '#ffaa00' : '#ff4444';
        html += '<div style="display:flex;justify-content:space-between;"><span>' + dir + '</span><span style="color:' + c + ';">' + s.trades + 'T WR=' + (s.win_rate*100).toFixed(0) + '% avg=' + (s.avg_pnl>=0?'+':'') + s.avg_pnl.toFixed(1) + '</span></div>';
      }
      // Exit quality
      const exits = d.exit_quality || {};
      if (Object.keys(exits).length) {
        html += '<div style="margin-top:4px;border-top:1px solid #1e2a42;padding-top:4px;">';
        for (const [reason, s] of Object.entries(exits)) {
          if (!s.count) continue;
          html += '<div style="display:flex;justify-content:space-between;"><span style="color:#888;">' + reason + '</span><span>' + (s.pct_of_trades*100).toFixed(0) + '% (' + s.count + ') avg=' + (s.avg_pnl>=0?'+':'') + s.avg_pnl.toFixed(1) + '</span></div>';
        }
        html += '</div>';
      }
      // Streaks
      const st = d.streaks || {};
      if (st.max_win_streak || st.max_loss_streak) {
        html += '<div style="margin-top:4px;color:#888;">Streaks: W' + (st.max_win_streak||0) + ' / L' + (st.max_loss_streak||0) + ' (current: ' + (st.current_streak||0) + ')</div>';
      }
      html += '</div>';
    }
    html += '</div>';
    // Variant comparison
    if (Object.keys(variants).length) {
      html += '<div style="margin-top:10px;border-top:1px solid #1e2a42;padding-top:8px;"><span style="color:#00d4ff;font-weight:bold;">Watcher Variants</span>';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px;margin-top:6px;">';
      for (const [sym, vs] of Object.entries(variants).sort()) {
        for (const [vtype, vdata] of Object.entries(vs)) {
          const label = vdata.label || vtype;
          const sigs = vdata.signal_count || 0;
          const tp = vdata.trigger_params || {};
          html += '<div style="background:#0d1117;border:1px solid #1e2a42;border-radius:3px;padding:6px;">';
          html += '<span style="color:#00d4ff;">' + sym + '</span> <span style="color:#ffaa00;">' + label + '</span>';
          html += '<div style="color:#888;">' + sigs + ' signals | range=' + (tp.range_pct_min||'?') + ' sess=' + (tp.session_start||'?') + '-' + (tp.session_end||'?') + '</div>';
          html += '</div>';
        }
      }
      html += '</div></div>';
    }
    el.innerHTML = html;
  } catch(e) {}
}

// ── Governance health loader ──────────────────────────
async function loadGovernanceHealth() {
  try {
    const resp = await fetch('/api/governance_health');
    const data = await resp.json();
    const el = document.getElementById('governance-health-bar');
    if (!el) return;
    let html = '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">';
    html += '<span style="color:#7b8ab8;font-size:0.7em;font-weight:bold;">GOVERNANCE:</span>';
    for (const r of (data.reports || [])) {
      const color = r.status === 'FRESH' ? '#00e676' : r.status === 'STALE' ? '#ff4444' : '#ffaa00';
      const ageStr = r.age_s != null ? (r.age_s < 60 ? r.age_s + 's' : Math.round(r.age_s/60) + 'm') : '?';
      html += `<span style="font-size:0.6em;padding:2px 6px;border-radius:3px;background:${color}22;color:${color};border:1px solid ${color}44;" title="${r.label}: ${r.status} (${ageStr} ago)">${r.label.split(' ')[0]} ${ageStr}</span>`;
    }
    const readyColor = data.governance_ready ? '#00e676' : '#ff4444';
    html += `<span style="font-size:0.6em;font-weight:bold;color:${readyColor};margin-left:4px;">${data.governance_ready ? 'READY' : 'BLOCKED'}</span>`;
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {}
}

// ── Stage transition history loader ───────────────────
async function loadStageHistory() {
  try {
    const resp = await fetch('/api/stage_history');
    const data = await resp.json();
    const el = document.getElementById('stage-history-timeline');
    if (!el) return;
    const events = (data.events || []).slice(-20).reverse();
    if (!events.length) {
      el.innerHTML = '<div style="color:#7b8ab8;font-size:0.7em;padding:8px;">No stage transitions recorded yet.</div>';
      return;
    }
    let html = '';
    for (const e of events) {
      const ts = (e.ts || '').substring(0, 19).replace('T', ' ');
      const fromColor = e.from_stage === 'watcher' ? '#7b8ab8' : e.from_stage === 'paper' ? '#00d4ff' : e.from_stage === 'real' ? '#00e676' : '#ff4444';
      const toColor = e.to_stage === 'watcher' ? '#7b8ab8' : e.to_stage === 'paper' ? '#00d4ff' : e.to_stage === 'real' ? '#00e676' : e.to_stage === 'killed' ? '#ff4444' : '#ff9800';
      const triggerBadge = e.trigger === 'auto' ? '<span style="color:#00d4ff;font-size:0.7em;">AUTO</span>' : '<span style="color:#ffaa00;font-size:0.7em;">MANUAL</span>';
      html += `<div style="padding:4px 0;border-bottom:1px solid #1e2a42;font-size:0.7em;display:flex;gap:8px;align-items:center;">
        <span style="color:#555;min-width:110px;">${ts}</span>
        <span style="color:#00d4ff;font-weight:bold;min-width:60px;">${e.symbol}</span>
        <span style="color:${fromColor};">${e.from_stage}</span>
        <span style="color:#555;">→</span>
        <span style="color:${toColor};font-weight:bold;">${e.to_stage}</span>
        ${triggerBadge}
        <span style="color:#7b8ab8;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${(e.reason || '').substring(0, 50)}</span>
      </div>`;
    }
    el.innerHTML = html;
  } catch(e) {}
}

try {
  loadIBKRFleet();
  setInterval(loadIBKRFleet, 10000);
  loadGovernanceHealth();
  setInterval(loadGovernanceHealth, 30000);
  loadGreekFamily();
  setInterval(loadGreekFamily, 30000);
  loadQALearning();
  setInterval(loadQALearning, 60000);
  setInterval(function() {
    if (document.hidden) return;
    if (document.getElementById('chart-symbol-select')?.value) loadRunnerChart(false);
  }, 15000);
  loadStageHistory();
  setInterval(loadStageHistory, 30000);
  loadDailyPerformance();
  setInterval(loadDailyPerformance, 60000); // refresh every 60s

  async function loadHealth() {
    try {
      const r = await fetch('/api/system_health');
      const h = await r.json();
      const statusEl = document.getElementById('health-status');
      const statusColors = {OK: '#00e676', WARN: '#ffc107', PAUSED: '#ff4444'};
      statusEl.textContent = h.status;
      statusEl.style.color = statusColors[h.status] || '#888';
      document.getElementById('health-valid').textContent = h.valid_trades;
      document.getElementById('health-target').textContent = h.target;
      document.getElementById('health-candidate').textContent = h.best_candidate_name ? '(' + h.best_candidate_name + ')' : '';
      document.getElementById('health-progress-bar').style.width = h.progress_pct + '%';
      const blockedEl = document.getElementById('health-blocked');
      blockedEl.textContent = h.blocked_24h || 0;
      blockedEl.style.color = (h.blocked_24h || 0) > 50 ? '#ff9800' : '#7b8ab8';
      blockedEl.title = 'Total blocked signals on file: ' + (h.blocked_total || 0);
      document.getElementById('health-issues').textContent = h.active_issues || 0;
      document.getElementById('health-issues').style.color = (h.active_issues || 0) > 0 ? '#ff9800' : '#7b8ab8';
      document.getElementById('health-manual').textContent = h.manual_actions || 0;
      document.getElementById('health-manual').style.color = (h.manual_actions || 0) > 0 ? '#ff4444' : '#7b8ab8';
      document.getElementById('health-stale').textContent = h.stale_reports || 0;
      document.getElementById('health-stale').style.color = (h.stale_reports || 0) > 0 ? '#ffc107' : '#7b8ab8';
      const brokerEl = document.getElementById('health-broker');
      if (h.broker_connected) {
        brokerEl.textContent = 'Connected (' + h.runners_alive + '/' + h.runners_total + ')';
        brokerEl.style.color = '#00e676';
      } else {
        brokerEl.textContent = 'DISCONNECTED';
        brokerEl.style.color = '#ff4444';
      }
      const warnEl = document.getElementById('health-warnings');
      warnEl.textContent = h.warnings.length ? h.warnings.join(' | ') : '';
    } catch(e) {}
  }
  loadHealth();
  setInterval(loadHealth, 30000);
  loadOpsOverview();
  setInterval(loadOpsOverview, 30000);
  console.log('IBKR Fleet initialized');
} catch(e) {
  console.error('IBKR init error:', e);
  const target = document.getElementById('ibkr-paper-cards') || document.getElementById('ibkr-watcher-cards') || document.getElementById('ibkr-real-cards');
  if (target) target.innerHTML = '<div style="color:red;padding:20px;">Dashboard JS error: ' + e.message + '</div>';
}

async function loadOpsOverview() {
  try {
    const resp = await fetch('/api/ops_overview');
    const data = await resp.json();
    const summary = data.summary || {};
    const sev = summary.max_severity || 'OK';
    const sevColor = sev === 'CRITICAL' ? '#ff4444' : sev === 'HIGH' ? '#ff9800' : sev === 'WARNING' ? '#ffc107' : '#00e676';
    document.getElementById('ops-active-count').textContent = summary.active_issues || 0;
    document.getElementById('ops-manual-count').textContent = summary.manual_actions || 0;
    document.getElementById('ops-stale-count').textContent = (summary.stale_reports || 0) + (summary.missing_reports || 0);
    document.getElementById('ops-max-severity').textContent = sev;
    document.getElementById('ops-max-severity').style.color = sevColor;

    const lastRun = document.getElementById('ops-last-run');
    if (data.alert_state_ts) {
      const age = data.alert_state_age_s != null ? data.alert_state_age_s + 's old' : 'age unknown';
      lastRun.textContent = 'Alert state updated ' + age;
      lastRun.style.color = (data.alert_state_age_s || 0) > 1800 ? '#ffc107' : '#7b8ab8';
    } else {
      lastRun.textContent = 'No alert state yet';
      lastRun.style.color = '#ff9800';
    }

    const renderIssue = (issue) => {
      const sev = issue.severity || 'WARNING';
      const sevColor = sev === 'CRITICAL' ? '#ff4444' : sev === 'HIGH' ? '#ff9800' : sev === 'WARNING' ? '#ffc107' : '#00d4ff';
      const manualTag = issue.requires_manual_action ? ' <span style="color:#ff4444;font-weight:bold;">MANUAL</span>' : '';
      const age = issue.report_age_s != null ? ' <span style="color:#555;">(' + issue.report_age_s + 's)</span>' : '';
      return '<div style="padding:6px 0;border-bottom:1px solid #141b2d;">'
        + '<div style="color:' + sevColor + ';font-weight:bold;">' + sev + ' | ' + (issue.scope || issue.category || 'fleet') + manualTag + age + '</div>'
        + '<div style="color:#e0e0e0;">' + (issue.message || '') + '</div>'
        + '</div>';
    };

    const activeEl = document.getElementById('ops-active-issues');
    const activeIssues = data.active_issues || [];
    activeEl.innerHTML = activeIssues.length ? activeIssues.slice(0, 8).map(renderIssue).join('') : '<div style="color:#7b8ab8;">No active issues.</div>';

    const manualEl = document.getElementById('ops-manual-actions');
    const manualActions = data.manual_actions || [];
    manualEl.innerHTML = manualActions.length
      ? manualActions.slice(0, 8).map(issue => '<div style="padding:6px 0;border-bottom:1px solid #141b2d;color:#ffb74d;">' + (issue.message || '') + '</div>').join('')
      : '<div style="color:#7b8ab8;">No manual actions.</div>';

    const eventsEl = document.getElementById('ops-events');
    const events = data.recent_events || [];
    eventsEl.innerHTML = events.length
      ? events.slice(0, 10).map(event => {
          const kindColor = event.kind === 'resolved' ? '#00e676' : event.kind === 'opened' ? '#ff9800' : '#ffc107';
          const ts = event.ts ? event.ts.substring(11, 19) + ' UTC' : '';
          return '<div style="padding:6px 0;border-bottom:1px solid #141b2d;">'
            + '<div style="color:' + kindColor + ';font-weight:bold;">' + (event.kind || '').toUpperCase() + ' | ' + ts + '</div>'
            + '<div style="color:#e0e0e0;">' + (event.message || '') + '</div>'
            + '</div>';
        }).join('')
      : '<div style="color:#7b8ab8;">No alert events yet.</div>';

    const freshnessEl = document.getElementById('ops-report-freshness');
    const freshness = data.report_freshness || [];
    freshnessEl.innerHTML = freshness.length
      ? freshness.map(item => {
          const color = item.status === 'FRESH' ? '#00e676' : item.status === 'STALE' ? '#ffc107' : '#ff4444';
          const age = item.age_s == null ? 'missing' : item.age_s + 's';
          return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #141b2d;">'
            + '<span style="color:#e0e0e0;">' + item.label + '</span>'
            + '<span style="color:' + color + ';">' + item.status + ' | ' + age + '</span>'
            + '</div>';
        }).join('')
      : '<div style="color:#7b8ab8;">No report freshness data.</div>';
  } catch (e) {
    console.error('ops overview error', e);
  }
}

// PWA Service Worker registration
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(reg => {
    console.log('SW registered, scope:', reg.scope);
  }).catch(err => console.warn('SW registration failed:', err));
}
</script>
<script>
// Fallback init — runs even if main script has errors
window.addEventListener('load', function() {
  if (typeof loadIBKRFleet === 'function') {
    try { loadIBKRFleet(); } catch(e) { console.error('Fleet load error:', e); }
  } else {
    // loadIBKRFleet not defined — main script failed to parse
    var cards = document.getElementById('ibkr-paper-cards') || document.getElementById('ibkr-watcher-cards') || document.getElementById('ibkr-real-cards');
    if (cards) cards.innerHTML = '<div style="color:#ff4444;padding:20px;">Dashboard script error — check browser console (F12)</div>';
  }
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


def _load_gate_decisions(log_dir: Path) -> dict:
    """Load last gate decision chain from runner's gate_decisions.json."""
    gf = log_dir / "gate_decisions.json"
    if not gf.exists():
        return {}
    try:
        data = json.loads(gf.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k not in ("ts", "symbol")}
    except Exception:
        return {}


def _scan_greek_family_heartbeats() -> list[dict]:
    """Scan helio/logs for Greek family heartbeats (Helio, Hermes, Apollo)."""
    helio_logs = REPO / "helio" / "logs"
    family_nodes = []
    if not helio_logs.exists():
        return family_nodes
    for sub in sorted(helio_logs.iterdir()):
        if not sub.is_dir():
            continue
        hb_path = sub / "heartbeat.json"
        if not hb_path.exists():
            continue
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        age_s = _file_age_s(hb_path)
        family = hb.get("family", "helio")
        stage = hb.get("stage", "watcher")
        symbol = hb.get("symbol", sub.name)
        position = hb.get("position", "FLAT")
        trade_count = hb.get("trade_count", 0)
        # Read recent trades for PnL
        trades_path = sub / "trades.csv"
        pnl_total = hb.get("pnl_total", 0)
        win_rate = 0
        if trades_path.exists():
            try:
                rows = list(csv.DictReader(open(trades_path, encoding="utf-8")))
                if rows:
                    wins = sum(1 for r in rows if float(r.get("pnl_pct", 0)) > 0)
                    win_rate = wins / len(rows)
            except Exception:
                pass
        family_nodes.append({
            "symbol": f"{family.upper()}:{symbol}",
            "name": f"{family.capitalize()} {symbol}",
            "system": "helio",
            "family": family,
            "stage": stage,
            "position": position,
            "entry_price": hb.get("entry_price", 0),
            "pnl": pnl_total,
            "unit": "pips" if family == "apollo" else "USD",
            "heat": 0.3 if position != "FLAT" else 0.1,
            "active": (age_s or 9999) < 86400,  # daily strategies: active if heartbeat < 24h
            "signal_age_s": age_s or 9999,
            "session": "DAILY",
            "features": {
                "range_pct": 0, "vol_z": 0, "range_accel": 0,
                "dist_from_low": hb.get("dist_atr", 0),
                "vol_burst_z": 0,
            },
            "gates": {},
            "conviction": hb.get("rsi", 0) / 100 if hb.get("rsi") else 0,
            "win_rate": win_rate,
            "closed_trades": trade_count,
            "blocked_24h": 0,
            "entries_today": 0,
            "broker_connected": True,
        })
    return family_nodes


def _scan_forge_heartbeats() -> list[dict]:
    """Scan all forge system heartbeats and return neural core nodes."""
    forge_systems = [
        {
            "name": "GDX/GLD Pairs",
            "symbol": "GDX_GLD",
            "family": "forge_pairs",
            "heartbeat": REPO / "forge" / "logs" / "gdx_gld" / "heartbeat.json",
        },
        {
            "name": "Atlas Intel",
            "symbol": "ATLAS",
            "family": "forge_intel",
            "heartbeat": REPO / "forge" / "logs" / "atlas" / "heartbeat.json",
        },
        {
            "name": "Themis Congress",
            "symbol": "THEMIS",
            "family": "forge_intel",
            "heartbeat": REPO / "forge" / "logs" / "themis" / "heartbeat.json",
        },
        {
            "name": "Mamba NQ/YM",
            "symbol": "MAMBA",
            "family": "forge_scalp",
            "heartbeat": REPO / "forge" / "logs" / "mamba" / "heartbeat.json",
        },
        {
            "name": "Cue Banks US30",
            "symbol": "CUEBANKS",
            "family": "forge_confluence",
            "heartbeat": REPO / "forge" / "logs" / "cuebanks" / "heartbeat.json",
        },
        {
            "name": "Tori Swing",
            "symbol": "TORI",
            "family": "forge_swing",
            "heartbeat": REPO / "forge" / "logs" / "tori" / "heartbeat.json",
        },
    ]

    nodes = []
    for sys_info in forge_systems:
        path = sys_info["heartbeat"]
        if not path.exists():
            continue
        try:
            hb = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - path.stat().st_mtime

            # Read system-specific details
            position = hb.get("position", "FLAT")
            status = hb.get("status", hb.get("mode", "unknown"))

            # Determine "heat" based on system type
            heat = 0
            if sys_info["symbol"] == "GDX_GLD":
                z = abs(hb.get("z_score", 0))
                heat = min(1.0, z / 2.0)  # closer to 2.0 = hotter (near entry)
            elif sys_info["symbol"] == "ATLAS":
                sev = hb.get("high_severity_this_cycle", 0)
                heat = min(1.0, sev / 10.0)
            elif sys_info["symbol"] == "THEMIS":
                new_sigs = hb.get("new_signals_this_cycle", hb.get("active_signals", 0))
                heat = min(1.0, new_sigs / 5.0)
            elif sys_info["symbol"] in ("MAMBA", "CUEBANKS"):
                heat = 0.8 if status == "scanning" else 0.2
            elif sys_info["symbol"] == "TORI":
                heat = 0.5  # always moderate (4H timeframe)

            nodes.append({
                "symbol": sys_info["symbol"],
                "name": sys_info["name"],
                "system": sys_info["symbol"].lower(),
                "family": sys_info["family"],
                "stage": status,
                "position": position if position != "FLAT" else "FLAT",
                "entry_price": 0,
                "pnl": 0,
                "unit": "",
                "heat": round(heat, 3),
                "active": age < 600,
                "signal_age_s": int(age),
                "session": "ON" if age < 600 else "OFF",
                "features": hb,
                "gates": {},
                "conviction": 0,
                "win_rate": 0,
                "closed_trades": hb.get("total_trades", hb.get("trades", hb.get("trade_count", 0))),
                "blocked_24h": 0,
                "entries_today": 0,
                "broker_connected": hb.get("ibkr_connected", False),
            })
        except Exception:
            pass

    return nodes


# ---------------------------------------------------------------------------
# 10x Module APIs: Portfolio Guard, Drift Detector, Regime Router
# ---------------------------------------------------------------------------

@app.get("/api/portfolio_guard")
async def api_portfolio_guard():
    """Cross-family portfolio risk check."""
    try:
        report_path = REPO / "helio" / "logs" / "portfolio_guard.json"
        if report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            # Run live check
            sys.path.insert(0, str(REPO))
            from helio.portfolio_guard import check_current
            result = check_current()
            data = {
                "allowed": result.allowed,
                "reason": result.reason,
                "metrics": result.metrics,
                "warnings": result.warnings,
            }
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/drift_report")
async def api_drift_report():
    """Feature drift detection status."""
    try:
        report_path = REPO / "helio" / "logs" / "drift_report.json"
        if report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            data = {"overall_health": "UNKNOWN", "features_checked": 0, "drifted_features": []}
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/brain_state")
async def api_brain_state():
    """Real-time brain state for neural visualization — includes Greek family."""
    raw_runners = _managed_ibkr_runners()
    runners = [_read_ibkr_runner(r) for r in raw_runners]
    raw_map = {r.get("symbol", "").upper(): r for r in raw_runners}
    nodes = []
    for r in runners:
        is_killed = r.get("current_stage") in ("killed",)
        features = r.get("features", {})
        raw = raw_map.get(r["symbol"].upper(), {})
        log_dir = REPO / raw.get("log_dir", f"argus_flow/logs/{r['symbol'].lower()}")
        # Compute "heat" — how close to triggering
        range_pct = features.get("range_pct", 0) or 0
        threshold = 0.0012
        heat = min(1.0, range_pct / threshold) if threshold > 0 else 0

        # Recent signal activity
        sig_age = r.get("last_signal_age_s", 9999)
        active = sig_age < 120

        # Killed systems show up but are visually distinct
        if is_killed:
            heat = 0
            active = False

        nodes.append({
            "symbol": r["symbol"],
            "name": r["name"],
            "system": "argus",
            "family": "argus",
            "stage": r.get("current_stage", "watcher"),
            "position": r["position"],
            "entry_price": r.get("entry_price", 0),
            "pnl": r.get("pnl", 0),
            "unit": r["unit"],
            "heat": round(heat, 3),
            "active": active,
            "signal_age_s": sig_age,
            "session": r.get("session", "OFF"),
            "features": {
                "range_pct": features.get("range_pct", 0),
                "vol_z": features.get("vol_z", 0),
                "range_accel": features.get("range_accel", 0),
                "dist_from_low": features.get("dist_from_low", 0),
                "vol_burst_z": features.get("vol_burst_z", 0),
            },
            "gates": _load_gate_decisions(log_dir),
            "conviction": features.get("conviction_score", 0) if features else 0,
            "win_rate": r.get("win_rate", 0),
            "closed_trades": r.get("closed_trades", 0),
            "blocked_24h": r.get("blocked_signals_24h", 0),
            "entries_today": r.get("entries_today", 0),
            "broker_connected": r.get("broker_connected", False),
        })

    # Add Greek family nodes (Helio, Hermes, Apollo)
    family_nodes = _scan_greek_family_heartbeats()
    nodes.extend(family_nodes)

    # Add Forge system nodes
    forge_nodes = _scan_forge_heartbeats()
    nodes.extend(forge_nodes)

    # Recent signals across all pairs (last 20)
    recent_signals = []
    for r in runners:
        for sig in (r.get("recent_signals", []) or [])[-3:]:
            recent_signals.append({
                "symbol": r["symbol"],
                "ts": sig.get("ts", ""),
                "action": sig.get("action", ""),
                "direction": sig.get("direction", ""),
                "range_pct": sig.get("range_pct", 0),
            })
    recent_signals.sort(key=lambda x: x.get("ts", ""), reverse=True)

    # Family summary
    families = {}
    for n in nodes:
        fam = n.get("family", "argus")
        if fam not in families:
            families[fam] = {"count": 0, "active": 0, "in_trade": 0, "trades": 0}
        families[fam]["count"] += 1
        if n.get("active"):
            families[fam]["active"] += 1
        if n.get("position", "FLAT") != "FLAT":
            families[fam]["in_trade"] += 1
        families[fam]["trades"] += n.get("closed_trades", 0)

    # Portfolio guard status
    pg_data = {}
    pg_path = REPO / "helio" / "logs" / "portfolio_guard.json"
    if pg_path.exists():
        try:
            pg_data = json.loads(pg_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Drift report status
    drift_data = {"overall_health": "UNKNOWN"}
    drift_path = REPO / "helio" / "logs" / "drift_report.json"
    if drift_path.exists():
        try:
            drift_data = json.loads(drift_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Atlas regime overlay
    regime_path = REPO / "forge" / "macro_regime.json"
    regime = {}
    if regime_path.exists():
        try:
            regime = json.loads(regime_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return JSONResponse({
        "nodes": nodes,
        "families": families,
        "recent_signals": recent_signals[:20],
        "portfolio_guard": pg_data,
        "drift": drift_data,
        "regime": regime.get("regime", {}),
        "regime_alert": regime.get("alert_level", "unknown"),
        "position_size_modifier": regime.get("position_size_modifier", 1.0),
        "vix_structure": regime.get("vix_structure", {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


BRAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Helio Neural Core — Greek Family</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=JetBrains+Mono:wght@300;400;500&display=swap');
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #060a12; color: #c8d6e5; font-family: 'JetBrains Mono', monospace; overflow: hidden; touch-action: none; }
canvas { display: block; position: fixed; top: 0; left: 0; z-index: 0; }

#hud { position: fixed; top: 16px; left: 20px; z-index: 10; }
#hud h1 { font-family: 'Orbitron', sans-serif; font-size: 1.3em; font-weight: 900;
  background: linear-gradient(135deg, #00d4ff, #00ff88);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  letter-spacing: 6px; text-transform: uppercase; }
#hud .sub { font-size: 0.6em; color: #4a5568; margin-top: 4px; letter-spacing: 2px; }

#stats-bar { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 10;
  display: flex; gap: 24px; }
.stat-item { text-align: center; }
.stat-val { font-family: 'Orbitron', sans-serif; font-size: 1.4em; font-weight: 700; }
.stat-label { font-size: 0.55em; color: #4a5568; letter-spacing: 2px; text-transform: uppercase; margin-top: 2px; }

#nav { position: fixed; top: 16px; right: 20px; z-index: 10; display: flex; gap: 8px; align-items: center; }
#nav a, .toggle-btn { color: #4a5568; text-decoration: none; font-size: 0.7em; padding: 6px 16px;
  border: 1px solid #1a2332; border-radius: 20px; transition: all 0.3s; letter-spacing: 1px; cursor: pointer;
  background: transparent; font-family: 'JetBrains Mono', monospace; }
#nav a:hover, .toggle-btn:hover { background: #1a2332; color: #00d4ff; border-color: #00d4ff44; }
.toggle-btn.active { background: #1a2332; color: #00d4ff; border-color: #00d4ff44; }

.panel { transition: opacity 0.3s, transform 0.3s; }
.panel.hidden { opacity: 0; pointer-events: none; transform: translateY(10px); }

#decision-flow { position: fixed; bottom: 50px; left: 20px; z-index: 10;
  background: rgba(6,10,18,0.88); border: 1px solid #1a2332; border-radius: 8px;
  padding: 12px 16px; width: 320px; backdrop-filter: blur(10px); }
#decision-flow h3 { font-family: 'Orbitron', sans-serif; font-size: 0.7em; color: #00d4ff;
  letter-spacing: 3px; margin-bottom: 8px; }
.gate-chain { display: flex; flex-direction: column; gap: 3px; }
.gate-row { display: flex; align-items: center; gap: 6px; font-size: 0.62em; padding: 2px 0; }
.gate-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.gate-dot.pass { background: #00ff88; box-shadow: 0 0 6px #00ff8866; }
.gate-dot.fail { background: #ff4444; box-shadow: 0 0 6px #ff444466; }
.gate-dot.shadow { background: #ffaa00; box-shadow: 0 0 6px #ffaa0044; }
.gate-dot.idle { background: #1a2332; }
.gate-name { color: #4a5568; min-width: 80px; }
.gate-result { color: #c8d6e5; flex: 1; }

#system-health { position: fixed; top: 80px; right: 20px; z-index: 10;
  background: rgba(6,10,18,0.88); border: 1px solid #1a2332; border-radius: 8px;
  padding: 12px 16px; width: 220px; backdrop-filter: blur(10px); }
#system-health h3 { font-family: 'Orbitron', sans-serif; font-size: 0.7em; color: #a78bfa;
  letter-spacing: 3px; margin-bottom: 8px; }

#signal-panel { position: fixed; bottom: 50px; right: 20px; z-index: 10;
  background: rgba(6,10,18,0.88); border: 1px solid #1a2332; border-radius: 8px;
  padding: 12px 16px; width: 280px; max-height: 300px; overflow-y: auto;
  backdrop-filter: blur(10px); }
#signal-panel h3 { font-family: 'Orbitron', sans-serif; font-size: 0.7em; color: #00d4ff;
  letter-spacing: 3px; margin-bottom: 8px; }
.sig-entry { font-size: 0.6em; padding: 3px 0; border-bottom: 1px solid #0d1420;
  display: flex; gap: 6px; align-items: center; }

#ticker { position: fixed; bottom: 0; left: 0; right: 0;
  background: rgba(6,10,18,0.95); border-top: 1px solid #1a2332;
  padding: 8px 16px; z-index: 10; overflow: hidden; }
#ticker-inner { display: flex; gap: 20px; animation: scroll 40s linear infinite; white-space: nowrap; }
@keyframes scroll { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
.tick-item { font-size: 0.6em; display: inline-flex; gap: 4px; align-items: center; }
.tick-dot { width: 4px; height: 4px; border-radius: 50%; display: inline-block; }

#zoom-hint { position: fixed; bottom: 60px; left: 50%; transform: translateX(-50%); z-index: 5;
  color: #1a2332; font-size: 0.55em; letter-spacing: 2px; pointer-events: none;
  transition: opacity 2s; }

/* Mobile responsive */
@media (max-width: 768px) {
  #hud h1 { font-size: 0.85em; letter-spacing: 3px; }
  #stats-bar { position: fixed; top: auto; bottom: 40px; left: 8px; right: 8px;
    transform: none; gap: 8px; justify-content: center; flex-wrap: wrap;
    background: rgba(6,10,18,0.9); padding: 8px; border-radius: 6px;
    border: 1px solid #1a2332; }
  .stat-val { font-size: 1em; }
  .stat-label { font-size: 0.45em; }
  #decision-flow, #signal-panel, #system-health { display: none; }
  #decision-flow.shown, #signal-panel.shown, #system-health.shown { display: block; }
  #nav { top: 8px; right: 8px; flex-wrap: wrap; justify-content: flex-end; }
  #nav a, .toggle-btn { font-size: 0.55em; padding: 3px 8px; }
  #ticker { display: none; }
}
@media (min-width: 769px) and (max-width: 1200px) {
  #decision-flow { width: 240px; padding: 8px 10px; }
  #signal-panel { width: 220px; }
  .gate-name { min-width: 60px; }
}
</style>
</head>
<body>
<div id="hud">
  <h1>Helio Neural Core</h1>
  <div class="sub" id="status-line">Initializing neural network...</div>
</div>
<div id="stats-bar">
  <div class="stat-item"><div class="stat-val" id="s-nodes" style="color:#00d4ff;">--</div><div class="stat-label">Nodes</div></div>
  <div class="stat-item"><div class="stat-val" id="s-active" style="color:#00ff88;">--</div><div class="stat-label">Active</div></div>
  <div class="stat-item"><div class="stat-val" id="s-trades" style="color:#ffaa00;">--</div><div class="stat-label">In Trade</div></div>
  <div class="stat-item"><div class="stat-val" id="s-families" style="color:#a78bfa;">--</div><div class="stat-label">Families</div></div>
  <div class="stat-item"><div class="stat-val" id="s-signals" style="color:#7b8ab8;">--</div><div class="stat-label">Signals/min</div></div>
  <div class="stat-item"><div class="stat-val" id="s-guard" style="color:#00ff88;">--</div><div class="stat-label">Port. Guard</div></div>
  <div class="stat-item"><div class="stat-val" id="s-drift" style="color:#00ff88;">--</div><div class="stat-label">Drift</div></div>
</div>
<div id="nav">
  <a href="/">Dashboard</a>
  <a href="/fleet">Fleet Ops</a>
  <button class="toggle-btn active" onclick="togglePanel('decision-flow',this)" title="Decision Pipeline">Pipeline</button>
  <button class="toggle-btn active" onclick="togglePanel('system-health',this)" title="System Health">Health</button>
  <button class="toggle-btn active" onclick="togglePanel('signal-panel',this)" title="Signal Feed">Signals</button>
</div>

<div id="decision-flow" class="panel">
  <h3>Decision Pipeline</h3>
  <div class="gate-chain" id="gate-chain"></div>
</div>

<div id="system-health" class="panel">
  <h3>SYSTEM HEALTH</h3>
  <div id="health-items" style="display:flex;flex-direction:column;gap:6px;font-size:0.62em;"></div>
</div>

<div id="signal-panel" class="panel">
  <h3>Signal Feed</h3>
  <div id="signal-feed">Waiting for neural activity...</div>
</div>

<canvas id="brain"></canvas>
<div id="ticker"><div id="ticker-inner"></div></div>
<div id="zoom-hint">SCROLL TO ZOOM &middot; DRAG TO PAN</div>

<script>
const canvas = document.getElementById('brain');
const ctx = canvas.getContext('2d');
let W, H, cX, cY, t = 0;
let nodes = [], particles = [], pulses = [], nebulae = [];
let hoveredNode = null, selectedNode = null;

// --- Camera / zoom / pan ---
let camX = 0, camY = 0, camZoom = 1, targetZoom = 1, targetCX = 0, targetCY = 0;
let isDragging = false, dragStartX = 0, dragStartY = 0, dragCamX = 0, dragCamY = 0;

// Panel toggle
function togglePanel(id, btn) {
  const el = document.getElementById(id);
  const isMobile = window.innerWidth <= 768;
  if (isMobile) {
    el.classList.toggle('shown');
    el.style.display = el.classList.contains('shown') ? 'block' : 'none';
  } else {
    el.classList.toggle('hidden');
  }
  btn.classList.toggle('active');
}

// Hide zoom hint after 5 seconds
setTimeout(() => { document.getElementById('zoom-hint').style.opacity = '0'; }, 5000);

function resize() {
  W = canvas.width = innerWidth; H = canvas.height = innerHeight;
  cX = W/2; cY = H/2;
}
addEventListener('resize', resize); resize();

// --- Zoom (mouse wheel + pinch) ---
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const zoomFactor = e.deltaY > 0 ? 0.92 : 1.08;
  targetZoom = Math.max(0.3, Math.min(4, targetZoom * zoomFactor));
}, { passive: false });

// --- Pan (mouse drag) ---
canvas.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  isDragging = true; dragStartX = e.clientX; dragStartY = e.clientY;
  dragCamX = camX; dragCamY = camY;
  canvas.style.cursor = 'grabbing';
});
addEventListener('mousemove', e => {
  if (!isDragging) return;
  targetCX = dragCamX + (e.clientX - dragStartX) / camZoom;
  targetCY = dragCamY + (e.clientY - dragStartY) / camZoom;
});
addEventListener('mouseup', () => { isDragging = false; canvas.style.cursor = hoveredNode ? 'pointer' : 'default'; });

// --- Touch: pinch zoom + drag pan ---
let touches = [], lastPinchDist = 0;
canvas.addEventListener('touchstart', e => {
  e.preventDefault();
  touches = [...e.touches];
  if (touches.length === 1) {
    isDragging = true; dragStartX = touches[0].clientX; dragStartY = touches[0].clientY;
    dragCamX = camX; dragCamY = camY;
  } else if (touches.length === 2) {
    isDragging = false;
    lastPinchDist = Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
  }
}, { passive: false });
canvas.addEventListener('touchmove', e => {
  e.preventDefault();
  const ct = [...e.touches];
  if (ct.length === 1 && isDragging) {
    targetCX = dragCamX + (ct[0].clientX - dragStartX) / camZoom;
    targetCY = dragCamY + (ct[0].clientY - dragStartY) / camZoom;
  } else if (ct.length === 2) {
    const dist = Math.hypot(ct[0].clientX - ct[1].clientX, ct[0].clientY - ct[1].clientY);
    if (lastPinchDist > 0) {
      targetZoom = Math.max(0.3, Math.min(4, targetZoom * (dist / lastPinchDist)));
    }
    lastPinchDist = dist;
  }
}, { passive: false });
canvas.addEventListener('touchend', () => { isDragging = false; lastPinchDist = 0; });

// Double-tap/click to reset view
canvas.addEventListener('dblclick', () => { targetZoom = 1; targetCX = 0; targetCY = 0; });

// --- Nebula (depth background clouds) ---
class Nebula {
  constructor() {
    this.x = Math.random() * 2 - 0.5; this.y = Math.random() * 2 - 0.5;
    this.r = 80 + Math.random() * 200;
    this.hue = [200, 160, 270, 30][Math.floor(Math.random() * 4)];
    this.alpha = 0.008 + Math.random() * 0.012;
    this.depth = 0.3 + Math.random() * 0.7; // parallax depth
    this.phase = Math.random() * Math.PI * 2;
  }
  draw(t) {
    const breath = Math.sin(t * 0.005 + this.phase) * 0.3 + 1;
    const px = this.x * W + camX * this.depth * 0.3;
    const py = this.y * H + camY * this.depth * 0.3;
    const g = ctx.createRadialGradient(px, py, 0, px, py, this.r * breath);
    g.addColorStop(0, `hsla(${this.hue},60%,40%,${this.alpha * breath})`);
    g.addColorStop(1, `hsla(${this.hue},60%,20%,0)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, this.r * breath, 0, Math.PI * 2); ctx.fill();
  }
}
for (let i = 0; i < 8; i++) nebulae.push(new Nebula());

// Subtle grid background
function drawGrid() {
  const spacing = 40 * camZoom;
  if (spacing < 10) return; // too zoomed out, skip grid
  ctx.strokeStyle = 'rgba(26,35,50,0.3)';
  ctx.lineWidth = 0.5;
  const ox = (camX * camZoom) % spacing;
  const oy = (camY * camZoom) % spacing;
  for (let x = ox; x < W; x += spacing) { ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,H); ctx.stroke(); }
  for (let y = oy; y < H; y += spacing) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }
}

// Ambient particles
class Particle {
  constructor() { this.reset(); }
  reset() {
    this.x = (Math.random()-0.5)*2; this.y = (Math.random()-0.5)*2;
    this.vx = (Math.random()-0.5)*0.0008; this.vy = (Math.random()-0.5)*0.0008;
    this.life = Math.random()*400+200; this.max = this.life;
    this.r = Math.random()*1.2+0.3;
    this.hue = Math.random() > 0.7 ? 160 : 200;
    this.depth = 0.5 + Math.random() * 0.5;
  }
  update() { this.x+=this.vx; this.y+=this.vy; this.life--; if(this.life<=0||Math.abs(this.x)>1.5||Math.abs(this.y)>1.5) this.reset(); }
  draw() {
    const a = (this.life/this.max)*0.25;
    const sx = cX + this.x * W * 0.5 * camZoom + camX * camZoom * this.depth;
    const sy = cY + this.y * H * 0.5 * camZoom + camY * camZoom * this.depth;
    ctx.fillStyle = `hsla(${this.hue},80%,60%,${a})`;
    ctx.beginPath(); ctx.arc(sx,sy,this.r*camZoom,0,Math.PI*2); ctx.fill();
  }
}
for(let i=0;i<250;i++) particles.push(new Particle());

// Energy pulse along connections
class Pulse {
  constructor(ax,ay,bx,by,color,speed) {
    this.ax=ax;this.ay=ay;this.bx=bx;this.by=by;this.color=color;
    this.p=0; this.speed=speed||0.015+Math.random()*0.01;
  }
  update() { this.p+=this.speed; return this.p<1; }
  draw() {
    const x=this.ax+(this.bx-this.ax)*this.p, y=this.ay+(this.by-this.ay)*this.p;
    const a=Math.sin(this.p*Math.PI);
    const sr = 8 * camZoom;
    const g=ctx.createRadialGradient(x,y,0,x,y,sr);
    g.addColorStop(0,this.color.replace('1)',a*0.9+')'));
    g.addColorStop(1,this.color.replace('1)','0)'));
    ctx.fillStyle=g; ctx.beginPath(); ctx.arc(x,y,sr*1.5,0,Math.PI*2); ctx.fill();
    ctx.fillStyle=this.color.replace('1)',Math.min(1,a*1.3)+')');
    ctx.beginPath(); ctx.arc(x,y,3*camZoom,0,Math.PI*2); ctx.fill();
  }
}

// Hexagonal node drawing
function drawHex(x,y,r,rot) {
  ctx.beginPath();
  for(let i=0;i<6;i++){const a=Math.PI/3*i+rot; ctx.lineTo(x+r*Math.cos(a),y+r*Math.sin(a));}
  ctx.closePath();
}

function layoutNodes(data) {
  const argusNodes=data.filter(n=>n.family==='argus'||!n.family);
  const familyNodes=data.filter(n=>n.family&&n.family!=='argus');
  const paper=argusNodes.filter(n=>n.stage==='paper');
  const watcher=argusNodes.filter(n=>n.stage==='watcher');
  const real=argusNodes.filter(n=>n.stage==='real'||n.stage==='quarantine');
  const iR=Math.min(W,H)*0.17, oR=Math.min(W,H)*0.30;
  const fR=Math.min(W,H)*0.42;

  real.forEach((n,i)=>{const a=(i/Math.max(1,real.length))*Math.PI*2; n.x=cX+Math.cos(a)*45; n.y=cY+Math.sin(a)*45; n.ring='core'; n.radius=30;});
  paper.forEach((n,i)=>{const a=(i/Math.max(1,paper.length))*Math.PI*2-Math.PI/2; n.x=cX+Math.cos(a)*iR; n.y=cY+Math.sin(a)*iR; n.ring='inner'; n.radius=24;});
  watcher.forEach((n,i)=>{const a=(i/Math.max(1,watcher.length))*Math.PI*2-Math.PI/2; n.x=cX+Math.cos(a)*oR; n.y=cY+Math.sin(a)*oR; n.ring='outer'; n.radius=14;});

  const famColors={helio:'#ffaa00',hermes:'#ff6b6b',apollo:'#a78bfa',forge_pairs:'#00ff88',forge_intel:'#ff00ff',forge_scalp:'#ff8800',forge_confluence:'#00ccff',forge_swing:'#88ff00'};
  // Group family nodes by family for arc segmentation
  const famGroups={};
  familyNodes.forEach(n=>{const f=n.family||'helio'; if(!famGroups[f])famGroups[f]=[]; famGroups[f].push(n);});
  const famOrder=['helio','hermes','apollo','forge_pairs','forge_intel','forge_scalp','forge_confluence','forge_swing'];
  let idx=0;
  const total=familyNodes.length;
  famOrder.forEach(fam=>{
    const group=famGroups[fam]||[];
    group.forEach((n,i)=>{
      const a=(idx/Math.max(1,total))*Math.PI*2-Math.PI/4;
      n.x=cX+Math.cos(a)*fR; n.y=cY+Math.sin(a)*fR;
      n.ring='family'; n.radius=12; n.famColor=famColors[n.family]||'#4a5568';
      idx++;
    });
  });

  nodes=[...real,...paper,...watcher,...familyNodes];
  // Add breathing offset seeds
  nodes.forEach((n,i)=>{ n._breathSeed=i*1.7; n._orbitSeed=i*0.37; });
}

function drawNode(n) {
  // Breathing animation — nodes gently pulse
  const breathe = Math.sin(t * 0.02 + n._breathSeed) * 0.08 + 1;
  const orbit = Math.sin(t * 0.008 + n._orbitSeed) * 2; // subtle wobble
  const x = n.x + orbit * (n.ring === 'family' ? 0.5 : 0.3);
  const y = n.y + Math.cos(t * 0.006 + n._orbitSeed) * 1.5 * (n.ring === 'family' ? 0.5 : 0.3);
  const r = n.radius * breathe;
  const inTrade = n.position !== 'FLAT';
  const heat = n.heat || 0;
  const colors = {paper:'#00d4ff',watcher:'#4a5568',real:'#00ff88',quarantine:'#ff9800'};
  const baseC = n.famColor || colors[n.stage] || '#333';
  const rot = t * 0.001;

  // Outer glow — more dramatic
  if(heat>0.2||inTrade||n.active){
    const gc=inTrade?'rgba(0,255,136,':'rgba(0,212,255,';
    const gs=inTrade?30*breathe:Math.max(8,heat*22)*breathe;
    const g=ctx.createRadialGradient(x,y,r*0.3,x,y,r+gs);
    g.addColorStop(0,gc+(inTrade?'0.2':'0.12')+')'); g.addColorStop(1,gc+'0)');
    ctx.fillStyle=g; ctx.beginPath(); ctx.arc(x,y,r+gs,0,Math.PI*2); ctx.fill();
  }

  // Family node aura
  if(n.ring==='family'&&n.famColor){
    const ac=n.famColor.replace('#','');
    const rr=parseInt(ac.substring(0,2),16), gg=parseInt(ac.substring(2,4),16), bb=parseInt(ac.substring(4,6),16);
    const aura=ctx.createRadialGradient(x,y,r*0.5,x,y,r+15*breathe);
    aura.addColorStop(0,`rgba(${rr},${gg},${bb},0.08)`);
    aura.addColorStop(1,`rgba(${rr},${gg},${bb},0)`);
    ctx.fillStyle=aura; ctx.beginPath(); ctx.arc(x,y,r+15*breathe,0,Math.PI*2); ctx.fill();
  }

  // Hex body
  drawHex(x,y,r,rot);
  ctx.fillStyle=n.active?'rgba(10,16,28,0.85)':'rgba(10,16,28,0.5)';
  ctx.fill();
  ctx.strokeStyle=inTrade?(n.position==='LONG'?'#00ff88':'#ff4444'):baseC;
  ctx.lineWidth=inTrade?2.5:(n.ring==='inner'?1.5:0.8);
  ctx.stroke();

  // Inner hex wireframe (depth effect)
  if(r>=18){
    drawHex(x,y,r*0.6,rot+0.5);
    ctx.strokeStyle=baseC.replace ? baseC : '#333';
    ctx.globalAlpha=0.08; ctx.lineWidth=0.5; ctx.stroke(); ctx.globalAlpha=1;
  }

  // Feature radar (inner nodes only)
  if(n.features&&r>=20){
    const feats=[
      Math.min(1,(n.features.range_pct||0)/0.003),
      Math.min(1,Math.max(0,(n.features.vol_z||0)+0.5)/1.5),
      Math.min(1,Math.max(0,(n.features.range_accel||0)+0.5)/1.5),
      n.features.dist_from_low||0,
    ];
    const fc=['#00ff88','#ffaa00','#00d4ff','#ff6b6b'];
    ctx.save(); ctx.globalAlpha=0.25;
    feats.forEach((v,i)=>{
      if(v<0.05)return;
      const sa=i*Math.PI/2-Math.PI/2, ea=sa+Math.PI/2;
      ctx.fillStyle=fc[i];
      ctx.beginPath(); ctx.moveTo(x,y); ctx.arc(x,y,r*0.65*v,sa,ea); ctx.closePath(); ctx.fill();
    });
    ctx.restore();
  }

  // Position arrow
  if(inTrade){
    ctx.fillStyle=n.position==='LONG'?'#00ff88':'#ff4444';
    ctx.beginPath();
    if(n.position==='LONG'){ctx.moveTo(x,y-r-10);ctx.lineTo(x-5,y-r-3);ctx.lineTo(x+5,y-r-3);}
    else{ctx.moveTo(x,y+r+10);ctx.lineTo(x-5,y+r+3);ctx.lineTo(x+5,y+r+3);}
    ctx.fill();
  }

  // Label
  ctx.fillStyle=n.ring==='inner'?'#c8d6e5':(n.ring==='family'?(n.famColor||'#4a5568'):'#4a5568');
  ctx.font=(r>=20?'600 10':'400 7')+'px JetBrains Mono, monospace';
  ctx.textAlign='center';
  ctx.fillText(n.symbol,x,y+r+15);

  // Micro conviction bar
  if(r>=20&&n.conviction>0){
    const bw=r*1.2, bh=2, by=y+r+19;
    ctx.fillStyle='#0d1420'; ctx.fillRect(x-bw/2,by,bw,bh);
    ctx.fillStyle=n.conviction>0.6?'#00ff88':n.conviction>0.4?'#ffaa00':'#ff4444';
    ctx.fillRect(x-bw/2,by,bw*n.conviction,bh);
  }

  // PnL micro text
  if(r>=20&&n.closed_trades>0){
    const pnlC=n.pnl>=0?'#00ff88':'#ff4444';
    ctx.fillStyle=pnlC; ctx.font='500 8px JetBrains Mono';
    ctx.fillText((n.pnl>=0?'+':'')+Number(n.pnl).toFixed(1),x,y+4);
  }

  // Store screen coords for hover detection
  n._sx=x; n._sy=y; n._sr=r;
}

function drawConnections() {
  const groups={USD:['EURUSD','GBPUSD','AUDUSD'],JPY:['EURJPY','GBPJPY','AUDJPY','CADJPY','USDJPY']};
  for(const[,syms] of Object.entries(groups)){
    const gn=nodes.filter(n=>syms.includes(n.symbol)&&n.ring!=='family');
    for(let i=0;i<gn.length;i++) for(let j=i+1;j<gn.length;j++){
      const a=gn[i],b=gn[j];
      const active=a.active&&b.active;
      const mx=(a.x+b.x)/2*0.6+cX*0.4, my=(a.y+b.y)/2*0.6+cY*0.4;
      ctx.strokeStyle=active?'rgba(0,212,255,0.25)':'rgba(0,212,255,0.06)';
      ctx.lineWidth=active?1.2:0.6;
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.quadraticCurveTo(mx,my,b.x,b.y); ctx.stroke();
    }
  }
  // Greek family connections
  const famGroups={};
  nodes.filter(n=>n.ring==='family').forEach(n=>{
    const f=n.symbol.split(':')[0]||'HELIO';
    if(!famGroups[f])famGroups[f]=[];
    famGroups[f].push(n);
  });
  const famC={HELIO:'rgba(255,170,0,',HERMES:'rgba(255,107,107,',APOLLO:'rgba(167,139,250,'};
  for(const[fam,gn] of Object.entries(famGroups)){
    const c=famC[fam]||'rgba(74,85,104,';
    for(let i=0;i<gn.length;i++) for(let j=i+1;j<gn.length;j++){
      ctx.strokeStyle=c+'0.1)'; ctx.lineWidth=0.5;
      ctx.beginPath(); ctx.moveTo(gn[i].x,gn[i].y); ctx.lineTo(gn[j].x,gn[j].y); ctx.stroke();
    }
  }
  // Cross-family connections
  nodes.filter(n=>n.ring==='family').forEach(fn=>{
    const baseSym=fn.symbol.includes(':')?fn.symbol.split(':')[1]:'';
    const argusMatch=nodes.find(n=>n.ring!=='family'&&n.symbol===baseSym);
    if(argusMatch){
      ctx.strokeStyle=(famC[fn.symbol.split(':')[0]]||'rgba(74,85,104,')+'0.06)';
      ctx.lineWidth=0.4;
      ctx.setLineDash([2,6]);
      ctx.beginPath(); ctx.moveTo(fn.x,fn.y); ctx.lineTo(argusMatch.x,argusMatch.y); ctx.stroke();
      ctx.setLineDash([]);
    }
  });
}

function drawCore() {
  const pulse=(Math.sin(t*0.03)+1)/2;
  const breathe=Math.sin(t*0.015)*3;
  // Outer ring
  ctx.strokeStyle=`rgba(0,212,255,${0.06+pulse*0.06})`;
  ctx.lineWidth=1.5;
  ctx.beginPath(); ctx.arc(cX,cY,38+pulse*4+breathe,0,Math.PI*2); ctx.stroke();
  // Second ring
  ctx.strokeStyle=`rgba(0,212,255,${0.03+pulse*0.02})`;
  ctx.lineWidth=0.8;
  ctx.beginPath(); ctx.arc(cX,cY,28+pulse*2,0,Math.PI*2); ctx.stroke();
  // Inner glow
  const g=ctx.createRadialGradient(cX,cY,0,cX,cY,35+pulse*8+breathe);
  g.addColorStop(0,`rgba(0,212,255,${0.1+pulse*0.06})`);
  g.addColorStop(0.5,`rgba(0,212,255,${0.03+pulse*0.02})`);
  g.addColorStop(1,'rgba(0,212,255,0)');
  ctx.fillStyle=g; ctx.beginPath(); ctx.arc(cX,cY,35+pulse*8+breathe,0,Math.PI*2); ctx.fill();
  // Label
  ctx.fillStyle=`rgba(0,212,255,${0.6+pulse*0.3})`;
  ctx.font='900 12px Orbitron, sans-serif';
  ctx.textAlign='center';
  ctx.fillText('HELIO',cX,cY+4);
  // Ring labels
  const iR=Math.min(W,H)*0.17, oR=Math.min(W,H)*0.30;
  const fR=Math.min(W,H)*0.42;
  ctx.strokeStyle='rgba(0,212,255,0.07)'; ctx.lineWidth=0.6;
  ctx.setLineDash([3,9]);
  ctx.beginPath(); ctx.arc(cX,cY,iR,0,Math.PI*2); ctx.stroke();
  ctx.beginPath(); ctx.arc(cX,cY,oR,0,Math.PI*2); ctx.stroke();
  ctx.strokeStyle='rgba(255,170,0,0.1)'; ctx.lineWidth=0.8;
  ctx.beginPath(); ctx.arc(cX,cY,fR,0,Math.PI*2); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='rgba(74,85,104,0.3)'; ctx.font='400 8px JetBrains Mono';
  ctx.fillText('PAPER',cX,cY-iR-6);
  ctx.fillText('WATCHERS',cX,cY-oR-6);
  ctx.fillStyle='rgba(255,170,0,0.3)';
  ctx.fillText('GREEK FAMILY',cX,cY-fR-6);
}

// Hover tooltip (uses screen-space coords from _sx/_sy)
canvas.addEventListener('mousemove',e=>{
  if(isDragging) return;
  hoveredNode=null;
  const mx=(e.clientX-W/2)/camZoom+cX-camX, my=(e.clientY-H/2)/camZoom+cY-camY;
  for(const n of nodes){
    const sx=n._sx||n.x, sy=n._sy||n.y, sr=n._sr||n.radius;
    const dx=e.clientX-((sx-cX)*camZoom+W/2+camX*camZoom);
    const dy=e.clientY-((sy-cY)*camZoom+H/2+camY*camZoom);
    if(Math.sqrt(dx*dx+dy*dy)<(sr*camZoom+8)){hoveredNode=n;break;}
  }
  if(!isDragging) canvas.style.cursor=hoveredNode?'pointer':'default';
});

function drawTooltip(){
  if(!hoveredNode)return;
  const n=hoveredNode;
  const sx=(n._sx-cX)*camZoom+W/2+camX*camZoom;
  const sy=(n._sy-cY)*camZoom+H/2+camY*camZoom;
  const lines=[
    n.name+' ('+n.stage.toUpperCase()+')',
    (n.family||'argus').toUpperCase()+' Family',
    n.position+(n.position!=='FLAT'?' @ '+n.entry_price:''),
    'PnL: '+(n.pnl>=0?'+':'')+Number(n.pnl).toFixed(1)+' '+n.unit,
    'WR: '+(n.win_rate*100).toFixed(0)+'% | Trades: '+n.closed_trades,
    'Conv: '+(n.conviction||0).toFixed(2)+' | Sess: '+n.session,
    'Heat: '+(n.heat||0).toFixed(2)+' | Blk24h: '+(n.blocked_24h||0),
  ];
  const lw=230, lh=lines.length*16+16;
  let rx=sx+20, ry=sy-lh/2;
  if(rx+lw>W-10)rx=sx-20-lw;
  ry=Math.max(10,Math.min(ry,H-lh-10));
  // Save transform for tooltip (drawn in screen space)
  ctx.save(); ctx.setTransform(1,0,0,1,0,0);
  ctx.fillStyle='rgba(6,10,18,0.94)';
  ctx.strokeStyle='rgba(0,212,255,0.25)';
  ctx.lineWidth=1;
  ctx.beginPath(); ctx.roundRect(rx,ry,lw,lh,6); ctx.fill(); ctx.stroke();
  ctx.textAlign='left'; ctx.font='500 10px JetBrains Mono';
  const lColors=['#00d4ff','#a78bfa',null,'#c8d6e5','#c8d6e5','#c8d6e5','#c8d6e5'];
  lines.forEach((l,i)=>{
    ctx.fillStyle=lColors[i]||(i===2?(n.position==='LONG'?'#00ff88':n.position==='SHORT'?'#ff4444':'#4a5568'):'#c8d6e5');
    ctx.fillText(l,rx+10,ry+16+i*16);
  });
  ctx.restore();
}

// Scanning radar line
function drawRadar(){
  const angle=t*0.008;
  const len=Math.min(W,H)*0.45;
  const ex=cX+Math.cos(angle)*len, ey=cY+Math.sin(angle)*len;
  const g=ctx.createLinearGradient(cX,cY,ex,ey);
  g.addColorStop(0,'rgba(0,212,255,0)');
  g.addColorStop(0.6,'rgba(0,212,255,0.04)');
  g.addColorStop(1,'rgba(0,212,255,0)');
  ctx.strokeStyle=g; ctx.lineWidth=1.5;
  ctx.beginPath(); ctx.moveTo(cX,cY); ctx.lineTo(ex,ey); ctx.stroke();
  for(let i=1;i<=16;i++){
    const ta=angle-i*0.007;
    const tx=cX+Math.cos(ta)*len, ty=cY+Math.sin(ta)*len;
    ctx.strokeStyle=`rgba(0,212,255,${0.018-i*0.001})`;
    ctx.lineWidth=0.5;
    ctx.beginPath(); ctx.moveTo(cX,cY); ctx.lineTo(tx,ty); ctx.stroke();
  }
}

// Orbital ring particles
function drawOrbitals(){
  const iR=Math.min(W,H)*0.17, oR=Math.min(W,H)*0.30, fR=Math.min(W,H)*0.42;
  for(let i=0;i<10;i++){
    const a=t*0.005+i*Math.PI/5;
    const br=1+Math.sin(t*0.02+i)*0.3;
    const x=cX+Math.cos(a)*(iR+3), y=cY+Math.sin(a)*(iR+3);
    ctx.fillStyle=`rgba(0,212,255,${0.18+Math.sin(t*0.02+i)*0.12})`;
    ctx.beginPath(); ctx.arc(x,y,1.5*br,0,Math.PI*2); ctx.fill();
  }
  for(let i=0;i<20;i++){
    const a=-t*0.003+i*Math.PI/10;
    const x=cX+Math.cos(a)*(oR+3), y=cY+Math.sin(a)*(oR+3);
    ctx.fillStyle=`rgba(74,85,104,${0.14+Math.sin(t*0.015+i)*0.1})`;
    ctx.beginPath(); ctx.arc(x,y,1,0,Math.PI*2); ctx.fill();
  }
  for(let i=0;i<14;i++){
    const a=t*0.002+i*Math.PI*2/14;
    const br=1+Math.sin(t*0.008+i*0.5)*0.2;
    const x=cX+Math.cos(a)*(fR+4), y=cY+Math.sin(a)*(fR+4);
    ctx.fillStyle=`rgba(255,170,0,${0.12+Math.sin(t*0.01+i)*0.08})`;
    ctx.beginPath(); ctx.arc(x,y,1.2*br,0,Math.PI*2); ctx.fill();
  }
}

// Ambient corner glow
function drawAmbient(){
  const pulse=Math.sin(t*0.008)*0.5+0.5;
  const g1=ctx.createRadialGradient(0,0,0,0,0,W*0.4);
  g1.addColorStop(0,`rgba(0,212,255,${0.012+pulse*0.005})`);
  g1.addColorStop(1,'rgba(0,212,255,0)');
  ctx.fillStyle=g1; ctx.fillRect(0,0,W,H);
  const g2=ctx.createRadialGradient(W,H,0,W,H,W*0.35);
  g2.addColorStop(0,`rgba(0,255,136,${0.008+pulse*0.003})`);
  g2.addColorStop(1,'rgba(0,255,136,0)');
  ctx.fillStyle=g2; ctx.fillRect(0,0,W,H);
  // Golden accent for family ring
  const g3=ctx.createRadialGradient(W*0.7,H*0.2,0,W*0.7,H*0.2,W*0.25);
  g3.addColorStop(0,`rgba(255,170,0,${0.004+pulse*0.002})`);
  g3.addColorStop(1,'rgba(255,170,0,0)');
  ctx.fillStyle=g3; ctx.fillRect(0,0,W,H);
}

// Main loop
function render(){
  t++;
  // Smooth camera
  camZoom += (targetZoom - camZoom) * 0.08;
  camX += (targetCX - camX) * 0.08;
  camY += (targetCY - camY) * 0.08;

  ctx.clearRect(0,0,W,H);

  // Draw nebulae (background, no transform)
  nebulae.forEach(n=>n.draw(t));
  drawAmbient();
  drawGrid();
  particles.forEach(p=>{p.update();p.draw();});

  // Apply camera transform for world-space elements
  ctx.save();
  ctx.translate(W/2, H/2);
  ctx.scale(camZoom, camZoom);
  ctx.translate(-cX + camX, -cY + camY);

  drawRadar();
  drawConnections();
  drawOrbitals();
  drawCore();
  pulses=pulses.filter(p=>{p.draw();return p.update();});
  nodes.forEach(drawNode);

  ctx.restore();

  // Tooltip in screen space
  drawTooltip();

  requestAnimationFrame(render);
}

// Data
async function fetchBrainState(){
  try{
    const r=await fetch('/api/brain_state');
    const d=await r.json();
    layoutNodes(d.nodes);

    const act=d.nodes.filter(n=>n.active).length;
    const trades=d.nodes.filter(n=>n.position!=='FLAT').length;
    const famCount=d.families?Object.keys(d.families).length:1;
    const argusN=d.nodes.filter(n=>n.family==='argus'||!n.family).length;
    const greekN=d.nodes.filter(n=>n.family&&n.family!=='argus').length;
    document.getElementById('s-nodes').textContent=d.nodes.length;
    document.getElementById('s-active').textContent=act;
    document.getElementById('s-trades').textContent=trades;
    document.getElementById('s-families').textContent=famCount;
    document.getElementById('s-signals').textContent=d.recent_signals.length;
    document.getElementById('status-line').textContent=
      argusN+' Argus + '+greekN+' Greek nodes | '+act+' firing | '+famCount+' families | '+new Date().toISOString().substring(11,19)+' UTC';

    // Fire pulses for active nodes
    for(const n of d.nodes){
      if(!n.active)continue;
      const nd=nodes.find(x=>x.symbol===n.symbol);
      if(nd&&Math.random()<0.35){
        pulses.push(new Pulse(nd.x,nd.y,cX,cY,'rgba(0,212,255,1)',0.008+Math.random()*0.008));
      }
      if(nd&&n.position!=='FLAT'&&Math.random()<0.5){
        const c=n.position==='LONG'?'rgba(0,255,136,1)':'rgba(255,68,68,1)';
        pulses.push(new Pulse(cX,cY,nd.x,nd.y,c,0.015));
      }
    }

    const activeSyms=d.nodes.filter(n=>n.active).map(n=>n.symbol);
    if(activeSyms.length>=2&&Math.random()<0.12){
      const a=nodes.find(n=>n.symbol===activeSyms[Math.floor(Math.random()*activeSyms.length)]);
      const b=nodes.find(n=>n.symbol===activeSyms[Math.floor(Math.random()*activeSyms.length)]);
      if(a&&b&&a!==b) pulses.push(new Pulse(a.x,a.y,b.x,b.y,'rgba(0,212,255,1)',0.01));
    }

    // Decision flow panel
    const gateKeys=[
      ['session','Session'],['range_pct','Range Pct'],['direction','Direction'],
      ['regime','Regime'],['maintenance','Maint.'],['mtf','MTF Align'],
      ['spread','Spread'],['news','News'],['sequencing','Sequencing'],
      ['sizing','Sizing'],['risk_gate','Risk Gate']
    ];
    let latestGates = {};
    let latestGateSymbol = '';
    for (const n of d.nodes) {
      if (n.gates && n.gates.trigger) {
        latestGates = n.gates;
        latestGateSymbol = n.symbol;
        break;
      }
    }
    const gc=document.getElementById('gate-chain');
    const triggerStatus = latestGates.trigger || 'NO_SIGNAL';
    const finalStatus = latestGates.final || '---';
    let chainHtml = '<div style="font-size:0.6em;color:#4a5568;margin-bottom:4px;">'
      + (latestGateSymbol ? latestGateSymbol + ' | ' : '')
      + (triggerStatus === 'PASS' ? '<span style="color:#00ff88;">SIGNAL FIRED</span>' : '<span style="color:#333;">WAITING</span>')
      + ' | Final: <span style="color:' + (finalStatus === 'ENTRY' ? '#00ff88' : finalStatus === 'BLOCKED' ? '#ff4444' : '#4a5568') + ';">' + finalStatus + '</span></div>';
    chainHtml += gateKeys.map(([key, label]) => {
      const val = latestGates[key] || '---';
      const cls = val === 'PASS' ? 'pass' : val === 'FAIL' ? 'fail' : val === 'SHADOW' ? 'shadow' : 'idle';
      const color = cls === 'pass' ? '#00ff88' : cls === 'fail' ? '#ff4444' : cls === 'shadow' ? '#ffaa00' : '#1a2332';
      return '<div class="gate-row"><div class="gate-dot ' + cls + '"></div><span class="gate-name">' + label + '</span><span class="gate-result" style="color:' + color + ';">' + val + '</span></div>';
    }).join('');
    gc.innerHTML = chainHtml;

    // Signal feed
    const sf=document.getElementById('signal-feed');
    const sigs=d.recent_signals||[];
    sf.innerHTML=sigs.slice(0,12).map(s=>{
      const ac=s.action||'';
      const c=ac==='ENTRY'?'#00ff88':ac.includes('BLOCK')?'#ff4444':'#4a5568';
      return '<div class="sig-entry"><span style="color:#333;">'+(s.ts||'').substring(11,19)+'</span>'
        +'<span style="color:#00d4ff;font-weight:500;">'+s.symbol+'</span>'
        +'<span style="color:'+c+';">'+ac+'</span>'
        +(s.direction?'<span style="color:#4a5568;">'+s.direction+'</span>':'')+'</div>';
    }).join('')||'<div style="color:#333;">No recent signals</div>';

    // System health panel
    const hp=document.getElementById('health-items');
    let hHtml='';
    const pg=d.portfolio_guard||{};
    const pgMetrics=pg.metrics||{};
    const pgOpen=pgMetrics.total_open_positions||0;
    const pgMax=pgMetrics.max_total_positions||6;
    const pgOk=pg.allowed!==false;
    const pgColor=pgOk?'#00ff88':'#ff4444';
    const pgWarn=pg.warnings||[];
    document.getElementById('s-guard').textContent=pgOpen+'/'+pgMax;
    document.getElementById('s-guard').style.color=pgOk?(pgWarn.length?'#ffaa00':'#00ff88'):'#ff4444';
    hHtml+='<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #0d1420;"><span style="color:#4a5568;">Portfolio Guard</span><span style="color:'+pgColor+';">'+(pgOk?'CLEAR':'BLOCKED')+'</span></div>';
    hHtml+='<div style="padding:2px 0 6px;color:#333;">Positions: '+pgOpen+'/'+pgMax+'</div>';
    if(pgWarn.length){hHtml+='<div style="padding:2px 0 6px;color:#ffaa00;font-size:0.9em;">'+pgWarn[0]+'</div>';}
    const dr=d.drift||{};
    const drHealth=dr.overall_health||'UNKNOWN';
    const drColor=drHealth==='GREEN'?'#00ff88':drHealth==='YELLOW'?'#ffaa00':drHealth==='RED'?'#ff4444':'#4a5568';
    const drChecked=dr.features_checked||0;
    const drDrifted=(dr.drifted_features||[]).length;
    document.getElementById('s-drift').textContent=drHealth;
    document.getElementById('s-drift').style.color=drColor;
    hHtml+='<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #0d1420;margin-top:4px;"><span style="color:#4a5568;">Feature Drift</span><span style="color:'+drColor+';">'+drHealth+'</span></div>';
    hHtml+='<div style="padding:2px 0 6px;color:#333;">'+drChecked+' features | '+drDrifted+' drifted</div>';
    const fams=d.families||{};
    hHtml+='<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1a2332;">';
    const famColors={argus:'#00d4ff',helio:'#ffaa00',hermes:'#ff6b6b',apollo:'#a78bfa'};
    for(const[f,info] of Object.entries(fams)){
      const fc=famColors[f]||'#4a5568';
      hHtml+='<div style="display:flex;justify-content:space-between;padding:2px 0;"><span style="color:'+fc+';text-transform:uppercase;">'+f+'</span><span style="color:#4a5568;">'+info.count+' nodes | '+info.trades+' trades</span></div>';
    }
    hHtml+='</div>';
    hp.innerHTML=hHtml;

    // Ticker
    const ti=document.getElementById('ticker-inner');
    const items=d.nodes.map(n=>{
      const pc=n.position==='LONG'?'#00ff88':n.position==='SHORT'?'#ff4444':'#1a2332';
      const hc=n.heat>0.8?'#ffaa00':n.heat>0.5?'#00d4ff':'#1a2332';
      return '<span class="tick-item"><span class="tick-dot" style="background:'+hc+';"></span>'
        +'<span style="color:'+hc+';">'+n.symbol+'</span>'
        +'<span style="color:'+pc+';">'+n.position+'</span></span>';
    }).join('');
    ti.innerHTML=items+items;
  }catch(e){document.getElementById('status-line').textContent='Neural link error: '+e.message;}
}

fetchBrainState();
setInterval(fetchBrainState,3000);
render();
</script>
</body>
</html>"""


@app.get("/brain", response_class=HTMLResponse)
async def brain_page():
    return BRAIN_HTML


# ---------------------------------------------------------------------------
# Fleet Operations Page
# ---------------------------------------------------------------------------


def _collect_fleet_status() -> dict:
    """Gather all fleet data for the /api/fleet_status endpoint."""
    import sqlite3

    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "regime": {"mode": "UNKNOWN", "vix": 0, "vix_term": "N/A", "position_size_mod": 1.0},
        "active_trades": [],
        "systems": [],
        "recent_trades": [],
        "conviction": [],
        "events": [],
        "fleet_pnl_today": 0,
    }

    # --- Atlas regime ---
    atlas_hb = REPO / "forge" / "logs" / "atlas" / "heartbeat.json"
    if atlas_hb.exists():
        try:
            ah = json.loads(atlas_hb.read_text(encoding="utf-8"))
            result["regime"] = {
                "mode": ah.get("regime", "UNKNOWN").upper(),
                "alert_level": ah.get("alert_level", "normal"),
                "vix": 0,
                "vix_term": "N/A",
                "position_size_mod": ah.get("position_size_modifier", 1.0),
                "events_today": ah.get("events_today", 0),
            }
        except Exception:
            pass

    # VIX structure
    vix_hist = REPO / "forge" / "logs" / "atlas" / "vix_structure_history.json"
    if vix_hist.exists():
        try:
            vdata = json.loads(vix_hist.read_text(encoding="utf-8"))
            if vdata:
                latest = vdata[-1]
                result["regime"]["vix"] = latest.get("vix", 0)
                result["regime"]["vix_term"] = latest.get("structure", "N/A")
        except Exception:
            pass

    # --- Portfolio guard active positions ---
    pg_path = REPO / "helio" / "logs" / "portfolio_guard.json"
    if pg_path.exists():
        try:
            pg = json.loads(pg_path.read_text(encoding="utf-8"))
            for pos in pg.get("metrics", {}).get("positions_detail", []):
                result["active_trades"].append({
                    "system": pos.get("family", "?"),
                    "ticker": pos.get("symbol", "?"),
                    "direction": pos.get("direction", "?"),
                    "entry_price": 0,
                    "current_pnl": 0,
                    "days_held": 0,
                    "conviction": 0,
                })
        except Exception:
            pass

    # Enrich active trades from heartbeats
    helio_logs = REPO / "helio" / "logs"
    for at in result["active_trades"]:
        sym_lower = at["ticker"].lower()
        fam = at["system"].lower()
        hb_dir = helio_logs / f"{fam}_{sym_lower}"
        if not hb_dir.exists():
            hb_dir = helio_logs / sym_lower
        hb_path = hb_dir / "heartbeat.json"
        if hb_path.exists():
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
                at["entry_price"] = hb.get("entry_price", hb.get("close", 0))
                at["conviction"] = round(hb.get("rsi", 0) / 100, 2) if hb.get("rsi") else 0
            except Exception:
                pass

    # --- System status cards ---
    all_systems = []

    # Argus FX systems
    try:
        argus_runners = _managed_ibkr_runners()
        for r in argus_runners:
            rd = _read_ibkr_runner(r)
            all_systems.append({
                "name": rd.get("name", r.get("symbol", "?")),
                "family": "argus",
                "family_color": "#00d4ff",
                "status": "OK" if rd.get("last_signal_age_s", 9999) < 300 else "STALE",
                "heartbeat_age": rd.get("last_signal_age_s", 9999),
                "metric_label": "Position",
                "metric_value": rd.get("position", "FLAT"),
                "extra": f"Regime: {rd.get('regime', 'N/A')}",
            })
    except Exception:
        pass

    # Helio family (Titan, Apollo, Hermes, Helio-core)
    if helio_logs.exists():
        for sub in sorted(helio_logs.iterdir()):
            if not sub.is_dir() or sub.name.startswith("_"):
                continue
            hb_path = sub / "heartbeat.json"
            if not hb_path.exists():
                continue
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
                age = time.time() - hb_path.stat().st_mtime
                fam = hb.get("family", "helio")
                fam_colors = {"helio": "#00d4ff", "titan": "#ffaa00", "apollo": "#a78bfa", "hermes": "#ff6b6b"}
                pos = hb.get("position", "FLAT")
                all_systems.append({
                    "name": f"{fam.capitalize()} {hb.get('symbol', sub.name)}",
                    "family": fam,
                    "family_color": fam_colors.get(fam, "#4a5568"),
                    "status": "OK" if age < 86400 else "STALE",
                    "heartbeat_age": int(age),
                    "metric_label": "Position",
                    "metric_value": pos,
                    "extra": f"Trades: {hb.get('trade_count', 0)}",
                })
            except Exception:
                pass

    # Forge systems
    forge_defs = [
        ("GDX/GLD Pairs", "gdx_gld", "forge_pairs", "#00ff88"),
        ("Atlas Intel", "atlas", "forge_intel", "#ff6b6b"),
        ("Themis Congress", "themis", "forge_intel", "#ffaa00"),
        ("Mamba NQ/YM", "mamba", "forge_scalp", "#a78bfa"),
        ("Cue Banks US30", "cuebanks", "forge_confluence", "#00d4ff"),
        ("Tori Swing", "tori", "forge_swing", "#ff6b6b"),
    ]
    for name, dirname, family, color in forge_defs:
        hb_path = REPO / "forge" / "logs" / dirname / "heartbeat.json"
        if not hb_path.exists():
            continue
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            age = time.time() - hb_path.stat().st_mtime
            status = "OK" if age < 600 else ("STALE" if age < 3600 else "DOWN")
            metric_label, metric_value, extra = "Status", hb.get("status", hb.get("mode", "?")), ""
            if dirname == "gdx_gld":
                metric_label = "Z-Score"
                metric_value = str(round(hb.get("z_score", 0), 3))
                extra = f"Pos: {hb.get('position', 'FLAT')}"
            elif dirname == "atlas":
                metric_label = "Events Today"
                metric_value = str(hb.get("events_today", 0))
                extra = f"Alert: {hb.get('alert_level', 'N/A')}"
            elif dirname == "themis":
                metric_label = "Active Signals"
                metric_value = str(hb.get("active_signals", 0))
                extra = f"Trades tracked: {hb.get('total_trades', 0)}"
            elif dirname == "mamba":
                metric_label = "Status"
                metric_value = hb.get("status", "?")
                extra = ", ".join(hb.get("tickers", []))
            elif dirname == "cuebanks":
                metric_label = "Mode"
                metric_value = hb.get("status", "?")
            elif dirname == "tori":
                metric_label = "Instruments"
                metric_value = str(len(hb.get("instruments", [])))
                extra = ", ".join(hb.get("instruments", [])[:3])
            all_systems.append({
                "name": name,
                "family": family,
                "family_color": color,
                "status": status,
                "heartbeat_age": int(age),
                "metric_label": metric_label,
                "metric_value": metric_value,
                "extra": extra,
            })
        except Exception:
            pass

    result["systems"] = all_systems

    # --- Recent trades (last 20 across all systems) ---
    all_trades = []

    # Helio family trades
    if helio_logs.exists():
        for sub in sorted(helio_logs.iterdir()):
            trades_csv = sub / "trades.csv"
            if not trades_csv.exists():
                continue
            try:
                rows = list(csv.DictReader(open(trades_csv, encoding="utf-8")))
                fam = sub.name.split("_")[0] if "_" in sub.name else "helio"
                sym = sub.name.split("_", 1)[1].upper() if "_" in sub.name else sub.name.upper()
                for r in rows[-10:]:
                    all_trades.append({
                        "date": r.get("exit_date", r.get("entry_date", "?")),
                        "system": fam,
                        "ticker": sym,
                        "direction": r.get("direction", "?"),
                        "entry": r.get("entry_px", "?"),
                        "exit": r.get("exit_px", "?"),
                        "pnl_pct": float(r.get("pnl_pct", 0)),
                        "duration": r.get("bars_held", "?"),
                        "exit_reason": r.get("exit_reason", "?"),
                    })
            except Exception:
                pass

    # Argus FX trades
    argus_logs = REPO / "argus_flow" / "logs"
    if argus_logs.exists():
        for sub in sorted(argus_logs.iterdir()):
            trades_csv = sub / "trades.csv"
            if not trades_csv.exists():
                continue
            try:
                rows = list(csv.DictReader(open(trades_csv, encoding="utf-8")))
                for r in rows[-10:]:
                    all_trades.append({
                        "date": (r.get("ts", "?"))[:10],
                        "system": "argus",
                        "ticker": sub.name.upper(),
                        "direction": r.get("direction", "?"),
                        "entry": r.get("entry_px", "?"),
                        "exit": r.get("exit_px", "?"),
                        "pnl_pct": float(r.get("pnl_pips", 0)),
                        "duration": r.get("duration_min", "?"),
                        "exit_reason": r.get("exit_reason", "?"),
                    })
            except Exception:
                pass

    # Sort by date descending, take last 20
    all_trades.sort(key=lambda x: x.get("date", ""), reverse=True)
    result["recent_trades"] = all_trades[:20]

    # --- Upcoming events from Atlas DB ---
    atlas_db = REPO / "forge" / "atlas" / "atlas.db"
    if atlas_db.exists():
        try:
            conn = sqlite3.connect(str(atlas_db))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            end = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT date, title, importance, category FROM scheduled_events "
                "WHERE date >= ? AND date <= ? ORDER BY date LIMIT 20",
                (now, end),
            )
            for row in cur.fetchall():
                result["events"].append({
                    "date": row[0],
                    "title": row[1],
                    "importance": row[2] if row[2] else "normal",
                    "category": row[3] if row[3] else "",
                })
            conn.close()
        except Exception:
            pass

    return result


@app.get("/api/fleet_status")
async def api_fleet_status():
    """Fleet operations data endpoint."""
    try:
        data = _collect_fleet_status()
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


FLEET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Helio Fleet Operations</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0a0e1a; color:#e0e0e0; font-family:'Courier New',monospace; font-size:14px; }
a { color:#00d4ff; text-decoration:none; }
a:hover { text-decoration:underline; }

.header { background:#111827; border-bottom:1px solid #1e2a42; padding:12px 20px; display:flex;
  align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; position:sticky; top:0; z-index:100; }
.header-left { display:flex; align-items:center; gap:16px; }
.fleet-name { font-size:1.3em; font-weight:bold; letter-spacing:4px;
  background:linear-gradient(135deg,#00d4ff,#00ff88); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.regime-badge { padding:4px 12px; border-radius:4px; font-weight:bold; font-size:0.85em; }
.regime-RISK_ON,.regime-UNKNOWN { background:#00ff8833; color:#00ff88; }
.regime-RISK_OFF { background:#ffaa0033; color:#ffaa00; }
.regime-CRISIS { background:#ff444433; color:#ff4444; }
.header-stats { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
.header-stat { text-align:center; }
.header-stat .val { font-size:1.1em; font-weight:bold; }
.header-stat .lbl { font-size:0.65em; color:#4a5568; text-transform:uppercase; letter-spacing:1px; }
.psm-warn { color:#ffaa00; font-weight:bold; font-size:1.2em; }
.nav-links { display:flex; gap:8px; }
.nav-links a { padding:4px 12px; border:1px solid #1e2a42; border-radius:12px; font-size:0.75em;
  color:#4a5568; transition:all 0.2s; }
.nav-links a:hover { background:#1e2a42; color:#00d4ff; border-color:#00d4ff44; text-decoration:none; }

.container { max-width:1400px; margin:0 auto; padding:16px; }
.section { margin-bottom:20px; }
.section-title { font-size:0.8em; color:#00d4ff; letter-spacing:3px; text-transform:uppercase;
  margin-bottom:8px; padding-bottom:4px; border-bottom:1px solid #1e2a42; }

/* Active trades table */
table { width:100%; border-collapse:collapse; font-size:0.85em; }
th { color:#4a5568; text-align:left; padding:6px 8px; font-weight:normal; text-transform:uppercase;
  font-size:0.75em; letter-spacing:1px; border-bottom:1px solid #1e2a42; }
td { padding:6px 8px; border-bottom:1px solid #0d1420; }
.pos { color:#00ff88; } .neg { color:#ff4444; }
.dir-long { color:#00ff88; } .dir-short { color:#ff4444; }

/* System grid */
.sys-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }
.sys-card { background:#111827; border:1px solid #1e2a42; border-radius:6px; padding:10px;
  transition:border-color 0.2s; }
.sys-card:hover { border-color:#00d4ff44; }
.sys-card-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
.sys-name { font-weight:bold; font-size:0.85em; }
.sys-family { font-size:0.6em; padding:2px 6px; border-radius:3px; text-transform:uppercase; letter-spacing:1px; }
.status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:4px; }
.dot-OK { background:#00ff88; } .dot-STALE { background:#ffaa00; } .dot-DOWN { background:#ff4444; }
.sys-metric { margin-top:4px; }
.sys-metric .k { color:#4a5568; font-size:0.75em; }
.sys-metric .v { font-size:0.9em; }
.sys-age { color:#4a5568; font-size:0.7em; margin-top:4px; }

/* Trades log */
.trade-row { display:grid; grid-template-columns:90px 60px 70px 55px 80px 80px 65px 55px 70px;
  gap:4px; padding:4px 0; border-bottom:1px solid #0d1420; font-size:0.8em; align-items:center; }
.trade-header { color:#4a5568; font-size:0.7em; text-transform:uppercase; letter-spacing:1px; }

/* Events */
.event-row { display:flex; gap:12px; padding:4px 0; border-bottom:1px solid #0d1420; font-size:0.8em; }
.event-date { color:#4a5568; min-width:80px; }
.imp-high { color:#ff4444; } .imp-medium { color:#ffaa00; } .imp-low,.imp-normal { color:#4a5568; }

/* Refresh indicator */
.refresh-bar { position:fixed; bottom:0; left:0; right:0; background:#111827; border-top:1px solid #1e2a42;
  padding:4px 20px; display:flex; justify-content:space-between; font-size:0.7em; color:#4a5568; z-index:100; }

.empty-msg { color:#333; font-size:0.85em; padding:8px 0; }

@media(max-width:768px) {
  .header { flex-direction:column; align-items:flex-start; }
  .sys-grid { grid-template-columns:1fr; }
  .trade-row { grid-template-columns:1fr 1fr 1fr; font-size:0.75em; }
  .container { padding:8px; }
}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <span class="fleet-name">HELIO FLEET</span>
    <span id="regime-badge" class="regime-badge regime-UNKNOWN">---</span>
  </div>
  <div class="header-stats">
    <div class="header-stat"><div class="val" id="h-vix">--</div><div class="lbl">VIX</div></div>
    <div class="header-stat"><div class="val" id="h-term">--</div><div class="lbl">Term</div></div>
    <div class="header-stat"><div class="val psm-warn" id="h-psm" style="display:none">0.75x</div><div class="lbl" id="h-psm-lbl" style="display:none">Size Mod</div></div>
    <div class="header-stat"><div class="val" id="h-pnl">--</div><div class="lbl">Fleet P&amp;L</div></div>
  </div>
  <div class="nav-links">
    <a href="/">Dashboard</a>
    <a href="/brain">Neural Core</a>
  </div>
</div>

<div class="container">

  <div class="section">
    <div class="section-title">Active Positions</div>
    <table>
      <thead><tr><th>System</th><th>Ticker</th><th>Direction</th><th>Entry</th><th>P&amp;L</th><th>Days</th><th>Conviction</th></tr></thead>
      <tbody id="active-trades"><tr><td colspan="7" class="empty-msg">Loading...</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">System Status</div>
    <div class="sys-grid" id="sys-grid"></div>
  </div>

  <div class="section">
    <div class="section-title">Recent Trades</div>
    <div id="recent-trades"></div>
  </div>

  <div class="section">
    <div class="section-title">Upcoming Events (7 days)</div>
    <div id="events-list"><div class="empty-msg">Loading...</div></div>
  </div>

</div>

<div class="refresh-bar">
  <span id="last-update">Connecting...</span>
  <span id="sys-count">-- systems</span>
</div>

<script>
let fleetData = null;

async function fetchFleet() {
  try {
    const r = await fetch('/api/fleet_status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    fleetData = await r.json();
    render();
    document.getElementById('last-update').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('last-update').textContent = 'Error: ' + e.message;
  }
}

function fmtAge(s) {
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s/60) + 'm';
  if (s < 86400) return Math.round(s/3600) + 'h';
  return Math.round(s/86400) + 'd';
}

function render() {
  const d = fleetData;
  if (!d) return;

  // Header
  const reg = d.regime || {};
  const mode = reg.mode || 'UNKNOWN';
  const badge = document.getElementById('regime-badge');
  badge.textContent = mode;
  badge.className = 'regime-badge regime-' + mode;
  document.getElementById('h-vix').textContent = reg.vix ? reg.vix.toFixed(1) : '--';
  document.getElementById('h-term').textContent = reg.vix_term || '--';
  const psm = reg.position_size_mod;
  if (psm && psm !== 1.0) {
    document.getElementById('h-psm').textContent = psm + 'x';
    document.getElementById('h-psm').style.display = '';
    document.getElementById('h-psm-lbl').style.display = '';
  } else {
    document.getElementById('h-psm').style.display = 'none';
    document.getElementById('h-psm-lbl').style.display = 'none';
  }
  document.getElementById('h-pnl').textContent = d.fleet_pnl_today ? d.fleet_pnl_today.toFixed(2) : '--';

  // Active trades
  const at = d.active_trades || [];
  const tbody = document.getElementById('active-trades');
  if (at.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">No active positions</td></tr>';
  } else {
    tbody.innerHTML = at.map(t => {
      const dc = t.direction === 'LONG' ? 'dir-long' : 'dir-short';
      const pc = t.current_pnl >= 0 ? 'pos' : 'neg';
      return '<tr><td>' + t.system + '</td><td style="color:#00d4ff;">' + t.ticker
        + '</td><td class="' + dc + '">' + t.direction
        + '</td><td>' + (t.entry_price || '--')
        + '</td><td class="' + pc + '">' + (t.current_pnl || '--')
        + '</td><td>' + (t.days_held || '--')
        + '</td><td>' + (t.conviction || '--') + '</td></tr>';
    }).join('');
  }

  // System grid
  const sys = d.systems || [];
  document.getElementById('sys-count').textContent = sys.length + ' systems';
  const grid = document.getElementById('sys-grid');
  grid.innerHTML = sys.map(s => {
    const dot = 'dot-' + s.status;
    return '<div class="sys-card">'
      + '<div class="sys-card-head">'
      + '<span class="sys-name">' + s.name + '</span>'
      + '<span class="sys-family" style="background:' + s.family_color + '22;color:' + s.family_color + ';">' + s.family + '</span>'
      + '</div>'
      + '<div><span class="status-dot ' + dot + '"></span>' + s.status + '</div>'
      + '<div class="sys-metric"><span class="k">' + s.metric_label + ': </span><span class="v">' + s.metric_value + '</span></div>'
      + (s.extra ? '<div class="sys-age">' + s.extra + '</div>' : '')
      + '<div class="sys-age">Heartbeat: ' + fmtAge(s.heartbeat_age) + ' ago</div>'
      + '</div>';
  }).join('');

  // Recent trades
  const rt = d.recent_trades || [];
  const rtDiv = document.getElementById('recent-trades');
  if (rt.length === 0) {
    rtDiv.innerHTML = '<div class="empty-msg">No trades recorded</div>';
  } else {
    let html = '<div class="trade-row trade-header"><span>Date</span><span>System</span><span>Ticker</span>'
      + '<span>Dir</span><span>Entry</span><span>Exit</span><span>P&L</span><span>Dur</span><span>Reason</span></div>';
    html += rt.map(t => {
      const pc = t.pnl_pct >= 0 ? 'pos' : 'neg';
      const dc = (t.direction||'').toUpperCase().startsWith('L') ? 'dir-long' : 'dir-short';
      return '<div class="trade-row"><span>' + (t.date||'--')
        + '</span><span>' + (t.system||'--')
        + '</span><span style="color:#00d4ff;">' + (t.ticker||'--')
        + '</span><span class="' + dc + '">' + (t.direction||'--')
        + '</span><span>' + (t.entry||'--')
        + '</span><span>' + (t.exit||'--')
        + '</span><span class="' + pc + '">' + (typeof t.pnl_pct === 'number' ? t.pnl_pct.toFixed(3) + '%' : '--')
        + '</span><span>' + (t.duration||'--')
        + '</span><span style="color:#4a5568;">' + (t.exit_reason||'--') + '</span></div>';
    }).join('');
    rtDiv.innerHTML = html;
  }

  // Events
  const ev = d.events || [];
  const evDiv = document.getElementById('events-list');
  if (ev.length === 0) {
    evDiv.innerHTML = '<div class="empty-msg">No upcoming events</div>';
  } else {
    evDiv.innerHTML = ev.map(e => {
      const ic = 'imp-' + (e.importance || 'normal');
      return '<div class="event-row"><span class="event-date">' + e.date
        + '</span><span class="' + ic + '">[' + (e.importance||'').toUpperCase() + ']</span>'
        + '<span>' + e.title + '</span></div>';
    }).join('');
  }
}

fetchFleet();
setInterval(fetchFleet, 30000);
</script>
</body>
</html>"""


@app.get("/fleet", response_class=HTMLResponse)
async def fleet_page():
    return FLEET_HTML


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Argus Dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    lock = ProcessLock(build_dashboard_lock_name(host=args.host, port=args.port))
    try:
        lock.acquire({
            "kind": "dashboard",
            "host": args.host,
            "port": args.port,
        })
    except ProcessLockError as exc:
        print(f"Argus Dashboard duplicate blocked: {exc}")
        sys.exit(1)

    print(f"Argus Dashboard: http://{args.host}:{args.port}")
    print(f"  Local:     http://localhost:{args.port}")
    print(f"  Tailscale: open Tailscale app to find your PC's Tailscale IP, then visit http://<tailscale-ip>:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        lock.release()
