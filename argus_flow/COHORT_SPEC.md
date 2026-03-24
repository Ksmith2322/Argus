# Argus FX Validation Cohort — Governance Spec
# Created: 2026-03-24
# Synthesized from Claude + ChatGPT dual-model review

## Cohort Identity

- **Instruments:** GBP/USD, EUR/USD, EUR/JPY (frozen)
- **Architecture:** Unified single-process runner (runner_unified.py)
- **IBKR:** TWS port 7496, account U24860535, Read-Only API
- **Config hashes:** Locked at cohort start (verified by config_check.py)
- **Code version:** Git tag at cohort start

## Cohort Invariants (any violation resets the cohort)

1. No changes to entry logic, exit logic, stop/target, timeout, session filters, or sizing
2. No manual intervention on positions
3. No config file modifications
4. No runner code changes that affect trade logic
5. Same git commit for the entire cohort duration

## Trade Validity Rules

Every trade must carry:
- `experiment_valid`: true/false
- `invalid_reason`: null or string
- `config_hash`: SHA256 of the config at trade time
- `session_id`: unique per continuous runtime session
- `runtime_epoch`: seconds since runner started

### A trade is INVALID if:
- Disconnect occurred during open position lifecycle
- State was restored from file (reconnect recovery trade)
- Stop/target were 0 on restore (forced FLAT)
- Manual intervention occurred
- Config changed mid-trade
- Dashboard/artifact divergence detected

### Invalid trades are:
- Logged normally (full data preserved)
- Excluded from performance statistics
- Counted toward invalidity rate

## Runtime Integrity Gates (must ALL pass)

1. No shared-process crash (one runner error must not kill others)
2. Reconnect restores state deterministically
3. No duplicate trade lifecycle records
4. No orphaned position state (runner says FLAT, state says LONG)
5. Dashboard reflects artifact truth correctly
6. Signal files update within 5 minutes during active sessions

## Invalidity Quarantine Rule

- If invalid trade rate > 20% over any 10-trade window: PAUSE cohort, investigate
- If invalid reason is UNKNOWN: PAUSE immediately
- If runtime bug touches open position lifecycle: exclude ALL affected trades until reviewed
- Resume only after root cause identified and fixed (which resets the cohort)

## Promotion Criteria (all must hold)

### Minimum requirements:
- 30 valid trades (not total — valid only)
- Same frozen config hash across all 30
- Same code version across all 30
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
- Dashboard truth matches artifact truth
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

### Class A — Proven provisional (in cohort)
- GBP/USD (69% WR, PF 1.67)
- EUR/USD (needs more trades)
- EUR/JPY (needs more trades)

### Class B — Under test (not in cohort yet)
- USD/JPY, AUD/USD, GBP/JPY, CAD/JPY, AUD/JPY

### Class C — Quarantined (must re-earn inclusion)
- MNQ, MES, MYM, MGC, MCL, M2K (futures — separate hypothesis)
- NKD (killed — 25% WR)
- BTC, ETH (no data subscription)