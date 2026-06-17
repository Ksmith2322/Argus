---
name: Composite scores must be evidence-capped, not weighted-averaged
description: When combining sub-scores into a headline number (fleet readiness, strategy health, etc), use binding floors not arithmetic mean. Weak components must cap the headline.
type: feedback
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
When combining sub-scores into a single headline number, use a **binding-cap / gating model**, not a weighted average.

**Why:** Weighted averages over-reward weak fleets. Example from the 2026-04-28 dashboard review: Fleet Dimensions showed `Operational 87.5% + Evidence 37.5% + Execution 100% + Risk 100% + Attribution 67.1% → Overall 78.4%`. That 78% reads "mostly healthy" when the truth is "operationally healthy, evidence immature." Averaging away a critical weakness produces dishonest scores; binding caps surface what's actually limiting readiness. User reaction: *"a more honest reading is 55-60%."* Now implemented in `/api/strategy_dimensions` with explicit caps (evidence<50%→cap 60%, risk/exec/operational<80%→cap 70%) and `binding_cap_reason` returned in the payload.

**How to apply:** Any composite score (readiness gates, strategy health, alpha confidence, etc) should:
1. Compute each sub-score independently (operational, evidence, execution, risk, attribution).
2. Apply explicit caps based on the *weakest* dimension that matters for the decision.
3. Return both the capped headline AND the naive average so the user can see the gap.
4. Surface the binding cap reason in human-readable text so the score is interrogable.

Don't blend evidence weakness into operational strength. The two are not interchangeable, and a number that pretends they are is worse than no number.
