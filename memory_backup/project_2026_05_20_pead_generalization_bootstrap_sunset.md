---
name: 2026-05-20 PEAD generalization + bootstrap robustness + sunset execution
description: Three honesty checks shipped in one session. PEAD GENERALIZES on a non-curated SPX-30 universe (PF=1.61 vs 2.04 curated). Bootstrap CIs reveal that only forge_xs_momentum's 95% CI lower bound clears the 1.20 promotion floor (and barely). Sunset roster mechanized via no_restart + factor=0 + a 6-test validation suite that pins the post-reset 5-strategy roster.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Three rigor items shipped today. Two of three produced findings that
materially change the bot's honest posture.

## What shipped

### 1. PEAD generalization test (`ops/audit/run_pead_generalization.py`)

Ran the same PEAD strategy against a non-curated SPX-30 universe (top
large-caps not in Apollo's watchlist: AAPL, MSFT, NVDA, JPM, JNJ, HD,
PFE, COST, DIS, XOM, etc.) over the same 5-year window.

| Metric | Curated (Apollo 16) | **Non-curated (SPX 30)** |
|---|---|---|
| n trades | 69 | **98** |
| win rate | 37.7% | 43.9% |
| **PF** | **2.04** | **1.61** ✓ |
| avg win / loss | +17.3% / -5.1% | +7.5% / -3.6% |
| CAGR (scaled 15%) | +7.47% | +3.95% |
| max DD | 11.5% | **5.4%** |

**Verdict: GENERALIZES.** PF=1.61 stays above the 1.5 threshold I set
for "real factor edge survives." Apollo's curation added about 0.4 PF
points and bigger per-trade size (+17% vs +7%) — meaningful but not
the whole story.

Report at `ops/reports/system_audit/pead_generalization_report.json`.

### 2. Bootstrap robustness check (`ops/audit/run_strategy_robustness.py`)

**Major honesty finding.** The Skeptic was right. Wilson-CI on win
rate + bootstrap-CI on PF for all 5 candidates:

| Strategy | n | WR (95% CI) | PF point | PF 95% CI | Floor (1.20) met? |
|---|---:|---|---:|---|---|
| forge_xs_momentum | 79 | 52% [41%, 63%] | 2.05 | [1.28, 3.25] | **YES** (+0.08) |
| forge_pead curated | 69 | 38% [27%, 50%] | 2.04 | [1.20, 3.30] | NO (margin 0) |
| forge_pead non-curated | 98 | 44% [35%, 54%] | 1.61 | [1.06, 2.36] | NO (-0.14) |
| forge_gld_pm_long LIVE | 15 | 53% [30%, 75%] | 2.73 | [0.80, 8.80] | NO — n too small |
| forge_nq_overnight LIVE | 13 | 62% [36%, 82%] | 1.21 | [0.52, 4.58] | NO — n too small |

**Only forge_xs_momentum has its CI lower bound above the 1.20 promotion
floor, and even that's borderline (+0.08).** Every other candidate's
CI either spans below the floor or is statistically unsupportable due
to small n.

**Implication for real-money sizing:** the operator should size for
the CI LOWER BOUND, not the point estimate. A strategy with PF=2.04
but CI=[1.20, 3.30] should be sized assuming PF=1.20 — modest expected
return, real risk of mean reversion.

This rebalances the optimism from yesterday. The strategies still pass
on point estimates, but the n=69-98 backtest samples don't reliably
exclude the null hypothesis that the true PF is at the promotion floor.

Helpers: `helio/bootstrap_stats.py` (wilson_ci, bootstrap_profit_factor,
bootstrap_cagr). 18 tests in `argus_flow/tests/test_bootstrap_stats.py`.
Report: `ops/reports/system_audit/strategy_robustness.json`.

### 3. Sunset execution

Mechanized the 5/31 sunset list from yesterday's decision doc:

- **`helio/fleet_monitor.SYSTEMS`** — added `no_restart=True` to 15
  archived entries (apollo, hermes, titan, atlas, themis, mamba, tori,
  cuebanks, vix_revert, rebalance, wick_gbpusd, aud_asian_breakout,
  fomc_drift, tom_international, gdx_gld). Existing 4 killed strategies
  (multi_orb, spy_mean_rev, vix_intraday, nq_london_close) already had it.
- **`argus_flow/configs/allocation_factors.json`** v8 — factor=0.0 on
  ALL 19 archived strategies (16 sunset + 3 already-killed). Includes
  argus_gbpusd, argus_usdjpy, argus_cadjpy.
- **`argus_flow/tests/test_sunset_roster.py`** — 6 tests that pin:
  - All 21 archived strategies have allocation 0.0
  - 3 argus FX pairs are explicitly archived
  - Every archived strategy in fleet_monitor has no_restart=True
  - Surviving strategies don't have no_restart=True (unless externally scheduled)
  - No overlap between SURVIVING_FLEET and ARCHIVED_FLEET
  - Roster size stays in target band (4-6)

**Post-reset surviving fleet (5 strategies):**
1. forge_gld_pm_long (live, proven)
2. forge_nq_overnight (live, probation — 74% drawdown from peak)
3. forge_pead (new, paper-candidate post-reset)
4. forge_xs_momentum (new, paper-candidate post-reset — only candidate with passing bootstrap CI)
5. forge_spy_trend_follower (passive beta benchmark)

Down from 22 active strategies. **Truth-seeker's finding of "16 produced
0 ledger traffic in 26 days" is now mechanically enforced — they can't
silently come back online.**

## Test scoreboard

90/90 green across today's new + key existing tests:
- test_bootstrap_stats.py: 18/18 (including 3 real-data CI interpretations)
- test_sunset_roster.py: 6/6
- test_pead.py: 16/16
- test_xs_momentum.py: 13/13
- test_killed_strategy_invariant.py: 4/4
- test_killed_strategy_runtime_invariant.py: 9/9
- test_canonical_fills.py: 22/22
- test_entry_write_smoke.py: 2/2

## Implications for the 7/1+ / 10/1+ real-money decision

The bootstrap finding is the most important. **Even with backtest PF
point estimates of 2.0+, our 95% CI lower bounds tell us PF=1.2 is
plausible.** That changes everything:

- Sharpe and expected-return projections must be computed at PF=1.2,
  not PF=2.0
- Real-money sizing should be HALF what point-estimate optimism suggests
- The 30-day post-reset clean window needs to ADD trades to the
  backtest, not REPLACE it — we need more n, not just more recent n

The path to a defensible real-money go signal:
1. forge_xs_momentum gets activated 0.5× at 6/1 reset (only candidate
   with bootstrap CI clearing the floor)
2. forge_pead gets activated 0.5× at 6/1 with the curated universe
   (the non-curated CI is below floor; the curated one is at the floor,
   so live-trade samples will arbitrate which version generalizes)
3. forge_gld_pm_long + forge_nq_overnight continue at current sizing
4. The 90-day window 6/1 → 9/1 accumulates n on each strategy
5. Re-run the bootstrap CI report at 9/1. If CIs still don't clear
   the floor with margin, **defer real money another 90 days**.

## What's parked

- forge_russell_recon (stretch goal this session) — skipped; batch
  already substantive.
- Live-vs-replay parity report (Codex fix-by-5/31) — still open.
- Order lifecycle table (Codex X7) — still open. lineage_id foundation
  exists; table itself isn't built.
- Operator restart of the fleet — still needed to pick up the 5/18
  ENTRY-write code + the 5/19 lineage_id fix + today's sunset changes.

## What this session's findings change

Yesterday's framing: "2 of 3 new candidates passed the backtest gate."
Today's correction: **the gate as defined (PF point estimate > 1.20)
is too lenient. The honest gate is the bootstrap CI lower bound > 1.20.
By that gate, 1 of 3 (xs_momentum) clearly passes; pead is borderline;
vix_carry fails.**

The bot is closer to having a real edge than the Skeptic claimed, but
further from "ready for real money" than the ROI optimizer suggested.
The path forward is still Path C — more clean post-reset evidence is
the only way through.
