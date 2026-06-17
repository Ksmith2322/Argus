---
name: 5-week roadmap to strategy freeze + real-money gate (4/24 → 5/31)
description: Master calendar integrating audit + silent-15 + validation + cull + risk hardening + real-money gate into a single week-by-week plan. The maturity jump from explore-mode to live-trading-ready.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Phases at a glance

- **Week 1 (4/26 → 5/2):** **VOLUME PUSH** + first review. Get every strategy firing, not optimizing PnL.
- **Week 2 (5/3 → 5/9):** cull dead + **BUILD 3 new strategies** (bond futures, short-vol, SPY/TLT pair) + promote winners
- **Week 3 (5/10 → 5/16):** risk hardening + drills + new strategies enter live observation
- **Week 4 (5/17 → 5/23):** real-money gate sign-off prep + new strategies have ~2 weeks of data
- **Week 5 (5/24 → 5/31):** freeze + final verdict (includes the 3 new strategies)

## Core principle: data > opinion

The plan **reassesses every Friday**. Each week's exit criteria are checked against actual data, not against intent. If data says "extend explore mode 2 weeks," the calendar shifts. If data says "ahead of schedule, pull forward Week 4 work," that's also fine. **The 5/31 strategy freeze is fixed; everything else flexes.**

5/1 success metric is **volume**, not PnL. We want behavior data — does each strategy fire when it should, does the execution path work, do the gates produce the expected signal-to-noise ratio. P&L stories come later, when there's enough sample to attribute them to edge vs noise.

Cross-references: this memo is the spine. Procedures live in `project_5_1_review_ceremony_20260501.md`, `project_risk_guardrails_real_money_20260501.md`, `project_real_money_readiness_gate_20260531.md`, `project_operations_solo_manual_20260501.md`, `project_test_coverage_matrix_20260426.md`, `project_expanded_failure_modes_20260501.md`. New-strategy specs in `project_pre_freeze_coverage_gaps_20260426.md`.

---

## Week 1 (2026-04-26 → 2026-05-02): Validate

**Goal:** confirm last week's fixes landed, surface real fills, run first formal review.

**Mon 4/27 — first market day post-conversion-fixes**
- Run validation per `project_monday_validation_20260427.md` (sequence: gateway_status → orders.csv → fleet_health → sizing sanity)
- Verify: gld_pm_long fires properly (no Saturday-spam), hermes submits (--execute fix landed), argus_gbpusd/cadjpy fire at looser gates, mamba/tori/cuebanks trade with RESEARCH_ONLY=False
- Fix-as-you-go any failure modes that surface
- USDJPY orphan-close was completed 4/26 night → argus accepts entries Monday open

**Tue-Wed 4/28-29**
- Wed 4/29: FOMC day → fomc_drift fires; turn-of-month → tom_international fires across 6 ETFs
- Watch real fills land in canonical_fills.jsonl
- Confirm Greek scanners (apollo/hermes/titan/ares) ran via cohort_report tonight (4/26) and continue nightly

**Thu 4/30**
- Pre-review prep: regenerate test coverage matrix (`/holdout`, `/kill-list`, `/portfolio`)
- Identify candidates for each verdict tier

**Fri 5/1 — FORMAL REVIEW CEREMONY**
- Run `project_5_1_review_ceremony_20260501.md` end-to-end
- Verdict per strategy: WINNER / REWORK / KILL
- Acceptance criteria: ≥1 WINNER, ≤5 REWORK, ≤6 KILL
- Save decision record to `argus_flow/logs/verdict_20260501.json`

**Sat-Sun 5/2-5/3 — weekly audit**
- Run `/full-audit --diff`
- Apply weekly routine per `reference_weekly_audit_routine.md`

**Week 1 exit criteria — VOLUME, not PnL:**
- At least 8 of 15 previously-silent strategies have produced ≥1 live trade
- ≥75 total trades across the fleet for the week (was 105 in 7d before; should be similar or more with silent-15 wake-up)
- All 22 strategies have a fresh heartbeat within their expected cadence
- Cohort_report.log shows `=== complete ===` (no "WITH FAILURES") for ≥5 of 7 nights
- 5/1 verdict record exists at `argus_flow/logs/verdict_20260501.json`
- Don't grade on PnL — sample is too small to mean anything yet

---

## Week 2 (5/3 → 5/9): Cull + promote + BUILD 3 NEW STRATEGIES

**Goal:** remove confirmed losers, scale up confirmed winners, AND build out the 3 approved pre-freeze additions to fill asset-class gaps.

**Cull + promote actions:**
- Apply KILL verdicts: shelve the runners (don't delete; move to research_only or stop the loop)
- Apply REWORK verdicts: implement the structural fix (loosen gate / fix data / address sizing) and document
- Apply WINNER verdicts: bump tier, increase risk_pct per fleet_sizing tier system
- Update operational_maturity scoring to reflect new fleet shape

**Build 3 new strategies (per `project_pre_freeze_coverage_gaps_20260426.md`):**
- **Mon-Tue 5/4-5/5: forge_zn_momentum** — port titan-pattern logic to ZN (10-yr Treasury futures) or ZF (5-yr). 4-6 hr build + initial backtest. IBKR_CLIENT_ID = 118.
- **Wed 5/6: forge_vix_short** — inverse of vix_intraday on UVXY/VIXY. Sell-the-spike pattern. 2-4 hr build (mostly copy-invert). IBKR_CLIENT_ID = 119.
- **Thu-Fri 5/7-5/8: forge_spy_tlt_pair** — port gdx_gld_runner.py for SPY/TLT (or SPY/IEF) ratio mean-reversion. 4-6 hr build. IBKR_CLIENT_ID = 120.

For each: backtest first, walk-forward second, signal-only mode third, real-IBKR-paper fourth. By Sunday 5/10 all 3 should be live with trades flowing.

**Decision gates (reassess Friday 5/8):**
- If <1 WINNER after 5/1: extend Week 1 evaluation by 2 weeks; defer Week 3 risk-hardening 1 week; 5/31 freeze still hits
- If ≥3 WINNERS: ahead of schedule — pull Week 3 risk drills forward; start writing the LLM overlay regimes config in spare time
- If ≥10 KILLS: aggressive cull; review whether any kills were premature
- If new-strategy build is slipping (<2 of 3 live by 5/10): cap at whatever's done, don't bend the schedule for the third
- **Don't add a 4th strategy** even if 1 of the 3 fails to build. The cap is firm.

**Week 2 exit criteria:**
- KILL/REWORK/WINNER actions from 5/1 verdict record EXECUTED (not deferred)
- ≥2 of the 3 new strategies live with trades in canonical_fills.jsonl
- Fleet count net-positive (added 3, killed N — track delta)

---

## Week 3 (5/10 → 5/16): Risk hardening + drills + new-strategy live observation + capital allocator code

**Goal:** make the risk infrastructure trustworthy enough for real money. Test it. Implement the capital allocator layer. Also: new strategies enter their live observation window.

**New-strategy observation:** the 3 strategies built in Week 2 are now live. By 5/16 each should have ~1 week of live trades. Track per-strategy trade count; if any has <5 trades by 5/16, it's a likely INSUFFICIENT_DATA at 5/31.

**Capital allocator implementation work:**
- Mon 5/11: build `helio/cluster_exposure.py` (per `project_cluster_exposure_model.md`)
- Tue 5/12: wire cluster cap checks into all 3 executor paths
- Wed 5/13: build `helio/kill_pause_engine.py` (per `project_kill_pause_engine.md`) + start `helio/real_money.py` for boundary
- Thu 5/14: drill — manual cluster-cap breach test, kill-switch drill, daily-loss circuit breaker drill
- Fri 5/15: drill — VIX>30 force-close drill (synthetic), TWS-down drill
- Sat 5/16: scripted walk-forward runner (`tools/run_walkforward.py`) to support 5/31 verdicts; standardize `experiment_valid` definition across runners

**Validation infrastructure (locked from user feedback):**
- Walk-forward must be SCRIPTED (not manual) before 5/31 verdicts depend on it
- `experiment_valid=false` criteria standardized: broker disconnect / bad fill state / recon drift / known runner bug / manual interruption / stale data / wrong sizing / wrong account. Loss alone does NOT make a trade invalid.

**Risk infrastructure work** (per `project_risk_guardrails_real_money_20260501.md`):
- Implement / verify: kill switch (`KILL_SWITCH` control file) — does it actually halt all entries within 60s of being created?
- Implement / verify: daily loss circuit breaker (-2% equity → pause all entries 24h)
- Implement / verify: position size cap per instrument (no single position > 1× anchor stock/etf, > 5× anchor futures, > 20× anchor FX)
- Implement / verify: manual override procedure (single user action that pauses + flat-by-EOD)

**Drill schedule:**
- Mon 5/11: kill-switch drill — flip it during market hours, verify all 22 runners halt entries within 60s
- Wed 5/13: TWS-down drill — kill TWS, verify runners log BrokerEquityUnavailableError and don't take silent action
- Fri 5/15: drawdown drill — manually inject a fake drawdown alert, verify circuit breaker fires

**Week 3 exit criteria:** all 3 drills pass cleanly. Risk infrastructure documented and tested.

---

## Week 4 (5/17 → 5/23): Real-money gate sign-off prep

**Goal:** walk through the 20-point readiness checklist. Address every gap.

**Use:** `project_real_money_readiness_gate_20260531.md` checklist.

**Likely gaps to surface:**
- Documentation: `reference_reboot_recovery.md` may be stale — refresh
- Solo manual: per `project_operations_solo_manual_20260501.md` — read end to end, fix any "I don't know what to do here" moments
- API latency: actually measure round-trip for a typical entry submission (target < 500ms)
- Failure modes: per `project_expanded_failure_modes_20260501.md` — surface 2-3 more from the past 4 weeks of operation

**Sign-off ceremony (Sat 5/23):**
- Walk through the 20-point gate
- For each item: yes/no/blocked
- If any blockers: document, schedule fix for week 5

**Week 4 exit criteria:** ≥17 of 20 gate items are GREEN. Remaining 3 have explicit fix dates.

---

## Week 5 (5/24 → 5/31): Freeze + final verdict (incl. 3 new strategies)

**Goal:** close out explore-mode. Final cull. Real-money go/no-go.

**3 new strategies get verdicts too:** by 5/31 each new strategy has ~3 weeks of live data. Apply the same template:
- WINNER if n ≥ 10 trades AND PF ≥ 1.20 (lower bar than 30-trade rule for old strategies — these are new)
- REWORK if structural issue identified
- INSUFFICIENT_DATA → auto-converts to research_only (kept alive but not in real-money pool)
- KILL if n ≥ 10 with PF < 1.0

**Mon-Wed 5/25-27:**
- Final hard-kill pass: any strategy still DEGRADED for 30+ days OR PF<1.1 at n=50 → auto-shelved
- Re-run `/full-audit --diff` to confirm fleet is stable
- Address remaining 3 gate items from week 4

**Thu 5/28:**
- LIVE-MONEY READINESS REVIEW
- Sign off the 20-point gate (or explicitly defer)
- Decision: real-money pilot Y/N? If Y → start with $500-$1,000 (per honest-engagement memo: paper IBKR is reasonable proxy below $500; real friction surfaces at $500+)
- If N: extend monitoring 2 more weeks, re-review 6/14

**Fri 5/29 → Sun 5/31:**
- Strategy freeze takes effect (per `project_strategy_freeze_20260531.md`)
- Post-5/31 plans queue: `project_phase2_compatibility_sweep.md`, `project_llm_overlay_post_531.md`
- Capture final state: `project_fleet_master_20260531.md` snapshot

**Week 5 exit criteria:** strategy freeze active. Real-money decision made (Y or explicit defer). All explore-mode work parked.

---

## Decision gates that change the calendar

- **No WINNERS by 5/1:** extend Week 1 by 2 weeks. 5/31 freeze stays; just less to scale.
- **Critical bug found Week 2-3:** pause cull, fix bug first, restart cull.
- **Real-money gate fails ≥5 items at 5/23:** defer go-live to 6/14.
- **3+ strategies break in week 4 drills:** root-cause before any further work.

## What's deliberately NOT in this roadmap

- New strategy ideas — frozen by `project_strategy_freeze_20260531.md`
- LLM overlay work — frozen until post-5/31 per `project_llm_overlay_post_531.md`
- Instrument compatibility sweeps — phase 2, post-5/31 per `project_phase2_compatibility_sweep.md`
- Dashboard cosmetic changes — only break-fixes
- Backtest re-runs unless specifically required for a verdict

## How to apply this memory

**Why:** this is the spine. Every week's work hangs off here.

**How to apply:**
- Each Friday, mark the week's exit criteria as MET / PARTIAL / MISSED
- If MISSED, surface root cause before the next week begins
- Use this memo to push back if mid-week ideas try to derail the schedule
- Don't rewrite this memo lightly — adjust it when reality demands, not when impulse demands
