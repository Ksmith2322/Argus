"""One-shot utility to stamp holdout_freeze blocks on the 8 validated artifacts.

Run once after the scope_down-wiring session (2026-04-20) to capture the
in-sample claims as a frozen hypothesis. Subsequent runs of
`helio.holdout_eval` compare OOS fills to these claims.

Each artifact gets:
  - frozen_at: today
  - filter_statement: matches the SCOPE_* flag enforced in the runner
  - in_sample_claim: lifted from the artifact's own bt_pf/bt_wr/etc.
  - oos_eval_earliest: today + 30 days (calendar, not trading days)
  - oos_eval_target_n: 20 trades (conservative minimum)
  - degradation_alert_pct: 0.30 (30% PF drop trips DEGRADED verdict)

Idempotent by default — skips artifacts that already carry a holdout_freeze.
Use --force to overwrite.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from helio.strategy_confidence import (  # noqa: E402
    StrategyConfidenceArtifact, HoldoutFreeze,
)

ARTIFACT_DIR = REPO / "strategy_confidence"

# Map of validated artifact filename -> scope filter rule enforced in the runner
FREEZE_MAP = {
    "titan_validated.json":       "strategy=='TREND_FOLLOW' AND direction=='LONG' (titan/runner.py SCOPE_TREND_FOLLOW_LONG_ONLY)",
    "tori_validated.json":        "name=='Dow' AND direction=='LONG' — TICKERS narrowed to YM=F (forge/tori/runner.py SCOPE_DOW_LONG_ONLY)",
    "cue_banks_validated.json":   "factors contains 'S/D supply zone' (forge/cuebanks/runner.py SCOPE_SD_SUPPLY_ZONE_ONLY)",
    "ares_validated.json":        "exit_reason=='rotation' — risk_off exits disabled (ares/strategies/rotation.py SCOPE_DISABLE_RISK_OFF_EXIT)",
    "apollo_validated.json":      "surprise 10-20% AND universe in {MU,ORCL,UPS,PLUG,SNOW,GOOGL} (apollo/strategies/position_rules.py SCOPE_APOLLO_VALIDATED_FILTER)",
    "index_rebal_validated.json": "action=='ADD' only (forge/index_rebalance.py SCOPE_ONLY_ADDS)",
    "hermes_validated.json":      "score>=80 AND direction=='long' AND GAP_DOWN (hermes/runner.py — already enforced pre-scope-down)",
    "mamba_ym.json":              "ticker=='YM=F' only (forge/mamba/runner.py TICKERS scope)",
}


def _build_freeze_block(art: dict, filter_statement: str) -> dict:
    now = datetime.now(timezone.utc)
    claim = {
        "pf": art.get("bt_pf"),
        "wr": art.get("bt_wr"),
        "n_trades": art.get("bt_trades"),
        "expectancy_usd": art.get("expectancy_usd"),
        "p_expectancy_positive": art.get("p_expectancy_positive"),
    }
    return {
        "frozen_at": now.isoformat(),
        "filter_statement": filter_statement,
        "in_sample_claim": claim,
        "oos_eval_earliest": (now + timedelta(days=30)).isoformat(),
        "oos_eval_target_n": 20,
        "degradation_alert_pct": 0.30,
        "notes": "Scope_down subset identified by in-sample selection on historical data; "
                 "this block captures the PF/WR/n at freeze time so later OOS fills "
                 "(post frozen_at, excluding backfill) can be compared unbiased.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing holdout_freeze blocks")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for fname, filter_stmt in FREEZE_MAP.items():
        path = ARTIFACT_DIR / fname
        if not path.exists():
            print(f"  SKIP {fname}: not found")
            continue
        art = json.loads(path.read_text(encoding="utf-8"))
        if art.get("holdout_freeze") and not args.force:
            print(f"  KEEP {fname}: already frozen at {art['holdout_freeze']['frozen_at']}")
            continue
        art["holdout_freeze"] = _build_freeze_block(art, filter_stmt)
        # Validate before writing
        StrategyConfidenceArtifact.model_validate(art)
        if args.dry_run:
            print(f"  [DRY] {fname}: would freeze. in_sample_claim={art['holdout_freeze']['in_sample_claim']}")
            continue
        path.write_text(json.dumps(art, indent=2), encoding="utf-8")
        print(f"  FROZEN {fname}: {art['holdout_freeze']['in_sample_claim']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
