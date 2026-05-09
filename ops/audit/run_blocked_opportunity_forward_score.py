"""Forward-score blocked opportunities when local bar data is available.

The script is deliberately conservative: if matching bars are missing, it emits
MISSING_FORWARD_PRICE_DATA instead of estimating outcomes.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
DATA_DIR = REPO / "argus_flow" / "data"


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "")


def candidate_bar_paths(symbol: str) -> list[Path]:
    sym = normalize_symbol(symbol)
    lower = sym.lower()
    return [
        DATA_DIR / f"ibkr_{lower}_1m.csv",
        DATA_DIR / f"{lower}_1m.csv",
        DATA_DIR / f"{sym}_1m.csv",
        REPO / "argus_flow" / "replay_out" / "bars_1s.csv",
        REPO / "argus_flow" / "replay_out" / "baseline" / "bars_with_features.csv",
    ]


def load_bars(symbol: str) -> tuple[list[dict[str, Any]], str]:
    for path in candidate_bar_paths(symbol):
        if not path.exists():
            continue
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                fields = {f.lower(): f for f in (reader.fieldnames or [])}
                ts_col = fields.get("ts") or fields.get("date") or fields.get("datetime") or fields.get("time")
                open_col = fields.get("open")
                high_col = fields.get("high")
                low_col = fields.get("low")
                close_col = fields.get("close")
                if not all([ts_col, open_col, high_col, low_col, close_col]):
                    continue
                bars = []
                for row in reader:
                    ts = parse_ts(row.get(ts_col, ""))
                    if ts is None:
                        continue
                    bars.append(
                        {
                            "ts": ts,
                            "open": to_float(row.get(open_col)),
                            "high": to_float(row.get(high_col)),
                            "low": to_float(row.get(low_col)),
                            "close": to_float(row.get(close_col)),
                        }
                    )
                bars.sort(key=lambda row: row["ts"])
                if bars:
                    return bars, path.relative_to(REPO).as_posix()
        except OSError:
            continue
    return [], ""


def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in normalize_symbol(symbol) else 0.0001


def resolve(row: dict[str, str], bars: list[dict[str, Any]]) -> dict[str, Any]:
    ts = parse_ts(row.get("ts", ""))
    direction = str(row.get("direction", "")).lower()
    price = to_float(row.get("price"))
    symbol = row.get("symbol", "")
    if ts is None or direction not in {"long", "short"} or price <= 0:
        return {"resolved": False, "status": "UNSCORABLE_ROW", "reason": "missing timestamp, direction, or price"}
    entry_idx = next((idx for idx, bar in enumerate(bars) if bar["ts"] > ts), None)
    if entry_idx is None:
        last_bar_ts = bars[-1]["ts"].isoformat() if bars else ""
        return {"resolved": False, "status": "STALE_FORWARD_PRICE_DATA", "reason": f"no bars after opportunity timestamp; last_bar_ts={last_bar_ts}"}

    unit = pip_size(symbol)
    stop_pips = 30.0
    target_pips = 30.0
    timeout_min = 75
    entry = bars[entry_idx]["open"] or price
    if direction == "long":
        stop = entry - stop_pips * unit
        target = entry + target_pips * unit
    else:
        stop = entry + stop_pips * unit
        target = entry - target_pips * unit

    end_ts = ts + timedelta(minutes=timeout_min)
    mfe = 0.0
    mae = 0.0
    exit_px = bars[entry_idx]["close"]
    exit_reason = "timeout"
    for bar in bars[entry_idx:]:
        if bar["ts"] > end_ts:
            break
        if direction == "long":
            mfe = max(mfe, (bar["high"] - entry) / unit)
            mae = max(mae, (entry - bar["low"]) / unit)
            if bar["low"] <= stop:
                exit_px = stop
                exit_reason = "stop"
                break
            if bar["high"] >= target:
                exit_px = target
                exit_reason = "target"
                break
            exit_px = bar["close"]
        else:
            mfe = max(mfe, (entry - bar["low"]) / unit)
            mae = max(mae, (bar["high"] - entry) / unit)
            if bar["high"] >= stop:
                exit_px = stop
                exit_reason = "stop"
                break
            if bar["low"] <= target:
                exit_px = target
                exit_reason = "target"
                break
            exit_px = bar["close"]
    pnl_pips = (exit_px - entry) / unit if direction == "long" else (entry - exit_px) / unit
    return {
        "resolved": True,
        "status": "RESOLVED",
        "reason": "",
        "entry_px": round(entry, 6),
        "exit_px": round(exit_px, 6),
        "exit_reason": exit_reason,
        "pnl_pips": round(pnl_pips, 2),
        "mfe_pips": round(mfe, 2),
        "mae_pips": round(mae, 2),
    }


def read_opportunities() -> list[dict[str, str]]:
    path = OUT_DIR / "opportunity_shadow_ledger.csv"
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    candidates = [
        row for row in rows
        if row.get("ledger_class") == "BLOCKED" and row.get("direction") and row.get("price")
    ]
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in candidates:
        key = (
            row.get("strategy", ""),
            row.get("symbol", ""),
            row.get("ts", ""),
            row.get("action", ""),
            row.get("direction", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def write_outputs(rows: list[dict[str, Any]]) -> None:
    fields = [
        "strategy", "symbol", "ts", "action", "direction", "block_reason", "resolved", "status", "reason",
        "bar_source", "entry_px", "exit_px", "exit_reason", "pnl_pips", "mfe_pips", "mae_pips",
        "source_file", "source_line",
    ]
    with (OUT_DIR / "blocked_opportunity_forward_scores.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    by_status = Counter(row["status"] for row in rows)
    resolved = [row for row in rows if row["status"] == "RESOLVED"]
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in resolved:
        by_reason[row.get("block_reason", "")].append(row)
    summary_rows = []
    for reason, items in sorted(by_reason.items()):
        pnl = [to_float(row.get("pnl_pips")) for row in items]
        wins = [p for p in pnl if p > 0]
        summary_rows.append({
            "block_reason": reason,
            "resolved": len(items),
            "win_rate": round(len(wins) / len(items), 4) if items else "",
            "expectancy_pips": round(sum(pnl) / len(pnl), 4) if pnl else "",
            "net_pips": round(sum(pnl), 2),
            "avg_mfe_pips": round(sum(to_float(row.get("mfe_pips")) for row in items) / len(items), 2) if items else "",
            "avg_mae_pips": round(sum(to_float(row.get("mae_pips")) for row in items) / len(items), 2) if items else "",
            "recommendation": "KEEP_GATE" if pnl and sum(pnl) < 0 else "REVIEW_GATE_WITH_MORE_DATA",
        })
    with (OUT_DIR / "blocked_opportunity_forward_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        fields2 = ["block_reason", "resolved", "win_rate", "expectancy_pips", "net_pips", "avg_mfe_pips", "avg_mae_pips", "recommendation"]
        writer = csv.DictWriter(fh, fieldnames=fields2)
        writer.writeheader()
        writer.writerows(summary_rows)

    lines = [
        "# Blocked Opportunity Forward Scoring",
        "",
        "This report scores blocked entries only when local forward bars are available.",
        "Missing bar data is a truth blocker for expectancy claims, not a reason to loosen filters.",
        "",
        "## Status Counts",
    ]
    for key, value in by_status.most_common():
        lines.append(f"- {key}: {value}")
    if summary_rows:
        lines.extend(["", "## Resolved Gate Summary"])
        for row in summary_rows:
            lines.append(f"- {row['block_reason']}: expectancy {row['expectancy_pips']} pips over {row['resolved']} resolved rows -> {row['recommendation']}")
    else:
        lines.extend([
            "",
            "## Result",
            "No blocked entries were forward-scored because matching local bar data is missing or stale.",
            "Current local Argus FX bars end before the post-reset blocked opportunities.",
            "",
            "## Exact Data Refresh Commands",
            "```powershell",
            "python -m argus_flow.ops.download_ibkr_bars --symbol GBPUSD --days 14",
            "python -m argus_flow.ops.download_ibkr_bars --symbol CADJPY --days 14",
            "python -m argus_flow.ops.download_ibkr_bars --symbol USDJPY --days 14",
            "python -B -m ops.audit.run_blocked_opportunity_forward_score",
            "```",
        ])
    (OUT_DIR / "blocked_opportunity_forward_scores.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward-score blocked opportunities from local bars")
    parser.parse_args()
    opportunities = read_opportunities()
    bar_cache: dict[str, tuple[list[dict[str, Any]], str]] = {}
    scored: list[dict[str, Any]] = []
    for opp in opportunities:
        symbol = normalize_symbol(opp.get("symbol", ""))
        if symbol not in bar_cache:
            bar_cache[symbol] = load_bars(symbol)
        bars, source = bar_cache[symbol]
        base = {key: opp.get(key, "") for key in ["strategy", "symbol", "ts", "action", "direction", "block_reason", "source_file", "source_line"]}
        if not bars:
            scored.append({**base, "status": "MISSING_FORWARD_PRICE_DATA", "reason": "No local 1m bar file found", "bar_source": source})
            continue
        outcome = resolve(opp, bars)
        scored.append({**base, **outcome, "bar_source": source})
    write_outputs(scored)
    print(json.dumps({"opportunities": len(opportunities), "scored": len(scored), "resolved": sum(1 for row in scored if row["status"] == "RESOLVED")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
