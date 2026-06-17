---
name: Risk guardrails for real-money phase
description: Hard rules and procedures for real-money operation. Kill switch, daily loss circuit breaker, position size caps, manual override, broker-equity drift tolerance. Designed and tested in Week 3, signed off in Week 4.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## What this covers

The infrastructure that prevents catastrophic loss when real money is in play. Most of these rules already exist in the paper system; the work for Week 3 is verifying they actually fire, documenting the procedures, and adding the few that don't exist.

## Kill switch

**Mechanism:** create file `argus_flow/logs/_locks/KILL_SWITCH` (touch is enough).

**What it does:** all 22 runners check for this file at the top of every cycle. If present, they:
- Refuse all new entries
- Optionally close existing positions at market (configurable per runner)
- Write `kill_switch_acknowledged: true` to heartbeat
- Sleep until file is removed

**Verification (Week 3 drill):**
```bash
touch c:/Argus/repo/argus_flow/logs/_locks/KILL_SWITCH
# Wait 60s
# Verify all runner heartbeats show kill_switch_acknowledged: true
# Verify no new entries in canonical_fills.jsonl over next 5 min
rm c:/Argus/repo/argus_flow/logs/_locks/KILL_SWITCH
```

**To enforce by 5/31:** every runner must have the kill-switch check. Some forge runners may not — verify each.

## Daily loss circuit breaker

**Rule:** if `argus_flow/logs/portfolio_risk_state.json` shows `current_pnl < -2% × broker_equity` over the trading day, set `drawdown_pause: true`. All runners must honor it.

**Threshold tiers (proposed):**
- -1% intraday: WARN only (Discord alert)
- -2% intraday: pause new entries (existing positions managed normally)
- -4% intraday: pause new entries + flatten all positions at next 5m boundary

**Verification (Week 3 drill):** manually inject `current_pnl: -250` into portfolio_risk_state.json on a $11.8K equity (simulating -2.1%). Verify all 22 runners pause within 60s. Restore.

## Position size caps

Per `argus_flow/configs/fleet_sizing.json` v6:
- stock / etf: 1.0× anchor (~$11.8K max per position)
- fx: 20.0× anchor (FX leverage; pip stops keep risk small)
- micro_future: 5.0× anchor
- future: 5.0× anchor

**Per-instrument cumulative cap (NEW for real money):** no single ticker (across all strategies) > 1.5× anchor. Catches the case where multi_orb + spy_mean_rev + vix_intraday all open SPY simultaneously.

**Implementation:** new check in `helio/fleet_sizing.py` that sums open positions by ticker before allowing entry. Add Week 3.

## Currency cluster cap (NEW for real money)

**Rule:** no single currency cluster > 2.5× anchor open exposure. JPY cluster = USDJPY + CADJPY + AUDJPY + EURJPY long/short combined notional. Prevents correlated FX trades stacking risk.

**Status:** not implemented. Add Week 3.

## Broker equity drift tolerance

**Rule:** if `broker_equity` and `internal_state_equity` diverge by more than 1% for 60+ minutes, halt all new entries until reconciled.

**Why:** caught us today (4/26) — argus's USDJPY orphan was the same mechanism but per-strategy. Generalize to fleet-wide: when fleet thinks it's flat-and-up but broker shows different equity, something is wrong.

**Status:** RECON_DRIFT exists per-pair in argus. Need fleet-wide aggregator. Add Week 3.

## Manual override procedure

**Single-button "pause everything and flatten by EOD":**

```bash
# 1. Create the kill switch (halts entries immediately)
touch c:/Argus/repo/argus_flow/logs/_locks/KILL_SWITCH

# 2. Set EOD flatten flag (closes positions at next valid boundary)
echo '{"flatten_at_eod": true, "reason": "manual override", "ts": "...", "by": "ksmith2322"}' > c:/Argus/repo/argus_flow/logs/_locks/FLATTEN_EOD

# 3. Discord notification fires automatically via the alert helper
```

**To resume:**
```bash
rm c:/Argus/repo/argus_flow/logs/_locks/KILL_SWITCH
rm c:/Argus/repo/argus_flow/logs/_locks/FLATTEN_EOD
# Verify reconciliation clean before letting runners resume
```

**Status:** KILL_SWITCH partial; FLATTEN_EOD doesn't exist yet. Add Week 3.

## Real-money-specific rules (transition phase)

When the user enables real-money (post-5/31), these rules apply for the first 30 days:

1. **Real-money risk_pct halved** vs paper. If paper tier A is 2%, real tier A starts at 1%.
2. **Daily trade count cap:** max 10 entries per strategy per day, max 30 fleet-wide. Prevents runaway loops.
3. **One-week cooldown after kill switch:** if kill switch was used, no real-money trading resumes for 7 days. Forces a real review, not just "unpause."
4. **Manual review of every trade for first 5 days:** every fill emits a Discord alert with full context. User reviews each.
5. **No promotion ladder for first 30 days:** real money tiers are FROZEN at the values set on the go-live date. No adaptive bumping.

## Drill schedule (Week 3)

| Day | Drill | Pass criteria |
|---|---|---|
| Mon 5/11 | Kill switch | All 22 runners halt entries within 60s of file creation |
| Tue 5/12 | Daily-loss circuit breaker | drawdown_pause fires when -2% injected |
| Wed 5/13 | TWS-down | Runners log BrokerEquityUnavailableError, take no silent action |
| Thu 5/14 | Per-instrument cap | Two strategies trying SPY simultaneously, second one rejected |
| Fri 5/15 | Manual override (full flow) | KILL+FLATTEN combo halts entries + closes positions by EOD |

## How to apply this memory

**Why:** the difference between paper and real money is whether bugs cost real dollars. These guardrails define what's allowed to fail without catastrophe.

**How to apply:**
- Week 3 work plan is taken from this memo directly
- Any new strategy added pre-5/31 must verify it honors KILL_SWITCH (drill it once)
- If a drill fails, that's a hard blocker for the 5/31 real-money gate — not optional
