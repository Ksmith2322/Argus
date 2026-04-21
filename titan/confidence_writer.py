"""Titan confidence-artifact writer.

Reads the latest `titan/data/backtest_results/trades_*.csv` (produced by
`python -m titan.runner --backtest` / `titan.ops.scanner`) and writes
`strategy_confidence/titan.json`.

Titan is a long-only stock/ETF scanner running across several sub-strategies
(TREND_FOLLOW, BREAKOUT, etc.) on a basket of symbols. The backtest CSV
stores `pnl_pct` only, so — like Hermes — we synthesize a dollar P&L by
treating each trade as a $1,000 nominal position. The mapping is linear so
PF, WR, and bootstrap shape are preserved regardless of assumed size.

The artifact surfaces a per-symbol bucket breakdown so a reader can see
which tickers carry the edge.

Invoke:
    python -m titan.confidence_writer
    python -m titan.confidence_writer --dry-run
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

_BACKTEST_DIR = _REPO / "titan" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "titan.json"


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


def build_titan_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    csv_path = _latest_backtest_csv()
    if csv_path is None:
        raise FileNotFoundError(
            f"no backtest trades.csv found in {_BACKTEST_DIR}. "
            f"Run `python -m titan.runner --backtest` first."
        )

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Titan CSV uses pnl_pct (percent), not pnl_usd. Synthesize $-scale
    # from a $1,000 nominal position — linear mapping preserves PF / shape.
    POSITION_USD = 1000.0
    for r in rows:
        try:
            r["_pnl_usd"] = float(r["pnl_pct"]) / 100.0 * POSITION_USD
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
        "strategy": "titan",
        "source": f"titan_backtest_{csv_path.stem}_git_{_git_sha_short()}",
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
    dd = _max_drawdown(pnls, starting_equity_usd=1000.0)
    if dd is not None:
        artifact["drawdown"] = dd
    wf = _walk_forward_stability(pnls, n_folds=4)
    if wf is not None:
        pfs = [f["profit_factor"] for f in wf["fold_details"]
               if f.get("profit_factor") is not None]
        artifact["walk_forward"] = {
            "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
            "pf_per_fold": pfs, **wf,
        }
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)  # stocks/ETFs — low commission
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-symbol bucket — which tickers carry Titan's edge.
    per_sym = _per_group_profitability(
        [{"symbol": r.get("symbol", "?"), "pnl_usd": r["_pnl_usd"]} for r in rows],
        group_key="symbol",
    )
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Scope-down disposition (decided 2026-04-20). Union is mildly
    # positive (PF 1.52, n=298) but spans four sub-strategies with
    # BREAKOUT (PF 0.58) and TRENDLINE (PF 0.37) dragging, plus shorts
    # (PF 1.13) underperforming longs (PF 1.87). A structural subset
    # (strategy=='TREND_FOLLOW' AND direction=='long') backtests
    # cleanly: PF 2.09 / WR 55.7% / exp $24.05/trade on n=174, all 4
    # walk-forward folds positive, survives 3x costs (see
    # strategy_confidence/titan_validated.json). Both filter terms are
    # known at entry -> implementable as a scanner gate.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            "Union: PF 1.52 / WR 49% on n=298, but BREAKOUT (PF 0.58) "
            "and TRENDLINE (PF 0.37) sub-strategies are net-losers and "
            "shorts (PF 1.13) underperform longs (PF 1.87). Scoped "
            "subset strategy=='TREND_FOLLOW' AND direction=='long' "
            "shows cleaner edge: PF 2.09 / WR 55.7% / exp $24.05/trade "
            "on n=174, all 4 walk-forward folds positive (see "
            "strategy_confidence/titan_validated.json). Wire the "
            "strategy+direction filter into titan.ops.scanner as a "
            "gate; unblock to paper_only when n>=30 accumulated on the "
            "scoped subset."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "next_review_date": "2026-07-20",
    }

    return artifact


def write_titan_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_titan_artifact()
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
    result = write_titan_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
