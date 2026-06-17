# Argus System Audit

## Executive Summary

Argus should optimize for validated risk-adjusted profitability, not trade count. The current highest-leverage work is truth surfaces, evidence gating, killed-strategy discipline, Atlas opportunity ledgers, and research-only expansion.

## System Health Grade

B

## Truth/Reconciliation Grade

B

## Strategy Performance Grade

C

## ROI/Capital Efficiency Grade

C

## Operations Grade

C

## Dashboard/Decision-Compression Grade

C

## Atlas/Data-Readiness Grade

C

## Expansion-Readiness Grade

B-for-research-only

## Top 10 Blockers

- None found by read-only audit.

## Top 10 Warnings

- STALE_STATE: Ghost strategy has no trades and no signal evidence
- STALE_STATE: Ghost strategy has no trades and no signal evidence
- PRE_RESET_DATA_CONTAMINATION: Strategy has both pre-reset and post-reset trade rows
- PRE_RESET_DATA_CONTAMINATION: Strategy has both pre-reset and post-reset trade rows
- DASHBOARD_TRUTH_MISMATCH: Killed strategy still has PnL-bearing artifacts
- PHANTOM_TRADE_EXCLUDED: Post-reset phantom-sized trades excluded from scorecard via sidecar annotations
- PHANTOM_TRADE_EXCLUDED: Post-reset phantom-sized trades excluded from scorecard via sidecar annotations
- PRE_RESET_DATA_CONTAMINATION: Strategy has both pre-reset and post-reset trade rows
- STALE_STATE: Ghost strategy has no trades and no signal evidence
- STALE_STATE: Ghost strategy has no trades and no signal evidence

## Top 10 Opportunities

- ATLAS_OPPORTUNITY_LEDGER: Build Atlas opportunity shadow ledger
- TIER1_EXPANSION_SHADOW: Start SPY/QQQ/IWM as shadow candidates only
- ARGUS_MTF_GATE_STUDY: Argus pairs need MTF threshold study, not blind kill

## Overall Performance Read

- Clean active post-reset PnL: $663.49
- Active post-reset closed trades: 28
- Clean active expectancy/trade: $23.6961
- Blocked Argus entries needing replay: 203
- Phantom PnL excluded from promotion math: $7130.29
- Forward-scoring status: RESOLVED: 117.
- Confirmed live PnL uplift available now: $0 until forward outcome, larger sample, and shadow A/B gates clear.
- Throughput upside: review only positive blocked-gate slices in shadow; keep negative-expectancy gates.

## What Is Working

- Real-money boundary primitives exist and default off.
- Real-money boundary checks are wired into the IBKR bracket and market-order executor paths.
- A read-only mismatch daemon exists and only halts when explicitly run with halt-on-violation.
- Halt truth is reconciled through helio.halt_state and fails closed when any halt source is active.
- Broker drift formula includes open unrealized PnL.
- Allocation factor 0.0 kill records are explicit.
- PnL reconciliation tests exist for vix_intraday.

## What Is Broken

- Current reconciled halt state: halted=True sources=['FLATTEN_EOD.flag'].
- Blocked opportunity forward scoring resolved 117 unique blocked entries.
- Real-money boundary behavior still needs market-open/TWS validation.
- Argus pair entries include REAL_ENTRY_FAILED rows that require broker/API failure diagnosis.
- Ghost strategies still need explicit dashboard treatment.

## What Is Misleading

- Flat/ghost strategies with no trades must not render as stable winners.
- Pre-reset data must not mix into promotion scoring.
- Paper phantoms must be excluded from ROI and strategy grade decisions.

## What Should Be Killed

- forge_multi_orb
- forge_spy_mean_rev

## What Should Remain Killed

- forge_multi_orb
- forge_spy_mean_rev

## What Should Be Resurrected As Shadow-Only

- Any resurrection candidate must be shadow/research only with hypothesis, minimum sample, pass/fail criteria, and expected failure mode.

## What Should Receive More Sample

- argus_cadjpy (UNKNOWN)
- argus_gbpusd (UNKNOWN)
- argus_usdjpy (UNKNOWN)
- cadjpy_mtf (UNKNOWN)
- forge_atlas (UNKNOWN)
- forge_aud_asian_breakout (UNKNOWN)
- forge_cuebanks (UNKNOWN)
- forge_fomc_drift (UNKNOWN)
- forge_gdx_gld (UNKNOWN)
- forge_gld_pm_long (C)
- forge_jpy_pm_short (UNKNOWN)
- forge_mamba (UNKNOWN)
- forge_nq_london_close (UNKNOWN)
- forge_nq_overnight (C)
- forge_rebalance (UNKNOWN)
- forge_spy_trend_follower (UNKNOWN)
- forge_themis (UNKNOWN)
- forge_tom_international (UNKNOWN)
- forge_tori (UNKNOWN)
- forge_vix_revert (UNKNOWN)

## What Should Receive More Capital Later

- None now. Capital increases wait for clean truth, sample, friction-adjusted expectancy, and concentration gates.

## What Should Never Receive Capital Until Fixed

- Strategies blocked by real-money boundary gaps, truth failures, low sample, negative expectancy, or payoff concentration.

## Best Next Strategy Candidates

- vix_intraday remains the main evidence candidate, subject to PF and reconciliation gates.
- gld_pm_long and nq_overnight need more sample.
- Argus pairs need MTF gate diagnosis before kill verdict.

## Best Next Instruments To Research

SPY, QQQ, IWM, DIA

## QQQ/SPY/TQQQ Recommendation

Start SPY, QQQ, and IWM as Tier 1 shadow/research candidates. Do not trade TQQQ/SQQQ as simple amplified versions; keep leveraged ETFs Tier 2 research-only until unlevered behavior, gap risk, decay, and sizing are understood.

## Atlas Roadmap

- Opportunity shadow ledger.
- Regime transition map.
- Filter attribution.
- Exit intelligence.
- Instrument similarity.
- Degradation detector.
- Bar-aligned forward outcome scorer for MTF-blocked and other rejected entries.

## Weekend Implementation Summary

Build and run audit scripts, fix only critical truth/reporting bugs, keep runtime strategy logic frozen.

## Market-Open Monitoring Checklist

- Truth first: feed, bars, heartbeats, positions, dashboard/artifact alignment.
- Opportunity funnel second.
- PnL last.

## 30-Day Roadmap

- Complete real-money boundary wiring and mismatch daemon.
- Stabilize truth/recon tests.
- Run 5/15 and 5/31 verdict gates.

## 60-Day Roadmap

- Paper-validate Tier 1 expansion candidates.
- Promote only strategies clearing evidence gates.

## 90-Day Roadmap

- Consider semi-auto allocation only after clean real evidence; never fully auto under current doctrine.
