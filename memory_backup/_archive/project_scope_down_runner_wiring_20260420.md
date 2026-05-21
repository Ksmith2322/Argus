---
name: Scope-down runner wiring + DD anchor + Hermes correlation fix — 2026-04-20
description: Six runners now enforce their validated scope_down subset via a SCOPE_* flag (default True). All 10 confidence writers thread starting_equity_usd=1000 into drawdown. Hermes correlation bug fixed.
type: project
originSessionId: 3a8e0a94-b591-4160-81af-20ab2d227878
---
Session on 2026-04-20 PM knocked out the pending items from `project_scope_down_siblings_20260420.md` §"Known loose ends".

**Why:** the scope_down siblings were validated on backtest but the live/research runners still took all setups — meaning live behavior would reproduce the losing union, not the validated subset. The writer DD% basis was peak-relative, which produced misleading 100%+ readings on near-zero peaks (Apollo/Mamba).

**How to apply:** every modified runner has a single `SCOPE_*` module-level flag, defaulted to True. Flip to False only for research reruns reconstructing the full union. Do NOT lift the disposition on any strategy just because the runner now matches the validated sibling — live evidence still has to accrue.

## Runner flags wired

| Strategy | Module | Flag | Behavior when True |
|---|---|---|---|
| Titan | `titan/runner.py` | `SCOPE_TREND_FOLLOW_LONG_ONLY` | Entry gate: `strategy=='TREND_FOLLOW' AND direction=='LONG'` |
| Tori | `forge/tori/runner.py` | `SCOPE_DOW_LONG_ONLY` | Narrows TICKERS to `["YM=F"]` + entry gate `direction=='LONG'` |
| Cue Banks | `forge/cuebanks/runner.py` | `SCOPE_SD_SUPPLY_ZONE_ONLY` | Entry gate: `"S/D supply zone" in result["factors"]` |
| Ares | `ares/strategies/rotation.py` | `SCOPE_DISABLE_RISK_OFF_EXIT` | Suppresses the SPY<200EMA panic-to-cash branch |
| Apollo | `apollo/strategies/position_rules.py` | `SCOPE_APOLLO_VALIDATED_FILTER` + `SCOPE_APOLLO_VALIDATED_UNIVERSE` | Narrows universe to 6 validated tickers (MU, ORCL, UPS, PLUG, SNOW, GOOGL) + entry gate `10<=surprise<=20` |
| Index Rebal | `forge/index_rebalance.py` | `SCOPE_ONLY_ADDS` | `get_active_rebalance_signals` skips DELETE entries |

Mamba was already wired via its TICKERS list (2026-04-20 AM). Hermes was already wired at the runner level (score≥80 AND long AND GAP_DOWN, `hermes/runner.py:62,127`).

## DD anchor threading

All 10 confidence writers now call `_max_drawdown(pnls, starting_equity_usd=1000.0)` so `drawdown.pct_basis == "starting_equity"` fleet-wide (previously peak-relative). Files patched:
- `titan/confidence_writer.py`, `apollo/confidence_writer.py`, `ares/confidence_writer.py`, `hermes/confidence_writer.py`
- `forge/vix_revert_confidence_writer.py`, `forge/sector_rot_confidence_writer.py`, `forge/index_rebalance_confidence_writer.py`
- `forge/tori/confidence_writer.py`, `forge/mamba/confidence_writer.py`, `forge/cuebanks/confidence_writer.py`

Dry-run verified (all 10 produce valid artifacts). Relative DD ranking post-change (lowest→highest): mamba 28%, sector_rot 24%, index_rebal 33%, vix_revert 39%, titan 53%, hermes 69%, cue_banks 139%, ares 157%, apollo 181%, tori 669%. Values >100% mean position sizing > $1000 anchor; that's expected for futures — what matters is that the ranking is now comparable.

## Hermes portfolio correlation fix

`ops/dashboard.py:_gather_backtest_trades_by_strategy` Hermes block previously read `r.get("exit_date") or r.get("entry_date")` — Hermes CSVs actually use column `date`. Added `or r.get("date")` fallback. Portfolio-correlation endpoint now includes Hermes: n=386 trades, overlaps 41 days with cue_banks (r=-0.24), 31 with tori (r=+0.04), 18 with mamba (r=+0.08). No high-correlation warnings (all |r|<0.7).

## Live dashboard note

The running dashboard processes (2 of them; see `reference_reboot_recovery.md`) still have the pre-patch code cached. Restart via `taskkill` + `launch_fleet.ps1` — OR just let the next reboot cycle pick it up. Nothing at risk if left alone.

## What's still pending from the original list

- **Live accumulation** — no action, just time. Titan Validated + Tori Validated cleared backtest bar; waiting on live trades.
- **Apollo forward returns** — target n=100, currently ~50. Waits on earnings cadence.
- **Mamba** — still shelved pending real 1m bar source + YM-only n≥30 on real data. Not unblocked by this session.
- **Apollo rework gate** — narrower universe is now enforced, but `disposition.status == "research_only"` until 2026-07-20. The filter being live doesn't lift the disposition.
