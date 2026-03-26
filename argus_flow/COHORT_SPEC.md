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
- `restored_from_file` after reconnect: runner mints a fresh session on reconnect; only the trade open during disconnect is tainted. Subsequent trades on rebuilt bar buffer are valid.
- `repeated_tick_failures`: 3+ consecutive tick errors for this instrument
- Manual intervention occurred
- Config changed mid-trade
- Dashboard/artifact divergence detected (see definition below)

### Invalid trades are:
- Logged normally (full data preserved)
- Excluded from performance statistics
- Counted toward invalidity rate

## Reconnect Invalidation Policy

**Trade-scoped with session reset.** On reconnect, the runner mints a fresh `session_id` and rebuilds all `BarBuffer`s from scratch. Only the trade that was open during the disconnect is tainted (via `restored_from_file`). Subsequent entries are computed on a fully rebuilt bar window and are valid.

Previous policy (session-scoped) invalidated ALL trades after any single disconnect, making it nearly impossible to accumulate 30 valid trades in environments with occasional IBKR blips. Changed 2026-03-25.

Evidence of survived disconnect: promotion gate detects multiple `session_id` values in the trade history.

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

### Sample robustness requirements (added 2026-03-26):
- Minimum 10 distinct trading days represented in valid trades
- No more than 30% of valid trades from a single calendar day
- Minimum 2 distinct runtime sessions (proves restart survivability)
- No single regime (TRENDING/RANGING/CHOPPY) accounting for >70% of entries

## Execution Reality Gate (REQUIRED before micro-live)

Paper promotion alone is NOT sufficient for live deployment. Each promoted strategy must also pass:

1. **Next-bar-open entry model**: backtest expectancy remains positive when entry executes at bar i+1 open (not signal bar close)
2. **Spread/slippage model**: expectancy remains positive after modeled round-trip friction per instrument
3. **Pessimistic same-bar sequencing**: stops checked before targets for position direction (worst case)
4. **Trailing stop parity**: backtest trailing uses close-based logic matching live mid-based behavior

Paper-only close-price backtest results are NOT promotable evidence.

## Portfolio Capital Protection (REQUIRED before live)

Before ANY real money deployment:

1. **Broker truth is primary**: risk manager must consume broker positions/orders, not only local state
2. **Max total open risk**: capped at 5% of account equity across all positions
3. **Max per-trade dollar risk**: pool * risk_pct (default 2%)
4. **Portfolio daily max loss**: 10R fleet-wide (currently implemented)
5. **Per-instrument daily max loss**: 3R (currently implemented)
6. **Correlated bucket risk cap**: max 3 same-currency-direction positions (currently implemented)
7. **Unknown symbol fail-closed**: unmapped symbols blocked from entry (currently implemented)
8. **No pyramiding without re-approval**: pyramid adds must re-check portfolio gate
9. **Reconciliation drift blocks entries**: any unresolved broker/local mismatch pauses all new entries
10. **Manual unlock after critical breaker**: auto-resume disabled for portfolio-level breakers in live mode

## Micro-Live Admission Gate

Transition from paper to micro-live requires ALL of:

### Prerequisites:
- Paper cohort PROMOTED (all gates passed)
- Execution Reality Gate passed
- Portfolio Capital Protection implemented and tested
- Kill-switch tested (manual entry freeze)
- Broker reconciliation tested under live-capable conditions

### Constraints:
- Smallest live size: 1 micro lot (FX) or 1 micro contract (futures)
- Max 3 instruments initially (top performers from paper cohort)
- No futures/crypto in first micro-live cohort (FX only)
- Config loader must reject non-cohort live instruments

### Operational requirements:
- Kill-switch file or API endpoint that halts all entries immediately
- Manual unlock required after any portfolio-level breaker trips
- Incident runbook documenting: who can unlock, what constitutes manual intervention, broker disconnect procedures

## Canonical Execution Truth (REQUIRED before live)

Before live deployment:
- Canonical orders/fills/positions/account artifacts must exist and reconcile
- Trade journal is derived from fills, not primary truth
- No unresolved order/fill lifecycle ambiguity
- Duplicate/partial fill defense tested
- Fill deduplication survives process restart

## Failure modes to test before trusting "stable":

1. Kill the unified process mid-trade → restart → verify state restored correctly
2. Disconnect IBKR → reconnect → verify no duplicate or missing trades
3. Inject an exception in one runner's tick() → verify others continue
4. Verify dashboard reads the same data the runner writes
5. Verify broker reconciliation catches phantom/orphan positions
6. Verify portfolio risk manager blocks over-correlated entries
7. Verify kill-switch halts all entries immediately

## Fleet Classification (updated 2026-03-26)

### Cohort Active (Class A) — in active validation
- GBP/USD, EUR/USD, EUR/JPY, GBP/JPY, CAD/JPY (London session)
- AUD/JPY, USD/JPY, AUD/USD (Asia session)

### Candidate (Class B) — collecting data, not yet at 30 valid trades
- MES, MNQ, MYM, M2K (equity index futures)
- MGC (gold), MCL (oil)
- NKD (Nikkei, Asia)

### Retired (Class D) — no longer running
- BTC, ETH (crypto — archived, pivot to IBKR FX/futures)
- Legacy crypto runners (runner_live.py) — fully retired 2026-03-25