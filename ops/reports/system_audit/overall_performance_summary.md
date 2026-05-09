# Overall Performance Summary

## Current State
- Clean active post-reset PnL: $663.49
- Active post-reset closed trades: 28
- Clean active expectancy/trade: $23.6961
- Blocked Argus entries needing replay: 203
- Unique blocked entries forward-scored: 117
- Blocked-entry replay net: -214.5 pips
- Blocked-entry replay expectancy: -1.8333 pips
- Phantom PnL excluded from scoring: $7130.29

## Honest Read
Argus is profitable on the clean active slice, but not yet robust enough to scale.
The positive systems are low-sample, while VIX has weak profit factor and large drawdown relative to net PnL.
The Argus FX pairs are blocked systems, not failed trading systems, because they have no broker fills.

## Can Performance Increase, And By How Much?
Confirmed live PnL uplift available now: $0. The audit does not prove a safe live trading change yet.
Confirmed truth uplift: $7130.29 of phantom PnL is excluded from promotion math.
Blocked-entry replay result: 117 resolved entries, -214.5 net pips.
Validated throughput upside: review MTF_LONG_NOT_AT_SUPPORT in shadow only; keep the other gates based on current replay.
Profitability upside is intentionally unclaimed for live until the positive slice survives larger sample and A/B shadow validation.

## Highest-Leverage Levers
- Keep phantom trades excluded from all promotion/ROI scoring: No direct PnL uplift; prevents false allocation based on fake PnL Confidence: HIGH.
- Repair or quarantine weak VIX intraday expectancy: Avoid capital scale-up until PF and drawdown improve Confidence: MEDIUM.
- Sample GLD PM long and NQ overnight without increasing capital: Keeps current positive systems alive without overfitting Confidence: LOW_UNTIL_SAMPLE.
- Replay Argus MTF-blocked entries as counterfactuals: Keep most gates; review only the positive MTF long-not-at-support slice in shadow. Confidence: MEDIUM for gate preservation, LOW for positive slice until larger sample.
- Add SPY/QQQ/IWM as research/shadow candidates only: Increases research opportunity surface without live risk Confidence: HIGH for safety, UNKNOWN for expectancy.

## Next Validation Gate
Before any capital increase: reconcile truth, replay MTF-blocked entries, collect 30-60 clean trades per candidate, and keep SPY/QQQ/IWM in research/shadow only.
