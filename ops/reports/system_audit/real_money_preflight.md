# Real-money preflight — 12-point audit card

Generated: 2026-05-24T15:38:50.883678+00:00

## forge_gld_pm_long — BLOCKED

GREEN=6, YELLOW=2, RED=4

| Check | Verdict | Reason |
|---|---|---|
| in_active_roster | GREEN | in test_sunset_roster.ACTIVE_ROSTER |
| allocation_factor_positive | GREEN | allocation_factor=0.5 |
| disciplined_gate_passes | RED | CI lower 1.080 < floor 1.20 (verdict in baseline: FAIL) |
| live_evidence_n | RED | n_live_trades=0 < 20 (insufficient post-reset evidence) |
| live_pf_band | YELLOW | insufficient live trades; cannot confirm PF band |
| trade_source_is_live | RED | trade_source=none (no live trades.csv found) |
| capacity_headroom_2x | YELLOW | max_safe_multiplier=1.0 < 2.0; strategy will breach cluster caps when scaled |
| real_money_allowlist | RED | real_money_allowlist.global_enabled=false (safe default) |
| evidence_epoch_clean | GREEN | current epoch is_clean=True |
| killed_strategy_invariant | GREEN | not in kill registry |
| heartbeat_fresh | GREEN | heartbeat 0.6h old (<= 24.0h) |
| halt_flag_absent | GREEN | no HALT.flag |

## forge_xs_momentum — BLOCKED

GREEN=7, YELLOW=2, RED=3

| Check | Verdict | Reason |
|---|---|---|
| in_active_roster | GREEN | in test_sunset_roster.ACTIVE_ROSTER |
| allocation_factor_positive | GREEN | allocation_factor=1.0 |
| disciplined_gate_passes | GREEN | CI lower 1.860 >= floor 1.20 |
| live_evidence_n | RED | n_live_trades=0 < 20 (insufficient post-reset evidence) |
| live_pf_band | YELLOW | insufficient live trades; cannot confirm PF band |
| trade_source_is_live | RED | trade_source=none (no live trades.csv found) |
| capacity_headroom_2x | YELLOW | forge_xs_momentum not in capacity_stress.json — re-run the audit to include it |
| real_money_allowlist | RED | real_money_allowlist.global_enabled=false (safe default) |
| evidence_epoch_clean | GREEN | current epoch is_clean=True |
| killed_strategy_invariant | GREEN | not in kill registry |
| heartbeat_fresh | GREEN | heartbeat 12.2h old (<= 24.0h) |
| halt_flag_absent | GREEN | no HALT.flag |
