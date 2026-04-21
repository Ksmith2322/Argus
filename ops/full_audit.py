"""ops/full_audit.py — comprehensive on-demand bot audit.

Produces a timestamped audit report at `docs/audits/<YYYYMMDD_HHMM>/`
containing both machine-readable (JSON) and human-readable (Markdown)
views. Complements the multi-agent judgment audits by giving you hard
numbers you can diff week-over-week.

Lenses (10):
  1. Fleet state        — broker, pairs, processes, heartbeats
  2. Risk posture       — drawdown, correlation, exposure, pauses
  3. Trading activity   — 24h/7d/30d trade counts, PnL, block histogram
  4. Data integrity     — canonical vs CSV divergence, reconciliation
  5. Strategy readiness — dispositions, promotion gates, holdout status
  6. Signal conversion  — signals → entries, top block reasons
  7. Scheduled tasks    — Argus* task health (last result, logon mode)
  8. Code hygiene       — recent errors, stale locks, git status
  9. External deps      — IBKR, yfinance, Discord webhook health
 10. Config drift       — active configs vs running launch args

Usage:
    python -m ops.full_audit                  # full audit, save to docs/audits/
    python -m ops.full_audit --lens fleet     # one lens only
    python -m ops.full_audit --diff prev      # compare to previous audit
    python -m ops.full_audit --json           # stdout JSON only

Output structure:
    docs/audits/<stamp>/
      report.json            # all lenses, machine-readable
      report.md              # human-readable summary
      <lens>.json            # per-lens detail
      diff.md                # vs previous audit (if --diff or >=1 prior exists)
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

AUDITS_DIR = REPO / "docs" / "audits"
CANONICAL_FILLS = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
RUNNER_LOG = REPO / "argus_flow" / "logs" / "runner_unified.log"
LOGS_DIR = REPO / "argus_flow" / "logs"
CONFIG_DIR = REPO / "argus_flow" / "configs"
CONFIDENCE_DIR = REPO / "strategy_confidence"


# ---------------- helpers ----------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ps(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout).decode("utf-8", errors="replace")
    except Exception as e:
        return f"<error: {e}>"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file_age_seconds(path: Path) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return float("inf")


# ---------------- lens 1: fleet state ----------------

def lens_fleet_state() -> dict:
    out: dict = {"lens": "fleet_state", "ts": _now().isoformat()}
    # Broker via dashboard API
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8080/api/gateway_status", timeout=5) as r:
            out["gateway"] = json.loads(r.read().decode())
    except Exception as e:
        out["gateway"] = {"error": str(e)}

    # Process inventory
    try:
        raw = _ps(["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/format:csv"])
        pyprocs = []
        for line in raw.splitlines():
            if "runner_unified" in line or "fleet_monitor" in line or "dashboard.py" in line or "gld_pm_long" in line or "aud_asian" in line or "nq_london" in line:
                parts = line.rsplit(",", 1)
                if len(parts) == 2 and parts[1].strip().isdigit():
                    pyprocs.append({"pid": int(parts[1].strip()), "cmd": parts[0].split(",", 1)[-1].strip()[:160]})
        out["python_processes"] = pyprocs
    except Exception as e:
        out["python_processes_error"] = str(e)

    try:
        ps_raw = _ps(["wmic", "process", "where", "name='powershell.exe'", "get", "ProcessId,CommandLine", "/format:csv"])
        ps_procs = []
        for line in ps_raw.splitlines():
            if "watchdog" in line.lower() or "meta_watchdog" in line.lower():
                parts = line.rsplit(",", 1)
                if len(parts) == 2 and parts[1].strip().isdigit():
                    ps_procs.append({"pid": int(parts[1].strip()), "cmd": parts[0].split(",", 1)[-1].strip()[:160]})
        out["powershell_processes"] = ps_procs
    except Exception as e:
        out["powershell_processes_error"] = str(e)

    # Per-pair heartbeat
    hbs = []
    for pair_dir in (LOGS_DIR / "usdjpy", LOGS_DIR / "gbpusd", LOGS_DIR / "cadjpy"):
        hb = pair_dir / "heartbeat.json"
        d = _load_json(hb)
        if d:
            d["_age_s"] = round(_file_age_seconds(hb), 1)
            d["_pair"] = pair_dir.name
            hbs.append(d)
    out["heartbeats"] = hbs
    return out


# ---------------- lens 2: risk posture ----------------

def lens_risk_posture() -> dict:
    out: dict = {"lens": "risk_posture", "ts": _now().isoformat()}
    risk = _load_json(LOGS_DIR / "_risk" / "portfolio_risk_state.json") or {}
    oversight = _load_json(LOGS_DIR / "risk_oversight_report.json") or {}
    out["portfolio_risk_state"] = risk
    out["drawdown_pause"] = risk.get("drawdown_pause")
    peak = risk.get("peak_pnl") or 0
    cur = risk.get("current_pnl") or 0
    out["peak_r"] = peak
    out["current_r"] = cur
    out["dd_pct"] = round((peak - cur) / abs(peak) * 100, 2) if peak else 0
    out["oversight_level"] = oversight.get("level")
    out["oversight_detail"] = oversight.get("message") or oversight.get("detail")
    out["min_peak_floor_r"] = 10.0  # keep in sync with runner_unified
    out["breaker_armed"] = peak >= 10.0
    # Manual reset flag?
    out["reset_drawdown_flag_present"] = (REPO / "RESET_DRAWDOWN").exists()
    return out


# ---------------- lens 3: trading activity ----------------

def _load_canonical_fills() -> list[dict]:
    if not CANONICAL_FILLS.exists():
        return []
    rows = []
    with open(CANONICAL_FILLS, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _trade_stats(rows: list[dict], cutoff_h: int) -> dict:
    cutoff = _now() - timedelta(hours=cutoff_h)
    window = []
    for r in rows:
        if r.get("source") == "backfill_from_trade_csv":
            continue
        ts_s = r.get("exit_ts") or r.get("ts")
        if not ts_s:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_s).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            window.append(r)
    per_strat: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    total_pnl = 0.0
    wins = 0
    for r in window:
        s = r.get("strategy", "?")
        try:
            pnl = float(r.get("pnl_usd") or 0)
        except Exception:
            pnl = 0
        per_strat[s]["n"] += 1
        per_strat[s]["pnl"] += pnl
        total_pnl += pnl
        if pnl > 0:
            per_strat[s]["wins"] += 1
            wins += 1
    return {
        "window_hours": cutoff_h,
        "total_trades": len(window),
        "total_pnl_usd": round(total_pnl, 2),
        "wins": wins,
        "win_rate": round(wins / len(window), 3) if window else 0,
        "per_strategy": {k: {"n": v["n"], "pnl": round(v["pnl"], 2), "wins": v["wins"]} for k, v in per_strat.items()},
    }


def lens_trading_activity() -> dict:
    out: dict = {"lens": "trading_activity", "ts": _now().isoformat()}
    rows = _load_canonical_fills()
    out["canonical_fills_total"] = len(rows)
    out["canonical_fills_backfill"] = sum(1 for r in rows if r.get("source") == "backfill_from_trade_csv")
    out["canonical_fills_live"] = out["canonical_fills_total"] - out["canonical_fills_backfill"]
    out["last_24h"] = _trade_stats(rows, 24)
    out["last_7d"] = _trade_stats(rows, 168)
    out["last_30d"] = _trade_stats(rows, 720)
    return out


# ---------------- lens 4: data integrity ----------------

def _count_rows(csv_path: Path) -> tuple[int, float]:
    if not csv_path.exists():
        return 0, 0.0
    try:
        with open(csv_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0, 0.0
    pnl = 0.0
    for r in rows:
        try:
            pnl += float(r.get("pnl_usd") or 0)
        except Exception:
            pass
    return len(rows), pnl


def lens_data_integrity() -> dict:
    out: dict = {"lens": "data_integrity", "ts": _now().isoformat()}
    # canonical rollup per strategy
    rows = _load_canonical_fills()
    canonical: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for r in rows:
        s = r.get("strategy", "?")
        canonical[s]["n"] += 1
        try:
            canonical[s]["pnl"] += float(r.get("pnl_usd") or 0)
        except Exception:
            pass

    # per-strategy CSVs
    csv_map = {
        "forge_jpy_pm_short": REPO / "forge/logs/jpy_pm_short/trades.csv",
        "forge_nq_overnight": REPO / "forge/logs/nq_overnight/trades.csv",
        "forge_gld_pm_long": REPO / "forge/logs/gld_pm_long/trades.csv",
        "forge_wick_gbpusd": REPO / "forge/logs/wick_gbpusd/trades.csv",
        "forge_gdx_gld": REPO / "forge/logs/gdx_gld/trades.csv",
        "forge_nq_london_close": REPO / "forge/logs/nq_london_close/trades.csv",
        "forge_aud_asian_breakout": REPO / "forge/logs/aud_asian_breakout/trades.csv",
        "argus_usdjpy": REPO / "argus_flow/logs/usdjpy/trades.csv",
        "argus_gbpusd": REPO / "argus_flow/logs/gbpusd/trades.csv",
        "argus_cadjpy": REPO / "argus_flow/logs/cadjpy/trades.csv",
    }
    per_strat = {}
    for strat, p in csv_map.items():
        csv_n, csv_pnl = _count_rows(p)
        can = canonical.get(strat, {"n": 0, "pnl": 0.0})
        row = {
            "csv_n": csv_n,
            "canonical_n": can["n"],
            "csv_pnl": round(csv_pnl, 2),
            "canonical_pnl": round(can["pnl"], 2),
            "delta_n": csv_n - can["n"],
            "delta_pnl": round(csv_pnl - can["pnl"], 2),
        }
        if abs(row["delta_n"]) > 1 or abs(row["delta_pnl"]) > 1:
            row["status"] = "DIVERGENT"
        else:
            row["status"] = "OK"
        per_strat[strat] = row
    out["per_strategy"] = per_strat
    out["divergent_strategies"] = [k for k, v in per_strat.items() if v["status"] == "DIVERGENT"]

    # Reconciliation report
    recon = _load_json(LOGS_DIR / "reconciliation_report.json") or {}
    out["reconciliation_all_reconciled"] = recon.get("all_reconciled")
    out["reconciliation_drift_strategies"] = [
        s.get("strategy") for s in (recon.get("strategies") or []) if s.get("status") == "DRIFT"
    ]

    # Apollo forward_returns freshness
    fr_path = REPO / "apollo" / "logs" / "forward_returns.jsonl"
    if fr_path.exists():
        age_h = _file_age_seconds(fr_path) / 3600
        with open(fr_path) as f:
            line_count = sum(1 for _ in f)
        out["apollo_forward_returns"] = {"lines": line_count, "age_hours": round(age_h, 1),
                                         "stale": age_h > 48}
    return out


# ---------------- lens 5: strategy readiness ----------------

def lens_strategy_readiness() -> dict:
    out: dict = {"lens": "strategy_readiness", "ts": _now().isoformat()}
    dispositions = defaultdict(list)
    holdouts = []
    artifacts = []
    for p in sorted(CONFIDENCE_DIR.glob("*.json")):
        a = _load_json(p)
        if not a:
            continue
        d = a.get("disposition")
        if d:
            dispositions[d.get("status", "?")].append({
                "strategy": a.get("strategy", p.stem),
                "reason": (d.get("reason") or "")[:140],
                "decided_at": (d.get("decided_at") or "")[:10],
            })
        if a.get("holdout_freeze"):
            hf = a["holdout_freeze"]
            holdouts.append({
                "strategy": a.get("strategy", p.stem),
                "frozen_at": (hf.get("frozen_at") or "")[:10],
                "in_sample_pf": (hf.get("in_sample_claim") or {}).get("pf"),
                "oos_eval_earliest": (hf.get("oos_eval_earliest") or "")[:10],
            })
        artifacts.append({
            "strategy": a.get("strategy", p.stem),
            "pf": a.get("bt_pf"),
            "n_trades": a.get("bt_trades"),
            "p_exp_pos": a.get("p_expectancy_positive"),
            "evidence_bar": a.get("evidence_bar"),
        })

    out["dispositions"] = dict(dispositions)
    out["dispositions_count"] = {k: len(v) for k, v in dispositions.items()}
    out["holdouts_frozen"] = holdouts
    out["artifact_count"] = len(artifacts)

    # Promotion readiness via dashboard API
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8080/api/promotion_readiness", timeout=5) as r:
            pr = json.loads(r.read().decode())
        promo = []
        for s in pr.get("strategies", []):
            promo.append({
                "strategy": s.get("strategy"),
                "trades": (s.get("stats") or {}).get("trades"),
                "pf": (s.get("stats") or {}).get("profit_factor"),
                "review_pass": s.get("review_gate_passed"),
                "canonical_pass": s.get("canonical_gate_passed"),
                "next_action": s.get("next_action"),
            })
        out["promotion_readiness"] = promo
    except Exception as e:
        out["promotion_readiness_error"] = str(e)
    return out


# ---------------- lens 6: signal conversion ----------------

def lens_signal_conversion() -> dict:
    out: dict = {"lens": "signal_conversion", "ts": _now().isoformat()}
    if not RUNNER_LOG.exists():
        out["error"] = "runner_unified.log missing"
        return out
    cutoff = _now() - timedelta(hours=24)
    counts = Counter()
    try:
        with open(RUNNER_LOG, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", line)
                if not m:
                    continue
                try:
                    ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                if "MTF SIGNAL" in line: counts["MTF_SIGNAL"] += 1
                if re.search(r"\| ENTRY (LONG|SHORT) ", line): counts["ENTRY"] += 1
                if "MTF_BLOCK" in line: counts["MTF_BLOCK"] += 1
                if "AI OVERLAY SKIP" in line or "AI SKIP" in line: counts["AI_SKIP"] += 1
                if "NOTIONAL_CAP" in line: counts["NOTIONAL_CAP"] += 1
                if "DRAWDOWN_PAUSE" in line: counts["DRAWDOWN_PAUSE"] += 1
                if "RECON_DRIFT" in line: counts["RECON_DRIFT"] += 1
                if "[ERROR]" in line: counts["ERROR"] += 1
                if "BLOCKED by pre-trade" in line: counts["PRETRADE_BLOCK"] += 1
    except Exception as e:
        out["error"] = str(e)
        return out

    out["counts_24h"] = dict(counts)
    out["signal_to_entry_pct"] = round(
        counts.get("ENTRY", 0) / max(counts.get("MTF_SIGNAL", 1), 1) * 100, 1
    )
    # Top 3 block reasons
    blocks = {k: v for k, v in counts.items() if k in
              ("MTF_BLOCK", "AI_SKIP", "DRAWDOWN_PAUSE", "RECON_DRIFT", "PRETRADE_BLOCK")}
    out["top_blocks"] = dict(sorted(blocks.items(), key=lambda x: -x[1])[:3])
    return out


# ---------------- lens 7: scheduled tasks ----------------

def lens_scheduled_tasks() -> dict:
    out: dict = {"lens": "scheduled_tasks", "ts": _now().isoformat()}
    tasks = ["ArgusCohortReport", "ArgusWatchdog", "ArgusGldPmLoop",
             "ArgusNqLondonCloseLoop", "ArgusAudOrbLoop", "ArgusMetaWatchdog"]
    results = {}
    for t in tasks:
        raw = _ps(["schtasks", "/query", "/TN", t, "/FO", "LIST", "/V"], timeout=10)
        if "<error" in raw.lower() or "error:" in raw.lower()[:60]:
            results[t] = {"exists": False, "error": raw.strip()[:120]}
            continue
        info = {"exists": True}
        for line in raw.splitlines():
            for key in ("Status", "Last Run Time", "Last Result", "Next Run Time", "Logon Mode", "Run As User"):
                if line.strip().startswith(f"{key}:"):
                    info[key.lower().replace(" ", "_")] = line.split(":", 1)[1].strip()
        results[t] = info
    out["tasks"] = results
    # Flag issues
    flags = []
    for name, info in results.items():
        if not info.get("exists"):
            flags.append(f"{name}: not registered")
            continue
        if info.get("last_result") and info.get("last_result") not in ("0", "267011", "267009"):
            flags.append(f"{name}: last_result={info.get('last_result')}")
        if info.get("logon_mode") == "Interactive only":
            flags.append(f"{name}: Interactive only (won't survive logoff)")
    out["flags"] = flags
    return out


# ---------------- lens 8: code hygiene ----------------

def lens_code_hygiene() -> dict:
    out: dict = {"lens": "code_hygiene", "ts": _now().isoformat()}
    # Recent errors (last 24h)
    if RUNNER_LOG.exists():
        cutoff = _now() - timedelta(hours=24)
        errors = []
        try:
            with open(RUNNER_LOG, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z.*\[ERROR\]", line)
                    if not m:
                        continue
                    try:
                        ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if ts >= cutoff:
                        errors.append(line.strip()[:200])
        except Exception:
            pass
        out["errors_24h_count"] = len(errors)
        out["errors_24h_sample"] = errors[:5]

    # Stale locks
    stale = []
    locks_dir = LOGS_DIR / "_locks"
    if locks_dir.exists():
        now = time.time()
        for lock in locks_dir.glob("*.lock"):
            age_h = (now - os.path.getmtime(lock)) / 3600
            if age_h > 48:
                stale.append({"name": lock.name, "age_hours": round(age_h, 1)})
    out["stale_locks"] = stale

    # Git status
    try:
        gs = _ps(["git", "-C", str(REPO), "status", "--short"])
        out["uncommitted_files"] = [l for l in gs.splitlines() if l.strip()]
        ahead = _ps(["git", "-C", str(REPO), "rev-list", "--count", "origin/phase6-hardening..HEAD"]).strip()
        out["commits_ahead_of_origin"] = int(ahead) if ahead.isdigit() else None
    except Exception as e:
        out["git_error"] = str(e)
    return out


# ---------------- lens 9: external deps ----------------

def lens_external_deps() -> dict:
    out: dict = {"lens": "external_deps", "ts": _now().isoformat()}
    # IBKR port 7497 (paper) listening?
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(("127.0.0.1", 7497))
        s.close()
        out["ibkr_port_7497_listening"] = result == 0
    except Exception as e:
        out["ibkr_port_7497_error"] = str(e)

    # yfinance smoke test
    try:
        import yfinance as yf
        t0 = time.time()
        df = yf.download("SPY", period="1d", interval="1h", progress=False, threads=False)
        t1 = time.time()
        out["yfinance"] = {
            "reachable": df is not None and not df.empty,
            "latency_s": round(t1 - t0, 2),
            "bars": len(df) if df is not None else 0,
        }
    except Exception as e:
        out["yfinance_error"] = str(e)[:200]

    # Discord failure log
    discord_fail = LOGS_DIR / "discord_failures.jsonl"
    if discord_fail.exists():
        try:
            with open(discord_fail) as f:
                lines = f.readlines()
            out["discord_failure_count"] = len(lines)
            out["discord_last_failure"] = (lines[-1][:200] if lines else None)
        except Exception:
            pass
    return out


# ---------------- lens 10: config drift ----------------

def lens_config_drift() -> dict:
    out: dict = {"lens": "config_drift", "ts": _now().isoformat()}
    # What runner is actually launched with?
    try:
        raw = _ps(["wmic", "process", "where", "name='python.exe' and CommandLine like '%%runner_unified%%'", "get", "CommandLine", "/format:csv"])
        launched = []
        for line in raw.splitlines():
            if "runner_unified" in line:
                # Extract configs
                configs = re.findall(r"argus_flow/configs/[^\s,]+\.json", line)
                launched.extend(configs)
        out["runner_launched_configs"] = sorted(set(launched))
    except Exception as e:
        out["runner_launched_error"] = str(e)

    # What configs are on disk?
    on_disk = sorted([p.name for p in CONFIG_DIR.glob("*.json")
                      if p.name != "fleet_sizing.json" and p.name != "hashes.json"
                      and p.name != "discovery_fx_universe.json"])
    out["configs_on_disk"] = on_disk

    # Pending configs
    pending = CONFIG_DIR / "_pending"
    if pending.exists():
        out["configs_pending"] = [p.name for p in pending.glob("*.json")]
    return out


# ---------------- entrypoint ----------------

LENSES = {
    "fleet": lens_fleet_state,
    "risk": lens_risk_posture,
    "trading": lens_trading_activity,
    "data": lens_data_integrity,
    "strategy": lens_strategy_readiness,
    "signals": lens_signal_conversion,
    "tasks": lens_scheduled_tasks,
    "code": lens_code_hygiene,
    "deps": lens_external_deps,
    "drift": lens_config_drift,
}


def run_all() -> dict:
    results = {}
    for name, fn in LENSES.items():
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"lens": name, "error": str(e)[:200]}
    return results


def _summarize_markdown(report: dict) -> str:
    """Produce a short human-readable digest."""
    lines = []
    lines.append(f"# Argus Full Audit — {report['_stamp']}\n")

    fleet = report.get("fleet", {})
    gw = fleet.get("gateway", {})
    lines.append("## Fleet")
    if "error" in gw:
        lines.append(f"- Gateway: **ERROR** — {gw['error']}")
    else:
        lines.append(f"- Equity: **${gw.get('broker_equity_usd', 0):,.2f}** | healthy: {gw.get('all_healthy')} | pause_entries: {gw.get('pause_entries_present')}")
        for pair, info in (gw.get("per_pair") or {}).items():
            lines.append(f"  - {pair}: pos={info.get('position')} age={info.get('age_s')}s broker={info.get('broker_connected')} blocked={info.get('entries_blocked')}")
    lines.append(f"- Python procs: {len(fleet.get('python_processes', []))}")
    lines.append(f"- PS procs: {len(fleet.get('powershell_processes', []))}")
    lines.append("")

    risk = report.get("risk", {})
    lines.append("## Risk")
    lines.append(f"- drawdown_pause={risk.get('drawdown_pause')} peak={risk.get('peak_r')}R current={risk.get('current_r')}R dd%={risk.get('dd_pct')}")
    lines.append(f"- breaker_armed={risk.get('breaker_armed')} (floor={risk.get('min_peak_floor_r')}R) oversight={risk.get('oversight_level')}")
    lines.append("")

    trading = report.get("trading", {})
    lines.append("## Trading Activity")
    for wk, key in [("24h", "last_24h"), ("7d", "last_7d"), ("30d", "last_30d")]:
        w = trading.get(key, {})
        lines.append(f"- {wk}: {w.get('total_trades')} trades | {w.get('wins')} wins ({w.get('win_rate', 0)*100:.0f}%) | ${w.get('total_pnl_usd', 0):+,.2f}")
    lines.append(f"- canonical: {trading.get('canonical_fills_live')} live + {trading.get('canonical_fills_backfill')} backfill = {trading.get('canonical_fills_total')} total")
    lines.append("")

    data = report.get("data", {})
    lines.append("## Data Integrity")
    div = data.get("divergent_strategies", [])
    lines.append(f"- Divergent (CSV vs canonical): {len(div)} — {div if div else 'none'}")
    recon_ok = data.get("reconciliation_all_reconciled")
    lines.append(f"- Reconciliation: {'OK' if recon_ok else 'DRIFT'} ({data.get('reconciliation_drift_strategies') or []})")
    apollo = data.get("apollo_forward_returns") or {}
    lines.append(f"- Apollo forward_returns: {apollo.get('lines')} lines, {apollo.get('age_hours')}h age ({'STALE' if apollo.get('stale') else 'fresh'})")
    lines.append("")

    strat = report.get("strategy", {})
    lines.append("## Strategy Readiness")
    lines.append(f"- Dispositions: {strat.get('dispositions_count', {})}")
    lines.append(f"- Holdouts frozen: {len(strat.get('holdouts_frozen', []))}")
    pr = strat.get("promotion_readiness") or []
    near_ready = [p for p in pr if p.get("trades", 0) >= 10]
    lines.append(f"- Strategies with 10+ live trades: {len(near_ready)}")
    lines.append("")

    sig = report.get("signals", {})
    lines.append("## Signal Conversion (24h)")
    c = sig.get("counts_24h", {})
    lines.append(f"- Signals: {c.get('MTF_SIGNAL', 0)} | Entries: {c.get('ENTRY', 0)} | conv={sig.get('signal_to_entry_pct')}%")
    lines.append(f"- Top blocks: {sig.get('top_blocks')}")
    lines.append("")

    tasks = report.get("tasks", {})
    lines.append("## Scheduled Tasks")
    for name, info in (tasks.get("tasks") or {}).items():
        if not info.get("exists"):
            lines.append(f"- {name}: **MISSING**")
        else:
            lines.append(f"- {name}: status={info.get('status')} last_result={info.get('last_result')} logon_mode={info.get('logon_mode')}")
    flags = tasks.get("flags") or []
    if flags:
        lines.append(f"- Flags: {flags}")
    lines.append("")

    code = report.get("code", {})
    lines.append("## Code Hygiene")
    lines.append(f"- Errors 24h: {code.get('errors_24h_count')}")
    lines.append(f"- Stale locks (>48h): {len(code.get('stale_locks') or [])}")
    lines.append(f"- Uncommitted files: {len(code.get('uncommitted_files') or [])}")
    lines.append(f"- Commits ahead of origin: {code.get('commits_ahead_of_origin')}")
    lines.append("")

    deps = report.get("deps", {})
    lines.append("## External Deps")
    lines.append(f"- IBKR port 7497 listening: {deps.get('ibkr_port_7497_listening')}")
    yf = deps.get("yfinance") or {}
    lines.append(f"- yfinance: reachable={yf.get('reachable')} latency={yf.get('latency_s')}s bars={yf.get('bars')}")
    lines.append(f"- Discord failures logged: {deps.get('discord_failure_count', 0)}")
    lines.append("")

    drift = report.get("drift", {})
    launched = drift.get("runner_launched_configs", [])
    on_disk = drift.get("configs_on_disk", [])
    disk_set = set(on_disk); launch_set = set([os.path.basename(c) for c in launched])
    lines.append("## Config Drift")
    lines.append(f"- Configs on disk: {len(on_disk)}")
    lines.append(f"- Runner launched with: {len(launched)}")
    missing_launch = disk_set - launch_set
    if missing_launch:
        lines.append(f"- **On disk but not launched:** {sorted(missing_launch)}")
    pending = drift.get("configs_pending") or []
    if pending:
        lines.append(f"- Pending (parked in _pending/): {pending}")
    lines.append("")

    return "\n".join(lines)


def _compute_diff(curr: dict, prev: dict) -> str:
    """Produce a markdown diff of key metrics between two audits."""
    lines = ["# Audit diff vs previous\n"]
    lines.append(f"- Previous stamp: {prev.get('_stamp')}")
    lines.append(f"- Current  stamp: {curr.get('_stamp')}\n")

    def _get(d, *keys, default=None):
        x = d
        for k in keys:
            x = (x or {}).get(k, default) if isinstance(x, dict) else default
        return x

    rows = []
    # Canonical fills growth
    rows.append(("canonical_fills_live",
                 _get(prev, "trading", "canonical_fills_live"),
                 _get(curr, "trading", "canonical_fills_live")))
    rows.append(("24h_total_trades",
                 _get(prev, "trading", "last_24h", "total_trades"),
                 _get(curr, "trading", "last_24h", "total_trades")))
    rows.append(("24h_pnl_usd",
                 _get(prev, "trading", "last_24h", "total_pnl_usd"),
                 _get(curr, "trading", "last_24h", "total_pnl_usd")))
    rows.append(("7d_total_trades",
                 _get(prev, "trading", "last_7d", "total_trades"),
                 _get(curr, "trading", "last_7d", "total_trades")))
    rows.append(("signal_to_entry_pct",
                 _get(prev, "signals", "signal_to_entry_pct"),
                 _get(curr, "signals", "signal_to_entry_pct")))
    rows.append(("errors_24h",
                 _get(prev, "code", "errors_24h_count"),
                 _get(curr, "code", "errors_24h_count")))
    rows.append(("divergent_strategies",
                 len(_get(prev, "data", "divergent_strategies") or []),
                 len(_get(curr, "data", "divergent_strategies") or [])))

    lines.append("| metric | previous | current | delta |")
    lines.append("|---|---|---|---|")
    for name, old, new in rows:
        try:
            delta = f"{(new - old):+}" if isinstance(old, (int, float)) and isinstance(new, (int, float)) else "—"
        except Exception:
            delta = "—"
        lines.append(f"| {name} | {old} | {new} | {delta} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lens", help="Run only this lens (e.g., fleet, risk, trading, ...)")
    ap.add_argument("--json", action="store_true", help="Print JSON to stdout, don't save")
    ap.add_argument("--diff", action="store_true", help="Include diff vs previous audit")
    args = ap.parse_args()

    if args.lens:
        fn = LENSES.get(args.lens)
        if not fn:
            print(f"Unknown lens: {args.lens}. Options: {list(LENSES.keys())}")
            return 2
        result = fn()
        print(json.dumps(result, indent=2, default=str))
        return 0

    stamp = _now().strftime("%Y%m%d_%H%M")
    report = run_all()
    report["_stamp"] = stamp
    report["_generated_at"] = _now().isoformat()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    out_dir = AUDITS_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for name, res in report.items():
        if name.startswith("_"):
            continue
        (out_dir / f"{name}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")

    md = _summarize_markdown(report)
    (out_dir / "report.md").write_text(md, encoding="utf-8")

    if args.diff:
        prior = sorted([p for p in AUDITS_DIR.glob("*/") if p.name != stamp])
        if prior:
            prev_report = _load_json(prior[-1] / "report.json")
            if prev_report:
                diff = _compute_diff(report, prev_report)
                (out_dir / "diff.md").write_text(diff, encoding="utf-8")

    print(f"Wrote audit to: {out_dir}")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
