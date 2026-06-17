---
name: 2026-05-18 pre-5/31-freeze cull audit
description: Per-strategy verdict for the 5/31 strategy freeze. 0 new kills, 3 keeps confirmed, 9 in OBSERVE bucket waiting for more data, 5 DEFER-event/mid-month.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# 5/31 pre-freeze cull audit (2026-05-18)

## Verdicts

### KEEP (3) — proven enough for freeze
| Strategy | n | Net $ | WR | Year proj | Reason |
|---|---|---|---|---|---|
| **forge_nq_overnight** | 19 | +$990 | 58% | +$15K | Cleanest signal in fleet, +61% annualized at this rate |
| **forge_gld_pm_long** | 25 | +$824 | 56% | +$13K | WINNER but sizing context changed 5/16 (21sh → 147sh cap); watch first month at new size |
| **forge_spy_trend_follower** | 0 | $0 | — | — | Passive trend-hold, currently long SPY 26sh from prior entry; works by design (no signals = correct behavior) |

### OBSERVE (9) — insufficient data, watch through 5/31
| Strategy | n | Net $ | Why observe |
|---|---|---|---|
| argus_usdjpy | 2 | +$16 | Only 2 fills — too small, watch with FX trio |
| argus_gbpusd | 0 | $0 | Had stuck SHORT cleared 5/16; sizing math overcap on every signal — investigate |
| argus_cadjpy | 0 | $0 | Had stuck LONG cleared 5/16; same sizing-overcap issue as gbpusd |
| forge_jpy_pm_short | 6 | +$213 | n=6 WR=67% — winning but sample too thin to call edge vs luck |
| forge_aud_asian_breakout | 3 | -$36 | n=3 mixed; recent +$35 winner on 5/15 |
| forge_wick_gbpusd | 0 | $0 | Was blocked by GBP cluster cap (now cleared); also had connect bug fixed 5/16 |
| forge_vix_revert | 0 | $0 | Regime-gated — VIX hasn't spiked into entry zone; harmless idle |
| forge_rebalance | 0 | $0 | Event-driven (S&P inclusion); no triggers in window — by design |
| forge_gdx_gld | 0 | $0 | Stat-arb z=-0.69 below entry threshold; was dead overnight 5/17, watchdog restarted |

### DEFER (5) — verdict at mid-month checkpoint or event
| Strategy | Verdict basis |
|---|---|
| forge_cuebanks | DEFER mid-month — just unblocked 5/16 (MYM CBOT fix), need 14d real data |
| forge_mamba | DEFER mid-month — same |
| forge_tori | DEFER mid-month — same |
| forge_fomc_drift | DEFER event — next FOMC 6/17 post-freeze, can't grade in window |
| forge_tom_international | DEFER event — next TOM entry ~5/28; verdict after that single trade |

### Already KILLED (4) — don't re-evaluate
- forge_multi_orb (5/7)
- forge_spy_mean_rev (4/30)
- forge_vix_intraday (5/12)
- forge_nq_london_close (5/13)

## Headline

**NO new kills.** With 24 days of post-conversion data and the 3 unblocked strategies just starting, insufficient evidence to kill anything new. The audit's job is honest grading, not forced trimming — and honestly, only 3 strategies have produced enough data to call.

## Expected 5/31 outcome

If the next 14 days look like the last 24:
- 3 KEEPs become real-money candidates
- 3 newly-unblocked strategies graded on their post-CBOT-fix sample
- 9 OBSERVE strategies probably stay OBSERVE (most are low-frequency or regime-gated)
- 5 DEFER stay DEFER (events are post-freeze)

**Realistic survivor count for real-money $10K deployment 6/30: 3-5 strategies.** Matches the master_game_plan_20260501 prediction of "1-3 to 3-5 realistic survivors."

## What to watch this week

1. **forge_gld_pm_long with 2.2× notional** — does per-trade edge survive 6× larger size?
2. **cuebanks/mamba/tori first fills** — Monday will be the test
3. **argus_gbpusd / argus_cadjpy** — both had stuck positions; will they actually re-engage and fire signals
4. **forensics log** at `argus_flow/logs/unfilled_orders.jsonl` — first place to check on any silent failure
5. **TWS daily disconnect window** — ~16:00-17:00 ET; all runners should auto-reconnect

## Concerns flagged

- **argus_gbpusd / cadjpy NOTIONAL_CAP warnings on every cycle** — FX sizing math is asking for 25-35× over fleet cap. Even when not stuck, the strategy may be hitting cap-rejection on most signals. Worth investigating but not urgent (the cap is doing its job; question is whether the size calc is right).
- **forge_gdx_gld dies overnight occasionally** — needs auto-restart from watchdog. Happened 5/16→5/17. Acceptable but not bulletproof.
