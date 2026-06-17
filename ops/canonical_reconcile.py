"""ops/canonical_reconcile.py — Diff per-strategy trades.csv vs canonical_fills.jsonl.

Catches drift between authoritative canonical_fills and per-strategy trade
logs. Known failure modes this surfaces:
  - Duplicate process double-writes (spy_mean_rev 2026-04-21 — same trade
    appeared twice in canonical_fills)
  - Missing dual-writes (strategy writes trades.csv but didn't publish to
    canonical_fills — reports would undercount)
  - Schema drift (trades.csv has fields that don't map)

Runs via managed_truth_loop subprocess. Writes
`argus_flow/logs/canonical_reconcile.json`. Flags any strategy with
|count_diff| > 0. Exit code 1 if any drift found (scriptable).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"
CANONICAL_PATH = LOGS_DIR / "canonical_fills.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] reconcile | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,
)
log = logging.getLogger("canonical_reconcile")


# Map per-strategy CSV → canonical strategy name.
# Keep in sync with ops/schema_validator.py + operational_maturity.py.
STRATEGY_CSVS = {
    "argus_usdjpy":            ("argus_flow/logs/usdjpy/trades.csv",         "ts",        True,  "USDJPY"),   # valid_filter
    "argus_gbpusd":            ("argus_flow/logs/gbpusd/trades.csv",         "ts",        True,  "GBPUSD"),
    "argus_cadjpy":            ("argus_flow/logs/cadjpy/trades.csv",         "ts",        True,  "CADJPY"),
    "forge_gdx_gld":           ("forge/logs/gdx_gld/trades.csv",             "exit_date", False, None),
    "forge_gld_pm_long":       ("forge/logs/gld_pm_long/trades.csv",         "ts",        False, "GLD"),
    "forge_jpy_pm_short":      ("forge/logs/jpy_pm_short/trades.csv",        "ts",        False, None),
    "forge_nq_overnight":      ("forge/logs/nq_overnight/trades.csv",        "ts",        False, "MNQ"),
    "forge_spy_mean_rev":      ("forge/logs/spy_mean_rev/trades.csv",        "ts",        False, "SPY"),
    "forge_multi_orb":         ("forge/logs/multi_orb/trades.csv",           "ts",        False, None),
    "forge_vix_intraday":      ("forge/logs/vix_intraday/trades.csv",        "ts",        False, "VIX"),
}


def _norm_ts(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _norm_symbol(value: str | None) -> str:
    symbol = str(value or "").strip().upper()
    if symbol == "NQ":
        return "MNQ"
    return symbol


def _logical_key(strategy: str, row: dict, symbol_hint: str | None) -> tuple:
    symbol = _norm_symbol(row.get("symbol") or symbol_hint or "")
    if strategy.startswith("argus_"):
        logical_ts = row.get("exit_ts") or row.get("entry_ts")
    else:
        logical_ts = row.get("entry_ts")
    return (
        _norm_ts(logical_ts),
        symbol,
        str(row.get("direction") or ""),
        str(row.get("entry_px") or ""),
        str(row.get("exit_px") or ""),
        str(row.get("pnl_usd") or ""),
    )


def _dedup_rows(strategy: str, rows: list[dict], symbol_hint: str | None) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for row in rows:
        key = _logical_key(strategy, row, symbol_hint)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        existing_is_backfill = existing.get("source") == "backfill_from_trade_csv"
        row_is_backfill = row.get("source") == "backfill_from_trade_csv"
        if existing_is_backfill and not row_is_backfill:
            by_key[key] = row
        elif existing_is_backfill == row_is_backfill and str(row.get("ts", "")) > str(existing.get("ts", "")):
            by_key[key] = row
    return list(by_key.values())


def _duplicate_stats(strategy: str, rows: list[dict], symbol_hint: str | None) -> dict:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[_logical_key(strategy, row, symbol_hint)].append(row)

    duplicate_same_source = 0
    shadowed_backfills = 0
    for group in groups.values():
        live_rows = [r for r in group if r.get("source") != "backfill_from_trade_csv"]
        backfill_rows = [r for r in group if r.get("source") == "backfill_from_trade_csv"]
        if live_rows:
            duplicate_same_source += max(0, len(live_rows) - 1)
            shadowed_backfills += len(backfill_rows)
        else:
            duplicate_same_source += max(0, len(backfill_rows) - 1)
    return {
        "duplicate_same_source": duplicate_same_source,
        "shadowed_backfills": shadowed_backfills,
    }


def load_canonical_counts() -> dict:
    """Return dict of strategy -> {count, duplicate_count, unique_count}
    keyed from canonical_fills.jsonl EXIT records."""
    rows_by_strategy: dict = defaultdict(list)
    if not CANONICAL_PATH.exists():
        return {}
    with CANONICAL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("side") != "EXIT":
                continue
            strat = r.get("strategy", "")
            if not strat:
                continue
            rows_by_strategy[strat].append(r)
    # Flatten set → count
    out = {}
    for strategy, rows in rows_by_strategy.items():
        symbol_hint = STRATEGY_CSVS[strategy][3] if strategy in STRATEGY_CSVS else None
        deduped = _dedup_rows(strategy, rows, symbol_hint)
        duplicate_stats = _duplicate_stats(strategy, rows, symbol_hint)
        out[strategy] = {
            "total": len(deduped),
            "unique": len({_logical_key(strategy, r, symbol_hint) for r in deduped}),
            "duplicates": duplicate_stats["duplicate_same_source"],
            "shadowed_backfills": duplicate_stats["shadowed_backfills"],
            "raw_total": len(rows),
        }
    return out


def load_csv_count(csv_path: Path, ts_col: str, valid_filter: bool) -> dict:
    if not csv_path.exists():
        return {"count": 0, "exists": False}
    try:
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return {"count": 0, "exists": True, "error": "read_failed"}
    if valid_filter:
        rows = [r for r in rows if str(r.get("experiment_valid", "")).lower() == "true"]
    # Only count rows with a parseable timestamp (excludes header-only files)
    parseable = 0
    for r in rows:
        if r.get(ts_col):
            parseable += 1
    return {"count": parseable, "exists": True, "raw_rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    canon_counts = load_canonical_counts()
    results = []
    drift_detected = False
    for strategy, (csv_rel, ts_col, valid_filter, _symbol_hint) in STRATEGY_CSVS.items():
        csv_stats = load_csv_count(REPO / csv_rel, ts_col, valid_filter)
        canon = canon_counts.get(strategy, {"total": 0, "unique": 0, "duplicates": 0})
        diff = canon["total"] - csv_stats["count"]
        dup_count = canon.get("duplicates", 0)
        status = "OK"
        if dup_count > 0:
            status = "DUPLICATES_IN_CANONICAL"
            drift_detected = True
        elif diff != 0:
            status = "COUNT_MISMATCH"
            drift_detected = True
        results.append({
            "strategy": strategy,
            "csv_path": csv_rel,
            "csv_count": csv_stats["count"],
            "csv_exists": csv_stats["exists"],
            "canonical_total": canon["total"],
            "canonical_unique": canon["unique"],
            "canonical_duplicates": dup_count,
            "canonical_minus_csv_diff": diff,
            "status": status,
        })

    out = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "canonical_fills_path": str(CANONICAL_PATH.relative_to(REPO)),
        "strategies_checked": len(results),
        "drift_count": sum(1 for r in results if r["status"] != "OK"),
        "results": results,
    }
    out_path = LOGS_DIR / "canonical_reconcile.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    drifted = [r for r in results if r["status"] != "OK"]
    if drifted:
        log.warning("%d strategies show drift:", len(drifted))
        for r in drifted:
            log.warning("  %s [%s]: csv=%d canonical=%d (diff=%+d, dups=%d)",
                        r["strategy"], r["status"], r["csv_count"],
                        r["canonical_total"], r["canonical_minus_csv_diff"],
                        r["canonical_duplicates"])

        # Discord alert with per-strategy cooldown (24h — drift persists until fixed)
        try:
            sys.path.insert(0, str(REPO / "ops"))
            from _alert_helper import post_discord, load_cooldown_state, save_cooldown_state, should_alert, mark_alerted
            cooldown = load_cooldown_state("canonical_reconcile_alert_state.json")
            to_alert = [r for r in drifted if should_alert(cooldown, r["strategy"], cooldown_min=1440)]  # 24h
            if to_alert:
                lines = [f"**{r['strategy']}** [{r['status']}]: csv={r['csv_count']} canonical={r['canonical_total']} "
                         f"diff={r['canonical_minus_csv_diff']:+d} dups={r['canonical_duplicates']}"
                         for r in to_alert]
                body = "canonical_fills.jsonl drifted from per-strategy trades.csv. Known causes: duplicate-process double-writes (spy_mean_rev 04-21), missing dual-writes, schema drift.\n\n" + "\n".join(lines)
                post_discord("Canonical reconcile DRIFT", body, color=16753920)
                for r in to_alert:
                    mark_alerted(cooldown, r["strategy"], r["status"])
                save_cooldown_state("canonical_reconcile_alert_state.json", cooldown)
                log.info("Discord alert sent for %d drifted strategies", len(to_alert))
        except Exception as e:
            log.warning("Discord alert path failed (non-fatal): %s", e)
    else:
        log.info("reconcile clean: %d strategies checked, 0 drift", len(results))
        # Clear cooldown state on clean
        try:
            sys.path.insert(0, str(REPO / "ops"))
            from _alert_helper import load_cooldown_state, save_cooldown_state, post_discord
            cooldown = load_cooldown_state("canonical_reconcile_alert_state.json")
            if cooldown:
                recovered = list(cooldown.keys())
                post_discord("Canonical reconcile CLEAR", "Drift resolved on: " + ", ".join(recovered), color=65280)
                save_cooldown_state("canonical_reconcile_alert_state.json", {})
        except Exception:
            pass

    if args.verbose:
        for r in results:
            log.info("  %s: csv=%d canonical=%d unique=%d dups=%d status=%s",
                     r["strategy"], r["csv_count"], r["canonical_total"],
                     r["canonical_unique"], r["canonical_duplicates"], r["status"])

    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
