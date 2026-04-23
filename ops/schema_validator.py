"""ops/schema_validator.py — Per-strategy CSV schema sanity check.

Validates that each strategy's trades.csv + signals.csv have the columns
that downstream code expects (operational_maturity, silent_block_check,
dashboard equity curve, canonical_fills, etc.). A silent column-name
mismatch manifests as 0 trades / 0 signals parsed — masquerading as
"no data yet" — which is exactly the kind of bug the mamba `timestamp`
vs `ts` incident surfaced.

Run:
    python -m ops.schema_validator             # validates, prints summary
    python -m ops.schema_validator --strict    # exit 1 on any mismatch

Writes: argus_flow/logs/schema_validation.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"


@dataclass
class SchemaExpectation:
    strategy: str
    file_role: str  # "trades" or "signals"
    csv_path: str   # relative to repo
    ts_col: str     # timestamp column name
    pnl_col: str | None = None  # required for trades, None for signals
    required_cols: list[str] = field(default_factory=list)  # hard requirements beyond ts/pnl


# Every CSV the fleet reads downstream. Keep in sync with:
#   - ops/silent_block_check.py (signals)
#   - ops/operational_maturity.py (trades)
#   - ops/dashboard.py::api_fleet_equity_curve (trades)
EXPECTATIONS: list[SchemaExpectation] = [
    # Argus (FX): experiment_valid filter, pnl_usd column
    SchemaExpectation("argus_usdjpy", "trades", "argus_flow/logs/usdjpy/trades.csv", "ts", "pnl_usd", ["experiment_valid"]),
    SchemaExpectation("argus_gbpusd", "trades", "argus_flow/logs/gbpusd/trades.csv", "ts", "pnl_usd", ["experiment_valid"]),
    SchemaExpectation("argus_cadjpy", "trades", "argus_flow/logs/cadjpy/trades.csv", "ts", "pnl_usd", ["experiment_valid"]),
    SchemaExpectation("argus_usdjpy", "signals", "argus_flow/logs/usdjpy/signals.csv", "ts"),
    SchemaExpectation("argus_gbpusd", "signals", "argus_flow/logs/gbpusd/signals.csv", "ts"),
    SchemaExpectation("argus_cadjpy", "signals", "argus_flow/logs/cadjpy/signals.csv", "ts"),

    # Forge — most use ts/pnl_usd
    SchemaExpectation("forge_gdx_gld",       "trades", "forge/logs/gdx_gld/trades.csv",           "exit_date", "pnl_usd"),
    SchemaExpectation("forge_gld_pm_long",   "trades", "forge/logs/gld_pm_long/trades.csv",       "ts",        "pnl_usd"),
    SchemaExpectation("forge_jpy_pm_short",  "trades", "forge/logs/jpy_pm_short/trades.csv",      "ts",        "pnl_usd"),
    SchemaExpectation("forge_nq_overnight",  "trades", "forge/logs/nq_overnight/trades.csv",      "ts",        "pnl_usd"),
    SchemaExpectation("forge_spy_mean_rev",  "trades", "forge/logs/spy_mean_rev/trades.csv",      "ts",        "pnl_usd"),
    SchemaExpectation("forge_multi_orb",     "trades", "forge/logs/multi_orb/trades.csv",         "ts",        "pnl_usd"),
    SchemaExpectation("forge_vix_intraday",  "trades", "forge/logs/vix_intraday/trades.csv",      "ts",        "pnl_usd"),

    # Forge signals — all 'ts' except mamba uses 'timestamp'
    SchemaExpectation("forge_gld_pm_long",   "signals", "forge/logs/gld_pm_long/signals.csv",       "ts"),
    SchemaExpectation("forge_jpy_pm_short",  "signals", "forge/logs/jpy_pm_short/signals.csv",      "ts"),
    SchemaExpectation("forge_nq_overnight",  "signals", "forge/logs/nq_overnight/signals.csv",      "ts"),
    SchemaExpectation("forge_spy_mean_rev",  "signals", "forge/logs/spy_mean_rev/signals.csv",      "ts"),
    SchemaExpectation("forge_multi_orb",     "signals", "forge/logs/multi_orb/signals.csv",         "ts"),
    SchemaExpectation("forge_vix_intraday",  "signals", "forge/logs/vix_intraday/signals.csv",      "ts"),
    SchemaExpectation("forge_nq_london_close",    "signals", "forge/logs/nq_london_close/signals.csv",    "ts"),
    SchemaExpectation("forge_aud_asian_breakout", "signals", "forge/logs/aud_asian_breakout/signals.csv", "ts"),
    SchemaExpectation("forge_mamba",         "signals", "forge/logs/mamba/signals.csv",             "timestamp"),  # unique!
]


def validate(exp: SchemaExpectation) -> dict:
    result = {
        "strategy": exp.strategy,
        "file_role": exp.file_role,
        "csv_path": exp.csv_path,
        "status": "PASS",
        "expected_ts_col": exp.ts_col,
        "expected_pnl_col": exp.pnl_col,
        "expected_required": exp.required_cols,
        "actual_columns": [],
        "missing": [],
    }
    p = REPO / exp.csv_path
    if not p.exists():
        result["status"] = "MISSING_FILE"
        return result
    try:
        with p.open(encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, [])
    except Exception as e:
        result["status"] = "READ_ERROR"
        result["error"] = str(e)
        return result

    result["actual_columns"] = header
    missing: list[str] = []
    if exp.ts_col not in header:
        missing.append(exp.ts_col)
    if exp.pnl_col and exp.pnl_col not in header:
        missing.append(exp.pnl_col)
    for col in exp.required_cols:
        if col not in header:
            missing.append(col)
    if missing:
        result["status"] = "SCHEMA_MISMATCH"
        result["missing"] = missing
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any mismatch")
    args = parser.parse_args(argv)

    results = [validate(e) for e in EXPECTATIONS]
    fails = [r for r in results if r["status"] == "SCHEMA_MISMATCH"]
    missing = [r for r in results if r["status"] == "MISSING_FILE"]
    errors = [r for r in results if r["status"] == "READ_ERROR"]

    out = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_expectations": len(results),
        "pass_count": sum(1 for r in results if r["status"] == "PASS"),
        "schema_mismatch_count": len(fails),
        "missing_file_count": len(missing),
        "read_error_count": len(errors),
        "results": results,
    }
    out_path = LOGS_DIR / "schema_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"[schema] checked {out['total_expectations']} | PASS={out['pass_count']} "
          f"MISMATCH={out['schema_mismatch_count']} MISSING={out['missing_file_count']} ERROR={out['read_error_count']}",
          file=sys.stderr)

    if fails:
        print("\nSCHEMA MISMATCHES:", file=sys.stderr)
        for r in fails:
            print(f"  {r['strategy']} ({r['file_role']}): missing {r['missing']} in {r['csv_path']}", file=sys.stderr)
            print(f"    actual cols: {r['actual_columns']}", file=sys.stderr)

        # Discord alert with 12h cooldown per file (schema usually doesn't flicker)
        try:
            sys.path.insert(0, str(REPO / "ops"))
            from _alert_helper import post_discord, load_cooldown_state, save_cooldown_state, should_alert, mark_alerted
            cooldown = load_cooldown_state("schema_alert_state.json")
            to_alert = [r for r in fails if should_alert(cooldown, f"{r['strategy']}:{r['file_role']}", cooldown_min=720)]  # 12h
            if to_alert:
                lines = [f"**{r['strategy']}** ({r['file_role']}): missing cols {r['missing']} in `{r['csv_path']}`"
                         for r in to_alert]
                body = "CSV schema mismatches — downstream consumers (operational_maturity, silent_block_check) may silently miss trades or signals. Fix the column names or update the expectation.\n\n" + "\n".join(lines)
                post_discord("Schema MISMATCH", body, color=16753920)
                for r in to_alert:
                    key = f"{r['strategy']}:{r['file_role']}"
                    mark_alerted(cooldown, key, f"missing {r['missing']}")
                save_cooldown_state("schema_alert_state.json", cooldown)
        except Exception as e:
            print(f"Discord alert path failed (non-fatal): {e}", file=sys.stderr)

    if args.strict and (fails or errors):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
