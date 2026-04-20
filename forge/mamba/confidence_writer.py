"""Mamba confidence-artifact writer.

Reads forge/logs/mamba/backtest_trades.csv, computes the overall
confidence block, and writes strategy_confidence/mamba.json. The
current backtest synthesizes 1-minute bars from 5-minute data
(see project_mamba_tori_review_20260419), so the confidence
number must carry that caveat in `sample_warning`.

The subset edge — YM PF 1.98 vs NQ PF 0.62 — is surfaced in
`sample_warning` so the narrow-scope action is legible to anyone
reading the artifact.

Invoke:
    python -m forge.mamba.confidence_writer            # write
    python -m forge.mamba.confidence_writer --dry-run  # print only
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

BACKTEST_CSV = _REPO / "forge" / "logs" / "mamba" / "backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "mamba.json"


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
        pnls = [float(r["pnl_usd"]) for r in grp]
        wins = sum(1 for p in pnls if p > 0)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        out[k] = {
            "trades": len(grp),
            "wr": wins / len(grp) if grp else 0.0,
            "pnl_usd": round(sum(pnls), 2),
            "pf": round(pf, 2) if pf != float("inf") else None,
        }
    return out


def build_mamba_artifact() -> dict:
    from helio.fleet_state import _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(f"missing {BACKTEST_CSV}")

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    n = len(rows)
    pnls = [float(r["pnl_usd"]) for r in rows]
    # Mamba stores rr_achieved (R-multiple) per trade — use directly
    r_mults = [float(r["rr_achieved"]) for r in rows if r.get("rr_achieved") not in (None, "")]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    by_ticker = _bucket_stats(rows, lambda r: r.get("ticker", "?"))

    def _best(buckets: dict[str, dict]) -> tuple[str, dict] | None:
        candidates = [(k, v) for k, v in buckets.items()
                      if v["pnl_usd"] > 0 and v["pf"] is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda kv: kv[1]["pf"])

    best_ticker = _best(by_ticker)
    # Read the current entry mode so the artifact records which rule set
    # generated these trades. Helps a future reader distinguish "strict_5
    # (synthetic 1m)" from "transcript_3 (real 1m)" runs.
    try:
        from forge.mamba.runner import MAMBA_ENTRY_MODE as _mode
    except Exception:
        _mode = "strict_5"
    warning_parts = [
        f"{n} backfill, 0 live",
        "synthetic 1m bars (resampled from 5m) -- real 1m validation pending",
        f"entry_mode={_mode}",
    ]
    if best_ticker:
        warning_parts.append(
            f"subset edge: {best_ticker[0]} only (n={best_ticker[1]['trades']}, PF={best_ticker[1]['pf']})"
        )
    if n and sum(pnls) < 0:
        warning_parts.append("union (current bot) is negative -- narrow scope")

    artifact = {
        "schema_version": 1,
        "strategy": "mamba",
        "source": f"mamba_backtest_trades_synthetic_1m_git_{_git_sha_short()}",
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
    return artifact


def write_mamba_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact

    data = build_mamba_artifact()
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
    result = write_mamba_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
