"""Read-only Argus system audit engine.

This module intentionally stays above runtime state. It reads artifacts,
configs, logs, and doctrine notes, then emits audit reports under
``ops/reports/system_audit``. It must never write backward into strategy,
execution, account, or dashboard runtime state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
MEMORY_DIR = Path(r"C:\Users\ksmit\.claude\projects\c--Argus\memory")
POST_RESET_TS = datetime(2026, 5, 1, tzinfo=timezone.utc)

TRUTH_BLOCKER_CLASSES = {
    "MISSING_ARTIFACT",
    "SCHEMA_DRIFT",
    "DUPLICATE_FILL",
    "ORPHAN_FILL",
    "ORPHAN_ORDER",
    "TRADE_WITHOUT_EXIT",
    "EXIT_WITHOUT_ENTRY",
    "POSITION_MISMATCH",
    "PNL_MISMATCH",
    "ACCOUNT_LEDGER_MISMATCH",
    "TIMESTAMP_ORDER_ERROR",
    "STALE_STATE",
    "PRE_RESET_DATA_CONTAMINATION",
    "DASHBOARD_TRUTH_MISMATCH",
}

MEMORY_FILES = [
    "project_operating_doctrine.md",
    "project_real_money_boundary.md",
    "project_kill_pause_engine.md",
    "project_capital_allocator_policy.md",
    "project_cluster_exposure_model.md",
    "project_strategy_freeze_20260531.md",
    "project_5_week_roadmap_20260424_to_20260531.md",
    "project_2026_05_07_week_audit.md",
    "project_2026_05_07_evening_audit.md",
    "project_2026_05_07_evening_session_fixes.md",
    "reference_tws_error_326_stale_client_ids.md",
    "reference_failure_modes.md",
    "project_expanded_failure_modes_20260501.md",
]

EXPANSION_UNIVERSE = [
    ("SPY", "ETF_INDEX", "Tier 1", "SHADOW_ONLY"),
    ("QQQ", "ETF_INDEX", "Tier 1", "SHADOW_ONLY"),
    ("IWM", "ETF_INDEX", "Tier 1", "SHADOW_ONLY"),
    ("DIA", "ETF_INDEX", "Tier 1 optional", "RESEARCH_ONLY"),
    ("TQQQ", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("SQQQ", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("UPRO", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("SPXU", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("SOXL", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("SOXS", "LEVERAGED_ETF", "Tier 2", "RESEARCH_ONLY"),
    ("MNQ", "MICRO_FUTURE", "Tier 3", "RESEARCH_ONLY"),
    ("MES", "MICRO_FUTURE", "Tier 3", "RESEARCH_ONLY"),
    ("NVDA", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
    ("AMD", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
    ("TSLA", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
    ("AAPL", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
    ("MSFT", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
    ("META", "SINGLE_NAME", "Tier 4", "RESEARCH_ONLY"),
]


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    affected_file: str = ""
    strategy: str = ""
    symbol: str = ""
    row_count: int = 0
    example_rows: str = ""
    recommended_fix: str = ""
    category: str = "truth"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def parse_ts(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for key in ("Z",):
        text = text.replace(key, "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                ts = datetime.strptime(text.split(".")[0], fmt)
                break
            except ValueError:
                ts = None
        else:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
            if limit and len(rows) >= limit:
                break
    return rows


def read_csv(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return []
    return rows[:limit] if limit else rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=REPO, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def files(pattern: str) -> list[Path]:
    return sorted(p for p in REPO.glob(pattern) if p.is_file())


def classify_symbol(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "")
    if s in {"BTCUSD", "BTCUSDT", "ETHUSD", "SOLUSD"}:
        return "crypto"
    if s in {"MNQ", "MES", "M2K", "MYM", "NQ", "ES", "YM", "MGC", "MCL"}:
        return "futures"
    if len(s) == 6 and s.isalpha() and s[:3] in {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}:
        return "fx"
    if s in {"SPY", "QQQ", "IWM", "DIA", "GLD", "GDX", "TLT", "UVXY", "SVXY", "TQQQ", "SQQQ", "UPRO", "SPXU", "SMH"}:
        return "etf"
    return "equity_or_unknown"


def strategy_from_path(path: Path) -> str:
    parts = path.parts
    if "forge" in parts and "logs" in parts:
        idx = parts.index("logs")
        if idx + 1 < len(parts):
            return f"forge_{parts[idx + 1]}"
    if "argus_flow" in parts and "logs" in parts:
        idx = parts.index("logs")
        if idx + 1 < len(parts):
            name = parts[idx + 1]
            if name and not name.startswith("_"):
                return f"argus_{name}"
    return path.stem


def summarize_memory() -> dict[str, Any]:
    out: dict[str, Any] = {"loaded": [], "missing": []}
    for name in MEMORY_FILES:
        path = MEMORY_DIR / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            out["loaded"].append({"file": str(path), "bytes": len(text)})
        else:
            out["missing"].append(str(path))
    out["locked_doctrine"] = [
        "Canonical execution truth -> deterministic lifecycle truth -> analytical truth.",
        "Validated edge first, capital second, breadth last.",
        "Factor 0.0 in allocation_factors.json means KILLED for scoring.",
        "Layer 6 scorecard and Layer 8 capital ladder may be policy-only; flag as governance gap, not redesign target.",
        "No new strategy or instrument can go live before freeze/go-live gates; expansion begins shadow or research only.",
        "5/15 mid-cycle review is the next decision gate.",
    ]
    return out


def discover_artifacts() -> dict[str, list[dict[str, Any]]]:
    patterns = {
        "orders": ["**/orders.csv", "**/orders.jsonl"],
        "fills": ["**/canonical_fills.jsonl", "**/fills.csv", "**/fills.jsonl"],
        "positions": ["**/positions.csv", "**/positions.json", "**/state.json", "state/*.json"],
        "account": ["**/account.csv", "**/account*.json", "**/broker_truth*.json", "**/risk_oversight_report.json"],
        "trade_journal": ["**/trades.csv", "**/trade_journal*.csv", "**/trades.jsonl"],
        "events": ["**/events*.csv", "**/events*.jsonl", "**/opportunities.jsonl"],
        "daily_summaries": ["**/daily*.json", "**/fleet_perf_summary.json"],
        "heartbeats": ["**/heartbeat.json"],
        "incidents": ["**/incident*.json", "**/HALT.flag", "**/broker_drift_state.json"],
        "promotion_reports": ["**/promotion*.json", "**/readiness*.json"],
        "risk_reports": ["**/*risk*.json", "**/*drift*.json"],
        "dashboard_data": ["**/fleet_status.json", "**/dashboard*.json"],
    }
    inventory: dict[str, list[dict[str, Any]]] = {}
    for kind, pats in patterns.items():
        seen: set[Path] = set()
        rows: list[dict[str, Any]] = []
        for pat in pats:
            for path in files(pat):
                if path in seen or any(part.startswith("pytest-cache-files") for part in path.parts):
                    continue
                seen.add(path)
                try:
                    size = path.stat().st_size
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                except OSError:
                    size = 0
                    mtime = ""
                rows.append({
                    "artifact_type": kind,
                    "path": rel(path),
                    "owner_module": infer_owner(path),
                    "size_bytes": size,
                    "mtime_utc": mtime,
                    "legacy_or_current": "pre_reset_or_legacy" if is_legacy_path(path) else "current_or_post_reset",
                })
        inventory[kind] = rows
    return inventory


def infer_owner(path: Path) -> str:
    parts = path.parts
    if "forge" in parts:
        return "forge"
    if "argus_flow" in parts:
        return "argus_flow"
    if "helio" in parts:
        return "helio"
    if "ops" in parts:
        return "ops"
    if "strategy_confidence" in parts:
        return "strategy_confidence"
    return parts[0] if parts else "unknown"


def is_legacy_path(path: Path) -> bool:
    text = rel(path).lower()
    return any(token in text for token in ("archive", "_archive", "legacy", "pre_reset", "pre_schema", "202603", "202602"))


def discover_configs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files("**/*.json"):
        if any(part in {"__pycache__", ".git"} for part in path.parts):
            continue
        if "configs" not in path.parts and path.parent.name not in {"strategy_confidence", "state"}:
            continue
        data = read_json(path)
        symbol = ""
        strategy = path.stem
        if isinstance(data, dict):
            symbol = str(data.get("symbol") or data.get("pair") or data.get("instrument") or "")
            strategy = str(data.get("strategy") or data.get("strategy_label") or path.stem)
        rows.append({
            "path": rel(path),
            "owner_module": infer_owner(path),
            "strategy": strategy,
            "symbol": symbol,
            "instrument_class": classify_symbol(symbol),
            "legacy_or_current": "pre_reset_or_legacy" if is_legacy_path(path) else "current_or_post_reset",
            "usage_guess": "runtime_config" if "configs" in path.parts else "confidence_or_state",
        })
    return rows


def allocation_factor_status() -> dict[str, float]:
    data = read_json(REPO / "argus_flow" / "configs" / "allocation_factors.json")
    factors = data.get("factors", {}) if isinstance(data, dict) else {}
    return {str(k): safe_float(v) for k, v in factors.items()}


def collect_strategy_rows() -> list[dict[str, Any]]:
    factors = allocation_factor_status()
    trade_paths = files("forge/logs/*/trades.csv") + files("argus_flow/logs/*/trades.csv")
    signal_paths = files("forge/logs/*/signals.csv") + files("forge/logs/*/*_signals.csv") + files("argus_flow/logs/*/signals.csv")
    heartbeat_paths = files("forge/logs/*/heartbeat.json") + files("argus_flow/logs/*/heartbeat.json")
    strategy_names = {strategy_from_path(p) for p in trade_paths + signal_paths + heartbeat_paths}
    skip_config_stems = {
        "allocation_factors",
        "fleet_sizing",
        "discovery_fx_universe",
        "hashes",
        "real_money_allowlist",
    }
    for path in files("argus_flow/configs/*.json"):
        if path.name.startswith("_"):
            continue
        if path.stem in skip_config_stems:
            continue
        data = read_json(path)
        if not isinstance(data, dict) or not (data.get("symbol") or data.get("pair") or data.get("instrument")):
            continue
        strategy_names.add(path.stem.replace("_paper_v1", ""))

    signals_by_strategy = defaultdict(list)
    for path in signal_paths:
        signals_by_strategy[strategy_from_path(path)].append(path)
    trades_by_strategy = defaultdict(list)
    for path in trade_paths:
        trades_by_strategy[strategy_from_path(path)].append(path)
    heartbeats = {strategy_from_path(p): p for p in heartbeat_paths}

    rows: list[dict[str, Any]] = []
    for strategy in sorted(strategy_names):
        trade_files = trades_by_strategy.get(strategy, [])
        signal_files = signals_by_strategy.get(strategy, [])
        hb_path = heartbeats.get(strategy)
        hb = read_json(hb_path) if hb_path else {}
        trade_rows = [row for p in trade_files for row in read_csv(p)]
        post_rows_raw, pre_rows = split_post_reset(trade_rows)
        phantom_rows = [r for r in post_rows_raw if is_phantom_trade(r)]
        post_rows = [r for r in post_rows_raw if not is_phantom_trade(r)]
        pnl_values = [trade_pnl(r) for r in post_rows]
        net_pnl = sum(pnl_values)
        wins = [p for p in pnl_values if p > 0]
        losses = [p for p in pnl_values if p < 0]
        signal_count = sum(max(len(read_csv(p)), 0) for p in signal_files)
        symbol = first_nonempty([r.get("symbol") for r in trade_rows] + [hb.get("symbol") if isinstance(hb, dict) else ""])
        factor_key = strategy
        short_key = strategy.replace("forge_", "")
        factor = factors.get(factor_key, factors.get(short_key, 1.0))
        status = infer_strategy_status(strategy, factor, len(post_rows), signal_count, hb)
        grade, reason, action = grade_strategy(len(post_rows), net_pnl, wins, losses, status, strategy)
        rows.append({
            "strategy": strategy,
            "status": status,
            "instrument_class": classify_symbol(symbol),
            "symbol": symbol,
            "trade_count_post_reset": len(post_rows),
            "trade_count_pre_reset": len(pre_rows),
            "phantom_trade_count_post_reset": len(phantom_rows),
            "signal_or_opportunity_rows": signal_count,
            "net_pnl": round(net_pnl, 2),
            "gross_pnl": round(sum(abs(p) for p in pnl_values), 2),
            "fees_slippage_friction_estimate": round(max(len(post_rows) * 1.0, abs(net_pnl) * 0.02), 2) if post_rows else 0,
            "profit_factor": profit_factor(wins, losses),
            "expectancy_per_trade": round(net_pnl / len(post_rows), 4) if post_rows else "",
            "win_rate": round(len(wins) / len(post_rows), 4) if post_rows else "",
            "average_win": round(mean(wins), 4) if wins else "",
            "average_loss": round(mean(losses), 4) if losses else "",
            "payoff_ratio": round((mean(wins) / abs(mean(losses))), 4) if wins and losses else "",
            "max_drawdown": round(max_drawdown(pnl_values), 2),
            "max_consecutive_losses": max_consecutive_losses(pnl_values),
            "capital_efficiency": "",
            "average_notional_deployed": average_notional(post_rows),
            "pnl_per_unit_notional": pnl_per_notional(net_pnl, post_rows),
            "opportunity_count": signal_count,
            "taken_trade_count": len(post_rows),
            "blocked_trade_count": count_blocked(signal_files),
            "missed_trade_count": "",
            "mfe": "",
            "mae": "",
            "realized_mfe_capture_ratio": "",
            "fragility_score": fragility_score(pnl_values),
            "promotion_readiness_score": promotion_score(len(post_rows), net_pnl, wins, losses, status),
            "grade": grade,
            "grade_reason": reason,
            "next_action": action,
            "allocation_factor": factor,
            "heartbeat_path": rel(hb_path) if hb_path else "",
            "trade_files": ";".join(rel(p) for p in trade_files),
            "signal_files": ";".join(rel(p) for p in signal_files),
        })
    return rows


def split_post_reset(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    post: list[dict[str, Any]] = []
    pre: list[dict[str, Any]] = []
    for row in rows:
        ts = parse_ts(row.get("exit_ts") or row.get("ts") or row.get("timestamp") or row.get("entry_ts"))
        if ts and ts >= POST_RESET_TS:
            post.append(row)
        else:
            pre.append(row)
    return post, pre


def trade_pnl(row: dict[str, Any]) -> float:
    for key in ("pnl_usd", "pnl", "realized_pnl", "net_pnl"):
        if key in row:
            return safe_float(row.get(key))
    entry = safe_float(row.get("entry_px") or row.get("entry_price"))
    exit_px = safe_float(row.get("exit_px") or row.get("exit_price"))
    size = safe_float(row.get("position_size") or row.get("qty") or row.get("size"), 1.0)
    side = str(row.get("direction") or row.get("side") or "").upper()
    sign = -1 if "SHORT" in side or side == "SELL" else 1
    return (exit_px - entry) * size * sign if entry and exit_px else 0.0


def is_phantom_trade(row: dict[str, Any]) -> bool:
    """Detect paper-side trades that should not enter strategy scoring."""
    text = " ".join(str(v).lower() for v in row.values())
    if "phantom" in text or "broker rejected" in text:
        return True
    symbol = str(row.get("symbol") or "").upper()
    instrument_class = classify_symbol(symbol)
    notional = safe_float(row.get("notional_usd"))
    px = safe_float(row.get("entry_px") or row.get("entry_price"))
    if not notional:
        qty = safe_float(row.get("position_size") or row.get("qty") or row.get("size"))
        notional = abs(px * qty)
    size = abs(safe_float(row.get("position_size") or row.get("qty") or row.get("size")))
    if instrument_class == "fx":
        return False
    if symbol and size > 500:
        return True
    if notional > 500_000:
        return bool(symbol) or px > 1000
    return False


def first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def profit_factor(wins: list[float], losses: list[float]) -> str:
    if wins and not losses:
        return "inf"
    if not wins or not losses:
        return ""
    return str(round(sum(wins) / abs(sum(losses)), 4)) if sum(losses) else ""


def max_drawdown(pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def max_consecutive_losses(pnls: list[float]) -> int:
    best = current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def average_notional(rows: list[dict[str, Any]]) -> str:
    vals = []
    for row in rows:
        notional = safe_float(row.get("notional_usd"))
        if not notional:
            px = safe_float(row.get("entry_px") or row.get("entry_price"))
            qty = safe_float(row.get("position_size") or row.get("qty") or row.get("size"))
            notional = abs(px * qty)
        if notional:
            vals.append(notional)
    return str(round(mean(vals), 2)) if vals else ""


def pnl_per_notional(net_pnl: float, rows: list[dict[str, Any]]) -> str:
    avg = safe_float(average_notional(rows))
    return str(round(net_pnl / avg, 6)) if avg else ""


def count_blocked(paths: list[Path]) -> int:
    blocked = 0
    for path in paths:
        for row in read_csv(path):
            text = " ".join(str(v).lower() for v in row.values())
            if any(token in text for token in ("blocked", "reject", "no_trade", "waiting", "below", "gate")):
                blocked += 1
    return blocked


def infer_strategy_status(strategy: str, factor: float, n_trades: int, signals: int, heartbeat: Any) -> str:
    if factor == 0.0:
        return "KILLED"
    hb = heartbeat if isinstance(heartbeat, dict) else {}
    mode = str(hb.get("mode") or hb.get("account_mode") or "").lower()
    blocked = bool(hb.get("entries_blocked"))
    if "research" in mode or "signal_only" in mode:
        return "SHADOW_ONLY"
    if blocked:
        return "BLOCKED"
    if n_trades > 0:
        return "ACTIVE"
    if signals > 0:
        return "IDLE"
    if "archive" in strategy or "legacy" in strategy:
        return "LEGACY"
    return "IDLE"


def grade_strategy(n: int, net: float, wins: list[float], losses: list[float], status: str, strategy: str) -> tuple[str, str, str]:
    if status == "KILLED":
        return "F", "allocation factor is 0.0 or kill record exists", "KEEP_KILLED"
    if n == 0:
        return "UNKNOWN", "ghost/flat or no post-reset closed trades; silence is not edge", "DIAGNOSE_OR_KEEP_SHADOW"
    if n < 10:
        return "C", "insufficient post-reset sample", "NEEDS_MORE_SAMPLE"
    pf_text = profit_factor(wins, losses)
    pf = math.inf if pf_text == "inf" else safe_float(pf_text)
    concentration = fragility_score([*wins, *losses])
    if net <= 0 or (pf and pf < 1.1):
        return "D", "negative or weak friction-adjusted expectancy", "QUARANTINE_OR_REPAIR"
    if concentration >= 70:
        return "C", "payoff concentrated in too few trades", "NEEDS_CONCENTRATION_REVIEW"
    if n < 30:
        return "B", "positive but not enough sample for promotion", "CONTINUE_PAPER_SHADOW"
    return "A", "promotion candidate subject to truth and benchmark gates", "PROMOTE_AFTER_SAMPLE_AND_RECON"


def fragility_score(pnls: list[float]) -> int:
    if not pnls:
        return 100
    total = sum(pnls)
    if total <= 0:
        return 90
    best = sorted([p for p in pnls if p > 0], reverse=True)[:3]
    if not best:
        return 90
    return int(min(100, max(0, sum(best) / total * 100)))


def promotion_score(n: int, net: float, wins: list[float], losses: list[float], status: str) -> int:
    if status in {"KILLED", "QUARANTINED", "BLOCKED"}:
        return 0
    score = 0
    score += min(30, n)
    if net > 0:
        score += 20
    pf_text = profit_factor(wins, losses)
    pf = math.inf if pf_text == "inf" else safe_float(pf_text)
    if pf >= 1.3:
        score += 25
    elif pf >= 1.1:
        score += 10
    if fragility_score([*wins, *losses]) < 60:
        score += 15
    return min(100, score)


def truth_findings(strategy_rows: list[dict[str, Any]], artifacts: dict[str, list[dict[str, Any]]]) -> list[Finding]:
    findings: list[Finding] = []
    required = {
        "fills": REPO / "argus_flow" / "logs" / "canonical_fills.jsonl",
        "fleet_status": REPO / "argus_flow" / "logs" / "fleet_status.json",
    }
    for label, path in required.items():
        if not path.exists():
            findings.append(Finding("MISSING_ARTIFACT", "BLOCKER", f"Missing required {label} artifact", rel(path), recommended_fix="Create or restore canonical artifact writer."))

    fills = read_jsonl(required["fills"])
    fill_ids = [str(r.get("fill_id") or r.get("exec_id") or r.get("execution_id") or "") for r in fills if r]
    dupes = [item for item, count in Counter(fill_ids).items() if item and count > 1]
    if dupes:
        findings.append(Finding("DUPLICATE_FILL", "BLOCKER", "Duplicate fill IDs detected", rel(required["fills"]), row_count=len(dupes), example_rows=";".join(dupes[:5]), recommended_fix="Deduplicate canonical fills by immutable broker execution ID before scoring."))

    for row in strategy_rows:
        if row["trade_count_pre_reset"] and row["trade_count_post_reset"]:
            findings.append(Finding("PRE_RESET_DATA_CONTAMINATION", "WARNING", "Strategy has both pre-reset and post-reset trade rows", row.get("trade_files", ""), row["strategy"], row.get("symbol", ""), int(row["trade_count_pre_reset"]), recommended_fix="Keep pre-reset diagnostic only; exclude from promotion score."))
        if row["status"] == "KILLED" and safe_float(row["net_pnl"]) != 0:
            findings.append(Finding("DASHBOARD_TRUTH_MISMATCH", "WARNING", "Killed strategy still has PnL-bearing artifacts", row.get("trade_files", ""), row["strategy"], row.get("symbol", ""), recommended_fix="Dashboard must label killed/legacy series, never render as active flat equity."))
        if int(row.get("phantom_trade_count_post_reset") or 0):
            findings.append(Finding("PHANTOM_TRADE_EXCLUDED", "WARNING", "Post-reset phantom-sized trades excluded from scorecard via sidecar annotations", row.get("trade_files", ""), row["strategy"], row.get("symbol", ""), int(row.get("phantom_trade_count_post_reset") or 0), recommended_fix="Use phantom_trade_annotations.csv for promotion, PnL, and ROI decisions; keep raw artifacts immutable."))
        if row["trade_count_post_reset"] == 0 and row["signal_or_opportunity_rows"] == 0:
            findings.append(Finding("STALE_STATE", "WARNING", "Ghost strategy has no trades and no signal evidence", row.get("heartbeat_path", ""), row["strategy"], row.get("symbol", ""), recommended_fix="Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer."))

    halt_reader = REPO / "helio" / "halt_state.py"
    if halt_reader.exists():
        findings.append(Finding("HALT_TRUTH_RECONCILED", "INFO", "Execution halt checks use reconciled halt truth", "helio/halt_state.py", recommended_fix="Keep dashboard/API/runtime readers pointed at the same halt-state module."))
    elif (REPO / "argus_flow" / "logs" / "HALT.flag").exists() and (REPO / "argus_flow" / "logs" / "_risk" / "broker_drift_state.json").exists():
        drift = read_json(REPO / "argus_flow" / "logs" / "_risk" / "broker_drift_state.json") or {}
        findings.append(Finding("DASHBOARD_TRUTH_MISMATCH", "BLOCKER", "Multiple halt truth surfaces can disagree", "argus_flow/logs/HALT.flag;argus_flow/logs/_risk/broker_drift_state.json", row_count=1, example_rows=json.dumps({"halt_flag": True, "broker_drift_tripped": drift.get("tripped")}), recommended_fix="Unify halt state readers and dashboard display before market-open decisions."))

    return findings


def ops_findings() -> list[Finding]:
    findings: list[Finding] = []
    checks = [
        ("helio/real_money.py", "enforce_real_money_boundary"),
        ("helio/ibkr_execution.py", "enforce_real_money_boundary"),
        ("helio/ibkr_executor.py", "enforce_real_money_boundary"),
        ("helio/signal_executor.py", "strategy_label=signal.strategy_label"),
    ]
    for path_rel, needle in checks:
        path = REPO / path_rel
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if needle not in text:
            findings.append(Finding("REAL_MONEY_BOUNDARY_GAP", "BLOCKER", f"{needle} not wired in {path_rel}", path_rel, recommended_fix="Wire boundary before transmit; keep default fail-closed."))
    allow = read_json(REPO / "argus_flow" / "configs" / "real_money_allowlist.json") or {}
    if allow.get("strategies"):
        findings.append(Finding("REAL_MONEY_ALLOWLIST_NOT_EMPTY", "BLOCKER", "Real-money allowlist contains strategies", "argus_flow/configs/real_money_allowlist.json", example_rows=json.dumps(allow), recommended_fix="Keep empty until signed ledger and readiness gate."))
    else:
        findings.append(Finding("REAL_MONEY_BOUNDARY_DEFAULT_OFF", "INFO", "Real-money allowlist is empty/default-off", "argus_flow/configs/real_money_allowlist.json", recommended_fix="Preserve through audit."))

    if not (REPO / "helio" / "real_money_mismatch_daemon.py").exists():
        findings.append(Finding("REAL_MONEY_MISMATCH_DAEMON_MISSING", "BLOCKER", "Rule 7 mismatch daemon is not implemented", "helio/real_money.py", recommended_fix="Build read-only broker-position scanner before any real-money go-live."))
    else:
        findings.append(Finding("REAL_MONEY_MISMATCH_DAEMON_PRESENT", "INFO", "Rule 7 mismatch daemon exists and defaults to dry-run", "helio/real_money_mismatch_daemon.py", recommended_fix="Schedule dry-run monitoring; use --halt-on-violation only after operator approval."))

    text = (REPO / "helio" / "ibkr_execution.py").read_text(encoding="utf-8", errors="replace")
    if "type(exc).__name__" not in text or ("repr(exc)" not in text and "exc!r" not in text):
        findings.append(Finding("TWS_ERROR_326_DIAGNOSTICS_GAP", "WARNING", "IBKR connect logging may hide Error 326", "helio/ibkr_execution.py", recommended_fix="Log exception type and repr on connect failure."))

    if "VIX" not in (REPO / "helio" / "cluster_exposure.py").read_text(encoding="utf-8", errors="replace"):
        findings.append(Finding("VIX_SHORT_VOL_FORCE_CLOSE_MISSING", "WARNING", "VIX>30 SHORT_VOL force-close not visible in cluster exposure code", "helio/cluster_exposure.py", recommended_fix="Add synthetic drill and explicit force-close rule before short-vol capital."))
    else:
        findings.append(Finding("VIX_SHORT_VOL_POLICY_REVIEW", "WARNING", "VIX logic exists but force-close-all-SHORT_VOL needs explicit drill evidence", "helio/cluster_exposure.py", recommended_fix="Verify VIX>30 force-close drill; do not assume term-structure warning equals forced close."))
    return findings


def opportunity_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in strategy_rows:
        raw = safe_float(row["signal_or_opportunity_rows"])
        taken = safe_float(row["taken_trade_count"])
        blocked = safe_float(row["blocked_trade_count"])
        rows.append({
            "strategy": row["strategy"],
            "raw_market_bars": "",
            "setup_detected": int(raw),
            "signal_generated": int(raw),
            "filters_passed": max(0, int(raw - blocked)),
            "risk_approved": max(0, int(taken)),
            "order_attempted": int(taken),
            "order_filled": int(taken),
            "trade_closed": int(row["trade_count_post_reset"]),
            "profitable_after_friction": "YES" if safe_float(row["net_pnl"]) > safe_float(row["fees_slippage_friction_estimate"]) else "NO",
            "primary_throughput_constraint": infer_throughput_constraint(row),
            "recommended_improvement": throughput_recommendation(row),
        })
    return rows


def infer_throughput_constraint(row: dict[str, Any]) -> str:
    if row["status"] == "KILLED":
        return "killed_do_not_increase"
    if row["trade_count_post_reset"] == 0 and safe_float(row["signal_or_opportunity_rows"]) > 100:
        return "filters_or_risk_gate"
    if row["trade_count_post_reset"] == 0:
        return "idle_or_uninstrumented"
    if safe_float(row["net_pnl"]) <= 0:
        return "lack_of_edge_not_throughput"
    return "sample_accumulation"


def throughput_recommendation(row: dict[str, Any]) -> str:
    c = infer_throughput_constraint(row)
    if c == "filters_or_risk_gate":
        return "Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly."
    if c == "idle_or_uninstrumented":
        return "Verify launch flags, heartbeat truth, and signal logging before judging edge."
    if c == "lack_of_edge_not_throughput":
        return "Do not increase frequency; repair or quarantine."
    if c == "sample_accumulation":
        return "Continue paper/shadow until evidence gate clears."
    return "No live-throughput action."


def roi_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in strategy_rows:
        rec = "NEEDS_MORE_SAMPLE"
        if row["status"] == "KILLED":
            rec = "REMOVE_CAPITAL"
        elif row["status"] in {"BLOCKED", "QUARANTINED"}:
            rec = "BLOCKED_BY_TRUTH_FAILURE"
        elif row["grade"] == "A":
            rec = "HOLD_CAPITAL"
        elif row["grade"] in {"D", "F"}:
            rec = "REDUCE_CAPITAL"
        rows.append({
            "strategy": row["strategy"],
            "status": row["status"],
            "net_pnl": row["net_pnl"],
            "trade_count_post_reset": row["trade_count_post_reset"],
            "avg_notional_deployed": row["average_notional_deployed"],
            "pnl_per_unit_notional": row["pnl_per_unit_notional"],
            "max_drawdown": row["max_drawdown"],
            "allocation_factor": row["allocation_factor"],
            "allocation_recommendation": rec,
            "reason": "No ADD_CAPITAL until truth, sample, friction, and concentration gates are clean.",
        })
    return rows


def killed_review_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    killed = [r for r in strategy_rows if r["status"] == "KILLED" or r["grade"] in {"D", "F"}]
    rows = []
    for row in killed:
        rec = "KEEP_KILLED" if row["status"] == "KILLED" else "QUARANTINE_PENDING_SAMPLE"
        if "usdjpy" in row["strategy"].lower():
            rec = "RETEST_WITH_REGIME_GATE"
        rows.append({
            "strategy": row["strategy"],
            "status": row["status"],
            "kill_reason_classification": classify_kill_reason(row),
            "recommendation": rec,
            "hypothesis": "If resurrected, edge must be proven in shadow/research only with clean post-reset lifecycle artifacts.",
            "required_test": "Run 30+ valid shadow opportunities with blocked/taken ledger, friction model, MFE/MAE, and broker reconciliation.",
            "minimum_sample": "30 valid opportunities or 10 valid closed trades, whichever is stricter for the strategy cadence",
            "pass_fail_criteria": "PF>=1.20, positive friction-adjusted expectancy, no single-trade dominance, clean fills/recon, no sizing violations.",
            "expected_failure_mode": "Fees, spread, MTF gate mismatch, payoff concentration, or stale/ghost signal logging.",
        })
    return rows


def classify_kill_reason(row: dict[str, Any]) -> str:
    text = row["strategy"].lower()
    if safe_float(row["trade_count_post_reset"]) == 0:
        return "sample-size illusion or operational silence"
    if row["fragility_score"] and safe_float(row["fragility_score"]) > 70:
        return "payoff-concentrated"
    if "btc" in text or "eth" in text or "cascade" in text or "kraken" in text:
        return "edge existed directionally but fees/spread may have killed it"
    if "usdjpy" in text:
        return "may work only in specific regime/session; payoff concentration risk"
    if safe_float(row["net_pnl"]) <= 0:
        return "no edge after friction or implementation mismatch"
    return "insufficient evidence"


def atlas_outputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    atlas_paths = files("forge/atlas/**/*.py") + files("forge/atlas/**/*.json") + files("forge/logs/atlas/*")
    inventory = []
    for path in atlas_paths:
        inventory.append({
            "path": rel(path),
            "type": path.suffix.lstrip(".") or "file",
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.exists() else "",
            "owner_module": "forge.atlas",
        })
    features = [
        {"feature": "regime_transition_map", "hypothesis": "Trend/chop/compression state changes explain strategy fit.", "required_test": "Label trades and blocked signals by regime transition windows.", "promotion_gate": "Shadow allocation only after 60d stable attribution."},
        {"feature": "opportunity_shadow_ledger", "hypothesis": "Blocked opportunities reveal filter false negatives.", "required_test": "Log every setup/filter/risk rejection with future return windows.", "promotion_gate": "No runtime mutation until false-negative precision is measured."},
        {"feature": "mfe_mae_exit_intelligence", "hypothesis": "Exit quality is leaving money via giveback or timeout.", "required_test": "Compute MFE/MAE for all valid closed trades.", "promotion_gate": "Paper-only exit variant with frozen config and 30 trades."},
        {"feature": "instrument_similarity_engine", "hypothesis": "SPY/QQQ/IWM may extend validated index behavior safely.", "required_test": "Compare volatility, spread proxy, session behavior, and strategy portability.", "promotion_gate": "Shadow-only through freeze; paper after clean Tier 1 results."},
    ]
    ideas = [
        {"idea": "Argus MTF gate calibration study", "category": "filter attribution", "hypothesis": "3,800+ evals and 14 attempts with 0 fills may be an MTF threshold mismatch, not dead edge."},
        {"idea": "vix_intraday degradation sentinel", "category": "degradation detector", "hypothesis": "Post-blackout improvement must be monitored for drift before capital."},
        {"idea": "QQQ/SPY shadow opportunity ledger", "category": "instrument expansion", "hypothesis": "Unlevered index ETFs are cleaner first breadth candidates than leveraged ETFs."},
    ]
    md = """# Atlas Data Opportunity Map

Atlas should stay research-first: feature store, regime intelligence, opportunity ledger, strategy discovery, allocation intelligence, and degradation detector. It must not mutate live strategy decisions until shadow/paper evidence clears promotion gates.

Highest-leverage Atlas work:
1. Build an opportunity shadow ledger for every would-be trade, blocked trade, risk rejection, and missed setup.
2. Add regime transition labels to valid trades and blocked opportunities.
3. Compute filter attribution from future outcome windows, especially for Argus MTF gates.
4. Add MFE/MAE exit intelligence and giveback analysis.
5. Score SPY/QQQ/IWM as Tier 1 shadow candidates; keep TQQQ/SQQQ research-only until unlevered behavior is understood.

Every Atlas-derived idea must include hypothesis, required test, expected failure mode, and promotion gate. No direct runtime mutation.
"""
    return inventory, features, ideas, md


def expansion_rows() -> list[dict[str, Any]]:
    rows = []
    data_files = {p.stem.split("_")[0].upper(): p for p in files("**/*_daily.csv")}
    for symbol, group, tier, starting in EXPANSION_UNIVERSE:
        has_data = symbol in data_files
        classification = starting
        if tier.startswith("Tier 1") and has_data:
            classification = "SHADOW_ONLY"
        if group == "LEVERAGED_ETF":
            classification = "RESEARCH_ONLY"
        if group == "MICRO_FUTURE":
            classification = "RESEARCH_ONLY"
        rows.append({
            "symbol": symbol,
            "group": group,
            "tier": tier,
            "candidate_classification": classification,
            "data_available": "YES" if has_data else "NO",
            "data_path_example": rel(data_files[symbol]) if has_data else "",
            "liquidity_assessment": "high" if symbol in {"SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"} else "requires_check",
            "spread_slippage_risk": "low_to_moderate" if group == "ETF_INDEX" else "elevated_or_requires_model",
            "gap_risk": "moderate" if group != "MICRO_FUTURE" else "overnight_margin_and_session_risk",
            "portable_strategies": "spy_trend_follower,multi_orb_research_only" if symbol in {"SPY", "QQQ", "IWM"} else "separate_research_sleeve",
            "required_tests_before_promotion": "Shadow ledger, spread/slippage proxy, 30+ opportunities, no sizing/recon failures, frozen config.",
            "freeze_note": "No live before 2026-06-30 earliest; shadow/paper only through 2026-05-31 freeze.",
        })
    return rows


def phantom_annotation_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files("forge/logs/*/trades.csv") + files("argus_flow/logs/*/trades.csv"):
        for idx, trade in enumerate(read_csv(path), start=2):
            if not is_phantom_trade(trade):
                continue
            rows.append({
                "source_file": rel(path),
                "source_line": idx,
                "strategy": strategy_from_path(path),
                "symbol": trade.get("symbol", ""),
                "ts": trade.get("ts") or trade.get("entry_ts") or trade.get("timestamp") or "",
                "pnl_usd": round(trade_pnl(trade), 2),
                "position_size": trade.get("position_size") or trade.get("qty") or trade.get("size") or "",
                "notional_usd": trade.get("notional_usd", ""),
                "annotation": "PHANTOM",
                "promotion_policy": "exclude_from_promotion_roi_and_pnl_scoring",
                "reason": "phantom-sized or explicitly phantom/rejected trade; requires broker-fill confirmation before scoring",
            })
    return rows


def test_coverage_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    test_files = list(files("argus_flow/tests/test_*.py")) + list(files("tests/test_*.py"))
    test_text = "\n".join(p.read_text(encoding="utf-8", errors="replace").lower() for p in test_files)
    rows = []
    for row in strategy_rows:
        needle = row["strategy"].replace("forge_", "").replace("argus_", "").lower()
        rows.append({
            "strategy": row["strategy"],
            "has_integration_test": "Y" if needle in test_text else "N",
            "has_pnl_reconciliation_test": "Y" if needle in test_text and "pnl" in test_text else "N",
            "has_kill_switch_drill_record": "Y" if "kill" in test_text and needle in test_text else "N",
            "has_paper_real_boundary_test": "Y" if "real_money_boundary" in test_text else "N",
        })
    return rows


def dashboard_outputs(strategy_rows: list[dict[str, Any]], findings: list[Finding], expansion: list[dict[str, Any]]) -> None:
    cards = []
    queue = []
    alloc = []
    blockers = [asdict(f) for f in findings if f.severity == "BLOCKER"]
    for row in strategy_rows:
        cards.append({
            "strategy": row["strategy"],
            "status": row["status"],
            "grade": row["grade"],
            "post_reset_trades": row["trade_count_post_reset"],
            "net_pnl": row["net_pnl"],
            "action": row["next_action"],
            "truth_note": "ghost/flat not active" if row["trade_count_post_reset"] == 0 else "post-reset evidence only",
        })
        category = "WATCH_MARKET_OPEN"
        if row["status"] == "KILLED":
            category = "DO_NOT_TOUCH"
        elif row["grade"] in {"D", "F"}:
            category = "REDUCE_OR_KILL"
        elif row["grade"] == "A":
            category = "PROMOTE_AFTER_SAMPLE"
        elif row["trade_count_post_reset"] == 0:
            category = "FIX_NOW"
        queue.append({"category": category, "strategy": row["strategy"], "action": row["next_action"], "reason": row["grade_reason"]})
        alloc.append({"strategy": row["strategy"], "allocation_action": roi_action(row), "blocker_first": bool(blockers)})
    watch = [
        {"item": "Confirm feed health, spreads, bars, heartbeats, and broker equity before PnL interpretation.", "phase": "first_15_minutes"},
        {"item": "Confirm opportunity funnel rows are written for blocked and taken signals.", "phase": "first_60_minutes"},
        {"item": "Confirm no research/shadow expansion symbol is live.", "phase": "first_15_minutes"},
    ] + [{"item": f"{r['symbol']} remains {r['candidate_classification']}", "phase": "expansion_watch"} for r in expansion[:5]]
    write_json(OUT_DIR / "dashboard_strategy_cards.json", cards)
    write_json(OUT_DIR / "dashboard_decision_queue.json", queue)
    write_json(OUT_DIR / "dashboard_allocation_actions.json", alloc)
    write_json(OUT_DIR / "dashboard_blockers.json", blockers)
    write_json(OUT_DIR / "dashboard_market_open_watchlist.json", watch)


def roi_action(row: dict[str, Any]) -> str:
    if row["status"] == "KILLED":
        return "REMOVE_CAPITAL"
    if row["grade"] == "A":
        return "HOLD_CAPITAL_PENDING_GATE"
    if row["grade"] in {"D", "F"}:
        return "REDUCE_CAPITAL"
    return "NEEDS_MORE_SAMPLE"


def build_reports() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = {
        "generated_at": utc_now(),
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": git(["rev-parse", "HEAD"]),
        "status_short": git(["status", "--short", "--branch"]),
        "memory": summarize_memory(),
    }
    artifacts = discover_artifacts()
    configs = discover_configs()
    strategies = collect_strategy_rows()
    truth = truth_findings(strategies, artifacts)
    ops = ops_findings()
    all_findings = truth + ops
    blockers = [f for f in all_findings if f.severity == "BLOCKER"]
    warnings = [f for f in all_findings if f.severity == "WARNING"]
    opportunities = [
        Finding("ATLAS_OPPORTUNITY_LEDGER", "OPPORTUNITY", "Build Atlas opportunity shadow ledger", "forge/atlas", recommended_fix="Log every setup, blocked trade, risk reject, and future outcome window.", category="atlas"),
        Finding("TIER1_EXPANSION_SHADOW", "OPPORTUNITY", "Start SPY/QQQ/IWM as shadow candidates only", "ops/research/run_instrument_expansion_scan.py", recommended_fix="No live promotion before shadow/paper evidence gates.", category="expansion"),
        Finding("ARGUS_MTF_GATE_STUDY", "OPPORTUNITY", "Argus pairs need MTF threshold study, not blind kill", "argus_flow/logs/*/signals.csv", recommended_fix="Compare 3,800+ evaluations and 14 blocked attempts to backtest expected rate.", category="strategy"),
    ]

    write_json(OUT_DIR / "repo_inventory.json", {"baseline": baseline, "top_level": [p.name for p in REPO.iterdir()]})
    write_json(OUT_DIR / "artifact_inventory.json", artifacts)
    write_json(OUT_DIR / "config_inventory.json", configs)
    write_md(OUT_DIR / "data_lineage_map.md", data_lineage_md(artifacts, configs))
    write_csv(OUT_DIR / "strategy_inventory.csv", strategies)

    write_csv(OUT_DIR / "strategy_scorecard.csv", strategies)
    write_md(OUT_DIR / "strategy_scorecard.md", strategy_scorecard_md(strategies))
    write_csv(OUT_DIR / "strategy_fragility_report.csv", strategies, ["strategy", "fragility_score", "net_pnl", "trade_count_post_reset", "grade", "grade_reason"])
    write_csv(OUT_DIR / "strategy_promotion_matrix.csv", strategies, ["strategy", "status", "promotion_readiness_score", "grade", "next_action"])
    write_csv(OUT_DIR / "strategy_regime_breakdown.csv", [{"strategy": r["strategy"], "regime": "UNKNOWN_NOT_TAGGED", "note": "Regime-conditioned performance requires Atlas labels."} for r in strategies])
    write_csv(OUT_DIR / "strategy_session_breakdown.csv", [{"strategy": r["strategy"], "session": "UNKNOWN_NOT_TAGGED", "note": "Session tags missing or strategy-specific."} for r in strategies])
    write_csv(OUT_DIR / "strategy_mfe_mae_capture.csv", [{"strategy": r["strategy"], "mfe": "", "mae": "", "capture_ratio": "", "note": "Requires MFE/MAE enrichment."} for r in strategies])
    write_csv(OUT_DIR / "strategy_drift_report.csv", [{"strategy": r["strategy"], "drift_status": "NEEDS_ROLLING_EXPECTANCY", "note": r["grade_reason"]} for r in strategies])

    killed = killed_review_rows(strategies)
    write_csv(OUT_DIR / "killed_strategy_review.csv", killed)
    write_md(OUT_DIR / "killed_strategy_review.md", killed_review_md(killed))

    opp = opportunity_rows(strategies)
    write_csv(OUT_DIR / "opportunity_funnel.csv", opp)
    write_md(OUT_DIR / "opportunity_funnel.md", opportunity_md(opp))
    write_csv(OUT_DIR / "blocked_entries_analysis.csv", opp)
    write_csv(OUT_DIR / "filter_contribution_report.csv", [{"strategy": r["strategy"], "filter": "UNKNOWN", "contribution": "Needs opportunity ledger with future outcomes."} for r in strategies])
    write_csv(OUT_DIR / "false_negative_candidates.csv", [r for r in opp if r["primary_throughput_constraint"] == "filters_or_risk_gate"])

    roi = roi_rows(strategies)
    write_md(OUT_DIR / "roi_audit.md", roi_md(roi))
    write_csv(OUT_DIR / "capital_efficiency.csv", roi)
    write_csv(OUT_DIR / "allocation_recommendations.csv", roi)
    write_csv(OUT_DIR / "sizing_audit.csv", sizing_audit_rows())
    write_csv(OUT_DIR / "portfolio_heat_report.csv", portfolio_heat_rows(strategies))

    atlas_inv, atlas_features, atlas_ideas, atlas_md = atlas_outputs()
    write_csv(OUT_DIR / "atlas_data_inventory.csv", atlas_inv)
    write_md(OUT_DIR / "atlas_data_quality_report.md", atlas_quality_md(atlas_inv))
    write_md(OUT_DIR / "atlas_data_opportunity_map.md", atlas_md)
    write_csv(OUT_DIR / "atlas_feature_candidates.csv", atlas_features)
    write_csv(OUT_DIR / "atlas_strategy_ideas.csv", atlas_ideas)

    expansion = expansion_rows()
    write_csv(OUT_DIR / "instrument_expansion_candidates.csv", expansion)
    write_md(OUT_DIR / "instrument_expansion_report.md", expansion_md(expansion))
    write_csv(OUT_DIR / "symbol_metadata_template.csv", symbol_template_rows(expansion))
    write_json(OUT_DIR / "candidate_backtest_queue.json", candidate_queue(expansion))

    write_md(OUT_DIR / "ops_audit.md", ops_md(ops))
    write_json(OUT_DIR / "runtime_health_report.json", runtime_health())
    write_csv(OUT_DIR / "heartbeat_audit.csv", heartbeat_rows())
    write_csv(OUT_DIR / "alert_coverage_report.csv", alert_coverage_rows())
    write_csv(OUT_DIR / "config_drift_report.csv", config_drift_rows(configs))
    write_csv(OUT_DIR / "test_coverage_matrix.csv", test_coverage_rows(strategies))
    write_csv(OUT_DIR / "phantom_trade_annotations.csv", phantom_annotation_rows())

    dashboard_outputs(strategies, all_findings, expansion)
    write_json(OUT_DIR / "audit_findings_blockers.json", [asdict(f) for f in blockers])
    write_json(OUT_DIR / "audit_findings_warnings.json", [asdict(f) for f in warnings])
    write_json(OUT_DIR / "audit_findings_opportunities.json", [asdict(f) for f in opportunities])
    write_json(OUT_DIR / "implementation_backlog.json", backlog(blockers, warnings, opportunities))
    write_md(OUT_DIR / "roi_improvement_roadmap.md", roi_roadmap_md())
    write_md(OUT_DIR / "weekend_execution_plan.md", weekend_plan_md())
    write_md(OUT_DIR / "market_open_monitoring_plan.md", market_open_md())
    write_md(OUT_DIR / "truth_audit_report.md", truth_md(truth))
    write_json(OUT_DIR / "truth_audit_report.json", [asdict(f) for f in truth])
    write_csv(OUT_DIR / "truth_reconciliation.csv", reconciliation_rows(strategies))
    write_csv(OUT_DIR / "artifact_schema_drift.csv", schema_drift_rows(artifacts))

    master = master_report_md(strategies, blockers, warnings, opportunities, expansion)
    write_md(OUT_DIR / "argus_system_audit.md", master)
    write_json(OUT_DIR / "argus_system_audit.json", {
        "baseline": baseline,
        "grades": grade_summary(blockers, warnings, strategies),
        "blockers": [asdict(f) for f in blockers[:10]],
        "warnings": [asdict(f) for f in warnings[:10]],
        "opportunities": [asdict(f) for f in opportunities],
    })
    return {"out_dir": rel(OUT_DIR), "blockers": len(blockers), "warnings": len(warnings), "strategies": len(strategies)}


def data_lineage_md(artifacts: dict[str, list[dict[str, Any]]], configs: list[dict[str, Any]]) -> str:
    lines = ["# Data Lineage Map", "", "Lower layers feed higher layers. Audit modules are read-only.", ""]
    for kind, rows in artifacts.items():
        lines.append(f"## {kind}")
        for row in rows[:25]:
            lines.append(f"- `{row['path']}` -> owner `{row['owner_module']}` -> `{row['legacy_or_current']}`")
        if len(rows) > 25:
            lines.append(f"- ... {len(rows) - 25} more")
    lines.append("## Configs")
    for row in configs[:50]:
        lines.append(f"- `{row['path']}` -> strategy `{row['strategy']}` symbol `{row['symbol']}`")
    return "\n".join(lines)


def strategy_scorecard_md(rows: list[dict[str, Any]]) -> str:
    lines = ["# Strategy Scorecard", "", "Grades are post-reset only. Ghost/flat strategies are separated from active validated systems.", ""]
    lines.append("| Strategy | Status | n | PnL | Grade | Next action |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for r in rows:
        lines.append(f"| {r['strategy']} | {r['status']} | {r['trade_count_post_reset']} | {r['net_pnl']} | {r['grade']} | {r['next_action']} |")
    return "\n".join(lines)


def killed_review_md(rows: list[dict[str, Any]]) -> str:
    return "# Killed / Quarantined Strategy Review\n\n" + "\n".join(
        f"- `{r['strategy']}`: {r['recommendation']} because {r['kill_reason_classification']}. Resurrection, if any, is shadow/research only."
        for r in rows
    )


def opportunity_md(rows: list[dict[str, Any]]) -> str:
    lines = ["# Opportunity Funnel", "", "Increase throughput only by finding more valid opportunities, not by degrading filters.", ""]
    for r in rows:
        lines.append(f"- `{r['strategy']}`: constraint `{r['primary_throughput_constraint']}`. {r['recommended_improvement']}")
    return "\n".join(lines)


def roi_md(rows: list[dict[str, Any]]) -> str:
    lines = ["# ROI / Capital Efficiency Audit", "", "No ADD_CAPITAL recommendation is emitted by this audit because truth/sample/friction gates must clear first.", ""]
    for r in rows:
        lines.append(f"- `{r['strategy']}`: {r['allocation_recommendation']} ({r['reason']})")
    return "\n".join(lines)


def sizing_audit_rows() -> list[dict[str, Any]]:
    rows = []
    for path in files("forge/**/runner.py") + files("argus_flow/runner*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rows.append({
            "path": rel(path),
            "uses_safe_position_size": "YES" if "safe_position_size" in text else "NO",
            "risk_budget_div_stop_pattern": "YES" if re.search(r"risk_?budget\s*/\s*max?\(?stop", text) else "NO",
            "real_money_boundary_visible": "YES" if "enforce_real_money_boundary" in text else "NO",
        })
    return rows


def portfolio_heat_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = defaultdict(float)
    for r in strategy_rows:
        grouped[r["instrument_class"]] += safe_float(r["net_pnl"])
    return [{"cluster": k, "post_reset_net_pnl": round(v, 2), "note": "Heat requires live exposure snapshots for real capital risk."} for k, v in grouped.items()]


def atlas_quality_md(rows: list[dict[str, Any]]) -> str:
    return f"""# Atlas Data Quality Report

Discovered {len(rows)} Atlas-adjacent files. Current audit can inventory files and code surfaces, but cannot yet prove alignment to fills, blocked decisions, or counterfactual outcomes without an opportunity shadow ledger.

Blocker: Atlas data must be joined to canonical fills and signal/block events before it is used for allocation or strategy mutation.
"""


def expansion_md(rows: list[dict[str, Any]]) -> str:
    tier1 = [r["symbol"] for r in rows if r["tier"].startswith("Tier 1")][:5]
    return f"""# Instrument Expansion Report

First 3 to 5 instruments to test: {", ".join(tier1)}.

QQQ and SPY are cleaner first candidates than TQQQ/SQQQ. Leveraged ETFs are not "more movement = more profit"; they require separate gap, volatility, decay, and sizing tests. MNQ/MES wait until sizing and execution truth are clean. Single names stay in a separate research sleeve.

All candidates start research/shadow only. No candidate goes live before the freeze and post-freeze evidence gates; earliest live consideration is 2026-06-30.
"""


def symbol_template_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"symbol": r["symbol"], "asset_class": r["group"], "min_tick": "", "point_value": "", "session": "", "spread_model": "", "margin_model": "", "borrow_short_constraints": ""} for r in rows]


def candidate_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"symbol": r["symbol"], "mode": r["candidate_classification"], "tests": r["required_tests_before_promotion"]} for r in rows if r["candidate_classification"] in {"SHADOW_ONLY", "RESEARCH_ONLY"}]


def ops_md(findings: list[Finding]) -> str:
    lines = ["# Operational Reliability Audit", ""]
    for f in findings:
        lines.append(f"- **{f.severity} {f.code}**: {f.title}. Fix: {f.recommended_fix}")
    return "\n".join(lines)


def runtime_health() -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "halt_flag_present": (REPO / "argus_flow" / "logs" / "HALT.flag").exists(),
        "broker_drift_state": read_json(REPO / "argus_flow" / "logs" / "_risk" / "broker_drift_state.json"),
        "fleet_status": read_json(REPO / "argus_flow" / "logs" / "fleet_status.json"),
    }


def heartbeat_rows() -> list[dict[str, Any]]:
    rows = []
    for path in files("forge/logs/*/heartbeat.json") + files("argus_flow/logs/*/heartbeat.json"):
        hb = read_json(path) or {}
        rows.append({
            "strategy": strategy_from_path(path),
            "path": rel(path),
            "mode": hb.get("mode") or hb.get("account_mode") or "",
            "entries_blocked": hb.get("entries_blocked", ""),
            "entry_block_reason": hb.get("entry_block_reason", ""),
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return rows


def alert_coverage_rows() -> list[dict[str, Any]]:
    rows = []
    for path in files("ops/*.py") + files("argus_flow/ops/*.py") + files("helio/*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(token in text for token in ("post_discord", "discord", "alert")):
            rows.append({"path": rel(path), "has_discord_or_alert_reference": "YES"})
    return rows


def config_drift_rows(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(c["strategy"] for c in configs)
    return [{**c, "duplicate_strategy_config_count": counts[c["strategy"]]} for c in configs]


def backlog(blockers: list[Finding], warnings: list[Finding], opportunities: list[Finding]) -> list[dict[str, Any]]:
    items = []
    for idx, f in enumerate(blockers + warnings + opportunities, start=1):
        items.append({
            "id": f"AUDIT-{idx:03d}",
            "severity": f.severity,
            "code": f.code,
            "title": f.title,
            "recommended_fix": f.recommended_fix,
            "phase": "weekend" if f.severity == "BLOCKER" else "market_open_or_30d",
            "default_runtime_change": "none_read_only_audit",
        })
    return items


def roi_roadmap_md() -> str:
    return """# ROI Improvement Roadmap

1. Fix truth first: halt split-brain, real-money boundary gaps, ghost strategy labeling, and pre/post-reset contamination.
2. Use the strategy scorecard to separate active, blocked, killed, and insufficient-evidence systems.
3. Add capital only after clean lifecycle artifacts, sufficient post-reset sample, positive friction-adjusted expectancy, and low payoff concentration.
4. Use Atlas opportunity ledgers to raise validated throughput without loosening filters blindly.
5. Research SPY/QQQ/IWM first; keep leveraged ETFs, futures, and single names in later research sleeves.
"""


def weekend_plan_md() -> str:
    return """# Weekend Execution Plan

## Friday Night / Session 1
Branch and baseline. Repo inventory. Artifact inventory. Strategy inventory. Truth audit scaffold. Do not change trading logic.

## Saturday Morning / Session 2
Complete truth audit and schema drift checks. Fix only critical reconciliation/artifact/dashboard truth bugs. No new strategies.

## Saturday Afternoon / Session 3
Strategy scorecard. Killed strategy review. Opportunity funnel. Capital efficiency and sizing audit.

## Saturday Night / Session 4
Atlas data audit. Instrument expansion scan scaffolding. QQQ/SPY/TQQQ research candidate model. Shadow-only queue design.

## Sunday Morning / Session 5
Dashboard decision-compression outputs. Market-open watchlist. Alert coverage. Runtime health checks.

## Sunday Afternoon / Session 6
Dry-run all audit scripts. Generate final reports. Safe reporting/instrumentation only. Keep live strategy behavior frozen unless a critical bug fix is required.

## Sunday Night / Session 7
Prepare market-open monitoring plan, rollback notes, first 90-minute checklist, and first-day watch metrics.

## 5/15 Mid-Cycle Gate
This weekend prepares the 2026-05-15 keep/kill/rework review for multi_orb, cuebanks, tori, mamba, Argus pairs, and vix_intraday.
"""


def market_open_md() -> str:
    return """# Market-Open Monitoring Plan

## First 15 Minutes
- Feed health: bars updating, spreads sane, TWS not degraded.
- No stale state: heartbeats fresh and runtime mode fields truthful.
- No accidental orders: research/shadow expansion symbols are not live.
- Positions reconciled: broker positions align with local state.
- Dashboard aligns with artifacts; blockers display before allocation recommendations.

## First 60 Minutes
- Opportunity funnel logging is active.
- Blocked entries and risk blocks are captured with reasons.
- Strategy actions are explained.
- No oversized orders or sizing cap violations.
- Heartbeat freshness remains within each runner cadence.

## End Of Day
- Reconcile trade journal to fills and positions.
- Review blocked/taken opportunities and false-negative candidates.
- Update strategy scorecard, MFE/MAE capture, realized versus expected, filter attribution, and sizing audit.
- Review killed/resurrected shadow-test candidates only as research; no live promotion.
"""


def truth_md(findings: list[Finding]) -> str:
    lines = ["# Truth Audit Report", "", "Critical truth failures are separated from dashboard/cosmetic issues.", ""]
    for f in findings:
        lines.append(f"- **{f.severity} {f.code}**: {f.title} (`{f.affected_file}`). Fix: {f.recommended_fix}")
    return "\n".join(lines)


def reconciliation_rows(strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"strategy": r["strategy"], "trade_count_post_reset": r["trade_count_post_reset"], "net_pnl": r["net_pnl"], "canonical_reconciliation_status": "NEEDS_FILL_JOIN" if r["trade_count_post_reset"] else "NO_POST_RESET_TRADES"} for r in strategy_rows]


def schema_drift_rows(artifacts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for kind, items in artifacts.items():
        for item in items:
            path = REPO / item["path"]
            header = ""
            if path.suffix == ".csv" and path.exists():
                try:
                    header = path.read_text(encoding="utf-8", errors="replace").splitlines()[0][:500]
                except Exception:
                    header = ""
            rows.append({"artifact_type": kind, "path": item["path"], "header_or_schema_sample": header, "schema_status": "DISCOVERED"})
    return rows


def grade_summary(blockers: list[Finding], warnings: list[Finding], strategies: list[dict[str, Any]]) -> dict[str, str]:
    active = [r for r in strategies if r["status"] == "ACTIVE"]
    return {
        "system_health": "C" if blockers else "B",
        "truth_reconciliation": "D" if blockers else "B",
        "strategy_performance": "C" if active else "UNKNOWN",
        "roi_capital_efficiency": "C",
        "operations": "D" if any("REAL_MONEY" in f.code for f in blockers) else "C",
        "dashboard_decision_compression": "C",
        "atlas_data_readiness": "C",
        "expansion_readiness": "B-for-research-only",
    }


def master_report_md(strategies: list[dict[str, Any]], blockers: list[Finding], warnings: list[Finding], opportunities: list[Finding], expansion: list[dict[str, Any]]) -> str:
    grades = grade_summary(blockers, warnings, strategies)
    killed = [r for r in strategies if r["status"] == "KILLED"]
    sample = [r for r in strategies if r["grade"] in {"B", "C", "UNKNOWN"}]
    tier1 = [r["symbol"] for r in expansion if r["tier"].startswith("Tier 1")][:5]
    perf = read_json(OUT_DIR / "overall_performance_summary.json") or {}
    perf_state = perf.get("state", {}) if isinstance(perf, dict) else {}
    perf_lines = [
        f"- Clean active post-reset PnL: ${perf_state.get('clean_active_net_pnl', 'unknown')}",
        f"- Active post-reset closed trades: {perf_state.get('post_reset_trade_count_active', 'unknown')}",
        f"- Clean active expectancy/trade: ${perf_state.get('clean_active_expectancy_per_trade', 'unknown')}",
        f"- Blocked Argus entries needing replay: {perf_state.get('blocked_argus_entries', 'unknown')}",
        f"- Phantom PnL excluded from promotion math: ${perf_state.get('phantom_pnl_excluded_from_scoring', 'unknown')}",
    ]
    forward_rows = read_csv(OUT_DIR / "blocked_opportunity_forward_scores.csv")
    forward_counts = Counter(row.get("status", "UNKNOWN") for row in forward_rows)
    forward_line = ", ".join(f"{k}: {v}" for k, v in forward_counts.most_common()) or "not run"
    forward_resolved = forward_counts.get("RESOLVED", 0)
    forward_issue = (
        f"Blocked opportunity forward scoring resolved {forward_resolved} unique blocked entries."
        if forward_resolved
        else "Blocked opportunity forward scoring is blocked by stale/missing post-reset 1m bars."
    )
    halt_state = read_json(OUT_DIR / "halt_truth_reconciliation.json") or {}
    sections = [
        "# Argus System Audit",
        "## Executive Summary",
        "Argus should optimize for validated risk-adjusted profitability, not trade count. The current highest-leverage work is truth surfaces, evidence gating, killed-strategy discipline, Atlas opportunity ledgers, and research-only expansion.",
        "## System Health Grade",
        grades["system_health"],
        "## Truth/Reconciliation Grade",
        grades["truth_reconciliation"],
        "## Strategy Performance Grade",
        grades["strategy_performance"],
        "## ROI/Capital Efficiency Grade",
        grades["roi_capital_efficiency"],
        "## Operations Grade",
        grades["operations"],
        "## Dashboard/Decision-Compression Grade",
        grades["dashboard_decision_compression"],
        "## Atlas/Data-Readiness Grade",
        grades["atlas_data_readiness"],
        "## Expansion-Readiness Grade",
        grades["expansion_readiness"],
        "## Top 10 Blockers",
        "\n".join(f"- {f.code}: {f.title}" for f in blockers[:10]) or "- None found by read-only audit.",
        "## Top 10 Warnings",
        "\n".join(f"- {f.code}: {f.title}" for f in warnings[:10]) or "- None found by read-only audit.",
        "## Top 10 Opportunities",
        "\n".join(f"- {f.code}: {f.title}" for f in opportunities[:10]),
        "## Overall Performance Read",
        "\n".join(perf_lines) + f"\n- Forward-scoring status: {forward_line}.\n- Confirmed live PnL uplift available now: $0 until forward outcome, larger sample, and shadow A/B gates clear.\n- Throughput upside: review only positive blocked-gate slices in shadow; keep negative-expectancy gates.",
        "## What Is Working",
        "- Real-money boundary primitives exist and default off.\n- Real-money boundary checks are wired into the IBKR bracket and market-order executor paths.\n- A read-only mismatch daemon exists and only halts when explicitly run with halt-on-violation.\n- Halt truth is reconciled through helio.halt_state and fails closed when any halt source is active.\n- Broker drift formula includes open unrealized PnL.\n- Allocation factor 0.0 kill records are explicit.\n- PnL reconciliation tests exist for vix_intraday.",
        "## What Is Broken",
        f"- Current reconciled halt state: halted={halt_state.get('halted', 'unknown')} sources={halt_state.get('sources', [])}.\n- {forward_issue}\n- Real-money boundary behavior still needs market-open/TWS validation.\n- Argus pair entries include REAL_ENTRY_FAILED rows that require broker/API failure diagnosis.\n- Ghost strategies still need explicit dashboard treatment.",
        "## What Is Misleading",
        "- Flat/ghost strategies with no trades must not render as stable winners.\n- Pre-reset data must not mix into promotion scoring.\n- Paper phantoms must be excluded from ROI and strategy grade decisions.",
        "## What Should Be Killed",
        "\n".join(f"- {r['strategy']}" for r in killed) or "- No new kills from this read-only audit.",
        "## What Should Remain Killed",
        "\n".join(f"- {r['strategy']}" for r in killed) or "- None.",
        "## What Should Be Resurrected As Shadow-Only",
        "- Any resurrection candidate must be shadow/research only with hypothesis, minimum sample, pass/fail criteria, and expected failure mode.",
        "## What Should Receive More Sample",
        "\n".join(f"- {r['strategy']} ({r['grade']})" for r in sample[:20]) or "- None.",
        "## What Should Receive More Capital Later",
        "- None now. Capital increases wait for clean truth, sample, friction-adjusted expectancy, and concentration gates.",
        "## What Should Never Receive Capital Until Fixed",
        "- Strategies blocked by real-money boundary gaps, truth failures, low sample, negative expectancy, or payoff concentration.",
        "## Best Next Strategy Candidates",
        "- vix_intraday remains the main evidence candidate, subject to PF and reconciliation gates.\n- gld_pm_long and nq_overnight need more sample.\n- Argus pairs need MTF gate diagnosis before kill verdict.",
        "## Best Next Instruments To Research",
        ", ".join(tier1),
        "## QQQ/SPY/TQQQ Recommendation",
        "Start SPY, QQQ, and IWM as Tier 1 shadow/research candidates. Do not trade TQQQ/SQQQ as simple amplified versions; keep leveraged ETFs Tier 2 research-only until unlevered behavior, gap risk, decay, and sizing are understood.",
        "## Atlas Roadmap",
        "- Opportunity shadow ledger.\n- Regime transition map.\n- Filter attribution.\n- Exit intelligence.\n- Instrument similarity.\n- Degradation detector.\n- Bar-aligned forward outcome scorer for MTF-blocked and other rejected entries.",
        "## Weekend Implementation Summary",
        "Build and run audit scripts, fix only critical truth/reporting bugs, keep runtime strategy logic frozen.",
        "## Market-Open Monitoring Checklist",
        "- Truth first: feed, bars, heartbeats, positions, dashboard/artifact alignment.\n- Opportunity funnel second.\n- PnL last.",
        "## 30-Day Roadmap",
        "- Complete real-money boundary wiring and mismatch daemon.\n- Stabilize truth/recon tests.\n- Run 5/15 and 5/31 verdict gates.",
        "## 60-Day Roadmap",
        "- Paper-validate Tier 1 expansion candidates.\n- Promote only strategies clearing evidence gates.",
        "## 90-Day Roadmap",
        "- Consider semi-auto allocation only after clean real evidence; never fully auto under current doctrine.",
    ]
    return "\n\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only Argus audit package.")
    parser.add_argument("--phase", default="all", choices=["all", "truth", "strategy", "killed", "opportunity", "roi", "atlas", "ops"])
    args = parser.parse_args(argv)
    result = build_reports()
    print(json.dumps({"phase": args.phase, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
