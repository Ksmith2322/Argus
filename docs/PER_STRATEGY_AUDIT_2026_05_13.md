# Per-Strategy Deep Audit — 2026-05-13

**Context:** Multi-agent audit answering the operator's 11-question framework per strategy. Bot is at $0 real-money (RESEARCH_FREEZE on capital ladder). All strategies on IBKR paper account DUP472829.

## TL;DR — Ranked verdicts

| Rank | Strategy | Verdict | n live fills | Top issue |
|---|---|---|---|---|
| 1 | **forge_gld_pm_long** | **WINNER-CANDIDATE** | 13–20 | Recent stop-bias (3 stops, 0 targets last 7d); thesis intact. **Promote to SMOKE_5K** |
| 2 | **forge_nq_overnight** | KEEP-PAPER | 16 | IBKR signal-only fallback today; ATR floor (1.0×) may be too restrictive |
| 3 | **forge_wick_gbpusd** | KEEP-PAPER | 3 | Stable; ~1.5 trades/week as designed |
| 4 | **forge_aud_asian_breakout** | KEEP-PAPER | 1 | No 180d backtest validation post-2026-04-26 param loosening |
| 5 | **forge_vix_revert** | KEEP-PAPER (dormant) | 0 | VIX 17-19 = below 30 entry threshold. Edge intact, env unfavorable |
| 6 | **forge_jpy_pm_short** | **OPS-BLOCKED** | 8 (4 unique dates) | Stale broker position drift; cluster cap blocking entries |
| 7 | **forge_spy_trend_follower** | DIAGNOSE | 0 canonical | SPY position exists at broker (size dropped 26→1 today); canonical not reflecting it |
| 8 | **forge_tom_international** | OBSERVE (event-driven) | 6 | Exit prices not captured; next event 5/28 |
| 9 | **forge_fomc_drift** | OBSERVE (event-driven) | 1 | Exit price not captured; next FOMC 6/17 |
| 10 | **forge_rebalance** | OBSERVE (event-driven) | 0 | Waiting for S&P 500 additions (sparse, 5-10/year) |
| 11 | **argus_usdjpy** | **OPS-DEGRADED** | 4 | Heartbeat 85+ min stale; cadjpy/gbpusd same |
| 12 | **argus_gbpusd** | OPS-DEGRADED | 0 | Same as usdjpy; hour_filter disabled killing 99.95% of signals |
| 13 | **argus_cadjpy** | OPS-DEGRADED | 1 | Same; thin backtest edge (PF 1.01) |
| 14 | **forge_cuebanks** | DIAGNOSE | 0 | Fires 12+ signals/hr; blocked by cluster TOTAL_NOTIONAL cap |
| 15 | **forge_mamba** | DIAGNOSE | 0 | Synthetic 1m data invalidates wick-rejection edge |
| 16 | **forge_tori** | **REWORK CRITICAL** | 0 | Exit trail logic INVERTED — returns flat level instead of angled trendline |
| 17 | **forge_gdx_gld** | OPS-BLOCKED | 0 | TWS WinError 1225 connection refused |
| 18 | **forge_nq_london_close** | **KILL CANDIDATE** | 3 | Negative PF (0.90) + 417-contract phantom order 5/5 = $23.5M notional bug |

---

## Cross-strategy systemic issues found

### S1 — Argus heartbeat degradation (CRITICAL, operational)

Dashboard reports usdjpy/gbpusd/cadjpy heartbeats at **age=5118s (85 minutes stale)**. cadjpy flipped to LONG during the dead window. broker_equity shows $0.0 in dashboard. Runner.log shows recent writes (17:23) — so the runner is alive but heartbeats stopped refreshing. This affects ALL 3 argus pairs.

### S2 — Exit price capture bug (data integrity)

`forge_fomc_drift` (n=1 trade) and `forge_tom_international` (n=6 trades) BOTH have `exit_px=None` in signals.csv. The close_position_market path isn't capturing the fill price for these strategies. Affects event-driven strategy evaluation specifically.

### S3 — Stale broker position (forge_jpy_pm_short)

Runner reports `BROKER_HAS_POSITION: CADJPY qty=42610` blocking new entries. But the dashboard shows cadjpy FLAT. There's a state divergence between forge_jpy_pm_short's view of the broker and the actual broker. Needs manual reconciliation.

### S4 — TWS client_id 1225 errors (gdx_gld, others)

gdx_gld and possibly others fail with WinError 1225 (connection refused). Different from yesterday's stale-slot Error 326. Likely TWS connection limit OR a stale client_id from the 5/8 rotation 101→120.

### S5 — hour_filter disabled killing 99.95% of signals (argus pairs)

Per agent 1's audit: `argus_gbpusd` and `argus_cadjpy` have `hour_filter` disabled because the default profitable hours only overlap session [7-20] at 3 hours, killing 99.95% of signals. Re-enable requires backtest-validated hours.

### S6 — Phantom 417-contract NQ trade (5/5)

`forge_nq_london_close` shows trade #3 with **position_size=417 contracts** = $23.5M notional on $32K anchor. The OVERSIZE guard should have caught this but didn't (this was pre-2026-05-12 fix). Trade closed for +$8,324 USD which inflates the ROI engine's view of the strategy. **The ROI proof for nq_london_close is contaminated by this phantom.**

### S7 — Tori exit-trail logic inverted

`_find_opposing_safety` returns a flat horizontal level instead of the angled trendline the rulebook specifies. This caps winners at 1-2R and lets losers run to -1R. Mathematically, tori will lose money on every Break setup until this is fixed.

---

## Per-strategy details

(Detailed Q1-Q11 answers below — kept concise; see commit messages and runner code for citations.)

### forge_gld_pm_long — THE WINNER CANDIDATE

- **Edge thesis:** Daily PM (18-20 UTC) GLD drift bias from London-fix + US-afternoon accumulation
- **Backtest:** Phase 2C walk-forward, 6/6 folds positive, PF 1.63-2.28 at hours 18-20, t-stats 6-7 (extraordinarily strong)
- **Live:** 13-20 fills (~$401-$926 PnL), continuous since 4/23. Stop-loss bias recently but thesis holding
- **Recommendation:** **Promote to SMOKE_5K when ladder permits.** Currently the only legitimate candidate with statistical structure + positive PnL.

### forge_nq_overnight

- **Live:** 16 fills, +$399 PnL (+1.24%), IBKR went signal-only today
- **Issue:** Today's IBKR connection failures forcing signal-only fallback
- **Recommendation:** Restore IBKR connection. Watch concentration risk (PF 8.7 may collapse on remove-best-trade).

### forge_cuebanks

- **Q1 (human caps):** MAX_TRADES_PER_DAY=2 already removed today (line 90 reference-only)
- **Q5 (missing edge):** confluence detector implements 9 factors (vs 7 in docstring). BreakTracker disabled (A/B showed it filters winners). Harmonics detector coded but disabled.
- **Q7 (current):** 0 fills since reset. Fires 12+ signals/hr during NY. Blocked by cluster TOTAL_NOTIONAL.
- **Recommendation:** Once cluster cap is addressed, expect ~3-5 fills/day. Watch for confluence-score distribution.

### forge_mamba

- **Q5 (missing edge):** Synthetic 1m data — MAMBA_1M_SOURCE="real" is a stub, no Databento/Alpaca pipeline. Wick-rejection edge unreliable on synthetic 1m.
- **Q11 (boundaries):** Edge may be false until real 1m data validates the wick-rejection logic
- **Recommendation:** Build real-1m ingestion before promoting. Until then, treat as hypothesis-only.

### forge_tori

- **CRITICAL BUG:** `_find_opposing_safety` returns flat level, not angled trendline. This is the entire exit-management edge of the strategy, inverted.
- **Q9 (trading):** 0 fills + broker-cancellation issue
- **Recommendation:** Fix exit logic BEFORE worrying about broker cancels. Strategy will lose money on every Break setup until this is fixed.

### forge_nq_london_close

- **Q5 (missing edge):** RESEARCH_ONLY flag advisory only, not enforced (line 48 vs line 56 contradiction)
- **Q8 (what's hurting):** 417-contract phantom trade on 5/5 (sizing bug pre-5/12). PF=0.90 underlying.
- **Recommendation:** **KILL.** Negative PF + phantom contamination + hung process + 8-day silence = not worth debugging.

### argus pairs (usdjpy, gbpusd, cadjpy)

- **Q11 (orphan risk):** Heartbeats stale. Whether the agent's "offline 19 days" claim is correct or whether this is a recent degradation is the question. Runner.log is fresh (17:23), heartbeats are 85 min stale. Diverging streams.
- **gbpusd specifically:** hour_filter disabled, killing 99.95% of signals
- **cadjpy specifically:** thin edge (PF 1.01) needs 50-trade live validation
- **Recommendation:** Investigate heartbeat divergence first. Then if argus is healthy, run 50-trade validation for cadjpy.

### Event-driven (fomc, tom_international, rebalance)

- **Common bug:** exit_px not captured by close_position_market path
- **Cadence:** rebalance is fine waiting. tom_international next event 5/28. fomc_drift next event 6/17.
- **Recommendation:** Fix exit-price capture before next events. Don't kill any — calendar-bound by design.

### forge_gdx_gld / forge_jpy_pm_short

- **Both ops-blocked:** Different mechanisms (TWS connect vs stale position)
- **Recommendation:** Today's work. Lazy-equity fix shipped. Stale position needs manual flatten OR a reconciliation script.

---

## What this audit answered, mapped to operator's 11 questions

1. **Human-emotion restrictions:** Almost none remain. MAX_TRADES_PER_DAY=2 in mamba/cuebanks was the only one — already removed today. Some param tunings have rulebook-cited reasons (mamba 9:25-10:30 ET window, tori multi-day hold) that are defensible structurally, not emotional.
2. **Complete picture:** Yes for all strategies. Each has docstring/rulebook/spec MD or backtest reference.
3. **Tracking potential vs actual:** Mixed. signal_executor users (cuebanks/mamba/tori) now log every signal; argus uses different logging path; event-driven runners have exit-price gap.
4. **Backtests done:** All have historical backtests. Most have not been re-run post-2026-05-12 silent-gate fixes — pending data.
5. **Missing edge:** Tori (exit logic inverted), mamba (synthetic 1m), nq_london_close (overshoot threshold too low).
6. **What edge:** Documented per strategy above.
7. **Why working/not:** gld_pm_long working (positive PnL, intact thesis). cuebanks/mamba/tori not working (silent gates + downstream blockers, ALL identified).
8. **What's hurting:** Cluster cap (cuebanks/mamba), exit logic (tori), broker connectivity (gdx_gld, nq_overnight today), data capture (event-driven).
9. **Actually trading:** Yes — gld_pm_long, nq_overnight, wick_gbpusd, aud_asian_breakout. Partial — argus pairs (degraded today). No — cuebanks/mamba/tori/gdx_gld/spy_trend_follower/event-driven.
10. **True design potential:** Most strategies ~1-5 trades/week per design; cuebanks/mamba could be 5-25/day if cluster cap and operational issues clear; event-driven fire 2-12/year by calendar.
11. **Boundaries:** Documented per strategy. Common: cluster cap, broker availability, signal-detector restrictiveness.

## Recommended next-30-day priorities

### Immediate (today/tomorrow)
1. **Investigate argus heartbeat staleness** (S1) — affects 3 strategies
2. **Manual flatten of forge_jpy_pm_short stale CADJPY position** (S3) — unblocks the strategy
3. **Restart TWS to clear client_id slots** (S4) — likely resolves gdx_gld

### This week
4. **Fix tori exit-trail logic** (S7) — strategy mathematically losing until fixed
5. **Fix event-driven exit_px capture** (S2) — needed before 5/28 TOM
6. **Kill forge_nq_london_close** — phantom-contaminated, negative PF
7. **Address cluster TOTAL_NOTIONAL cap** — design question, blocking cuebanks/mamba

### This month (data-driven decisions)
8. **Promote forge_gld_pm_long to SMOKE_5K** once ladder permits + watch stop-loss bias
9. **Run 50-trade validation on argus_cadjpy** with loosened thresholds
10. **Build real-1m data pipeline** for mamba before promoting

### NEVER (without evidence)
- Re-introduce MAX_TRADES_PER_DAY caps anywhere
- Auto-quarantine without manual review (capital allocator policy)
- New strategy sleeves before 5/31 freeze

---

## Key calibration notes

- **Argus dashboard is partially LYING right now.** broker_equity=0.0, age_s=5118s on heartbeats, but runner.log is fresh. Two source-of-truth streams diverging. Investigate before trusting any single dashboard number.
- **The ROI proof CSV is contaminated** for forge_nq_london_close by the 417-contract phantom trade. Re-running won't fix it; the trade record needs explicit handling.
- **Most "not trading" strategies are silent for honest reasons** (event-driven cadence, regime-dependent like vix_revert). Only tori, mamba, cuebanks, nq_london_close have actual problems.

End of audit.
