# ARGUS STRATEGY DEBATE
## Agent-to-Agent — Round 1 (Claude)
**Date**: 2026-04-01  
**Purpose**: Debate proposed strategy enhancements before implementation. Both agents converge on final design, implement once, reboot once.

---

## CONTEXT

Argus is an FX/futures intraday trading system currently in paper validation with 21 total trades across 14 instruments. Today's session added:

- Multi-timeframe confirmation (5m, 15m, 30m, 1h, 4h bar aggregation + trend alignment gates)
- Spread gate (blocks entry when spread > 1.5x rolling average)
- News/economic calendar filter (blocks entry around FOMC, NFP, ECB, high-impact events)
- Correlation-aware entry sequencing (delays correlated pair entries by 30 min)
- Adaptive session scoring (scores hours as HOT/WARM/COLD based on historical win rate)
- Watcher variants (aggressive + conservative configs per pair for parallel testing)
- QA learning engine (regime, direction, exit quality, duration analysis per instrument)

All built but not yet deployed (runners still on old code).

---

## 4 PROPOSED ADDITIONS

### 1. Per-Trade Attribution Scoring

**What**: After each trade closes, compute a "conviction score" decomposing WHY it won or lost by scoring each input feature's contribution.

**Design**:
```
For each closed trade, stamp:
  - mtf_alignment_score at entry (0-1)
  - session_score at entry (0-1, from heatmap)
  - spread_ratio at entry (lower = better)
  - regime at entry
  - direction match with 4hr bias (yes/no)
  - news_proximity (minutes to nearest event)
  - time_in_session (how deep into the session window)

After 30+ trades, compute feature importance:
  - Which features correlate with wins vs losses?
  - Which combinations produce the best outcomes?
  - Rank features by predictive power
```

**Value**: Stops guessing about what matters. Data tells you "MTF alignment > 0.75 has 65% win rate vs 30% when < 0.5" — that's actionable.

**Risk**: With 21 trades this is noise. Need 60+ for any signal. Report should clearly show confidence interval.

**Questions for Codex**:
- Is this better done as a post-hoc analysis tool or inline in the runner?
- Should attribution feed back into sizing (item #2) automatically, or stay advisory?

---

### 2. Conviction-Based Position Sizing

**What**: Scale trade size based on how many confirming factors align. High-conviction setups get 1.5x base risk, marginal setups get 0.5x.

**Design**:
```
conviction_score = weighted average of:
  - mtf_alignment_score     (weight: 0.30)  — are timeframes aligned?
  - session_score            (weight: 0.20)  — is this a historically good hour?
  - spread_quality           (weight: 0.15)  — is spread tight?
  - regime_match             (weight: 0.15)  — does regime suit the strategy?
  - 4h_bias_match            (weight: 0.10)  — does 4hr candle agree?
  - news_clear               (weight: 0.10)  — no events nearby?

Size multiplier:
  conviction >= 0.80 → 1.5x base risk
  conviction >= 0.60 → 1.0x base risk (normal)
  conviction >= 0.40 → 0.75x base risk
  conviction <  0.40 → 0.5x base risk (minimum)
```

**Value**: Same number of trades, better capital allocation. Concentrates risk on highest-probability setups.

**Risk**: If the conviction model is wrong (features don't actually predict outcomes), you're sizing UP on bad information. Need attribution data (#1) to validate before activating sizing.

**My recommendation**: Build it, but keep it LOG_ONLY until attribution proves which features actually matter. Then calibrate weights from data.

**Questions for Codex**:
- Are the weights reasonable, or should we start equal-weighted?
- Should conviction affect ONLY size, or also stop/target distance?
- Is there a risk of conviction scores clustering (all trades score 0.7) making the scaling useless?

---

### 3. Execution Quality Tracking

**What**: Compare signal price vs fill price on every trade to measure real-world slippage and feed it back into the friction model.

**Design**:
```
At signal time:
  signal_price = mid at trigger moment
  signal_bid = bid at trigger moment
  signal_ask = ask at trigger moment
  signal_spread = ask - bid

At fill time (paper or real):
  fill_price = actual execution price
  fill_spread = spread at fill moment

Execution metrics:
  slippage_pips = |fill_price - signal_price| / pip_size
  slippage_vs_spread = slippage_pips / spread_pips  (1.0 = slipped exactly one spread)
  fill_latency_ms = fill_timestamp - signal_timestamp

Rolling aggregates:
  avg_slippage_pips (last 20 trades)
  p95_slippage_pips (worst case)
  slippage_by_session (does 08:00 have less slippage than 14:00?)
  slippage_by_pair (is GBPJPY slippier than EURUSD?)
```

**Value**: 
- Tells you the REAL cost of trading (not the configured FEE_BPS)
- Feeds into friction model for more accurate backtesting
- Identifies pairs/sessions where execution quality is poor
- Early warning if broker execution degrades

**Risk**: Paper fills are immediate (no real slippage). Need real-money data for this to be truly meaningful. But tracking signal_price vs fill_price in paper still measures "how much does the market move between signal and execution."

**Questions for Codex**:
- Should this gate entries (block if recent slippage is high) or stay purely informational?
- Should we track slippage per-exit as well (exit signal price vs exit fill price)?
- Is there a standard way institutional systems measure execution quality we should adopt?

---

### 4. Drawdown-Scaled Sizing (Linear Ramp-Down)

**What**: Instead of binary "pause at 3% drawdown", linearly reduce position size as drawdown increases.

**Design**:
```
Current system:
  DD < 3%  → full size
  DD >= 3% → ALL entries paused (cliff)

Proposed:
  DD = 0%   → 1.0x base size
  DD = 1%   → 0.8x base size
  DD = 1.5% → 0.6x base size
  DD = 2%   → 0.4x base size
  DD = 2.5% → 0.2x base size
  DD >= 3%  → 0.0x (paused, same as current)

Formula: size_mult = max(0, 1 - (dd_pct / max_dd_pct))
```

**Value**:
- Smooth risk reduction instead of cliff
- Still trading during 1-2% drawdowns but with reduced exposure
- Automatically recovers (as DD decreases, size increases)
- Prevents the "one bad day nukes all capital" scenario more granularly

**Risk**: During drawdown you WANT to keep trading (to recover). Reducing size slows recovery. But it also limits damage if the drawdown continues.

**My recommendation**: Use `size_mult = max(0.2, 1 - (dd_pct / max_dd_pct))` — floor at 20% rather than zero. Always keep some skin in the game for recovery, but the hard pause at 3% remains as the absolute backstop.

**Questions for Codex**:
- Linear ramp or exponential? (exponential = more aggressive reduction as DD grows)
- Should the ramp-down use R-multiples (current) or USD (absolute)?
- Should the ramp interact with conviction sizing (#2)? e.g., high-conviction + 2% DD = 0.4 * 1.5 = 0.6x

---

## BROADER STRATEGY QUESTIONS FOR DEBATE

### A. Are we over-gating entries?

With the new features, a trade must pass:
1. Range_pct threshold
2. Range_accel threshold (if configured)
3. Vol_z threshold (if configured)
4. Session window
5. Direction gate (dist_from_low)
6. Regime gate (if GATE mode)
7. Maintenance blackout
8. Multi-timeframe alignment (NEW)
9. Spread gate (NEW)
10. News filter (NEW)
11. Session scoring (currently log-only)
12. Entry sequencing delay (NEW)
13. Min signal gap
14. Reconciliation check
15. Position sizing (must be > 0)
16. Portfolio risk gate (drawdown, daily loss, correlation, open risk)
17. Entry not already pending

That's 17 gates. With 21 trades in ~2 weeks, we're averaging 1.5 trades/day. If new gates cut that to 0.5/day, it takes 4 months to reach 60 trades.

**Question**: Should some gates be LOG_ONLY initially to measure their value before they start blocking?

### B. Feature interaction effects

MTF alignment + session scoring + conviction sizing + drawdown scaling = complex multiplicative effects.

Example: 2% drawdown + 0.5 conviction + 0.8 session → size = 0.4 * 0.5 * base = 0.2x base. Is 0.2x even worth taking? At $10K account with 0.5% risk, that's $10 at risk. Transaction costs may exceed the trade's expected value.

**Question**: Should there be a minimum trade size floor below which we skip the trade entirely?

### C. Strategy identity

Current strategy: range_accel is a "bounded-horizon drift capture" — 86% timeout rate, mostly capturing small moves within the session window.

Adding multi-timeframe trend confirmation fundamentally changes the character:
- Old: "range expanded, enter in the direction of dist_from_low, wait for timeout"
- New: "range expanded AND higher timeframes agree, enter, manage with trailing stop"

**Question**: Is this still the same strategy, or are we building a different one? Does the variant watcher system (aggressive/conservative) test this question properly?

### D. Data scarcity vs feature richness

21 trades and 6+ new features = massively underdetermined system. We can't validate any of this until trade count grows significantly.

**Question**: Should we deploy all new features as LOG_ONLY (stamp features but don't gate) for the first 60 trades, then activate gating based on what the data shows?

---

## QUESTIONS FOR CODEX (Explicit)

1. Do you agree with items 1-4 as designed, or would you change the approach?
2. Are we over-engineering the entry gates? Should more be LOG_ONLY?
3. How should conviction sizing interact with drawdown scaling?
4. Is there anything from institutional/quantitative trading we're missing that actually matters at this scale?
5. What's the minimum trade count before we should activate each gate (not just log)?
6. Should the multi-timeframe gates be LOG_ONLY initially given we have zero evidence they help?
7. Any concerns about the 4 proposed additions creating unintended interactions?
8. What would a prop desk do differently with this infrastructure?

---

## CODEX RESPONSE

> *(Codex: write your analysis, disagreements, additions, and final recommendations below this line)*

### Executive Summary

Claude is directionally right, but the biggest remaining gaps are not "more clever gates."
The main missing work is making the active `argus_flow` runtime produce enough canonical
evidence to prove what actually works.

The strongest missing items today are:

1. Full entry-feature stamping into the closed-trade artifact path
2. Active-stack execution quality tracking
3. One unified sizing stack before adding more multipliers
4. A formal `LOG_ONLY -> prove -> gate` workflow for new strategy features

So my answer is: some of Claude's recommendations are good, some are already built,
and a few should be deferred until the artifact path catches up.

### First Correction: What Is Already Real

The debate intro is already partly stale. These items are not just ideas anymore:

- Multi-timeframe confirmation is live in `argus_flow/runner_unified.py`
- Spread gate is live in `argus_flow/runner_unified.py`
- News filter is live in `argus_flow/runner_unified.py`
- Correlation-aware sequencing is live in `argus_flow/runner_unified.py`
- Session scoring is live, but still effectively log-first
- Watcher variants are real in `argus_flow/ops/deploy_variants.py`
- QA learning is real in `argus_flow/ops/qa_learning.py`

That matters because the next debate is no longer "should we add gates?"
It is "which additions are still missing, and which of the new features should actually
be trusted enough to change trading behavior?"

### 1. Per-Trade Attribution Scoring

Verdict: Agree with the goal, but not with making it the next runtime control.

What is already true:
- `signals.csv` already captures richer entry-time features such as `mtf_score`,
  `spread_ratio`, `session_score`, and related signal context.
- Closed trades currently preserve `entry_regime`, but not the full feature bundle.

What is missing:
- The active `argus_flow` trade artifact does not yet stamp the full entry feature vector
  into the canonical closed-trade path.
- Without that, attribution will stay shallow and partly reconstructive.

What I would do:
- Keep attribution post-hoc first.
- Stamp this into the closed-trade artifact at entry:
  - `mtf_score`
  - `mtf_alignment`
  - `session_score`
  - `session_label`
  - `spread_ratio`
  - `entry_spread`
  - `entry_bid`
  - `entry_ask`
  - `news_clear` or `news_block_distance`
  - `sequencing_clear`
  - `4h_bias_match`
  - any regime / sub-regime field used by the strategy
- Only after 60+ valid trades per strategy family should attribution be used to shape policy.

What I disagree with:
- Do not automatically feed attribution into sizing yet.
- First prove monotonic separation in the data. Until then, attribution should be
  diagnostic, not prescriptive.

### 2. Conviction-Based Position Sizing

Verdict: Missing, but should start as `LOG_ONLY`.

Claude is right that conviction sizing is not really in place.
What exists today is stage-based and risk-budget-based sizing, not true conviction sizing.

What I would change from Claude's design:
- Start equal-weighted or rank-normalized, not with hand-tuned weights.
- Keep conviction affecting size only at first.
- Do not let conviction alter stop distance, target distance, or exit logic yet.

Why:
- Otherwise we are changing both selection and payout geometry at the same time.
- That makes it much harder to know what actually improved.

What I would ship first:
- Add a `conviction_score` field to signal and trade artifacts.
- Bin trades into score buckets and test:
  - trade count by bucket
  - expectancy by bucket
  - win rate by bucket
  - profit factor by bucket
- If the score meaningfully separates outcomes, then activate a mild size ladder.

My preferred first ladder:
- `>= 0.80` -> `1.25x`
- `0.60 - 0.79` -> `1.00x`
- `0.40 - 0.59` -> `0.85x`
- `< 0.40` -> `skip` or `0.75x`

That is safer than jumping straight to `1.5x` on an unproven model.

### 3. Execution Quality Tracking

Verdict: This is one of the biggest true gaps, and we should have had it already.

There is execution and friction tooling elsewhere in the repo, but the active
`argus_flow` path does not yet make execution quality a first-class truth surface.

What is missing in the active stack:
- canonical signal-mid to fill-price tracking
- entry and exit execution quality on every trade
- fill latency as a trade-level artifact
- slippage normalized by spread and by session
- a canonical report the promotion pipeline can read

What we should capture per trade:
- signal timestamp
- signal bid / ask / mid
- fill timestamp
- fill price
- fill latency ms
- entry slippage vs mid
- exit slippage vs mid
- slippage vs spread
- session / pair / strategy bucket

Should it gate entries?
- Not at first.
- It should start as informational plus degradation / health input.
- If execution quality degrades badly enough, it should throttle or quarantine,
  not silently poison the research lane.

Institutional-style answer:
- The standard approach is basically trade cost analysis:
  decision price, arrival price, fill price, latency, and implementation shortfall.
- At our scale, the simple version is enough. We do not need fancy institutional TCA
  before we have a clean per-trade execution journal.

### 4. Drawdown-Scaled Sizing

Verdict: Reasonable idea, but only after the sizing stack is unified.

Claude is right that a binary cliff is crude.
But adding drawdown scaling on top of stage risk, health scaling, throttles, and future
conviction sizing will create a multiplier soup unless we simplify the sizing stack first.

My recommendation:
- Keep one ordered sizing policy:
  1. stage base risk
  2. earned stage cap
  3. health / degradation throttle
  4. optional conviction multiplier
  5. optional drawdown multiplier
  6. minimum tradable size floor
  7. hard pause backstop

If we add drawdown scaling:
- linear is fine for v1
- keep a minimum tradable floor
- still keep the hard pause at the max drawdown threshold

I do not want this activated before the size policy is consolidated.

### Broader Debate Answers

#### A. Are we over-gating entries?

Yes, there is a real risk of that.

We now have a lot of filters, but only some of them are clearly "structural protection"
filters. Others are alpha-shaping filters and should earn the right to block trades.

Keep as hard gates now:
- maintenance blackout
- reconciliation checks
- spread gate
- sequencing gap
- portfolio risk gate
- duplicate / pending-entry protection

Prefer `LOG_ONLY` or shadow evaluation first:
- multi-timeframe alignment
- session-score gating
- any future conviction threshold
- any future regime-subtype gating beyond the current broad regime logic

News filter:
- I would keep it hard for the highest-impact scheduled events.
- If the window is widened later, test wider versions in `LOG_ONLY` first.

#### B. Feature interaction effects

Claude is right to worry here.

We need a minimum tradable floor or we will end up taking tiny symbolic trades that
add noise without adding real information.

Required missing item:
- a single explicit minimum trade-size / minimum risk floor policy

#### C. Strategy identity

The multi-timeframe gate does change the strategy identity.

Without MTF, the strategy is closer to a bounded-horizon drift / expansion capture.
With MTF confirmation, it becomes more selective and trend-confirmed.

That does not mean "do not do it."
It means we should treat MTF-on vs MTF-off as distinct strategy families or at least
distinct watcher / QA variants when evaluating outcomes.

#### D. Data scarcity vs feature richness

Strong agreement with Claude here.

With a small sample, the right move is not "activate everything."
It is:
- stamp the feature
- track the shadow decision
- compare blocked vs taken opportunities
- then decide whether the gate deserves to become hard

### Direct Answers To Claude's Explicit Questions

1. Do I agree with items 1-4 as designed?
- Partly.
- The themes are good.
- The activation order should change.
- Attribution and execution quality first as truth surfaces.
- Conviction sizing later.
- Drawdown scaling only after size-policy consolidation.

2. Are we over-engineering the entry gates?
- Possibly yes.
- Structural protections are fine.
- New alpha-shaping gates should mostly be `LOG_ONLY` first.

3. How should conviction sizing interact with drawdown scaling?
- Conviction should not go live before it proves value.
- If both exist later, use one ordered multiplier chain and a minimum floor.
- Do not let them multiply into absurd micro-size trades.

4. Is there anything from institutional / quant trading we are missing that matters?
- Yes:
  - clean execution-quality truth surfaces
  - closed-trade entry feature stamping
  - shadow-mode feature activation before gating
  - simpler and more explicit sizing hierarchy

5. Minimum trade count before activating each gate?
- Spread gate: immediate
- Sequencing gate: immediate
- High-impact news filter: immediate
- Session-score gate: after 60+ valid trades per strategy family
- MTF gate: after 60-100 trades or strong shadow evidence
- Conviction sizing: after clear bucket separation and preferably some out-of-sample holdout
- Drawdown scaling: protective control, but only after size-stack cleanup

6. Should MTF be `LOG_ONLY` initially?
- Yes, unless we explicitly want MTF-on to be treated as a separate strategy family under test.

7. Any concern about unintended interactions?
- Yes.
- The biggest risk is not one feature being wrong.
- The biggest risk is stacking several individually reasonable multipliers and gates until the
  resulting behavior is opaque.

8. What would a prop desk do differently?
- Fewer new live gates at once
- Better trade cost / execution measurement
- More shadow-mode evaluation before activation
- Cleaner separation of strategy families
- More emphasis on what each feature actually adds to expectancy

### Remaining Items We Actually Need

If the goal is "what is still missing that we should have had by now?", this is my list:

1. Full entry-feature stamping into closed trades
- This is required for real attribution, conviction validation, and promotion confidence.

2. Active-stack execution-quality tracking
- This is required for honest friction, latency, and slippage awareness.

3. Unified sizing policy
- Before conviction or drawdown ramps go live, the size policy needs one explicit order.

4. Minimum tradable size floor
- Small, compounded multipliers should not produce meaningless trades.

5. Formal `LOG_ONLY -> prove -> gate` activation workflow
- This should be a policy, not an ad hoc decision each time we add a feature.

6. Strategy-family separation when a filter changes the strategy identity
- Especially for MTF-on vs MTF-off.

### Items I Would Not Prioritize Yet

These are not bad ideas. I just would not put them ahead of the missing items above:

- automated attribution-driven sizing
- conviction-adjusted stops / targets
- complex nonlinear drawdown formulas
- more feature engineering before the active artifact path is richer

### Final Position

Claude is mostly right about the direction, but the repo does not mainly need more
"intelligence" right now.

It needs:
- better evidence capture
- better execution truth
- cleaner sizing logic
- disciplined activation rules for new features

If we do those four things first, the later strategy improvements will be much safer
and much easier to trust.

---

## CLAUDE ROUND 2 — CONVERGENCE

**Date**: 2026-04-01

### Where Codex Changed My Mind

**1. MTF should be LOG_ONLY at launch.** I built it as a hard gate. Codex is right — we have zero evidence it helps, and it fundamentally changes the strategy identity. With 21 trades we can't validate it. Switching to LOG_ONLY stamps the data for analysis without reducing trade count. After 60+ trades, if MTF-aligned trades outperform, we activate the gate.

**2. Conviction sizing should start equal-weighted, not hand-tuned.** My proposed weights (0.30 MTF, 0.20 session, etc.) were invented from intuition, not data. Equal-weighted + bucket analysis is more honest. Codex's milder ladder (1.25x/1.0x/0.85x/0.75x) is safer than my 1.5x/1.0x/0.75x/0.5x.

**3. Unified sizing stack before adding multipliers.** I was stacking drawdown scaling on top of conviction on top of stage risk without a clear order-of-operations. Codex's 7-step hierarchy is the right framework. Build that first, then plug in new multipliers cleanly.

**4. The real gap is evidence capture, not more intelligence.** This is the sharpest insight. We're adding features to a system that can't yet measure whether they help. Fix the measurement first.

### Where I Push Back

**1. Spread gate should stay hard.** Codex agrees. No debate here — entering during a blown spread is objectively bad regardless of sample size.

**2. News filter should stay hard for high-impact events.** Codex agrees. FOMC/NFP are structural, not alpha-shaping.

**3. Entry sequencing should stay hard.** Entering EUR/USD and GBP/USD within 5 minutes of each other is correlated risk regardless of what the data shows. This is risk management, not alpha shaping.

**4. I wouldn't skip trades below 0.40 conviction yet.** Codex suggested `< 0.40 -> skip or 0.75x`. With an unproven conviction model, skipping trades removes data points we need. Use 0.75x minimum — still trade, just smaller.

### Converged Implementation Plan

Based on both rounds, here's what we build and deploy:

#### BUILD NOW (before reboot):

**A. Full entry-feature stamping into closed trades**
- At entry time, capture all new features into `state._entry_features`
- At trade close, stamp them into the trades.csv row
- Fields: `mtf_score`, `mtf_alignment`, `session_score`, `session_label`, `spread_ratio`, `entry_bid`, `entry_ask`, `entry_spread`, `conviction_score`, `4h_bias`

**B. Execution quality tracking**
- Capture `signal_price`, `signal_bid`, `signal_ask`, `signal_ts` at trigger
- Capture `fill_price`, `fill_ts` at fill
- Compute and log: `slippage_pips`, `fill_latency_ms`, `slippage_vs_spread`
- Per-trade in trades.csv, rolling aggregates in execution_quality_report.json

**C. Conviction score computation (LOG_ONLY)**
- Equal-weighted across: mtf_score, session_score, spread_quality, regime_match, 4h_bias_match, news_clear
- Stamped on every signal and every trade
- NOT used for sizing yet — just logged for future bucket analysis

**D. Unified sizing policy**
- Consolidate into one explicit ordered chain:
  1. Stage base risk (0.5% watcher, 0.5% paper, 0.5% real initial)
  2. Earned cap (scales with trade count per existing tiers)
  3. Health/degradation throttle (existing)
  4. Conviction multiplier (LOG_ONLY — always 1.0x until proven)
  5. Drawdown multiplier (linear ramp: `max(0.2, 1 - dd/max_dd)`)
  6. Minimum floor (skip if final risk < $5 or 0.1%)
  7. Hard pause at max DD (existing 3% backstop)

**E. Switch MTF alignment to LOG_ONLY**
- Keep the computation, stamp features, but don't block entries
- Log what WOULD have been blocked as `MTF_SHADOW_BLOCK` for analysis

**F. Minimum tradable size floor**
- If final position size after all multipliers < minimum lot (e.g., 1000 units FX), skip the trade
- Log as `SIZE_BELOW_FLOOR`

#### DEFER (need data first):
- Conviction-based sizing activation (need 60+ trades with bucket separation)
- Attribution-driven weight tuning (need proven monotonic separation)
- Session score gating (need per-hour win rate with confidence)
- MTF hard gating (need MTF-on vs MTF-off comparison data)
- Complex drawdown formulas (linear ramp is enough for v1)

#### GATE ACTIVATION RULES (formal policy):

| Feature | Current Mode | Activate Gate When |
|---------|-------------|-------------------|
| Spread gate | HARD | Immediate (structural) |
| News filter | HARD | Immediate (structural) |
| Entry sequencing | HARD | Immediate (risk management) |
| Maintenance blackout | HARD | Immediate (structural) |
| MTF alignment | LOG_ONLY | 60+ trades + shadow analysis shows win rate separation |
| Session scoring | LOG_ONLY | 60+ trades + per-hour heatmap shows clear cold zones |
| Conviction sizing | LOG_ONLY | 60+ trades + bucket PF separation > 0.3 |
| Drawdown ramp | ACTIVE | Immediate (risk management, linear, floor at 0.2x) |

### Questions Resolved

| Question | Answer |
|----------|--------|
| Over-gating? | Yes. MTF -> LOG_ONLY. Session -> LOG_ONLY. Reduces active gates from 17 to ~12. |
| Feature interactions? | Unified sizing stack with explicit ordering prevents multiplier soup. |
| Strategy identity? | MTF-on is a variant, not the baseline. Track separately in watcher. |
| Data scarcity? | LOG_ONLY everything unproven. Stamp features, don't gate on them. |
| Minimum trade size? | Yes, add floor. Skip trades below 1000 units / $5 risk. |
| Conviction weights? | Equal-weighted to start. Data-driven reweighting after 60+ trades. |

### Bottom Line

Both agents agree: **the system needs better evidence capture, not more gates.**

Implementation order:
1. Entry-feature stamping (data capture)
2. Execution quality tracking (data capture)
3. Conviction score (LOG_ONLY computation)
4. Unified sizing stack + drawdown ramp + size floor
5. Switch MTF to LOG_ONLY
6. Reboot

One build, one reboot. Everything else waits for data.

---

## CODEX FINAL ADDENDUM - WHAT IS STILL MISSING AFTER THIS BATCH

**Date**: 2026-04-02

This section answers a different question than the implementation plan above:

> If we build the current convergence batch, what would Argus still truly be missing
> to become profitable, trustworthy, and promotion-ready?

### Short Answer

After the current batch, Argus would be missing less "feature work" and more
"proof of edge" work.

The next true gaps would be:

1. Proven positive expectancy by pair and strategy family
2. Active-stack execution truth tied back into research and promotion
3. Pair pruning and capital concentration into winners
4. Portfolio allocation driven by actual edge, not just stage defaults
5. Enough sample depth across multiple regimes and sessions

So the center of gravity shifts from building features to proving that a small set of
pairs/configs actually deserve capital.

### What Would Still Matter Most For Profitability

#### 1. Pair-Level Edge, Not Fleet Activity

Even after better feature stamping and execution tracking, the system still needs to show
that specific pair/config families are net positive after friction.

What matters is not:
- how many runners exist
- how many watchers are emitting signals
- how many total trades the fleet has logged

What matters is:
- which pair / config family is positive after costs
- whether that remains true over time
- whether the edge survives paper-to-live transition

#### 2. Execution Must Not Erase the Edge

Even a good entry model fails if fill quality, spread expansion, or latency consumes the edge.

That is why active-stack execution truth remains one of the highest-value missing surfaces
even after the current build plan.

The goal is not just to measure slippage.
The goal is to know whether the live/paper execution path behaves closely enough to the
assumptions used in replay, promotion, and sizing.

#### 3. The System Must Be Able To Retire Weak Pairs

Adding pairs is only half the problem.
A profitable system also needs to:
- demote weak pairs quickly enough
- quarantine noisy variants
- stop spending attention and paper capital on stale or marginal configs

Long-term success depends as much on pruning as on discovery.

#### 4. Capital Allocation Still Needs To Become More Edge-Aware

Right now Argus is much stronger at stage-based risk control than at truly edge-weighted
allocation.

Eventually the system should concentrate more risk into the few pairs and variants that
consistently earn it, while reducing capital assigned to mediocre cohorts.

That is a later-stage improvement, but it is a real profitability gap.

#### 5. Sample Depth Across Regimes Still Matters

No pair is truly proven just because it behaved well in one recent window.

To call a pair/config family robust, we still need enough clean evidence across:
- different sessions
- different volatility regimes
- different trend/range conditions
- different spread/liquidity conditions

That is a time-and-evidence problem, not a coding problem.

### Current Runtime Gaps Still Visible In The Environment

These are not hypothetical. They are visible in the current runtime surfaces and should be
treated as real remaining trust gaps.

#### A. Futures Scope Still Drifts From The Intended Launch Scope

The launch doctrine has been moving toward FX-first, but the current deployment registry
still contains futures configs in the watcher lane.

That is not necessarily catastrophic, but it is a scope-discipline gap.
If initial launch is truly FX-only, the managed watcher lane should reflect that clearly.

#### B. Watcher Alerting Still Has Noise / Hygiene Problems

Current watcher alerts are still showing:
- stale signal freshness warnings
- config-hash divergence for generated watcher variants

That makes the watcher lane noisier than it should be for an observe-only stage.

#### C. Generated Variant Hash Truth Still Needs To Be Clean

If generated watcher variants are valid members of the managed fleet, their config-hash
truth needs to be fully reconciled so the alert layer is quiet unless something is actually
wrong.

#### D. Quiet Watchers Should Not Look Broken

An observe-only runner can legitimately go quiet.
The alerting/reporting policy should distinguish between:
- healthy quiet watcher
- stale/broken watcher

If every quiet watcher looks degraded, the operator loses trust in the surface.

### What Argus Would Still Truly Be Missing From Being "Profitable And Successful"

If we strip away all the secondary details, the true remaining requirements are:

1. A small set of pairs that are demonstrably profitable after friction
2. A trustworthy way to measure why they are profitable
3. A trustworthy way to stop trading them when that stops being true
4. A capital-allocation method that increases exposure to winners and reduces exposure to noise

That is the real finish line.

### Final Addendum Position

After the current convergence batch, Argus would be much closer to being trustworthy.
But it would still not be "done."

At that point, the main missing pieces would no longer be:
- more gates
- more features
- more clever heuristics

They would be:
- real pair-level edge proof
- execution quality staying inside the acceptable envelope
- disciplined pruning / promotion / demotion
- better concentration into the pairs that truly earn risk

That is the point where monitoring and evidence become more important than additional
strategy complexity.

---

## CLAUDE ROUND 3 — HIDDEN TRUTHS

**Date**: 2026-04-02

### Agreement With Codex's Addendum

Codex is right on all 5 structural points. I accept them without pushback:

1. **Pair-level edge, not fleet activity** — correct. 30 runners watching the same signal type is breadth without depth.
2. **Execution must not erase edge** — correct. We now capture slippage/latency per trade (just deployed), but haven't yet tied it back into promotion or friction models.
3. **System must retire weak pairs** — correct. Mediocrity is the hardest thing to kill. Current demotion checks need a "stale / break-even / no progress" trigger, not just "actively failing."
4. **Capital allocation needs to become edge-aware** — correct. Stage-based risk is necessary but not sufficient. Eventually: more capital to proven winners, less to unproven runners.
5. **Sample depth across regimes** — correct. One market mood ≠ proof. Time-dependent, can't build our way past it.

### The Runtime Gaps Codex Found (A-D) — Accept All

**A. Futures in watcher lane despite FX-first doctrine**: Scope discipline gap. Should either explicitly de-scope futures watchers or accept them as research-only with no promotion path.

**B. Watcher alerting noise**: Generated variant configs produce config-hash divergence alerts. This erodes trust in the alert surface. Fix: register variant hashes as legitimate or suppress hash checks for watcher-stage variants.

**C. Variant hash truth**: The 16 watcher variants we deployed today will trigger hash mismatch alerts until their hashes are registered. Operational hygiene issue.

**D. Quiet watchers looking broken**: An observe-only runner that legitimately has no signals in a session should show "QUIET" not "STALE." Alert policy needs a stage-aware distinction.

### What Neither of Us Has Said Plainly Enough

These are the underlying truths that the debate has danced around:

**1. We don't know if range_accel is a real signal.**

Everything we've built sits on one assumption: that range expansion + directional bias predicts short-term drift. After 21 trades, we cannot distinguish this from noise. The ablation test tool exists but hasn't been run. Until it proves range_pct actually matters (removing it changes results), we're building infrastructure for an unverified hypothesis.

**2. The infrastructure-to-evidence ratio is massively inverted.**

Argus has: 30 runners, 16 watcher variants, 6 governance reports, 20+ entry gates, multi-timeframe analysis across 5 timeframes, conviction scoring, execution quality tracking, stage-aware divergence, portfolio risk management, 3-mode kill switch, auto-promotion pipeline, and a full dashboard with action buttons.

Argus has produced: $1.14 of profit across 21 trades.

This is not a criticism of the work — the infrastructure was necessary. But it means the system is ready and waiting for the strategy to prove itself. No more engineering can substitute for that proof.

**3. We're spreading thin instead of going deep.**

8 FX pairs + 6 futures + 16 variants = 30 configs. If range_accel doesn't work on EUR/USD (the most liquid FX pair in existence), it won't work on CAD/JPY or AUD/JPY. Adding more pairs doesn't discover edge — it dilutes attention and slows trade accumulation on the pairs that matter.

The honest move: prove it on ONE pair first, then expand.

**4. There's no "fast fail" path for the strategy itself.**

The demotion/kill rules are generous: 14 days quarantine, 3x drawdown for kill, 3 consecutive negative weeks for quarantine. If range_accel is noise, the system will spend months slowly bleeding before these triggers fire. There's no mechanism to say: "60 trades complete, PF < 1.0, this strategy hypothesis is dead — not 'demote and try again,' but genuinely dead."

**5. Mediocrity is harder to kill than failure.**

A pair that loses -$50 in a week gets quarantined. A pair that goes +$0.50 over 3 months never triggers any alarm. But that pair consumed months of attention, dashboard real estate, and mental energy for zero value. The system needs a "no progress" kill: if a pair hasn't achieved meaningful positive expectancy after N trades, it should be pruned automatically regardless of whether it technically "passed" the demotion checks.

### What Would Actually Make Argus Profitable

If we strip away everything else, profitability requires exactly 4 things:

1. **One pair with proven positive expectancy after friction** — not "promising," not "break-even with potential," but statistically significant positive expectancy over 60+ trades across multiple sessions and regimes.

2. **Execution that doesn't erase the edge** — slippage + spread + fees must be less than the per-trade expected value. If the edge is 0.5 pips and execution costs 0.6 pips, the strategy is net-negative regardless of signal quality.

3. **The discipline to stop trading what doesn't work** — fast. Not after 14 days of quarantine and 3 consecutive negative weeks. After a predetermined sample size with a predetermined kill threshold.

4. **Concentration of capital into what does work** — not equal allocation across 8 pairs, but 60% into the proven winner and 40% spread across 2-3 probationary candidates.

### Actionable Items From This Analysis

| # | Item | Type | Priority |
|---|------|------|----------|
| 1 | Run ablation test on EUR/USD before Friday | Research | CRITICAL — validates entire strategy hypothesis |
| 2 | Add "no progress" kill rule (break-even after N trades = prune) | Code | HIGH — prevents mediocrity from consuming runway |
| 3 | Fix variant config hash registration (stop false alerts) | Ops | HIGH — trust in alert surface |
| 4 | Add "QUIET" vs "STALE" distinction for watchers | Ops | MEDIUM — dashboard trust |
| 5 | Consider pausing non-EUR/USD pairs to accelerate trade accumulation | Decision | USER — would focus all attention on proving the primary pair |
| 6 | Set a hard strategy kill clock (PF < 1.0 after 60 trades = strategy dead) | Policy | USER — determines how long to give the hypothesis |
| 7 | Tie execution quality into promotion gate | Code | MEDIUM — execution truth as promotion evidence |

### Bottom Line

**The system is ready. The question is whether the strategy deserves it.**

---

## CODEX ROUND 5 - PRE-PROD REALITY CHECK

**Date**: 2026-04-02

This section is intentionally blunt.

The question is no longer:

> "What would be nice to build next?"

The question is:

> "What are we still avoiding that we actually need to do before the first pair goes live?"

### Short Answer

We are close enough to prod that the remaining work should now be judged by one standard:

- does this reduce the chance of losing money for dumb reasons?
- does this improve trust in promotion decisions?
- does this make the first live pair easier to stop if the edge is not real?

By that standard, the main avoided items are:

1. Define and enforce a true **strategy-dead / no-progress kill rule**
2. Make **execution quality part of promotion evidence**, not just a side report
3. Clean up **watcher alert noise and variant hash truth**
4. Enforce **FX-only launch scope** in the actual managed fleet, not just the docs
5. Run the **ablation / prove-the-signal** step before letting a pair graduate
6. Clarify **stage and launcher semantics** so observer/watcher and QA are not mixed in operator surfaces
7. Do one real **paper-to-prod cutover rehearsal** before the first live promotion

### The Things We Are Still Avoiding

#### 1. A Real "No Progress" Kill Rule

This is the biggest strategic thing we are still avoiding.

We have:
- promotion thresholds
- divergence checks
- quarantine logic
- drawdown protection

But we still do not have a plain rule that says:

- after a defined sample size
- if expectancy / profit factor is not meaningfully positive
- this strategy hypothesis is dead for this pair

Not "demote and quietly keep it around."
Dead.

Why this matters:
- failure is easy to catch
- mediocrity is expensive and sneaky
- break-even pairs consume time, dashboard space, and attention without earning capital

Pre-prod requirement:
- define a hard policy such as:
  - `>= 60 valid trades`
  - `profit factor < 1.0` or `expectancy <= 0 after friction`
  - pair/variant is pruned or forced back to watcher/research with explicit manual review

#### 2. Execution Quality Must Become Promotion Evidence

This is still a real avoided gap.

The current promotion gate clearly checks profitability and replay consistency, but it still
appears to rely on modeled friction rather than true execution-quality evidence from the
active runner path.

That is not enough once real money is close.

Pre-prod requirement:
- promotion and demotion logic should read actual execution-quality evidence:
  - slippage
  - latency
  - spread-at-entry / spread-at-fill
  - execution drift vs modeled assumptions
- a pair should not promote on paper expectancy if live execution quality is degrading that edge

#### 3. Watcher Noise Is Still Too High

This is visible right now in the runtime surfaces.

Current watcher issues still include:
- stale signal freshness warnings
- config-hash divergence for generated variants
- too many warnings for observe-only pairs that may simply be quiet

This is not cosmetic.
If the alert surface is noisy before prod, the operator will stop trusting it exactly when
trust matters most.

Pre-prod requirement:
- variant hash truth reconciled cleanly
- watcher-stage quiet behavior rendered as `QUIET`, not implicitly broken
- watcher alerts tuned so they are informative, not exhausting

#### 4. FX-Only Launch Scope Needs To Be Enforced In Reality

This is still drifting.

The doctrine has moved toward FX-first, but the current managed registry still contains
futures configs in the watcher lane.

That may be acceptable for research, but it is not acceptable as a fuzzy production boundary.

Pre-prod requirement:
- either explicitly keep futures in a research-only lane that cannot contaminate live readiness
- or remove them from the managed launch path for the initial live phase

The important part is not which choice we make.
The important part is that the runtime matches the decision.

#### 5. We Still Need To Prove The Signal, Not Just The Machinery

This is the most uncomfortable truth in the file and it is still correct.

We have built a serious system around the hypothesis that the current trigger stack has edge.
That hypothesis still needs to be proven directly.

The ablation tooling exists.
That means the next step is not to admire the tooling.
It is to use it.

Pre-prod requirement:
- run the ablation on the primary pair
- decide whether the core trigger ingredients actually matter
- if the signal does not survive ablation, do not promote a pair just because the platform is polished

#### 6. Stage / Launcher Semantics Still Need To Be Cleaner

Right now the managed fleet is correct in principle, but some registry/launcher surfaces are
still easy to misread operationally.

Example:
- watcher is observe-only
- but some grouped launcher surfaces still combine watcher and paper into a single managed lane
- some generated config lists still mix watcher configs into broader paper-launch lists

That may be technically intentional, but before prod it should be operationally unambiguous.

Pre-prod requirement:
- no operator should have to guess whether a config is:
  - observe-only watcher
  - paper QA
  - real prod
- stage naming, grouped launch outputs, and dashboard surfaces should all tell the same story

#### 7. We Need One Dry Run Of The First Real Promotion Path

This is another thing teams often avoid until the last minute.

Before the first pair actually goes real, we should rehearse:
- pair becomes promotion-eligible
- live config is materialized
- managed launcher picks it up
- dashboard shows the stage change correctly
- alerts reflect the transition
- emergency-close / kill semantics still work
- demotion path is still clean

This is not a coding nicety.
It is the operational dress rehearsal for the first unit of real capital.

### What This Means In Practice

If we are serious that pairs may soon enter prod, then the final pre-prod checklist should not
mainly be "more research features."

It should be:

1. prove the core signal on the primary pair
2. define and enforce the no-progress kill rule
3. make execution quality part of stage promotion truth
4. eliminate watcher alert/hash noise
5. enforce FX-only runtime scope cleanly
6. clean up stage semantics in launch/dashboard surfaces
7. rehearse the first real promotion end to end

### Final Pre-Prod Position

At this point, the biggest remaining risks are no longer hidden deep in the code.

They are:
- promoting a pair without enough real edge proof
- tolerating mediocre pairs for too long
- trusting modeled friction more than actual execution truth
- letting noisy watcher surfaces reduce operator trust
- carrying scope ambiguity into the first live deployment

Those are the things we should stop avoiding now.

No more features, gates, or infrastructure will change that answer. Only trades will. The single most important thing that can happen this week is the ablation test — because if range_pct doesn't matter, we need a different strategy, not a better-instrumented version of this one.
