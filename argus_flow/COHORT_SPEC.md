# Argus FX Validation Cohort — Governance Spec
# Created: 2026-03-24
# Updated: 2026-03-24 (tightened per dual-model review)
# Synthesized from Claude + ChatGPT dual-model review

## Cohort Identity

- **Instruments:** GBP/USD, EUR/USD, EUR/JPY (frozen)
- **Architecture:** Unified single-process runner (runner_unified.py)
- **IBKR:** TWS port 7496, account U24860535, Read-Only API
- **Config hashes:** Locked at cohort start (verified by cohort compliance report)
- **Code version:** Git SHA recorded in every trade row (`git_sha` field)

## Cohort Invariants (any violation resets the cohort)

1. No changes to entry logic, exit logic, stop/target, timeout, session filters, or sizing
2. No manual intervention on positions
3. No config file modifications (any config change between trades resets the cohort, even if no trade was open)
4. No runner code changes that affect trade logic (any code change resets the cohort)
5. Same git commit for the entire cohort duration — enforced by `git_sha` field in trades.csv

## Trade Validity Rules

Every trade must carry:
- `experiment_valid`: true/false
- `invalid_reason`: null or string
- `config_hash`: SHA256 of the config at trade time
- `session_id`: unique per continuous runtime session
- `runtime_epoch`: seconds since runner started
- `git_sha`: short git commit hash at runner startup

### A trade is INVALID if:
- `restored_from_file`: state was restored from file (reconnect recovery trade)
- `zero_stops_or_timeout_missing`: stop/target were 0 or timeout_time was missing on restore (forced FLAT)
- `reconnect_during_session`: runner reconnected to IBKR during this session (session-scoped — ALL trades in a reconnected session are invalid, not just trades open during the disconnect)
- `repeated_tick_failures`: 3+ consecutive tick errors for this instrument
- Manual intervention occurred
- Config changed mid-trade
- Dashboard/artifact divergence detected (see definition below)

### Invalid trades are:
- Logged normally (full data preserved)
- Excluded from performance statistics
- Counted toward invalidity rate

## Reconnect Invalidation Policy

**Session-scoped.** If the runner reconnects to IBKR at any point during a session, ALL subsequent trades in that session are marked `experiment_valid=false` with `invalid_reason=reconnect_during_session`. This is conservative by design — we cannot prove that bar data continuity was maintained across the disconnect, so we distrust the entire post-reconnect session.

Rationale: lifecycle-scoped invalidation (only invalidating the trade that was open during disconnect) is weaker because the bar buffer may have gaps that affect feature computation for future entries, not just the current trade.

## Dashboard/Artifact Divergence — Definition

A divergence exists if ANY of the following are true:
1. **Trade count mismatch**: dashboard shows different total trades than trades.csv row count
2. **P&L mismatch**: dashboard fleet PnL differs from sum of trades.csv pnl columns by more than 0.1 pip/pt
3. **Position mismatch**: dashboard shows FLAT but state.json shows LONG/SHORT (or vice versa)
4. **Stale signal file**: signal file not updated within 5 minutes during active session hours
5. **Config hash mismatch**: dashboard shows a config hash that doesn't match the on-disk config hash

Detection: manual spot-check at least once per trading day. Automated check planned for future.

## Code Version Tracking

The `git_sha` field is captured once at runner startup (`git rev-parse --short HEAD`) and written to every trade row. This makes code version machine-auditable, not just socially enforced.

**Promotion requires:** all 30+ valid trades carry the same `git_sha` value.

If the runner is restarted from a different commit, the new `git_sha` will differ, and the cohort compliance report will flag `git_sha_consistent: false`.

## Runtime Integrity Gates (must ALL pass)

1. No shared-process crash (one runner error must not kill others)
2. Reconnect restores state deterministically
3. No duplicate trade lifecycle records
4. No orphaned position state (runner says FLAT, state says LONG)
5. Dashboard reflects artifact truth correctly (see divergence definition above)
6. Signal files update within 5 minutes during active sessions

## Invalidity Quarantine Rule

- If invalid trade rate > 20% over any 10-trade window: PAUSE cohort, investigate
- If invalid reason is UNKNOWN or empty: PAUSE immediately
- If runtime bug touches open position lifecycle: exclude ALL affected trades until reviewed
- Resume only after root cause identified and fixed (which resets the cohort)

## Promotion Criteria (all must hold)

### Minimum requirements:
- 30 valid trades (not total — valid only)
- Same frozen config hash across all 30
- Same `git_sha` across all 30
- Invalid trade rate < 10%
- No unresolved runtime anomalies

### Performance requirements:
- Expectancy positive after modeled friction
- No single-session concentration > 40% of total P&L
- No single outlier trade > 30% of total P&L
- Win rate within 15pp of replay expectation
- Signal frequency within 50% of replay expectation

### Runtime requirements:
- Survived at least one forced disconnect/reconnect without data loss
- Dashboard truth matches artifact truth (no divergence per definition above)
- No manual intervention during cohort

## What happens after promotion:

- Top 2-3 FX runners advance to micro-live consideration
- Futures remain quarantined as separate hypothesis
- Additional FX pairs can be added to the unified runner
- Each new pair starts its own 30-trade cohort

## Failure modes to test before trusting "stable":

1. Kill the unified process mid-trade → restart → verify state restored correctly
2. Disconnect IBKR → reconnect → verify no duplicate or missing trades
3. Inject an exception in one runner's tick() → verify others continue
4. Verify dashboard reads the same data the runner writes

## Fleet Classification

### Cohort Active (Class A) — in active validation
- GBP/USD (69% WR, PF 1.67 from pre-cohort data)
- EUR/USD (needs cohort trades)
- EUR/JPY (needs cohort trades)

### Candidate (Class B) — not in cohort yet
- USD/JPY, AUD/USD, GBP/JPY, CAD/JPY, AUD/JPY

### Quarantined (Class C) — must re-earn inclusion
- MNQ, MES, MYM, MGC, MCL, M2K (futures — separate hypothesis, wider stops needed)
- NKD (killed — 25% WR)
- BTC, ETH (no IBKR data subscription)