# Test Evidence Matrix

Every component marked VERIFIED in the Infrastructure Maturity Register
must have its test evidence documented here. This prevents VERIFIED from
becoming a vibe.

Last updated: 2026-03-25

---

## 1. Fault Injection Tests (test_unified_faults.py)

| Test | Failure Class | Deterministic | Blind Spots |
|------|--------------|---------------|-------------|
| test_state_corruption | Corrupt JSON → forced FLAT | Yes | Partial write (OS crash mid-write) |
| test_missing_stop_target | Position with stop=0 → forced FLAT | Yes | - |
| test_signal_log_recreation | Deleted signal CSV → recreated | Yes | - |
| test_nan_features | Zero-price bar → no crash | Yes | Inf propagation to downstream logic |
| test_per_runner_isolation | One runner error → others continue | Yes (code inspection) | Shared-state mutation between runners |

Last run: 2026-03-24. All PASS. Automated: yes (python -m argus_flow.tests.test_unified_faults).

**Known blind spots not covered:**
- Partial write timing (OS kills process mid-JSON-write)
- Cross-artifact inconsistency (state.json updated but trades.csv not)
- Duplicate recovery after restart at exact lifecycle boundary

---

## 2. FX System Tests (test_fx_system.py)

| Test | Failure Class | Deterministic | Blind Spots |
|------|--------------|---------------|-------------|
| test_schema_signal_headers (3 checks) | Schema contract violation | Yes | Schema version drift |
| test_schema_trade_headers (2 checks) | Trade CSV contract | Yes | - |
| test_schema_build_signal_row (3 checks) | Row builder output | Yes | - |
| test_schema_validity_fields (2 checks) | Validity field presence | Yes | - |
| test_config_required_fields | Missing config fields | Yes | New required fields added but not tested |
| test_config_hash_integrity | Config tampering | Yes | Windows line-ending drift |
| test_config_unique_client_ids | Client ID collision | Yes | - |
| test_config_replay_expectations | Missing replay baselines | Yes | - |
| test_cohort_validity_flags | Validity field alignment | Yes | - |
| test_bar_buffer_basic (3 checks) | BarBuffer contract | Yes | - |
| test_bar_buffer_overflow (2 checks) | Buffer trimming | Yes | - |
| test_state_roundtrip | State persistence | Yes | Concurrent access |
| test_state_missing_file | Missing state → FLAT | Yes | - |
| test_divergence_guard_runners | Guard alignment with cohort | Yes | - |

Last run: 2026-03-24. All 25 checks PASS. Automated: yes (python -m argus_flow.tests.test_fx_system).

**Known blind spots not covered:**
- Concurrent state file access (two processes)
- Schema version migration (old CSV with fewer columns)
- Config file encoding issues across platforms

---

## 3. Chaos Tests (chaos_test.py)

| Test | Failure Class | Deterministic | Blind Spots |
|------|--------------|---------------|-------------|
| test_flash_crash | 10% price drop in one bar | Yes | Multi-bar cascade crash |
| test_feed_gap | 30 missing bars | Yes | Partial bar (incomplete OHLC) |
| test_zero_volume | All volume=0 | Yes | - |
| test_zero_price | close=0 | Yes | Inf propagation (acknowledged, degrades gracefully) |
| test_extreme_spread | 100x normal range | Yes | - |
| test_stale_replay | 200 frozen bars | Yes | Stale-but-slowly-drifting feed |
| test_timestamp_gap | 1-hour gap in timestamps | Yes | DST transitions |
| test_reverse_timestamps | Decreasing timestamps | Yes | - |
| test_duplicate_bars | 50 identical bars | Yes | Near-duplicate (1 tick difference) |
| test_spike_and_recover | 5% spike then revert | Yes | - |

Last run: 2026-03-24. All 10 suites PASS. Automated: yes (python -m argus_flow.tests.chaos_test).

**Known blind spots not covered:**
- Same-session contamination (feature buffer shared across instruments)
- Scheduler/launcher race conditions (two runners starting simultaneously)
- Symbol normalization drift (EURUSD vs EUR.USD vs EUR/USD)
- Backfill/reconnect timing skew
- Stale-but-plausible market data (prices move but at wrong magnitude)

---

## Components NOT yet at VERIFIED (still BUILT)

These components have no structured test evidence. They run but have not
been tested under adversarial conditions:

- Correlation guard
- Kill discipline (no real kill event observed)
- Promotion gate (no real promotion event observed)
- Artifact divergence (currently clean — untested on real mismatch)
- Risk oversight (untested under stress)
- Alert escalation (untested by real alerts)
- Watchdog (untested by real crash)
- Fleet launcher (untested end-to-end)
- FX backtest harness (runs but no validation against known outcomes)
- Evidence registry (new, needs cross-check against manual truth)

These will graduate to VERIFIED when they correctly handle their first
real-world event and the handling is documented.
