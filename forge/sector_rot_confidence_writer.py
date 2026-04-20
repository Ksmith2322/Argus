"""Sector Rotation confidence-artifact writer.

Reads cached monthly records from `forge/data/sector_rot/backtest_trades.csv`
(seeded by `python -m forge.run_macro_backtests`) and writes
`strategy_confidence/sector_rot.json`.

The Sector Rotation strategy rebalances monthly into top-3 regime-scored
sector ETFs (XLK/XLV/XLF/etc.). Each "trade" is actually a monthly
rebalance — not a per-tradelet record — so the reader must not misread
the PF as a per-trade number. The sample_warning surfaces this.

pnl_usd is synthesized as month_return * $1,000 nominal. PF and
bootstrap shape are preserved regardless of exact size.

Invoke:
    python -m forge.sector_rot_confidence_writer
    python -m forge.sector_rot_confidence_writer --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BACKTEST_CSV = _REPO / "forge" / "data" / "sector_rot" / "backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "sector_rot.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def build_sector_rot_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(
            f"missing {BACKTEST_CSV}. "
            f"Run `python -m forge.run_macro_backtests` first."
        )

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    POSITION_USD = 1000.0
    processed = []
    for r in rows:
        try:
            month_ret = float(r.get("month_return") or 0)
        except (TypeError, ValueError):
            month_ret = 0.0
        r["_pnl_usd"] = month_ret / 100.0 * POSITION_USD
        processed.append(r)

    n = len(processed)
    pnls = [r["_pnl_usd"] for r in processed]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"{n} backfill, 0 live",
        f"pnl = month_return * ${POSITION_USD:.0f} nominal",
        "monthly rebalances, not tradelets — PF is per-month, not per-trade",
    ]
    if sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "sector_rot",
        "source": f"sector_rot_backtest_csv_git_{_git_sha_short()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bt_pf": round(pf, 2) if pf != float("inf") else None,
        "bt_wr": round(wins / n, 4) if n else 0.0,
        "bt_trades": n,
        "n_total": n,
        "n_live": 0,
        "n_paper": n,
        "expectancy_usd": round(sum(pnls) / n, 4) if n else None,
        "p_expectancy_positive": _bootstrap_p_positive(pnls) if n >= 10 else None,
        "evidence_bar": _evidence_bar(n),
        "sample_warning": " | ".join(warn_parts),
    }
    mc = _monte_carlo_shuffle(pnls)
    if mc is not None:
        artifact["mc_stress"] = mc
    wf = _walk_forward_stability(pnls, n_folds=4)
    if wf is not None:
        pfs = [f["profit_factor"] for f in wf["fold_details"]
               if f.get("profit_factor") is not None]
        artifact["walk_forward"] = {
            "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
            "pf_per_fold": pfs, **wf,
        }
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=3)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-regime bucketing: surfaces whether RISK_ON / RISK_OFF / CRISIS /
    # NEUTRAL months each make money separately — the key question for a
    # regime-conditional strategy.
    per_regime = _per_group_profitability(
        [{"regime": r.get("regime", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="regime",
    )
    if per_regime is not None:
        artifact["per_instrument"] = per_regime

    return artifact


def write_sector_rot_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_sector_rot_artifact()
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
    result = write_sector_rot_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
