"""Mamba YM-only confidence-artifact writer.

Sibling to mamba.confidence_writer. Reads only the YM=F subset of the
Mamba backtest CSV — the subset analysis found that YM carries real
edge (PF ~2.0 on strict_5 entries) while NQ is net-negative, and the
union PF of 1.00 hides this.

This artifact tracks mamba_ym separately so the scope-down decision is
a single-line flip in the runner (TICKERS=["YM=F"]) rather than a
strategy-level rewrite. If live evidence confirms the subset edge with
n>=30, the `scope_down` disposition lifts to `paper_only`.

Invoke:
    python -m forge.mamba.confidence_writer_ym
    python -m forge.mamba.confidence_writer_ym --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Default source: the existing Mamba backtest CSV, filtered to YM rows.
# An alternate source (forge/logs/mamba/backtest_trades_ym_only_60d.csv)
# exists from the YM-only re-run; fall back to the multi-ticker CSV if
# it's missing.
_LOG_DIR = _REPO / "forge" / "logs" / "mamba"
BACKTEST_CSV_YM_ONLY = _LOG_DIR / "backtest_trades_ym_only_60d.csv"
BACKTEST_CSV_MULTI = _LOG_DIR / "backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "mamba_ym.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _pick_csv() -> Path:
    if BACKTEST_CSV_YM_ONLY.exists():
        return BACKTEST_CSV_YM_ONLY
    if BACKTEST_CSV_MULTI.exists():
        return BACKTEST_CSV_MULTI
    raise FileNotFoundError(
        f"no YM backtest CSV found. Expected {BACKTEST_CSV_YM_ONLY} "
        f"or {BACKTEST_CSV_MULTI}."
    )


def build_mamba_ym_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _max_drawdown,
    )

    csv_path = _pick_csv()
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Filter to YM=F only. The YM-only CSV has only YM rows; the multi
    # CSV needs the filter applied.
    rows = [r for r in rows if r.get("ticker") == "YM=F" or csv_path == BACKTEST_CSV_YM_ONLY]

    pnls = []
    r_mults = []
    for r in rows:
        try:
            pnl = float(r.get("pnl_usd") or 0)
        except (TypeError, ValueError):
            continue
        if pnl == 0.0:
            continue
        pnls.append(pnl)
        try:
            r_mults.append(float(r["rr_achieved"]))
        except (KeyError, TypeError, ValueError):
            pass

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warning_parts = [
        f"{n} backfill, 0 live",
        "YM=F subset of Mamba backtest (NQ=F excluded — NQ is net-negative)",
        "entry_mode=strict_5",
        f"source={csv_path.name}",
    ]
    if n < 10:
        warning_parts.append(
            f"n={n} below sanity bar — confidence number suppressed"
        )
    elif sum(pnls) < 0:
        warning_parts.append("union negative")
    else:
        warning_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "mamba_ym",
        "source": f"mamba_ym_backtest_{csv_path.stem}_git_{_git_sha_short()}",
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
    dd = _max_drawdown(pnls)
    if dd is not None:
        artifact["drawdown"] = dd
    # Walk-forward needs min 2 trades per fold; with n~15 we can do 2 folds
    if n >= 8:
        wf = _walk_forward_stability(pnls, n_folds=min(4, max(2, n // 6)))
        if wf is not None:
            pfs = [f["profit_factor"] for f in wf["fold_details"]
                   if f.get("profit_factor") is not None]
            artifact["walk_forward"] = {
                "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
                "pf_per_fold": pfs, **wf,
            }
    cs = _cost_stress(pnls, cost_per_trade_usd=2.0)  # futures
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=3)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Scope-down disposition (decided 2026-04-20 after YM-only re-run).
    # YM=F subset backtests cleanly at PF ~2.0 on strict_5, unlike the
    # NQ+YM union. Extended IBKR window couldn't be validated (no
    # volume for strict_5); that's the unblock gate to paper_only.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"YM-only strict_5 subset: PF {pf:.2f} / exp "
            f"${sum(pnls)/n if n else 0:.2f} / trade on n={n}. "
            "Meets sanity bar (n>=10) but below 30-trade review gate. "
            "NQ=F subset is net-negative and should NOT be run. "
            "Unblock to paper_only when: (a) n>=30 accumulated on YM-only "
            "strict_5 backfill+live combined, OR (b) IBKR market-data "
            "subscription enables volume-aware validation on extended window."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_mamba_ym_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_mamba_ym_artifact()
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = write_mamba_ym_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
