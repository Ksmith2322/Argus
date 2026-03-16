#!/usr/bin/env python3
"""Argus Live Dashboard — real-time monitoring for paper/live trading.

Usage:
    python ops/dashboard.py              # default localhost:8080
    python ops/dashboard.py --port 9090  # custom port

Reads live runner artifacts (state, account, fills, signals, events) and
serves a single-page dashboard with auto-refreshing panels.
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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


COINS = ["ETH", "BTC", "SOL"]


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
    """Read last N account rows."""
    path = get_coin_log_dir(coin) / "account.csv"
    if not path.exists():
        return []
    try:
        rows = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        log.warning("Failed to read account: %s", path, exc_info=True)
        return []


def read_fills_tail(n: int = 20, coin: str = "ETH") -> list:
    """Read last N fills."""
    path = get_coin_log_dir(coin) / "fills.csv"
    if not path.exists():
        return []
    try:
        rows = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        log.warning("Failed to read fills: %s", path, exc_info=True)
        return []


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


def read_signals_tail(n: int = 5, coin: str = "ETH") -> list:
    """Read last N live signal rows."""
    # Signals may be in per-coin dir even when account.csv is in main dir (ETH case)
    coin_dir = OPS_LOGS / coin.lower()
    per_coin_path = coin_dir / "live_signals.csv"
    fallback_path = get_coin_log_dir(coin) / "live_signals.csv"
    path = per_coin_path if per_coin_path.exists() else fallback_path
    if not path.exists():
        return []
    try:
        rows = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        log.warning("Failed to read signals: %s", path, exc_info=True)
        return []


def read_events_tail(n: int = 20, coin: str = "ETH") -> list:
    """Read last N live event rows."""
    coin_dir = OPS_LOGS / coin.lower()
    per_coin_path = coin_dir / "live_events.csv"
    fallback_path = get_coin_log_dir(coin) / "live_events.csv"
    path = per_coin_path if per_coin_path.exists() else fallback_path
    if not path.exists():
        return []
    try:
        rows = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-n:]
    except Exception:
        log.warning("Failed to read events: %s", path, exc_info=True)
        return []


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
            with open(path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    event = row.get("event", "")
                    if any(k in event for k in ["BUY", "MISSED_BUY", "ENTRY", "GOVERNOR"]):
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
            with open(sig_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
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
        with open(sig_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                last = row
        prob = last.get("governor_win_prob", "")
        if prob:
            return {
                "win_prob": float(prob),
                "recommendation": last.get("governor_recommendation", ""),
                "score_modifier": last.get("governor_score_modifier", ""),
                "confluence_score": last.get("confluence_score", ""),
                "regime": last.get("regime", ""),
                "session": last.get("session", ""),
            }
        return {}
    except Exception:
        log.warning("Failed to read governor latest: %s", sig_path, exc_info=True)
        return {}


def read_queue_status() -> dict:
    """Read queue files and detect running/pending/completed jobs."""
    result = {"pending_pc1": 0, "pending_pc2": 0, "pending_labels": [],
              "running_job": None, "completed_today": 0}
    # Pending PC1
    q1 = REPO / "ops" / "backtest_queue.jsonl"
    if q1.exists():
        try:
            lines = [l.strip() for l in open(q1).readlines() if l.strip()]
            result["pending_pc1"] = len(lines)
            for l in lines:
                try:
                    result["pending_labels"].append(json.loads(l).get("label", "?"))
                except Exception:
                    pass
        except Exception:
            pass
    # Pending PC2
    q2 = REPO / "ops" / "backtest_queue_pc2.jsonl"
    if q2.exists():
        try:
            lines = [l.strip() for l in open(q2).readlines() if l.strip()]
            result["pending_pc2"] = len(lines)
        except Exception:
            pass
    # Running job: most recent run_header with no matching bt_summary
    try:
        headers = sorted(OPS_LOGS.glob("run_header_bt_*.json"), key=os.path.getmtime, reverse=True)
        for h in headers[:5]:
            hdr = json.load(open(h))
            rid = hdr.get("run_id", "")
            if not (OPS_LOGS / f"bt_summary_{rid}.json").exists():
                progress = 0
                total_bars = 0
                eq_path = OPS_LOGS / f"equity_{rid}.csv"
                if eq_path.exists():
                    with open(eq_path) as f:
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
        "queue": read_queue_status(),
        "governor": read_governor_latest(coin),
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
    # Add recent completed summaries (last 15)
    recent = []
    try:
        sums = sorted(OPS_LOGS.glob("bt_summary_bt_*.json"), key=os.path.getmtime, reverse=True)[:15]
        for sp in sums:
            d = json.load(open(sp))
            rid = d.get("run_id", "")
            # Try to get label from run_header
            lbl = ""
            hdr_path = OPS_LOGS / f"run_header_{rid}.json"
            if hdr_path.exists():
                try:
                    lbl = json.load(open(hdr_path)).get("label", "")
                except Exception:
                    pass
            recent.append({
                "run_id": rid, "label": lbl,
                "pf": float(d.get("profit_factor", 0)),
                "wr": float(d.get("win_rate_pct", 0)),
                "pnl": str(d.get("pnl_usd", "0")),
                "trades": int(d.get("trades_closed", 0)),
                "ts": datetime.fromtimestamp(os.path.getmtime(sp), tz=timezone.utc).isoformat(),
            })
    except Exception:
        pass
    base["recent"] = recent
    return JSONResponse(base)


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


@app.get("/api/stream")
async def stream(request: Request):
    coin = _coin_param(request)
    return EventSourceResponse(status_stream(request, coin))


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
    start_cash = 500.0
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
        pnl = float(s.get("total_pnl_usd", 0) or 0)
        trades = int(s.get("total_closed", s.get("trades_closed", 0)) or 0)
        # Try to get backtest duration in days
        dataset_bars = int(s.get("dataset_bars", s.get("bars_processed", 0)) or 0)
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

    # Sort by PF to get best, median, worst
    runs_with_pnl.sort(key=lambda r: r["pf"])

    now = datetime.now(timezone.utc)
    days_left_in_year = max(1, (datetime(now.year, 12, 31, tzinfo=timezone.utc) - now).days)

    scenarios = []
    for tag, run in [
        ("worst", runs_with_pnl[0]),
        ("median", runs_with_pnl[len(runs_with_pnl) // 2]),
        ("best", runs_with_pnl[-1]),
    ]:
        daily_pnl = run["pnl"] / run["days"]
        projected_gain = daily_pnl * days_left_in_year
        year_end_balance = start_cash + projected_gain
        pct_gain = (projected_gain / start_cash) * 100 if start_cash > 0 else 0

        scenarios.append({
            "tag": tag,
            "label": str(run["label"])[:30],
            "pf": round(run["pf"], 3),
            "daily_pnl": round(daily_pnl, 4),
            "bt_days": round(run["days"], 1),
            "projected_gain": round(projected_gain, 2),
            "year_end_balance": round(year_end_balance, 2),
            "pct_gain": round(pct_gain, 1),
            "days_left": days_left_in_year,
        })

    # Monthly projection curve for best scenario
    best = scenarios[-1]
    monthly_curve = []
    for m in range(13):  # 0=now through 12=Dec
        month_num = now.month + m
        if month_num > 12:
            month_num -= 12
        days_into_projection = m * 30.44
        balance = start_cash + best["daily_pnl"] * days_into_projection
        monthly_curve.append({
            "month": month_num,
            "balance": round(balance, 2),
        })

    return {
        "start_cash": start_cash,
        "scenarios": scenarios,
        "monthly_curve": monthly_curve,
    }


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


def _build_ml_network_data() -> dict:
    """Extract ML governor network structure + feature importances for visualization."""
    model_path = REPO / "data" / "ml_governor.pkl"
    if not model_path.exists():
        return {}
    try:
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
        return {
            "features": features,
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "roc_auc": stats.get("roc_auc", 0),
            "win_rate": round(stats.get("win_rate", 0) * 100, 1),
            "train_samples": stats.get("n_trades", stats.get("train_samples", 0)),
        }
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
                    label = json.load(open(hdr_path)).get("label", "")
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
  .page-nav { display: flex; gap: 0; margin-bottom: 14px; border-bottom: 2px solid #1e2a42; }
  .page-nav-btn { padding: 10px 24px; border: none; border-bottom: 2px solid transparent; background: transparent; color: #7b8ab8; cursor: pointer; font-family: inherit; font-size: 0.95em; font-weight: bold; letter-spacing: 1px; transition: all 0.2s; margin-bottom: -2px; }
  .page-nav-btn:hover { color: #00d4ff; }
  .page-nav-btn.active { color: #00d4ff; border-bottom-color: #00d4ff; }
  .page-content { display: none; }
  .page-content.active { display: block; }
  #evo-page .evo-stats { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin: 12px 0; }
  #evo-page .evo-stat { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 8px 16px; text-align: center; }
  #evo-page .evo-stat .val { font-size: 1.3em; font-weight: bold; }
  #evo-page .evo-stat .lbl { font-size: 0.7em; color: #7b8ab8; }
  #evo-page .evo-chart-wrap { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 8px; margin-bottom: 10px; position: relative; }
  #evo-page canvas { width: 100%; height: 180px; display: block; }
  #evo-page .evo-legend { display: flex; gap: 12px; justify-content: center; font-size: 0.7em; padding: 4px; flex-wrap: wrap; }
  #evo-page .evo-legend .edot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; vertical-align: middle; }
  #evo-page .evo-tip { position: absolute; background: #1a1a2e; border: 1px solid #00d4ff; border-radius: 4px; padding: 6px 10px; font-size: 0.72em; pointer-events: none; display: none; z-index: 100; max-width: 320px; line-height: 1.4; }
  @media (max-width: 768px) {
    #evo-page canvas { height: 140px !important; }
    #evo-page .evo-chart-wrap { padding: 6px; margin-bottom: 8px; }
    #evo-page h2 { font-size: 0.75em !important; margin: 4px 0 2px !important; }
    #ml-network-canvas { height: 250px !important; }
    #evo-page .evo-stats { gap: 6px; }
    #evo-page .evo-stat { padding: 4px 8px; }
    #evo-page .evo-stat .val { font-size: 1em; }
    #projection-section { display: none; }
  }
  .coin-tabs { display: flex; gap: 4px; margin-bottom: 10px; }
  .coin-tab { padding: 6px 16px; border-radius: 4px; border: 1px solid #1e2a42; background: #141b2d; color: #7b8ab8; cursor: pointer; font-family: inherit; font-size: 0.85em; font-weight: bold; transition: all 0.2s; }
  .coin-tab:hover { border-color: #00d4ff; color: #00d4ff; }
  .coin-tab.active { background: #1e2a42; color: #00d4ff; border-color: #00d4ff; }
  .multi-overview { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }
  .coin-summary { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 12px; cursor: pointer; transition: border-color 0.2s; }
  .coin-summary:hover { border-color: #00d4ff; }
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
</style>
</head>
<body>
<div style="display:flex; justify-content:space-between; align-items:center;">
  <h1>ARGUS DASHBOARD</h1>
  <span id="connection-status" class="disconnected">CONNECTING...</span>
</div>

<div class="page-nav">
  <button class="page-nav-btn active" onclick="switchPage('live')">LIVE</button>
  <button class="page-nav-btn" onclick="switchPage('evolution')">BACKTEST EVOLUTION</button>
  <button class="page-nav-btn" onclick="switchPage('queue')">QUEUE STATUS</button>
</div>

<div id="live-page" class="page-content active">
<div class="coin-tabs">
  <button class="coin-tab active" data-coin="ETH" onclick="switchCoin('ETH')">ETH</button>
  <button class="coin-tab" data-coin="BTC" onclick="switchCoin('BTC')">BTC</button>
  <button class="coin-tab" data-coin="SOL" onclick="switchCoin('SOL')">SOL</button>
</div>

<div class="multi-overview" id="multi-overview">
  <div class="coin-summary" onclick="switchCoin('ETH')" id="summary-ETH">
    <div class="coin-name">ETH-USD</div>
    <div class="coin-state">State: <span id="ms-state-ETH" class="state-FLAT">—</span></div>
    <div class="coin-pnl" id="ms-pnl-ETH">$0.00</div>
    <div class="coin-detail">Equity: <span id="ms-eq-ETH">—</span> | Qty: <span id="ms-qty-ETH">0</span></div>
    <div class="coin-detail">Updated: <span id="ms-ts-ETH">—</span></div>
  </div>
  <div class="coin-summary" onclick="switchCoin('BTC')" id="summary-BTC">
    <div class="coin-name">BTC-USD</div>
    <div class="coin-state">State: <span id="ms-state-BTC" class="state-FLAT">—</span></div>
    <div class="coin-pnl" id="ms-pnl-BTC">$0.00</div>
    <div class="coin-detail">Equity: <span id="ms-eq-BTC">—</span> | Qty: <span id="ms-qty-BTC">0</span></div>
    <div class="coin-detail">Updated: <span id="ms-ts-BTC">—</span></div>
  </div>
  <div class="coin-summary" onclick="switchCoin('SOL')" id="summary-SOL">
    <div class="coin-name">SOL-USD</div>
    <div class="coin-state">State: <span id="ms-state-SOL" class="state-FLAT">—</span></div>
    <div class="coin-pnl" id="ms-pnl-SOL">$0.00</div>
    <div class="coin-detail">Equity: <span id="ms-eq-SOL">—</span> | Qty: <span id="ms-qty-SOL">0</span></div>
    <div class="coin-detail">Updated: <span id="ms-ts-SOL">—</span></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Position</h2>
    <div id="bot-state" class="metric state-FLAT">FLAT</div>
    <div class="label">Symbol: <span id="symbol">ETH-USD</span></div>
    <div class="label">Qty: <span id="position-qty">0</span></div>
  </div>
  <div class="card">
    <h2>Equity</h2>
    <div id="equity" class="metric">$0.00</div>
    <div class="label">Cash: $<span id="cash">0</span></div>
    <div class="label">Unrealized: $<span id="unrealized">0</span></div>
  </div>
  <div class="card">
    <h2>Realized P&L</h2>
    <div id="realized-pnl" class="metric">$0.00</div>
    <div class="label">Total fills: <span id="total-fills">0</span></div>
    <div class="label">Journal entries: <span id="total-journal">0</span></div>
  </div>
  <div class="card">
    <h2>Runtime</h2>
    <div id="uptime" class="metric" style="font-size:1.2em">0h</div>
    <div class="label">Mode: <span id="runtime-mode">FULL</span></div>
    <div class="label">Status: <span id="manifest-status">UNKNOWN</span></div>
    <div class="label">Config: <span id="config-hash">—</span></div>
  </div>
  <div class="card">
    <h2>Queue Status</h2>
    <div id="queue-running" class="metric" style="font-size:1.0em; color:#00d4ff;">Idle</div>
    <div id="queue-progress" style="margin:6px 0;">
      <div style="background:#1e2a42; border-radius:3px; height:14px; overflow:hidden;">
        <div id="queue-progress-bar" style="background:#00d4ff; height:100%; width:0%; transition:width 0.5s;"></div>
      </div>
      <div class="label" id="queue-progress-text" style="margin-top:2px;">—</div>
    </div>
    <div class="label">Pending PC1: <span id="queue-pending-pc1">0</span> | PC2: <span id="queue-pending-pc2">0</span></div>
    <div class="label">Completed today: <span id="queue-completed">0</span></div>
    <div id="queue-labels" class="label" style="margin-top:4px;"></div>
  </div>
  <div class="card">
    <h2>ML Governor</h2>
    <div id="gov-status" class="metric" style="font-size:1.0em; color:#7b8ab8;">DISABLED</div>
    <div class="gov-gauge">
      <div class="gov-bar-bg">
        <div id="gov-bar" class="gov-bar-fill" style="width:0%; background:#7b8ab8;"></div>
        <span id="gov-bar-pct" class="gov-bar-label">—</span>
      </div>
    </div>
    <div class="label" style="margin-top:4px;">Recommendation: <span id="gov-rec" style="font-weight:bold;">—</span></div>
    <div class="label">Score mod: <span id="gov-mod">0</span> | Confluence: <span id="gov-conf">—</span></div>
    <div class="label">Regime: <span id="gov-regime">—</span> | Session: <span id="gov-session">—</span></div>
  </div>
</div>

<div class="chart-container">
  <h2 style="color:#7b8ab8; font-size:0.85em; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Equity Curve</h2>
  <canvas id="equity-chart" height="200"></canvas>
</div>

<div class="card" style="margin-bottom:12px;">
  <h2>Decision Flow</h2>
  <div id="decision-flow" style="max-height:250px; overflow-y:auto; padding:4px;">
    <div style="color:#7b8ab8; font-size:0.8em;">Waiting for entry signals...</div>
  </div>
</div>

<div class="grid" style="grid-template-columns: 1fr 1fr;">
  <div class="card">
    <h2>Recent Fills</h2>
    <table id="fills-table">
      <thead><tr><th>Time</th><th>Side</th><th>Qty</th><th>Price</th><th>Symbol</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Recent Events</h2>
    <table id="events-table">
      <thead><tr><th>Time</th><th>Event</th><th>Action</th><th>Score</th><th>Regime</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-top:10px;">
  <h2>Live Config (.env)</h2>
  <div id="config-panel" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:4px; font-size:0.78em;"></div>
</div>

<div class="card" style="margin-top:10px;">
  <h2>Backtest Runs</h2>
  <table id="bt-table">
    <thead><tr><th>Run ID</th><th>Status</th><th>Progress</th><th>Trades</th><th>WR%</th><th>PF</th><th>PnL</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="card" style="margin-top:10px;">
  <h2>Trade Journal</h2>
  <div id="journal-filters" style="display:flex; gap:8px; margin-bottom:8px; flex-wrap:wrap; font-size:0.78em;">
    <select id="jf-result" style="background:#1e2a42; color:#e0e0e0; border:1px solid #2a3a5c; border-radius:3px; padding:2px 6px;">
      <option value="all">All Trades</option>
      <option value="win">Wins Only</option>
      <option value="loss">Losses Only</option>
    </select>
    <select id="jf-exit" style="background:#1e2a42; color:#e0e0e0; border:1px solid #2a3a5c; border-radius:3px; padding:2px 6px;">
      <option value="all">All Exits</option>
    </select>
    <select id="jf-regime" style="background:#1e2a42; color:#e0e0e0; border:1px solid #2a3a5c; border-radius:3px; padding:2px 6px;">
      <option value="all">All Regimes</option>
    </select>
    <span id="jf-stats" style="color:#7b8ab8; margin-left:auto; line-height:24px;"></span>
  </div>
  <div style="max-height:400px; overflow-y:auto;">
  <table id="journal-table">
    <thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Qty</th><th>Entry Px</th><th>Exit Px</th><th>PnL</th><th>Duration</th><th>Exit Reason</th><th>Regime</th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
</div>

<div class="card" style="margin-top:10px;">
  <h2>Backtest Leaderboard</h2>
  <div style="display:flex; gap:8px; margin-bottom:8px; font-size:0.78em;">
    <span id="lb-count" style="color:#7b8ab8;">Loading...</span>
    <button onclick="loadLeaderboard()" style="margin-left:auto; background:#1e2a42; color:#7b8ab8; border:1px solid #2a3a5c; border-radius:3px; padding:2px 10px; cursor:pointer; font-size:0.9em;">Refresh</button>
  </div>
  <div style="max-height:350px; overflow-y:auto;">
  <table id="leaderboard-table">
    <thead><tr>
      <th>#</th><th>Label</th><th>Trades</th><th>WR%</th><th>PF</th>
      <th>PnL</th><th>Exp</th><th>DD%</th><th>Avg W</th><th>Avg L</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  </div>
</div>

<div class="footer">
  Last update: <span id="last-update">—</span> | Run: <span id="run-id">—</span> | Saved: <span id="saved-at">—</span>
</div>
</div><!-- end live-page -->

<div id="evo-page" class="page-content">
  <div class="evo-stats" id="evo-stats-bar"></div>

  <div id="projection-section" style="margin-bottom:16px;">
    <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">YEAR-END PROJECTION</h2>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
      <div id="projection-cards" style="display:flex; flex-direction:column; gap:8px;"></div>
      <div class="evo-chart-wrap" style="margin-bottom:0;">
        <canvas id="projection-chart" style="height:160px;"></canvas>
      </div>
    </div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">PROFIT FACTOR & WIN RATE OVER TIME</h2>
  <div class="evo-chart-wrap">
    <canvas id="evo-pf-wr"></canvas>
    <div class="evo-tip" id="evo-tip-pf"></div>
    <div class="evo-legend">
      <span><span class="edot" style="background:#00d4ff"></span> Profit Factor</span>
      <span><span class="edot" style="background:#00e676"></span> Win Rate %</span>
      <span style="color:#ffb74d">--- PF=1.0 breakeven</span>
    </div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">PNL & EXPECTANCY OVER TIME</h2>
  <div class="evo-chart-wrap">
    <canvas id="evo-pnl"></canvas>
    <div class="evo-tip" id="evo-tip-pnl"></div>
    <div class="evo-legend">
      <span><span class="edot" style="background:#ffb74d"></span> Total PnL ($)</span>
      <span><span class="edot" style="background:#ba68c8"></span> Expectancy ($/trade)</span>
    </div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">STRATEGY COMPARISON</h2>
  <div class="evo-chart-wrap">
    <canvas id="strategy-compare-chart" style="height:180px;"></canvas>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">TIME-OF-DAY PERFORMANCE HEATMAP</h2>
  <div class="evo-chart-wrap" style="overflow-x:auto;">
    <canvas id="heatmap-canvas" style="width:100%; height:160px;"></canvas>
    <div class="evo-legend">
      <span><span class="edot" style="background:#00e676"></span> Profitable hours</span>
      <span><span class="edot" style="background:#ff5252"></span> Losing hours</span>
      <span style="color:#7b8ab8">Times shown in CT (Austin)</span>
    </div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">WIN/LOSS PNL DISTRIBUTION</h2>
  <div class="evo-chart-wrap">
    <canvas id="dist-chart" style="height:150px;"></canvas>
    <div id="dist-stats" style="text-align:center; font-size:0.75em; color:#7b8ab8; margin-top:4px;"></div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">DRAWDOWN CURVE (Latest Run)</h2>
  <div class="evo-chart-wrap">
    <canvas id="drawdown-chart" style="height:120px;"></canvas>
    <div class="evo-legend">
      <span><span class="edot" style="background:#ff5252"></span> Drawdown %</span>
      <span style="color:#7b8ab8">Lower is better — 0% = at equity peak</span>
    </div>
  </div>

  <h2 style="color:#7b8ab8; font-size:0.85em; letter-spacing:1px; padding:0 0 4px;">ML GOVERNOR NEURAL NETWORK</h2>
  <div class="evo-chart-wrap" style="min-height:280px; position:relative;">
    <canvas id="ml-network-canvas" style="width:100%; height:280px;"></canvas>
    <div id="ml-network-stats" style="position:absolute; top:8px; right:12px; font-size:0.7em; color:#7b8ab8; text-align:right;"></div>
    <div class="evo-legend">
      <span><span class="edot" style="background:#00d4ff"></span> Input Features</span>
      <span><span class="edot" style="background:#ffc107"></span> Hidden Layer</span>
      <span><span class="edot" style="background:#00e676"></span> TRADE</span>
      <span><span class="edot" style="background:#ff5252"></span> BLOCK</span>
      <span style="color:#7b8ab8">Line thickness = feature importance</span>
    </div>
  </div>
</div><!-- end evo-page -->

<div id="queue-page" class="page-content">
  <h2 style="color:#00d4ff; margin-bottom:12px;">QUEUE & BACKTEST STATUS</h2>

  <!-- Running job -->
  <div class="card" style="margin-bottom:12px; border-left:3px solid #ffc107;">
    <h2>CURRENTLY RUNNING</h2>
    <div id="q-running" style="color:#7b8ab8;">Loading...</div>
  </div>

  <!-- Progress bar area -->
  <div id="q-progress-wrap" style="display:none; margin-bottom:12px;">
    <div style="background:#1e2a42; border-radius:4px; height:22px; overflow:hidden; position:relative;">
      <div id="q-progress-bar" style="background:linear-gradient(90deg,#00d4ff,#00e676); height:100%; transition:width 0.5s;"></div>
      <div id="q-progress-label" style="position:absolute; top:0; left:0; width:100%; text-align:center; line-height:22px; font-size:0.75em; color:#fff; font-weight:bold;"></div>
    </div>
  </div>

  <!-- Pending queue -->
  <div class="card" style="margin-bottom:12px; border-left:3px solid #00d4ff;">
    <h2>PENDING QUEUE (<span id="q-pending-count">0</span> jobs)</h2>
    <div id="q-pending" style="color:#7b8ab8;">None</div>
  </div>

  <div class="grid" style="grid-template-columns: 1fr 1fr;">
    <!-- Recent results -->
    <div class="card" style="border-left:3px solid #00e676;">
      <h2>RECENT RESULTS (last 15)</h2>
      <div style="overflow-y:auto; max-height:400px;">
        <table id="q-results-table">
          <thead><tr style="color:#7b8ab8; font-size:0.75em;">
            <th style="text-align:left;">Label</th><th>Trades</th><th>PF</th><th>WR%</th><th>PnL</th>
          </tr></thead>
          <tbody id="q-results"></tbody>
        </table>
      </div>
    </div>

    <!-- Log / failures -->
    <div class="card" style="border-left:3px solid #ff5252;">
      <h2>QUEUE LOG (last 30 entries)</h2>
      <div style="overflow-y:auto; max-height:400px;">
        <div id="q-log" style="font-size:0.75em; font-family:monospace;"></div>
      </div>
    </div>
  </div>
</div><!-- end queue-page -->

<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
let equityChart = null;
let currentCoin = 'ETH';
let sseConnection = null;
let evoLoaded = false;

function switchPage(page) {
  document.querySelectorAll('.page-nav-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  if (page === 'evolution') {
    document.querySelectorAll('.page-nav-btn')[1].classList.add('active');
    document.getElementById('evo-page').classList.add('active');
    if (!evoLoaded) { loadEvolution(); }
  } else if (page === 'queue') {
    document.querySelectorAll('.page-nav-btn')[2].classList.add('active');
    document.getElementById('queue-page').classList.add('active');
    loadQueueStatus();
  } else {
    document.querySelectorAll('.page-nav-btn')[0].classList.add('active');
    document.getElementById('live-page').classList.add('active');
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
      ['Best PF', bestPf.toFixed(2), bestPf >= 1 ? 'positive' : 'negative'],
      ['Latest WR', latest.wr.toFixed(1) + '%', latest.wr >= 35 ? 'positive' : 'negative'],
      ['Best WR', bestWr.toFixed(1) + '%', ''],
      ['Latest PnL', '$' + latest.pnl.toFixed(2), latest.pnl >= 0 ? 'positive' : 'negative'],
      ['Total Trades', EVO.reduce((s,d) => s + d.trades, 0), ''],
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

    // Pending queue
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

    // Recent results table
    const recent = data.recent || [];
    const tbody = document.getElementById('q-results');
    if (!recent.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:#7b8ab8; text-align:center;">No results</td></tr>';
    } else {
      tbody.innerHTML = recent.map(r => {
        const pfColor = r.pf >= 1.2 ? '#00e676' : r.pf >= 1.0 ? '#ffc107' : '#ff5252';
        const wrColor = r.wr >= 40 ? '#00e676' : r.wr >= 30 ? '#ffc107' : '#ff5252';
        const pnlVal = parseFloat(r.pnl) || 0;
        const pnlColor = pnlVal >= 0 ? '#00e676' : '#ff5252';
        return '<tr style="border-bottom:1px solid #1e2a42;">' +
          '<td style="text-align:left; padding:4px 2px; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="' + r.run_id + '">' +
            (r.label || r.run_id.slice(-12)) + '</td>' +
          '<td style="text-align:center; padding:4px;">' + r.trades + '</td>' +
          '<td style="text-align:center; padding:4px; color:' + pfColor + '; font-weight:bold;">' + r.pf.toFixed(2) + '</td>' +
          '<td style="text-align:center; padding:4px; color:' + wrColor + ';">' + r.wr.toFixed(1) + '%</td>' +
          '<td style="text-align:center; padding:4px; color:' + pnlColor + ';">$' + pnlVal.toFixed(2) + '</td>' +
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
        }
      }, 30000);
    }
  }).catch(e => {
    console.error('Queue load error', e);
    document.getElementById('q-running').innerHTML = '<span style="color:#ff5252;">Load Error</span>';
  });
}

function renderProjection(proj) {
  const cards = document.getElementById('projection-cards');
  if (!proj.scenarios || !proj.scenarios.length) {
    cards.innerHTML = '<div class="evo-stat" style="width:100%"><div class="val" style="color:#7b8ab8">No projection data</div></div>';
    return;
  }

  const startCash = proj.start_cash || 500;
  const tagColors = {worst:'#ff5252', median:'#ffb74d', best:'#00e676'};
  const tagLabels = {worst:'Conservative', median:'Median', best:'Optimistic'};

  // Also compute live-adjusted projection if we have live PnL
  let liveNote = '';
  const realizedEl = document.getElementById('realized-pnl');
  if (realizedEl) {
    const livePnl = parseFloat(realizedEl.textContent.replace(/[^0-9.-]/g, ''));
    if (!isNaN(livePnl) && livePnl !== 0) {
      liveNote = '<div style="margin-top:8px; padding:8px; background:#0d1321; border:1px solid #1e2a42; border-radius:4px; font-size:0.78em;">' +
        '<span style="color:#00d4ff;">LIVE P&L:</span> <span style="color:' + (livePnl >= 0 ? '#00e676' : '#ff5252') + '; font-weight:bold;">$' + livePnl.toFixed(2) + '</span>' +
        ' — Current balance: <b>$' + (startCash + livePnl).toFixed(2) + '</b></div>';
    }
  }

  let html = '';
  proj.scenarios.forEach(sc => {
    const color = tagColors[sc.tag] || '#7b8ab8';
    const label = tagLabels[sc.tag] || sc.tag;
    const arrow = sc.pct_gain >= 0 ? '&#9650;' : '&#9660;';
    html += '<div style="background:#141b2d; border:1px solid #1e2a42; border-left:3px solid '+color+'; border-radius:6px; padding:10px 14px;">' +
      '<div style="display:flex; justify-content:space-between; align-items:center;">' +
      '<div><span style="color:'+color+'; font-weight:bold; font-size:0.9em;">'+label+'</span>' +
      '<span style="color:#7b8ab8; font-size:0.7em; margin-left:8px;">PF='+sc.pf+' | '+sc.bt_days+'d backtest</span></div>' +
      '<div style="font-size:0.72em; color:#7b8ab8;">'+sc.days_left+' days left</div></div>' +
      '<div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:6px;">' +
      '<div style="font-size:1.5em; font-weight:bold; color:'+color+';">$'+sc.year_end_balance.toFixed(2)+'</div>' +
      '<div style="font-size:1.0em; color:'+color+';">'+arrow+' '+sc.pct_gain.toFixed(1)+'%</div></div>' +
      '<div style="font-size:0.7em; color:#7b8ab8; margin-top:2px;">$'+sc.daily_pnl.toFixed(4)+'/day &rarr; $'+sc.projected_gain.toFixed(2)+' gain from $'+startCash.toFixed(0)+'</div>' +
      '</div>';
  });
  html += liveNote;
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

  const startCash = proj.start_cash || 500;
  const currentBalance = liveEquity > 0 ? liveEquity : startCash + livePnl;
  const gainPct = ((currentBalance - startCash) / startCash * 100);
  const color = livePnl >= 0 ? '#00e676' : '#ff5252';
  const arrow = livePnl >= 0 ? '&#9650;' : '&#9660;';

  // Annualize from live performance
  // Use the elapsed days of paper trading (started ~2026-03-11)
  const startDate = new Date('2026-03-11T00:00:00Z');
  const now = new Date();
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
  statsEl.innerHTML = '<div style="color:#00d4ff; font-weight:bold;">ML GOVERNOR</div>' +
    '<div>' + aucLine + '</div>' +
    '<div>Trees: ' + mlData.n_estimators + ' | Depth: ' + mlData.max_depth + '</div>' +
    '<div>Trained on: ' + (mlData.train_samples || 0).toLocaleString() + ' trades</div>';

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  // Layout: features on left, 2 hidden layers in middle, output on right
  const features = mlData.features.slice(0, 16); // top 16 by importance
  const maxImp = Math.max(...features.map(f => f.importance));

  // Positions
  const leftX = 140, midX1 = W * 0.38, midX2 = W * 0.58, rightX = W - 60;
  const inputNodes = features.map((f, i) => ({
    x: leftX, y: 30 + i * ((H - 60) / (features.length - 1 || 1)),
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
      ctx.font = '10px monospace';
      ctx.textAlign = 'right';
      ctx.fillText(n.name, n.x - r - 6, n.y + 3);
      // Importance bar
      const barW = (n.imp / maxImp) * 30;
      ctx.fillStyle = 'rgba(0,212,255,0.2)';
      ctx.fillRect(n.x - r - 6 - barW, n.y - 2, barW, 4);
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

async function loadMultiOverview() {
  try {
    const resp = await fetch('/api/multi');
    const data = await resp.json();
    ['ETH', 'BTC', 'SOL'].forEach(coin => {
      const c = data[coin];
      if (!c) return;
      const stEl = document.getElementById('ms-state-' + coin);
      if (stEl) { stEl.textContent = c.bot_state || 'UNKNOWN'; stEl.className = 'state-' + (c.bot_state || 'FLAT'); }
      const pnlVal = parseFloat(c.realized_pnl) || 0;
      const pnlEl = document.getElementById('ms-pnl-' + coin);
      if (pnlEl) { pnlEl.textContent = '$' + pnlVal.toFixed(4); pnlEl.style.color = pnlVal > 0 ? '#00e676' : pnlVal < 0 ? '#ff5252' : '#e0e0e0'; }
      const eqEl = document.getElementById('ms-eq-' + coin);
      if (eqEl) eqEl.textContent = '$' + (parseFloat(c.equity) || 0).toFixed(2);
      const qtyEl = document.getElementById('ms-qty-' + coin);
      if (qtyEl) qtyEl.textContent = c.position_qty || '0';
      const tsEl = document.getElementById('ms-ts-' + coin);
      if (tsEl) tsEl.textContent = c.saved_at_iso ? new Date(c.saved_at_iso).toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '—';
    });
  } catch(e) { console.error('multi fetch error', e); }
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
  document.getElementById('saved-at').textContent = data.saved_at_iso || '—';
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
    const evtTime = e.ts ? new Date(e.ts+'Z').toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '';
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
  if (data.governor && data.governor.win_prob !== undefined) {
    const g = data.governor;
    const prob = g.win_prob;
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

  // Queue panel
  if (data.queue) {
    const q = data.queue;
    const runEl = document.getElementById('queue-running');
    const barEl = document.getElementById('queue-progress-bar');
    const textEl = document.getElementById('queue-progress-text');
    if (q.running_job) {
      const rj = q.running_job;
      runEl.textContent = rj.label || rj.run_id.slice(-16);
      runEl.style.color = '#00d4ff';
      barEl.style.width = rj.pct + '%';
      textEl.textContent = rj.total_bars ? (rj.pct + '% (' + rj.progress + '/' + rj.total_bars + ' bars)') : (rj.progress + ' bars');
    } else {
      runEl.textContent = 'Idle';
      runEl.style.color = '#7b8ab8';
      barEl.style.width = '0%';
      textEl.textContent = 'No job running';
    }
    document.getElementById('queue-pending-pc1').textContent = q.pending_pc1 || 0;
    document.getElementById('queue-pending-pc2').textContent = q.pending_pc2 || 0;
    document.getElementById('queue-completed').textContent = q.completed_today || 0;
    const labelsEl = document.getElementById('queue-labels');
    if (q.pending_labels && q.pending_labels.length > 0) {
      labelsEl.innerHTML = 'Next: ' + q.pending_labels.map(l => '<span class="badge badge-running" style="margin:1px;">' + l + '</span>').join(' ');
    } else {
      labelsEl.innerHTML = '';
    }
  }
}

async function loadEquity() {
  try {
    const resp = await fetch('/api/equity?coin=' + currentCoin);
    const data = await resp.json();
    if (equityChart && data.length > 0) {
      equityChart.data.datasets[0].data = data;
      equityChart.update('none');
    }
  } catch(e) { console.error('equity fetch error', e); }
}

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

      const ts = d.ts ? new Date(d.ts+'Z').toLocaleString('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',hour12:true}) : '??:??';
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
    const fmtJTs = (t) => t ? new Date(t+'Z').toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).replace(',','') : '';
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

// Init
initChart();
loadEquity();
loadConfig();
loadBacktests();
loadJournal();
loadDecisions();
loadMultiOverview();
loadLeaderboard();
setInterval(loadEquity, 30000);
setInterval(loadBacktests, 60000);
setInterval(loadJournal, 120000);
setInterval(loadDecisions, 15000);
setInterval(loadMultiOverview, 10000);
setInterval(loadLeaderboard, 120000);  // refresh leaderboard every 2 min
connectSSE();

// PWA Service Worker registration
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(reg => {
    console.log('SW registered, scope:', reg.scope);
  }).catch(err => console.warn('SW registration failed:', err));
}
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