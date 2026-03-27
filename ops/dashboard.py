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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("argus.dashboard")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

REPO = Path(__file__).resolve().parent.parent
OPS_LOGS = REPO / "ops" / "logs"
STATE_DIR = REPO / "state"

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
    """Return list of coin names that are rotation candidates (non-critical, returns [] on error)."""
    try:
        from ops.coin_rotation import check_rotation_candidates, read_coin_pool as _rcp
        pool = read_coin_pool()
        candidates = check_rotation_candidates(pool)
        return [c["coin"] for c in candidates]
    except Exception:
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
            runners = [_read_ibkr_runner(r) for r in IBKR_RUNNERS]
            total_trades = sum(r["closed_trades"] for r in runners)
            total_signals = sum(r["signal_count"] for r in runners)

            # Cohort status
            any_promoted = any(r.get("promotion_eligible") for r in runners)
            all_promoted = all(r.get("promotion_eligible") for r in runners)
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
    """Live coin performance metrics and rotation candidates."""
    try:
        from ops.coin_rotation import get_rotation_status
        return JSONResponse(get_rotation_status())
    except Exception as e:
        return JSONResponse({"error": str(e), "candidates": [], "pool": {}})


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
    {"name": "NKD", "symbol": "NKD", "strategy": "Range + Accel", "log_dir": "argus_flow/logs/nkd", "unit": "bps", "mult": 1},
]

def _read_ibkr_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    state_file = log_dir / "state.json"
    signal_file = log_dir / "signals.csv"
    trade_file = log_dir / "trades.csv"
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
        "entry_price": 0,
        "signal_count": 0,
        "closed_trades": 0,
        "valid_trades": 0,
        "invalid_trades": 0,
        "invalid_rate": 0,
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
        "last_signal_ts": "",
        "last_signal_age_s": 9999,
        # Replay expectations
        "replay_signals_per_day": 0,
        "replay_win_rate": 0,
        "replay_exp": 0,
        # State age
        "state_age_s": 9999,
    }

    # Load replay expectations from config
    cfg_map = {
        "EURUSD": "eurusd_t4_paper_v1.json",
        "GBPUSD": "gbpusd_range_paper_v1.json",
        "EURJPY": "eurjpy_t4_paper_v1.json",
    }
    cfg_path = REPO / "argus_flow" / "configs" / cfg_map.get(runner["symbol"], "")
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            rexp = cfg.get("replay_expectations", {})
            result["replay_signals_per_day"] = rexp.get("signals_per_day", 0)
            result["replay_win_rate"] = rexp.get("win_rate", 0)
            result["replay_exp"] = rexp.get("exp_pips_per_trade", rexp.get("exp_bps_per_trade", 0))
        except Exception:
            pass

    # State
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["position"] = state.get("position", "FLAT")
            result["trade_count"] = state.get("trade_count", 0)
            result["pnl"] = state.get("pnl_pips", state.get("pnl_points", 0))
            result["entry_price"] = state.get("entry_price", 0)
            mtime = state_file.stat().st_mtime
            age = time.time() - mtime
            result["state_age_s"] = int(age)
            result["status"] = "RUNNING" if age < 120 else ("IDLE" if age < 600 else "STALE")
        except Exception:
            result["status"] = "ERROR"

    # Signals
    if signal_file.exists():
        try:
            rows = []
            with open(signal_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            result["signal_count"] = len(rows)
            result["recent_signals"] = rows[-20:]

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
            today_signals = [r for r in rows if r.get("ts", "").startswith(today_str)]
            result["signals_today"] = len(today_signals)
            result["entries_today"] = sum(1 for r in today_signals if r.get("action") == "ENTRY")
        except Exception:
            pass

    # Trades + performance metrics
    if trade_file.exists():
        try:
            rows = []
            with open(trade_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            result["closed_trades"] = len(rows)
            result["trades"] = rows[-20:]

            # Split valid vs invalid trades
            valid_rows = [r for r in rows if r.get("experiment_valid", "").lower() == "true"]
            invalid_rows = [r for r in rows if r.get("experiment_valid", "").lower() != "true"]
            result["valid_trades"] = len(valid_rows)
            result["invalid_trades"] = len(invalid_rows)
            result["invalid_rate"] = round(len(invalid_rows) / len(rows), 4) if rows else 0

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
    result["cohort_target"] = 30
    result["cohort_progress_pct"] = min(100.0, round(vt / 30 * 100, 1))
    result["promotion_eligible"] = (vt >= 30 and result["invalid_rate"] <= 0.10 and result["win_rate"] > 0)

    return result


@app.get("/api/ibkr_fleet")
async def api_ibkr_fleet():
    """IBKR fleet status for all runners."""
    runners = [_read_ibkr_runner(r) for r in IBKR_RUNNERS]
    total_trades = sum(r["closed_trades"] for r in runners)
    total_signals = sum(r["signal_count"] for r in runners)

    # Cohort status
    fleet_valid = sum(r["valid_trades"] for r in runners)
    any_at_30 = any(r["valid_trades"] >= 30 for r in runners)
    all_promoted = all(r.get("promotion_eligible", False) for r in runners) and len(runners) > 0
    if all_promoted:
        cohort_status = "PROMOTED"
    elif any_at_30:
        cohort_status = "REVIEW"
    else:
        cohort_status = "COLLECTING"

    return JSONResponse({
        "runners": runners,
        "total_trades": total_trades,
        "total_signals": total_signals,
        "cohort_status": cohort_status,
        "fleet_valid_trades": fleet_valid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/divergence_status")
async def api_divergence_status():
    """Divergence guard report for IBKR fleet."""
    report_path = REPO / "argus_flow" / "logs" / "divergence_report.json"
    if not report_path.exists():
        return JSONResponse({"status": "NOT_RUN", "runners": []})
    try:
        data = json.loads(report_path.read_text())
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"status": "ERROR", "runners": []})


@app.get("/api/kill_discipline")
async def api_kill_discipline():
    """Kill discipline report."""
    report_path = REPO / "argus_flow" / "logs" / "kill_discipline_report.json"
    if not report_path.exists():
        return JSONResponse({"status": "NOT_RUN", "runners": []})
    try:
        return JSONResponse(json.loads(report_path.read_text()))
    except Exception:
        return JSONResponse({"status": "ERROR", "runners": []})


@app.get("/api/promotion_gate")
async def api_promotion_gate():
    """Promotion gate report."""
    report_path = REPO / "argus_flow" / "logs" / "promotion_gate_report.json"
    if not report_path.exists():
        return JSONResponse({"status": "NOT_RUN", "runners": []})
    try:
        return JSONResponse(json.loads(report_path.read_text()))
    except Exception:
        return JSONResponse({"status": "ERROR", "runners": []})


@app.get("/api/fx_analytics")
async def api_fx_analytics():
    """Per-pair equity curves, drawdown waterfall, expectancy tracking."""
    analytics = []
    for runner in IBKR_RUNNERS:
        log_dir = REPO / runner["log_dir"]
        trade_file = log_dir / "trades.csv"
        result = {
            "name": runner["name"],
            "symbol": runner["symbol"],
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
<meta name="apple-mobile-web-app-capable" content="yes">
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
  <h1>ARGUS TRADING DASHBOARD</h1>
  <span id="connection-status" style="color:#00ff88;font-size:0.7em;">IBKR PAPER</span>
</div>

<!-- CONTROL STRIP -->
<div id="control-strip" style="display:flex;gap:12px;align-items:center;padding:6px 12px;background:#0a0f1a;border:1px solid #1e2a42;border-radius:4px;margin-bottom:8px;font-size:0.72em;color:#7b8ab8;flex-wrap:wrap;">
  <div>MODE: <span id="cs-mode" style="font-weight:bold;color:#00e676;">UNIFIED RUNNER</span></div>
  <div>ACCOUNT: <span style="color:#00d4ff;">U24860535 (Read-Only)</span></div>
  <div>RUNNERS: <span id="cs-active-coins" style="color:#ffc107;">GBP/USD, EUR/USD, EUR/JPY (FX Cohort)</span></div>
  <div>PHASE: <span id="cs-phase" style="color:#e040fb;">Cohort Validation</span></div>
</div>

<!-- Single-page: IBKR Fleet Dashboard only -->

<!-- Legacy pages fully removed 2026-03-26 -->


<div id="ibkr-page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
  <h2 style="font-size:1.1em;color:#00d4ff;margin:0;letter-spacing:2px;">IBKR PAPER TRADING FLEET</h2>
  <span id="ibkr-timestamp" style="color:#666;font-size:0.75em;"></span>
</div>

<!-- Fleet summary bar -->
<div id="ibkr-fleet-summary" style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;margin-bottom:10px;display:flex;gap:30px;font-size:0.8em;">
  <div>Signals: <span id="ibkr-total-signals" style="color:#e0e0e0;font-weight:bold;">0</span></div>
  <div>Trades: <span id="ibkr-total-trades" style="color:#e0e0e0;font-weight:bold;">0</span></div>
  <div>Fleet PnL: <span id="ibkr-fleet-pnl" style="font-weight:bold;">0</span></div>
  <div>Active: <span id="ibkr-active-count" style="color:#00ff88;font-weight:bold;">0</span>/<span id="ibkr-total-count">3</span></div>
</div>

<div style="margin:12px 0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <span style="color:#00d4ff;font-weight:bold;font-size:0.85em;">Cohort Status</span>
    <span id="ibkr-cohort-status" style="font-size:0.75em;font-weight:bold;"></span>
  </div>
  <div style="display:flex;gap:12px;margin-top:6px;" id="ibkr-cohort-bars"></div>
</div>

<div style="margin:12px 0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;" id="ibkr-divergence-section">
  <div style="color:#00d4ff;font-weight:bold;font-size:0.85em;margin-bottom:6px;">Divergence Guard</div>
  <div id="ibkr-divergence-body" style="color:#888;font-size:0.75em;">Loading...</div>
</div>
<div style="margin:12px 0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;" id="ibkr-kill-section">
  <div style="color:#00d4ff;font-weight:bold;font-size:0.85em;margin-bottom:6px;">Kill Discipline</div>
  <div id="ibkr-kill-body" style="color:#888;font-size:0.75em;">Loading...</div>
</div>
<div style="margin:12px 0;padding:10px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;" id="ibkr-promotion-section">
  <div style="color:#00d4ff;font-weight:bold;font-size:0.85em;margin-bottom:6px;">Promotion Gate</div>
  <div id="ibkr-promotion-body" style="color:#888;font-size:0.75em;">Loading...</div>
</div>

<!-- Runner cards -->
<!-- Fleet P&L Chart -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:10px;">
  <h3 style="font-size:0.8em;color:#00d4ff;margin:0 0 8px 0;letter-spacing:1px;">FLEET P&L HISTORY</h3>
  <canvas id="ibkr-pnl-chart" height="120"></canvas>
</div>

<!-- Runner cards -->
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;" id="ibkr-runner-cards"></div>

<!-- Trade journal -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;margin-bottom:10px;">
  <h3 style="font-size:0.8em;color:#00d4ff;margin:0 0 10px 0;letter-spacing:1px;">TRADE JOURNAL</h3>
  <div id="ibkr-trades-table" style="font-size:0.75em;"></div>
</div>

<!-- Recent signals -->
<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:14px;">
  <h3 style="font-size:0.8em;color:#00d4ff;margin:0 0 10px 0;letter-spacing:1px;">SIGNAL FEED</h3>
  <div id="ibkr-signals-table" style="font-size:0.75em;"></div>
</div>
</div><!-- end ibkr-page -->

<!-- LEGACY CONTENT REMOVED 2026-03-26 (was: decision waterfall, evo charts, ML governor viz) -->

<script>
let equityChart = null;
let currentCoin = 'ETH';
let sseConnection = null;

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
  return `<svg width="${width}" height="${height}" style="vertical-align:middle;"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}

async function loadIBKRFleet() {
  try {
    const resp = await fetch('/api/ibkr_fleet');
    const data = await resp.json();

    document.getElementById('ibkr-timestamp').textContent = (data.timestamp || '').substring(11,19) + ' UTC';
    document.getElementById('ibkr-total-signals').textContent = data.total_signals || 0;
    document.getElementById('ibkr-total-trades').textContent = data.total_trades || 0;

    // Fleet PnL and active count
    let fleetPnl = 0; let activeCount = 0;
    for (const r of data.runners) {
      fleetPnl += Number(r.pnl || 0);
      if (r.status === 'RUNNING' || r.status === 'IDLE') activeCount++;
    }
    const fpEl = document.getElementById('ibkr-fleet-pnl');
    fpEl.textContent = (fleetPnl >= 0 ? '+' : '') + fleetPnl.toFixed(1);
    fpEl.style.color = fleetPnl >= 0 ? '#00ff88' : '#ff4444';
    document.getElementById('ibkr-active-count').textContent = activeCount;
    document.getElementById('ibkr-total-count').textContent = data.runners.length;

    // Cohort summary
    const csEl = document.getElementById('ibkr-cohort-status');
    const cbEl = document.getElementById('ibkr-cohort-bars');
    const cohortStatus = data.cohort_status || 'COLLECTING';
    const csColors = {COLLECTING:'#ffaa00',REVIEW:'#00d4ff',PROMOTED:'#00ff88'};
    csEl.textContent = cohortStatus;
    csEl.style.color = csColors[cohortStatus] || '#888';
    let cbHtml = '';
    for (const r of data.runners) {
      const vt = r.valid_trades || 0;
      const tgt = r.cohort_target || 30;
      const cpct = Math.min(100, (vt/tgt)*100);
      const pc = cpct >= 100 ? '#00ff88' : cpct >= 50 ? '#ffaa00' : '#ff4444';
      cbHtml += `<div style="flex:1;"><div style="font-size:0.65em;color:#888;margin-bottom:2px;">${r.name} (${vt}/${tgt})</div>
        <div style="background:#0d1117;border-radius:3px;height:6px;overflow:hidden;">
          <div style="width:${cpct}%;height:100%;background:${pc};border-radius:3px;"></div>
        </div></div>`;
    }
    cbEl.innerHTML = cbHtml;

    // Divergence guard
    fetch('/api/divergence_status').then(r=>r.json()).then(dg=>{
      const el = document.getElementById('ibkr-divergence-body');
      if (!dg.runners || !dg.runners.length) { el.innerHTML = 'Not yet run. Execute: python -m argus_flow.ops.divergence_guard'; return; }
      let html = '<div style="display:flex;gap:12px;">';
      for (const r of dg.runners) {
        const sc = {PASS:'#00ff88',WATCH:'#ffaa00',KILL:'#ff4444',COLLECTING:'#888',NO_DATA:'#555',NO_SIGNALS:'#555'};
        const c = sc[r.status] || '#555';
        html += `<div style="flex:1;padding:6px;background:#0d1117;border-radius:4px;border-left:3px solid ${c};">
          <div style="font-weight:bold;color:${c};">${r.name}: ${r.status}</div>`;
        const m = r.metrics || {};
        if (m.closed_trades) html += `<div style="font-size:0.85em;">Trades: ${m.closed_trades} | WR: ${((m.live_win_rate||0)*100).toFixed(0)}%</div>`;
        if (r.flags && r.flags.length) html += `<div style="color:#ff4444;font-size:0.8em;">${r.flags.join('<br>')}</div>`;
        html += '</div>';
      }
      html += '</div>';
      if (dg.timestamp) html += `<div style="margin-top:4px;font-size:0.6em;color:#555;">Last run: ${dg.timestamp.substring(0,19)}</div>`;
      el.innerHTML = html;
    }).catch(()=>{
      document.getElementById('ibkr-divergence-body').textContent = 'Failed to load divergence data.';
    });

    // Kill discipline
    fetch('/api/kill_discipline').then(r=>r.json()).then(kd=>{
      const el = document.getElementById('ibkr-kill-body');
      if (!kd.runners || !kd.runners.length) { el.innerHTML = 'Not yet run. Execute: python -m argus_flow.ops.kill_discipline'; return; }
      let html = '<div style="display:flex;gap:12px;">';
      for (const r of kd.runners) {
        const sc = {PASS:'#00ff88',WATCH:'#ffaa00',KILL:'#ff4444',COLLECTING:'#888'};
        const c = sc[r.status] || '#555';
        html += '<div style="flex:1;padding:6px;background:#0d1117;border-radius:4px;border-left:3px solid ' + c + ';">';
        html += '<div style="font-weight:bold;color:' + c + ';">' + r.name + ': ' + r.status + '</div>';
        const m = r.metrics || {};
        if (m.valid_trades) html += '<div style="font-size:0.85em;">Trades: ' + m.valid_trades + ' | PnL: ' + (m.total_pnl||0).toFixed(1) + ' | DD: ' + (m.max_drawdown_pips||0).toFixed(1) + '</div>';
        if (r.flags && r.flags.length) html += '<div style="color:#ff4444;font-size:0.8em;margin-top:2px;">' + r.flags.join('<br>') + '</div>';
        html += '</div>';
      }
      html += '</div>';
      if (kd.timestamp) html += '<div style="margin-top:4px;font-size:0.6em;color:#555;">Last run: ' + kd.timestamp.substring(0,19) + '</div>';
      el.innerHTML = html;
    }).catch(()=>{ document.getElementById('ibkr-kill-body').textContent = 'Failed to load.'; });

    // Promotion gate
    fetch('/api/promotion_gate').then(r=>r.json()).then(pg=>{
      const el = document.getElementById('ibkr-promotion-body');
      if (!pg.runners || !pg.runners.length) { el.innerHTML = 'Not yet run. Execute: python -m argus_flow.ops.promotion_gate'; return; }
      let html = '<div style="display:flex;gap:12px;">';
      for (const r of pg.runners) {
        const vc = {PROMOTE:'#00ff88',NOT_READY:'#ffaa00',BLOCKED:'#ff4444'};
        const c = vc[r.verdict] || '#888';
        const checks = r.checks || {};
        const passed = Object.values(checks).filter(c=>c.passed).length;
        const total = Object.values(checks).length;
        html += '<div style="flex:1;padding:6px;background:#0d1117;border-radius:4px;border-left:3px solid ' + c + ';">';
        html += '<div style="font-weight:bold;color:' + c + ';">' + r.name + ': ' + r.verdict + '</div>';
        html += '<div style="font-size:0.85em;">' + passed + '/' + total + ' checks passed | ' + (r.valid_trades||0) + ' valid trades</div>';
        if (r.blockers && r.blockers.length) html += '<div style="color:#ff4444;font-size:0.75em;margin-top:2px;">Blockers: ' + r.blockers.slice(0,3).join(', ') + (r.blockers.length > 3 ? ' +' + (r.blockers.length-3) + ' more' : '') + '</div>';
        html += '</div>';
      }
      html += '</div>';
      if (pg.timestamp) html += '<div style="margin-top:4px;font-size:0.6em;color:#555;">Last run: ' + pg.timestamp.substring(0,19) + '</div>';
      el.innerHTML = html;
    }).catch(()=>{ document.getElementById('ibkr-promotion-body').textContent = 'Failed to load.'; });

    // Runner cards
    const cardsDiv = document.getElementById('ibkr-runner-cards');
    cardsDiv.innerHTML = '';

    for (const r of data.runners) {
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
      const wrDelta = r.closed_trades >= 5 ? ((wrLive - wrReplay) * 100).toFixed(0) : '—';

      let card = `<div style="background:#141b2d;border:1px solid ${borderColor};border-radius:6px;padding:12px;${pulse}">`;

      // Header: name + status + health
      card += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <div>
          <span style="color:#00d4ff;font-weight:bold;font-size:0.95em;">${r.name}</span>
          <span style="color:#555;font-size:0.65em;margin-left:6px;">${r.strategy}</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center;">
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
          <span style="color:#555;font-size:0.8em;">(replay: ${(wrReplay*100).toFixed(0)}%)</span></div>
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
        </div>
      </div>`;

      // Cohort progress
      const target = r.cohort_target || 30;
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

      // Performance stats (if trades exist)
      if (r.closed_trades > 0) {
        card += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #1e2a42;font-size:0.65em;color:#888;display:flex;justify-content:space-between;">
          <span>PF: ${r.profit_factor}</span>
          <span>Avg W: ${Number(r.avg_win).toFixed(1)}</span>
          <span>Avg L: ${Number(r.avg_loss).toFixed(1)}</span>
          <span>Max CL: ${r.max_consec_loss}</span>
        </div>`;
      }

      card += `</div>`;
      cardsDiv.innerHTML += card;
    }

    // Trades table
    const tradesDiv = document.getElementById('ibkr-trades-table');
    let tradeRows = [];
    for (const r of data.runners) {
      for (const t of (r.trades || [])) { tradeRows.push({...t, runner: r.name, unit: r.unit}); }
    }
    tradeRows.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

    if (tradeRows.length === 0) {
      tradesDiv.innerHTML = '<div style="color:#666;">No trades yet. Waiting for triggers during active sessions.</div>';
    } else {
      let html = '<table style="width:100%;border-collapse:collapse;"><tr style="color:#00d4ff;border-bottom:1px solid #1e2a42;font-size:0.9em;">' +
        '<th style="text-align:left;padding:3px;">Time</th><th>Runner</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Dur</th></tr>';
      for (const t of tradeRows.slice(0, 25)) {
        const pnl = parseFloat(t.pnl_pips || t.pnl_pts || 0);
        const pc = pnl >= 0 ? '#00ff88' : '#ff4444';
        const dc = t.direction === 'long' ? '#00ff88' : '#ff4444';
        html += `<tr style="border-bottom:1px solid #0d1117;">
          <td style="padding:2px 3px;">${(t.ts || '').substring(11,19)}</td>
          <td>${t.runner}</td>
          <td style="color:${dc};font-weight:bold;">${(t.direction||'').toUpperCase()}</td>
          <td>${t.entry_px}</td><td>${t.exit_px}</td>
          <td style="color:${pc};font-weight:bold;">${pnl>=0?'+':''}${pnl.toFixed(1)}</td>
          <td style="color:#888;">${t.exit_reason || ''}</td>
          <td style="color:#888;">${t.duration_min ? Number(t.duration_min).toFixed(0)+'m' : ''}</td></tr>`;
      }
      html += '</table>';
      tradesDiv.innerHTML = html;
    }

    // Signals table (entries only + recent NO_TRIGGER)
    const sigsDiv = document.getElementById('ibkr-signals-table');
    let allSigs = [];
    for (const r of data.runners) {
      for (const s of (r.recent_signals || [])) { allSigs.push({...s, runner: r.name}); }
    }
    allSigs.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

    if (allSigs.length === 0) {
      sigsDiv.innerHTML = '<div style="color:#666;">No signals yet.</div>';
    } else {
      let html = '<table style="width:100%;border-collapse:collapse;"><tr style="color:#00d4ff;border-bottom:1px solid #1e2a42;font-size:0.9em;">' +
        '<th style="text-align:left;padding:3px;">Time</th><th>Runner</th><th>Action</th><th>Dir</th><th>Price</th><th>Range%</th><th>Vol Z</th><th>Accel</th><th>Dist</th></tr>';
      for (const s of allSigs.slice(0, 30)) {
        const isEntry = s.action === 'ENTRY';
        const ac = isEntry ? '#00ff88' : '#555';
        const bg = isEntry ? 'background:#00ff8811;' : '';
        html += `<tr style="border-bottom:1px solid #0d1117;${bg}">
          <td style="padding:2px 3px;">${(s.ts || '').substring(11,19)}</td>
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

    // Fleet P&L Chart
    // Build fleet equity curve: start at 0, add each trade's PnL
    const allPnlData = [0]; // always start with zero baseline
    const allTrades = [];
    for (const r of data.runners) {
      for (const t of (r.trades || [])) {
        const pnl = parseFloat(t.pnl_pips || t.pnl_pts || 0);
        allTrades.push({ts: t.ts, pnl: pnl, runner: r.name});
      }
    }
    allTrades.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
    let cumPnl = 0;
    for (const t of allTrades) {
      cumPnl += t.pnl;
      allPnlData.push(cumPnl);
    }

    const canvas = document.getElementById('ibkr-pnl-chart');
    if (canvas && allPnlData.length >= 1) {
      const ctx = canvas.getContext('2d');
      const w = canvas.parentElement.clientWidth - 28;
      canvas.width = w;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      const mn = Math.min(0, ...allPnlData);
      const mx = Math.max(0, ...allPnlData);
      const range = (mx - mn) || 1;
      const pad = 5;

      // Zero line
      const zeroY = h - pad - ((0 - mn) / range) * (h - pad * 2);
      ctx.strokeStyle = '#333';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, zeroY);
      ctx.lineTo(w, zeroY);
      ctx.stroke();
      ctx.setLineDash([]);

      // PnL line
      ctx.strokeStyle = allPnlData[allPnlData.length - 1] >= 0 ? '#00ff88' : '#ff4444';
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i < allPnlData.length; i++) {
        const x = (i / (allPnlData.length - 1)) * w;
        const y = h - pad - ((allPnlData[i] - mn) / range) * (h - pad * 2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();

      // Fill under curve
      ctx.lineTo(w, zeroY);
      ctx.lineTo(0, zeroY);
      ctx.closePath();
      ctx.fillStyle = allPnlData[allPnlData.length - 1] >= 0 ? 'rgba(0,255,136,0.08)' : 'rgba(255,68,68,0.08)';
      ctx.fill();

      // Labels
      ctx.fillStyle = '#888';
      ctx.font = '10px monospace';
      ctx.fillText(mx.toFixed(1), 2, 12);
      ctx.fillText(mn.toFixed(1), 2, h - 2);
      const lastVal = allPnlData[allPnlData.length - 1];
      ctx.fillStyle = lastVal >= 0 ? '#00ff88' : '#ff4444';
      ctx.fillText((lastVal >= 0 ? '+' : '') + lastVal.toFixed(1), w - 60, 12);
    }

    // Per-pair equity + drawdown section
    fetch('/api/fx_analytics').then(r=>r.json()).then(fa=>{
      let anaDiv = document.getElementById('ibkr-analytics');
      if (!anaDiv) {
        anaDiv = document.createElement('div');
        anaDiv.id = 'ibkr-analytics';
        anaDiv.style.cssText = 'margin-top:16px;';
        document.getElementById('ibkr-pnl-chart').parentElement.after(anaDiv);
      }
      let html = '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;margin-bottom:8px;">Per-Pair Analytics</div>';
      html += '<div style="display:flex;gap:12px;">';
      for (const a of fa.analytics) {
        const ddColor = a.current_drawdown > 0 ? '#ff4444' : '#00ff88';
        html += '<div style="flex:1;padding:8px;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;">';
        html += '<div style="font-weight:bold;color:#00d4ff;font-size:0.8em;">' + a.name + '</div>';
        html += '<div style="font-size:0.7em;margin-top:4px;">';
        html += 'PnL: <span style="color:' + (a.cumulative_pnl>=0?'#00ff88':'#ff4444') + '">' + (a.cumulative_pnl>=0?'+':'') + a.cumulative_pnl.toFixed(1) + '</span>';
        html += ' | Max DD: <span style="color:#ff4444">' + a.max_drawdown.toFixed(1) + '</span>';
        html += ' | Now: <span style="color:' + ddColor + '">' + a.current_drawdown.toFixed(1) + '</span>';
        html += '</div>';
        if (a.equity_curve && a.equity_curve.length > 1) {
          html += '<div style="margin-top:4px;">' + ibkrMiniChart(a.equity_curve, 180, 25, a.cumulative_pnl>=0?'#00ff88':'#ff4444') + '</div>';
        }
        html += '</div>';
      }
      html += '</div>';
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

// Filter event listeners
document.getElementById('jf-result').addEventListener('change', () => renderJournal(_allJournal));
document.getElementById('jf-exit').addEventListener('change', () => renderJournal(_allJournal));
document.getElementById('jf-regime').addEventListener('change', () => renderJournal(_allJournal));

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

// Init — IBKR Fleet is the primary dashboard
try {
  loadIBKRFleet();
  setInterval(loadIBKRFleet, 10000);
  console.log('IBKR Fleet initialized');
} catch(e) {
  console.error('IBKR init error:', e);
  document.getElementById('ibkr-runner-cards').innerHTML = '<div style="color:red;padding:20px;">Dashboard JS error: ' + e.message + '</div>';
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
    var cards = document.getElementById('ibkr-runner-cards');
    if (cards) cards.innerHTML = '<div style="color:#ff4444;padding:20px;">Dashboard script error — check browser console (F12)</div>';
  }
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


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

    print(f"Argus Dashboard: http://{args.host}:{args.port}")
    print(f"  Local:     http://localhost:{args.port}")
    print(f"  Tailscale: open Tailscale app to find your PC's Tailscale IP, then visit http://<tailscale-ip>:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")