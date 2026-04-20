"""Hermes confidence-artifact writer.

Reads the latest `hermes/data/backtest_results/trades_*.csv` (produced by
`python -m hermes.runner --backtest`) and writes
`strategy_confidence/hermes.json`.

Hermes is a gap-fill scanner on 30 high-volatility stocks. Backtest bucket
analysis (per 2026-04-19 run) showed the edge concentrates at score 80+:
    Score 60-70: PF < 1 (losing)
    Score 70-80: break-even
    Score 80+:   PF ~2+, 63% WR — the real edge

The per-score bucket analysis is surfaced in the artifact's per-bucket
breakdown so a reader can see the subset concentration.

Invoke:
    python -m hermes.confidence_writer
    python -m hermes.confidence_writer --dry-run
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

_BACKTEST_DIR = _REPO / "hermes" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "hermes.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _latest_backtest_csv() -> Path | None:
    if not _BACKTEST_DIR.exists():
        return None
    candidates = sorted(_BACKTEST_DIR.glob("trades_*.csv"))
    return candidates[-1] if candidates else None


def _score_bucket(score_raw) -> str:
    try:
        s = float(score_raw)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 80:
        return "80+"
    if s >= 70:
        return "70-80"
    if s >= 60:
        return "60-70"
    return "<60"


def build_hermes_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    csv_path = _latest_backtest_csv()
    if csv_path is None:
        raise FileNotFoundError(
            f"no backtest trades.csv found in {_BACKTEST_DIR}. "
            f"Run `python -m hermes.runner --backtest` first."
        )

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Hermes CSV uses pnl_pct (percent), not pnl_usd. Synthesize a $-scale
    # for the bootstrap by assuming a $1,000 position per trade — the
    # percentage-to-dollar mapping is linear, so PF and bootstrap shape are
    # preserved regardless of the exact assumed position size.
    POSITION_USD = 1000.0
    for r in rows:
        try:
            r["_pnl_usd"] = float(r["pnl_pct"]) / 100.0 * POSITION_USD
            r["_score_bucket"] = _score_bucket(r.get("score"))
        except (TypeError, ValueError):
            r["_pnl_usd"] = 0.0

    rows = [r for r in rows if r["_pnl_usd"] != 0.0]
    n = len(rows)
    pnls = [r["_pnl_usd"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [f"{n} backfill, 0 live",
                  f"pnl synthesized from pnl_pct * ${POSITION_USD:.0f} nominal"]
    if sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "hermes",
        "source": f"hermes_backtest_{csv_path.stem}_git_{_git_sha_short()}",
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
    cs = _cost_stress(pnls, cost_per_trade_usd=2.0)  # lower for stocks
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Surface per-score-bucket profitability — this is Hermes's subset edge
    score_groups = _per_group_profitability(
        [{"bucket": r["_score_bucket"], "pnl_usd": r["_pnl_usd"]} for r in rows],
        group_key="bucket",
    )
    if score_groups is not None:
        artifact["per_score_bucket"] = score_groups

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return artifact


def write_hermes_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_hermes_artifact()
    # per_score_bucket is a custom field — validator allows extras on the
    # PerGroupProfitability model, but attach as generic not schema-typed
    # Copy to per_instrument field to fit schema; drop custom key
    if "per_score_bucket" in data:
        data["per_instrument"] = data.pop("per_score_bucket")
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = write_hermes_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
