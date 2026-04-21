"""Cue Banks confidence-artifact writer.

Reads forge/logs/cuebanks/cuebanks_backtest_trades.csv, computes the
overall confidence block, and writes strategy_confidence/cue_banks.json.

The artifact's sample_warning surfaces the gap between Cue Banks' actual
teaching and the current bot implementation — the bot lacks retest
enforcement, candle-closure validation, harmonic patterns, and
supply/demand zones per the 2026-04-19 transcript audit. Any confidence
number here reflects the partial-implementation bot, not a faithful
Cue Banks system.

Invoke:
    python -m forge.cuebanks.confidence_writer            # write
    python -m forge.cuebanks.confidence_writer --dry-run  # print only
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BACKTEST_CSV = _REPO / "forge" / "logs" / "cuebanks" / "cuebanks_backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "cue_banks.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _bucket_stats(rows: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    out = {}
    for k, grp in groups.items():
        pnls = [float(r["pnl_usd"]) for r in grp if r.get("pnl_usd") not in (None, "")]
        wins = sum(1 for p in pnls if p > 0)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        out[k] = {
            "trades": len(pnls),
            "wr": wins / len(pnls) if pnls else 0.0,
            "pnl_usd": round(sum(pnls), 2),
            "pf": round(pf, 2) if pf != float("inf") else None,
        }
    return out


def build_cuebanks_artifact() -> dict:
    from helio.fleet_state import _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle, _max_drawdown

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(f"missing {BACKTEST_CSV}")

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("pnl_usd") not in (None, "")]

    n = len(rows)
    pnls = [float(r["pnl_usd"]) for r in rows]
    r_mults = [float(r["rr_achieved"]) for r in rows if r.get("rr_achieved") not in (None, "")]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    # Buckets: by direction and by exit reason (no natural instrument split
    # since Cue Banks trades YM exclusively on the current bot)
    by_direction = _bucket_stats(rows, lambda r: r.get("direction", "?"))
    by_exit = _bucket_stats(rows, lambda r: r.get("exit_reason", "?"))

    def _best(buckets: dict[str, dict]) -> tuple[str, dict] | None:
        candidates = [(k, v) for k, v in buckets.items()
                      if v["pnl_usd"] > 0 and v["pf"] is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda kv: kv[1]["pf"])

    best_dir = _best(by_direction)
    best_exit = _best(by_exit)

    warning_parts = [f"{n} backfill, 0 live"]
    # Current bot state (2026-04-20): S/D zones now integrated (v3 lift from
    # PF 0.89 → 1.34). Retest gate built (BreakTracker) but disabled default
    # because A/B showed it removes big winners. Harmonic bats built but
    # default OFF (doesn't fire often enough on H4 to matter).
    warning_parts.append(
        "v3 bot: S/D zones ON, retest-gate and harmonics OFF by default "
        "-- see CUEBANKS_RULEBOOK.md v3 notes"
    )
    if best_dir:
        warning_parts.append(
            f"best direction: {best_dir[0]} (n={best_dir[1]['trades']}, PF={best_dir[1]['pf']})"
        )
    if best_exit:
        warning_parts.append(
            f"best exit: {best_exit[0]} (n={best_exit[1]['trades']}, PF={best_exit[1]['pf']})"
        )
    if n and sum(pnls) < 0:
        warning_parts.append("union is negative -- faithful rebuild required")

    artifact = {
        "schema_version": 1,
        "strategy": "cue_banks",
        "source": f"cuebanks_backtest_trades_partial_impl_git_{_git_sha_short()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bt_pf": round(pf, 2) if pf != float("inf") else None,
        "bt_wr": round(wins / n, 4) if n else 0.0,
        "bt_trades": n,
        "n_total": n,
        "n_live": 0,
        "n_paper": n,
        "expectancy_usd": round(sum(pnls) / n, 4) if n else None,
        "expectancy_r": round(sum(r_mults) / len(r_mults), 4) if r_mults else None,
        "p_expectancy_positive": _bootstrap_p_positive(pnls) if n >= 10 else None,
        "evidence_bar": _evidence_bar(n),
        "sample_warning": " | ".join(warning_parts),
    }
    mc = _monte_carlo_shuffle(pnls)
    if mc is not None:
        artifact["mc_stress"] = mc
    dd = _max_drawdown(pnls, starting_equity_usd=1000.0)
    if dd is not None:
        artifact["drawdown"] = dd

    # Scope-down disposition (decided 2026-04-19). Union is mediocre
    # (PF 1.34 on n=88). A filter-scoped subset — trades whose `factors`
    # field contains "S/D supply zone" — carries the edge: PF 3.63 /
    # P(exp>0)=0.998 / MC ruin 0% on n=30 (see
    # strategy_confidence/cue_banks_validated.json). The demand-zone
    # cohort (n=45) is net-negative (PF 0.55, -$1,948) and drags the
    # union. Wire the supply-zone factor gate into forge.cuebanks.runner
    # and/or drop demand-zone-only entries; unblock to paper_only when
    # n>=30 accumulates on the scoped subset.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            "Union: PF 1.34 mediocre (n=88). Filter-scoped subset "
            "(factors contains 'S/D supply zone') shows real edge: "
            "PF 3.63 / P(exp>0)=0.998 / MC ruin 0% on n=30 (see "
            "strategy_confidence/cue_banks_validated.json). Demand-zone "
            "cohort (n=45) is net-negative (PF 0.55, -$1,948). Wire the "
            "supply-zone filter into forge.cuebanks.runner as a factor "
            "gate (or drop demand-zone-only entries); unblock to "
            "paper_only when n>=30 accumulated on the scoped subset."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "next_review_date": "2026-07-19",
    }
    return artifact


def write_cuebanks_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact

    data = build_cuebanks_artifact()
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the artifact instead of writing")
    args = ap.parse_args()
    result = write_cuebanks_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
