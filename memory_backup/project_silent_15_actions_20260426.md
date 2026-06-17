---
name: Silent-15 strategies — gate-loosening + RESEARCH_ONLY removal
description: 2026-04-26 actions to wake up the 15 strategies that fired 0 trades in 90 days. Gates loosened, research-only flags removed, 5 strategies restarted. First fires expected Monday open.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Actions taken 2026-04-26 (markets closed)

### Gates loosened (3)
| strategy | change | rationale |
|---|---|---|
| argus_gbpusd | range_pct_min 0.0008→0.0005, session_end 17→20 UTC | 0 signals in 90d vs 18.9/day expected |
| argus_cadjpy | min_trend_strength 0.5→0.3, min_confidence 0.5→0.3 | AI consensus typically 0.1-0.3, threshold blocking everything |
| forge_aud_asian_breakout | min_range_pips 15→8 | persistent NO_TRIGGER `range_pips_outside_band` since 4/21 |

### RESEARCH_ONLY flags removed (3)
| strategy | line | client_id | comment |
|---|---|---|---|
| forge_mamba | runner.py:968 | 114 | was True since 2026-04-17 (4-iter audit no edge) |
| forge_tori | runner.py:47 | 115 | was True since 2026-04-17 (sizing module not aligned — fleet_sizing v6 caps will clamp) |
| forge_cuebanks | runner.py:63 | 116 | was True since 2026-04-17 (rulebook exists, prod readiness not validated) |

### Hash registry updates (config drift acknowledgment)
- argus_flow/configs/hashes.json updated for gbpusd_range_paper_v1 + cadjpy_mtf_paper_v1. Required because `_validate_config_registry()` in runner_unified.py:870 enforces frozen-hash safety on startup.

### Restarts (5 runners)
- argus_unified (PIDs 24144, 13108) — confirmed clean reconcile, equity $11,815.11, CADJPY shows `min_conf=0.3` ✓
- forge.aud_asian_breakout (--loop)
- forge.mamba (--loop)
- forge.tori (--loop)
- forge.cuebanks (--loop)

## Confirmed leave-alone (5)
- forge_vix_revert — needs VIX>25 close; last 90d max ~22
- forge_wick_gbpusd — monthly wick patterns, ~13/yr expected
- forge_rebalance — S&P additions only, no events in window
- forge_fomc_drift — next FOMC 2026-04-29 (Wed)
- forge_tom_international — next entry 2026-04-29 (turn-of-month)

## Wave 2 — Greek scanners + gdx_gld + tom_international (2026-04-26 evening)

### gdx_gld: false alarm
Previous diagnosis claimed "2006 timestamps in CSV" but the 2006 references are BACKTEST start defaults at lines 474, 859, 889 of forge/gdx_gld_runner.py — not corrupt CSV. Actual log shows clean `Loaded 120 aligned daily bars from IBKR` after last night's restart. Real issue is transient TWS socket disconnects (known pattern, runner reconnects). Leaving alone. First scheduled eval Monday 16:00 ET.

### apollo: working as designed
`actionable=0` from 113 candidates is correct. apollo/runner.py:742 gates on `post_er_play` flag, set only for stocks with days_until in [-3, 0). Current candidates (GOOGL, UPS) are 4-6 days BEFORE earnings. Pre-earnings entries would mean holding INTO the report — asymmetric volatility risk. Apollo correctly fires only on post-ER drift. Will fire when actual post-ER candidates appear.

### hermes: real bug, fixed
`hermes/runner.py:485` was `if args.live and _IBKR_AVAILABLE:` but cohort_report passes `--execute` (not `--live`). Result: executor stayed None, entries generated but never submitted. Changed to `if (args.live or args.execute)`. Restarted with `--execute --loop --interval-min 30`. Heartbeat now confirms `mode="LIVE"`, `ibkr_connected: true`. Should submit gap-fill entries Monday open.

### titan: alive, by-design lock
PIDs 29832 + 20484 (`titan.runner --loop --live`) are alive, heartbeat fresh. The "lock already held" error in cohort_report.log was the nightly one-shot `titan.runner --live` colliding with the persistent `--loop` — that's correct single-instance enforcement. The reason no new scans since 4/10 is that the SCANNER (`titan.ops.scanner --refresh --long-only`) runs via cohort_report, and cohort_report has been failing. With C1 fix landed, tonight's cohort run should produce a fresh scan, and titan --loop will pick it up.

### tom_international: expanded 3 → 6 instruments
Was `["EEM", "EWJ", "VGK"]`, now `["EEM", "EWJ", "VGK", "EFA", "FXI", "INDA"]`. NOTIONAL_FRACTION_MULTIPLIER halved 100→50 to keep total per-event exposure approximately the same as 3-name × 30%. Per-event volume now doubles. Next entry 2026-04-29 (turn-of-month) will fire across 6 names instead of 3. Heartbeat confirms instruments list updated.

### Deferred expansions (cost vs 5/31 timeline)
- **wick_gbpusd → multi-pair**: refactoring single-pair to GBPJPY+EURGBP touches state, sizing, IBKR client_ids, heartbeat, fetch path. ~60-90 min careful work for ~5 extra trades by 5/31 — not enough sample to upgrade verdict from "decorative" to "evaluable." Defer. Verdict by 5/31 will be based on whatever GBPUSD-only sample exists, likely INSUFFICIENT_DATA → kill_or_research_only.
- **rebalance → S&P deletions / Russell**: separate runner = larger effort. No S&P additions in current window anyway. Defer. Same 5/31 verdict logic.

## Expansion proposals (NOT implemented; need user green-light)
- **wick_gbpusd**: add GBPJPY, EURGBP. Strategy currently single-pair; ~13 signals/yr. Code change required, not config change.
- **rebalance**: add S&P deletions or Russell additions. Different signal entirely; would require separate runner.
- **tom_international**: add EFA, FXI, INDA. Just instrument list expansion (config-only).

## What to watch Monday open
- argus_gbpusd: should now fire signals during 7-20 UTC at the looser threshold. Expected: handful per day (down from replay 18.9, since loosened version is still tighter than replay assumed).
- argus_cadjpy: should fire MTF signals at lower confidence. Expected: maybe 0.5-1/day.
- aud_asian_breakout: Asian session 01-07 UTC; should catch the next moderate-range Tokyo session.
- mamba/tori/cuebanks: will trade when their futures session windows hit. Expect first fires within 1-2 trading days.

## Verification command (run Monday EOD)
```bash
cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m ops.full_audit --diff
```
Look for: `forge_aud_asian_breakout`, `forge_mamba`, `forge_tori`, `forge_cuebanks`, `argus_gbpusd`, `argus_cadjpy` showing trades > 0 in the 24h window.

If any of these still show 0 trades after a full Monday session, dig deeper — may need a second loosening pass or the gate-loosening was insufficient.

## How to apply this memory

**Why:** captures the silent-15 wake-up action so the verdict-by-5/31 process has a baseline.

**How to apply:**
- If user asks "did silent strategies start firing": run the verification command above
- If a specific silent strategy STILL doesn't fire by 5/15: that's the kill signal. No more loosening — accept it has no edge in this configuration.
- If the gate-loosening produces lots of bad trades: that's also the kill signal. Loosened gates that produce noise are the same as no edge.
- Cross-reference with [project_strategy_freeze_20260531.md](project_strategy_freeze_20260531.md) for the 5/31 cutoff timeline.
