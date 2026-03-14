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
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

REPO = Path(__file__).resolve().parent.parent
OPS_LOGS = REPO / "ops" / "logs"
STATE_DIR = REPO / "state"

app = FastAPI(title="Argus Dashboard")


# ---------------------------------------------------------------------------
# Data readers
# ---------------------------------------------------------------------------

def read_runtime_state() -> dict:
    """Read the latest runtime state snapshot."""
    path = STATE_DIR / "runtime_state_ETH_USD.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
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
        return {}


def read_account_tail(n: int = 5) -> list:
    """Read last N account rows."""
    path = OPS_LOGS / "account.csv"
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
        return []


def read_fills_tail(n: int = 20) -> list:
    """Read last N fills."""
    path = OPS_LOGS / "fills.csv"
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
        return []


def read_trade_journal(n: int = 20) -> list:
    """Read the latest trade journal entries."""
    journals = sorted(OPS_LOGS.glob("trade_journal_*.csv"), key=os.path.getmtime)
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
        return []


def read_signals_tail(n: int = 5) -> list:
    """Read last N live signal rows."""
    path = OPS_LOGS / "live_signals.csv"
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
        return []


def read_events_tail(n: int = 20) -> list:
    """Read last N live event rows."""
    path = OPS_LOGS / "live_events.csv"
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
        return []


def read_equity_series() -> list:
    """Read account.csv for equity time series."""
    path = OPS_LOGS / "account.csv"
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
        return []


def build_status() -> dict:
    """Aggregate all status info into a single dict."""
    state = read_runtime_state()
    manifest = read_run_manifest()
    mode = read_runtime_mode()
    account = read_account_tail(1)
    fills = read_fills_tail(20)
    journal = read_trade_journal(10)
    signals = read_signals_tail(3)
    events = read_events_tail(20)

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
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    return JSONResponse(build_status())


@app.get("/api/equity")
async def api_equity():
    return JSONResponse(read_equity_series())


@app.get("/api/events")
async def api_events():
    return JSONResponse(read_events_tail(50))


@app.get("/api/fills")
async def api_fills():
    return JSONResponse(read_fills_tail(50))


@app.get("/api/journal")
async def api_journal():
    return JSONResponse(read_trade_journal(50))


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
            if equity_path.exists():
                with open(equity_path) as f:
                    progress = sum(1 for _ in f) - 1  # subtract header
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
                "pnl": summary.get("pnl_usd", ""),
                "trades": summary.get("trades_closed", ""),
                "win_rate": summary.get("win_rate_pct", ""),
                "profit_factor": summary.get("profit_factor", ""),
            })
        except Exception:
            continue
    return JSONResponse(runs)


# ---------------------------------------------------------------------------
# SSE stream for real-time updates
# ---------------------------------------------------------------------------

async def status_stream(request: Request):
    """Push status updates every 5 seconds."""
    while True:
        if await request.is_disconnected():
            break
        data = json.dumps(build_status(), default=str)
        yield {"event": "status", "data": data}
        await asyncio.sleep(5)


@app.get("/api/stream")
async def stream(request: Request):
    return EventSourceResponse(status_stream(request))


# ---------------------------------------------------------------------------
# Dashboard HTML (single page, self-contained)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
  .chart-container { background: #141b2d; border: 1px solid #1e2a42; border-radius: 6px; padding: 12px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78em; }
  th { color: #7b8ab8; text-align: left; padding: 4px 6px; border-bottom: 1px solid #1e2a42; }
  td { padding: 4px 6px; border-bottom: 1px solid #0d1321; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.75em; font-weight: bold; }
  .badge-flat { background: #1e2a42; color: #7b8ab8; }
  .badge-open { background: #1b3a1b; color: #00e676; }
  .badge-running { background: #1b2a3a; color: #00d4ff; }
  .badge-error { background: #3a1b1b; color: #ff5252; }
  .footer { color: #3a4a6b; font-size: 0.7em; margin-top: 8px; text-align: center; }
  #connection-status { position: fixed; top: 8px; right: 12px; font-size: 0.75em; }
  .connected { color: #00e676; }
  .disconnected { color: #ff5252; }
</style>
</head>
<body>
<div style="display:flex; justify-content:space-between; align-items:center;">
  <h1>ARGUS DASHBOARD</h1>
  <span id="connection-status" class="disconnected">CONNECTING...</span>
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
</div>

<div class="chart-container">
  <h2 style="color:#7b8ab8; font-size:0.85em; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Equity Curve</h2>
  <canvas id="equity-chart" height="200"></canvas>
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
  <h2>Backtest Runs</h2>
  <table id="bt-table">
    <thead><tr><th>Run ID</th><th>Status</th><th>Progress</th><th>Trades</th><th>WR%</th><th>PF</th><th>PnL</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="card" style="margin-top:10px;">
  <h2>Trade Journal</h2>
  <table id="journal-table">
    <thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Qty</th><th>Entry Px</th><th>Exit Px</th><th>PnL</th><th>Duration</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="footer">
  Last update: <span id="last-update">—</span> | Run: <span id="run-id">—</span> | Saved: <span id="saved-at">—</span>
</div>

<script>
let equityChart = null;

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
  return d.toISOString().slice(11, 19) + 'Z';
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

  // Runtime
  document.getElementById('uptime').textContent = (data.uptime_hours || 0) + 'h';
  document.getElementById('runtime-mode').textContent = data.runtime_mode || 'FULL';
  document.getElementById('manifest-status').textContent = data.manifest_status || 'UNKNOWN';
  document.getElementById('config-hash').textContent = data.config_hash || '—';

  // Footer
  document.getElementById('run-id').textContent = data.run_id || '—';
  document.getElementById('saved-at').textContent = data.saved_at_iso || '—';
  document.getElementById('last-update').textContent = new Date().toISOString().slice(11, 19) + 'Z';

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
    tr.innerHTML = '<td>' + (e.ts || '').slice(11, 19) + '</td>'
      + '<td>' + (e.event || '') + '</td>'
      + '<td style="color:' + actionColor + '">' + action + '</td>'
      + '<td>' + (e.confluence_score || '') + '</td>'
      + '<td>' + (e.regime || '') + '</td>';
    eventsBody.appendChild(tr);
  });

  // Journal table
  const journalBody = document.querySelector('#journal-table tbody');
  journalBody.innerHTML = '';
  (data.journal || []).reverse().forEach(j => {
    const tr = document.createElement('tr');
    const pnlVal = parseFloat(j.realized_pnl || j.pnl || 0);
    const pnlColor = pnlVal > 0 ? '#00e676' : pnlVal < 0 ? '#ff5252' : '#e0e0e0';
    const dur = parseInt(j.duration_s || 0);
    const durStr = dur > 3600 ? (dur/3600).toFixed(1) + 'h' : dur > 60 ? Math.round(dur/60) + 'm' : dur + 's';
    tr.innerHTML = '<td>' + (j.entry_ts || j.entry_time || '').slice(11, 19) + '</td>'
      + '<td>' + (j.exit_ts || j.exit_time || '').slice(11, 19) + '</td>'
      + '<td>' + (j.side || 'LONG') + '</td>'
      + '<td>' + (j.qty || '') + '</td>'
      + '<td>$' + (parseFloat(j.entry_px || j.entry_price || 0)).toFixed(2) + '</td>'
      + '<td>$' + (parseFloat(j.exit_px || j.exit_price || 0)).toFixed(2) + '</td>'
      + '<td style="color:' + pnlColor + '">$' + pnlVal.toFixed(4) + '</td>'
      + '<td>' + durStr + '</td>';
    journalBody.appendChild(tr);
  });
}

async function loadEquity() {
  try {
    const resp = await fetch('/api/equity');
    const data = await resp.json();
    if (equityChart && data.length > 0) {
      equityChart.data.datasets[0].data = data;
      equityChart.update('none');
    }
  } catch(e) { console.error('equity fetch error', e); }
}

function connectSSE() {
  const status = document.getElementById('connection-status');
  const es = new EventSource('/api/stream');

  es.addEventListener('status', (e) => {
    try {
      const data = JSON.parse(e.data);
      updateDashboard(data);
    } catch(err) { console.error('parse error', err); }
  });

  es.onopen = () => { status.textContent = 'LIVE'; status.className = 'connected'; };
  es.onerror = () => {
    status.textContent = 'RECONNECTING...'; status.className = 'disconnected';
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

async function loadBacktests() {
  try {
    const resp = await fetch('/api/backtest');
    const runs = await resp.json();
    const body = document.querySelector('#bt-table tbody');
    body.innerHTML = '';
    runs.forEach(r => {
      const tr = document.createElement('tr');
      const status = r.completed ? '<span class="badge badge-flat">DONE</span>' : '<span class="badge badge-running">RUNNING</span>';
      const pnl = r.pnl ? parseFloat(r.pnl) : 0;
      const pnlColor = pnl > 0 ? '#00e676' : pnl < 0 ? '#ff5252' : '#e0e0e0';
      const pct = r.progress_bars ? Math.round(r.progress_bars / 43172 * 100) : 0;
      const progressStr = r.completed ? 'Complete' : pct + '%';
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

// Init
initChart();
loadEquity();
loadBacktests();
setInterval(loadEquity, 30000);
setInterval(loadBacktests, 60000);  // refresh backtest status every 60s
connectSSE();
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
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    print(f"Argus Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")