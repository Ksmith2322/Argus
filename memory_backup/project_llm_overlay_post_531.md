---
name: LLM overlay design — post-5/31, scoped to safe version
description: Plan for adding an LLM regime-classifier layer over the validated fleet, after strategy freeze. Bounded responses (deterministic regime → deterministic action), not autonomous parameter tuning. Plus weekly digest + post-mortem features.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## When this kicks in

After 2026-05-31 strategy freeze + post-cull. The LLM overlay only adds value if the underlying strategies have known, validated edge to *protect*. Adding it over a fleet you don't trust is just adding a second source of confusion. Sequence is fleet stable → world-data feeds reliable → LLM regime layer on top.

## Two versions — only the narrow one is safe

### NARROW (build this) — Bounded regime classifier

LLM reads atlas + themis + headlines every N hours. Picks ONE of ~10 pre-defined regimes. Each regime has a deterministic response stored in a config file:

| regime | risk_mod | allowed_instruments | entry_pause_min |
|---|---|---|---|
| NORMAL | 1.0 | all | 0 |
| RISK_OFF | 0.5 | major FX, gold, treasuries only | 60 |
| CENTRAL_BANK_SURPRISE | 0.3 | all but exclude affected currency 2hr | 120 |
| EARNINGS_HEAVY | 0.7 | all but exclude pre-ER stocks | 0 |
| GEOPOLITICAL_TAIL | 0.2 | gold, USD/CHF only | 240 |
| FOMC_WEEK | 0.6 | all (already gated by fomc_drift) | 0 |
| ... | ... | ... | ... |

LLM picks the regime; the *response is hardcoded*. Every classification logged with reasoning. Auditable. Testable.

### BROAD (do NOT build) — Autonomous parameter tuner

"LLM watches everything and adjusts strategy parameters dynamically based on news interpretation." Three reasons this fails:

1. **Speed.** Professional desks have priced in news within 30 seconds. By the time an LLM finishes processing, the edge is consumed. Retail LLM-driven news trading is a losing game.
2. **Hallucination + opaqueness.** LLMs are confident even when wrong. Project's existing philosophy is "no fallbacks, no hidden behavior, raise BrokerEquityUnavailableError instead of guessing." Autonomous LLM violates that by design.
3. **Attribution.** Without parallel A/B (LLM-on, LLM-off), you can never prove the LLM helped. Just one timeline and a hunch.

## Three components to actually build

1. **Scenario classifier** (bounded responses, above)
2. **Weekly trade-quality digest** — LLM reads the week's trades + market context, produces "what worked, what didn't, what was the macro context." Pure observer, no live action. Build on `argus_flow/agents/dual_analyst.py` (currently a 2026-03-22 stub, references old 3-runner fleet — needs update to current 22-strategy fleet).
3. **Post-mortem on losing days** — when fleet PnL hits a drawdown band (e.g. -2R in a day), LLM auto-generates a writeup: which strategies lost, what events correlated, was it a regime change, was it a bug. Helps understand without manual digging.

## Implementation skeleton

```
helio/llm_overlay/
  classifier.py        — calls Anthropic API with atlas+themis+news, returns regime tag
  regimes.json         — config: regime → {risk_mod, allowed_instruments, entry_pause_min}
  weekly_digest.py     — pulls 7d trades + market context, generates writeup
  post_mortem.py       — triggered on drawdown band, generates analysis
  cli.py               — schedulable entrypoints

config/llm_overlay.json
  poll_interval_min: 60
  model: "claude-sonnet-4-6"  (use Haiku for cost-sensitive, Sonnet for quality)
  audit_log: argus_flow/logs/llm_overlay_decisions.jsonl
  max_overrides_per_day: 10
```

Cost estimate: 24 classifier calls/day × $0.05 = ~$1.20/day with Sonnet, ~$0.10/day with Haiku 4.5. Weekly digest + post-mortems add $5-10/month. Total ≤ $50/month for live overlay.

## Hard constraints (carry forward to implementation)

- **Every classification logs full reasoning** to a JSONL audit trail — must be reviewable after the fact
- **No retraining or self-modification** — the classifier is stateless, regime config is human-edited
- **Bounded daily impact** — `max_overrides_per_day` cap so a runaway LLM can't disable the entire fleet for a week
- **Kill switch** — `KILL_SWITCH` control file already exists in argus_flow/logs/_locks; honor it
- **Backtest the classifier** before going live — apply LLM to historical news + past 90d fleet behavior, evaluate whether its classifications would have helped or hurt

## How to apply this memory

**Why:** captures the "good idea, scoped properly" version of the LLM overlay so it doesn't drift back to the broad-autonomous version when the conversation comes back to it.

**How to apply:**
- If user asks "should we add an LLM" or "where are we with the LLM": reference this memory.
- If user proposes an autonomous tuning version: push back with the three failure modes above.
- Don't start implementation before 5/31 fleet cull is done. The regime → response config can only be written once we know which strategies are actually in the fleet.
- First milestone after 5/31: build classifier.py + regimes.json with NORMAL regime only, verify the plumbing works, then add additional regimes one at a time.
