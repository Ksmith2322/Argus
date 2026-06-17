---
name: Expanded failure modes — extends reference_failure_modes from 5 → 15+
description: Additional failure modes discovered through April 2026 operation. Each entry: symptom → root cause → detection command → fix. Adds to the 5 modes already documented in reference_failure_modes.md.
type: reference
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## How this relates to reference_failure_modes.md

`reference_failure_modes.md` has the original 5 modes (duplicate daemon, stale heartbeat, TWS socket, FRIDAY_CLOSE, 4hr-cycle stale). This memo ADDS modes 6-15+ surfaced through ongoing operation. Both should be searched when something breaks.

## #6 — Cohort report lock contention (4/22-25 incident)

**Symptoms:** `refresh_managed_truth ERROR: exit code 2 — refresh lock already held and existing status is stale or failed` in cohort_report.log. Per 4/25 fix, now non-fatal (logged as WARNING, chain continues).

**Root cause:** managed_truth_loop daemon holds the lock continuously via 3-min refresh. cohort_report's nightly `refresh_managed_truth` invocation collides. If the daemon's most recent status is "OK" within `--accept-existing-age-s 600`, the call returns 0 (no problem). Otherwise returns 2.

**Detection:** `tail -50 c:/Argus/repo/argus_flow/logs/cohort_report.log | grep refresh_managed_truth`

**Fix:** post-4/25, the ps1 catches exit code 2 and continues. If you ever see the chain ABORTING again: check ps1 file at line 22-25 didn't get reverted.

## #7 — Strategy submitting orders during weekends

**Symptoms:** runner log shows `REAL_ENTRY FAILED: PreSubmitted` or `REAL_ENTRY FAILED: Inactive` cycling hourly on Saturday/Sunday.

**Root cause:** runner has no weekday guard. Wakes on its loop interval, evaluates signal, submits to IBKR. IBKR queues the order (PreSubmitted) but won't activate until market reopens. Runner interprets queued state as "failed" and retries next cycle, potentially stacking orders for Monday open.

**First seen:** gld_pm_long Saturday 4/25 16:05-22:05 UTC.

**Detection:** `grep "REAL_ENTRY FAILED" forge/logs/<strat>/runner.log | tail -10` — if cluster of failures during Sat/Sun, this is it.

**Fix:** add `if datetime.utcnow().weekday() >= 5: skip_signal_path()` guard at top of `evaluate_once()` for any equity/ETF runner. FX runners exempt (FX trades 24×5 starting Sun 22:00 UTC). See gld_pm_long/runner.py:296-310 for pattern.

## #8 — IBKR client_id collision (latent bug from RESEARCH_ONLY=False flip)

**Symptoms:** runner can't connect to IBKR after enabling. Log: `ConnectionError: clientId already in use`.

**Root cause:** two runners configured with same IBKR_CLIENT_ID. One is RESEARCH_ONLY=True (silent) until someone flips it; collision goes from latent to active.

**Detection:** `grep -rn "IBKR_CLIENT_ID = [0-9]" forge/ apollo/ hermes/ titan/ ares/ | sort -t= -k2 -n` — duplicates in the right column = collision.

**Fix:** assign unique IDs per `project_execution_conversion_20260424.md` table. Forge range 100-199. Greek family 60/70/80/90. Argus 1-19.

**Prevention:** the audit's full_audit.py should add a collision-check lens (Tier 2 work, not done yet).

## #9 — Argus runner refuses to start after config edit

**Symptoms:** `FATAL: config 'X.json' hash mismatch expected=ABCD actual=EFGH` in runner_unified.log. Runner exits.

**Root cause:** `_validate_config_registry()` in runner_unified.py:870 enforces frozen-hash safety. After editing a config (e.g. loosening a gate), the on-disk hash differs from `argus_flow/configs/hashes.json`. Safety feature working as intended.

**Detection:** the FATAL log line tells you both the expected and actual hash.

**Fix:** copy the `actual=` hash into `hashes.json` for that filename. Runner accepts the new hash on next start.

## #10 — RECON_DRIFT on a single instrument (orphan position)

**Symptoms:** runner shows `entries_blocked=true` and `entry_block_reason=RECON_DRIFT`. Heartbeat shows `local=FLAT, broker_qty=<nonzero>`.

**Root cause:** a previous order EXIT didn't actually close at IBKR despite the runner recording it as closed. Could be: partial fill never followed by remainder, exit order rejected silently, position migration during reset.

**First seen:** USDJPY 47260 LONG orphan caught 4/26.

**Detection:** read `argus_flow/logs/<pair>/broker_state.json` — if `runner.local_position != broker.position`, drift active.

**Fix:** close the orphan via `ops/close_orphan_usdjpy.py` (template — edit SYMBOL/QTY for other pairs). Wait 5 min for next reconciliation cycle to confirm clear.

**Prevention:** post-real-money, this becomes a Tier 1 concern. Add periodic full-fleet reconciliation (Week 3 of 5-week roadmap).

## #11 — Hermes generates entries that don't submit

**Symptoms:** hermes log shows scan results with score≥75, "entries generated", but `open_positions=0` and no fills in canonical_fills.

**Root cause:** flag mismatch. Cohort_report passes `--execute`, but hermes/runner.py:485 was checking `if args.live` only. Executor stayed None.

**First seen:** Hermes 4/26 — fixed same day.

**Detection:** `grep "open_positions" hermes/logs/heartbeat.json` — if always 0 despite gaps_today > 0, this pattern.

**Fix:** ensure `if args.live or args.execute` at executor init. Restart hermes with `--execute`.

## #12 — Apollo "actionable=0" with N candidates (NOT a bug)

**Symptoms:** apollo scans 100+ candidates with high scores, but `actionable=0` in heartbeat.

**Root cause:** apollo's gate at runner.py:742 fires only on `post_er_play` flag (days_until_earnings ∈ [-3, 0)). Pre-earnings setups score high but don't submit — by design, to avoid holding into reports.

**Detection:** look at apollo's scan output. If candidates are days_until > 0, this is expected.

**Fix:** none. Working as designed. Document only.

## #13 — Titan scan loop doesn't refresh

**Symptoms:** titan's scan_<date>.json files stop appearing in titan/logs/ even though runner is alive.

**Root cause:** the scanner refresh runs via cohort_report (`titan.ops.scanner --refresh`). If cohort_report fails or doesn't run, scanner data goes stale. Runner --loop just manages existing positions; doesn't scan on its own.

**Detection:** `ls -la titan/logs/scan_*.json | tail -5` — if last scan > 2 days old, scanner refresh not running.

**Fix:** verify cohort_report's nightly run completed. If failing for unrelated reasons, run scanner manually: `python -m titan.ops.scanner --refresh --long-only`.

## #14 — H1: scheduled task fires but exits immediately (Logon Mode bug)

**Symptoms:** schtasks shows `Last Result: -1` for ArgusGldPmLoop / ArgusNqLondonCloseLoop / ArgusAudOrbLoop. Tasks scheduled but runner not actually started.

**Root cause:** Logon Mode = "Interactive only" — task only runs when user is logged on. If user has logged off or task fired during a logged-off period, it dies immediately with exit -1.

**First seen:** 4/22+; fixed 4/26 by switching to S4U (Service-for-User) logon.

**Detection:** `schtasks /Query /TN <task> /FO LIST /V | grep "Logon Mode"` — should show "Interactive/Background", not "Interactive only".

**Fix:** run `ops/fix_h1_logon_mode.ps1` from elevated PowerShell.

## #15 — Strategy stuck silent at strict gate (silent-15 class)

**Symptoms:** strategy fires 0 trades in 30+ days. Process alive, heartbeat fresh, no errors. Just no signals.

**Root cause:** gate threshold too strict for current market regime. Replay backtest expects 18.9 signals/day but live = 0/day. Indicates threshold based on different volatility regime or different data source.

**First seen:** silent-15 class on 4/26 — argus_gbpusd, argus_cadjpy, aud_asian_breakout all silent for weeks.

**Detection:** dashboard's per-strategy equity curve panel shows N=0 trades for 30d+.

**Fix:** loosen ONE parameter at a time (range_pct, min_confidence, min_range_pips). Observe 1-2 weeks. Don't loosen 3 things at once — you can't isolate which fix worked.

**Anti-pattern:** "loosen everything to maximize signal" — produces noise, not edge.

## #16 — Weekend spam orders activate at Monday open → orphan position + RECOVERY_REQUIRED

**Symptoms (sequence):**
- Strategy without weekday-guard fires hourly during Sat/Sun
- Each cycle's "REAL_ENTRY FAILED: PreSubmitted" / "Inactive" log line is treated as failure by runner, but **the orders ARE queued at IBKR**
- Monday market open (13:30 UTC for US equities): N of the queued orders activate and fill
- Argus reconciles next cycle, finds broker has LONG position no runner is tracking, enters `RECOVERY_REQUIRED` mode
- Whole fleet is BLOCKED until manually resolved

**Root cause:**
Two compounding failures:
1. Strategy lacked weekday/market-hours guard (fixed for gld_pm_long 4/25 weekend-guard, but Saturday spam happened BEFORE the fix landed and orders persisted)
2. Runner's order-status check treats PreSubmitted/Inactive as failure and exits, but doesn't cancel the queued order → it sits at IBKR until expired or activated

**First seen:** 2026-04-27 Monday open. gld_pm_long had spammed 10 hourly orders during 4/25-4/26 weekend. 2 of them (54 shares total at avg $431.62) activated at the open. Other 8 expired by Monday open. -$31.32 realized loss on close.

**Detection:**
```bash
grep "RECOVERY_REQUIRED\|ORPHAN DETECTED" c:/Argus/repo/argus_flow/logs/runner_unified.log | tail -5
```

**Fix:**
1. Run `ops/close_orphan_gld.py` (template — adjust SYMBOL for other instruments)
   - Cancels any remaining queued orders
   - Closes the orphan position at market
2. Manually edit the runner's `state.json` to set `open_trade: null`
3. Wait 5 min for argus to re-reconcile and clear `RECOVERY_REQUIRED`

**Prevention (now in place):**
- gld_pm_long has weekend guard added 4/25 (PR commit 35bc81c) — won't re-spam future weekends
- Pattern check: any forge runner that submits real orders should have weekday guard at top of `evaluate_once()`
- TODO (Week 3): runner-side order-cancel-on-failure path — if `REAL_ENTRY FAILED` because of PreSubmitted/Inactive status, the runner should `ib.cancelOrder()` immediately, not just log and exit

**The sister incident (mode #10 — RECON_DRIFT on a single instrument)** is the per-pair version of this. #16 is the cross-strategy version where the orphan isn't claimed by any runner. Both flow through the same `ops/close_orphan_*.py` template.

## How to apply this memory

**Why:** this catalog grows over operational time. Each entry was either learned from a real incident OR documented during prevention work.

**How to apply:**
- When a new failure happens: match symptoms to existing modes first. If novel, add a #16, #17 entry here.
- Don't combine modes with the same fix. Better to have 2 separate entries that both apply than one mushy one.
- Cross-reference numbers stay stable — once #11 is documented as Hermes flag, that number is reserved.
