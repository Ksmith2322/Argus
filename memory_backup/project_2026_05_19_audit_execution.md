---
name: 2026-05-19 multi-agent audit + Path C execution
description: 5-agent audit synthesis (Truth/Skeptic/Architect/ROI/Risk) yielded Path C — don't shelve, don't go real 7/1, redirect. Shipped same session — tier evaluator check, 0-ENTRY ledger root-cause (real bug fixed + operational gap surfaced), forge_vix_carry built end-to-end and BACKTEST PROVED IT DOESN'T WORK 2021-2026, capacity_stress CLI surfacing 8 strategies that fail at their own 1× cap. 113/113 tests green.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Session executes against the Path C recommendation from the 5-agent
synthesis. Four things shipped, plus a critical truth-finding that
should change how the next 30 days look.

## What was supposed to be a "free $15K" — was not

ROI optimizer agent's Lever #1 claimed nq_overnight and gld_pm_long
might be stuck at 0.5% "unproven" when they should be at 1.0%
"emerging". **Checked directly.** Both ARE on the emerging tier
correctly. gld_pm_long sizes at 1.00% as expected. nq_overnight reads
1.00% emerging but the drawdown brake halves it to 0.50% **because
nq_overnight is in a 74% drawdown from its $710 peak** (current PnL
$181.76, peak $709.55, current_dd $527.79, dd_pct_of_peak 0.7438).
That's the system working correctly, not a bug.

**Implication:** the bot's "WINNER" strategy is actually in a deep
drawdown — calling it a winner was generous in the cull audit. The
sizing brake is the right call.

## 0-ENTRY ledger root cause: real bug + operational gap

Truth-seeker agent flagged canonical_fills.jsonl has 330 rows, all
side=EXIT, zero ENTRY since the 5/18 ENTRY-write code shipped. Two
findings on investigation:

1. **Real bug (fixed today):** `helio/canonical_fills.write_fill_typed`
   was silently dropping `lineage_id` from the serialized row. I added
   the field to `helio.domain.Fill` on 5/18 and stamped it at the call
   sites, but the writer never copied it into the JSON. End-to-end
   smoke test (`argus_flow/tests/test_entry_write_smoke.py`) caught it
   live. Fix: `if lineage is not None: row["lineage_id"] = lineage`.
2. **Operational gap (operator action needed):** the production
   daemons imported `helio.ibkr_execution` at boot. They hold the OLD
   `submit_bracket` in memory. **The fleet needs a full restart** to
   pick up both the 5/18 ENTRY-write code AND today's lineage_id fix.
   Until then, every new entry still skips canonical_fills writes.

## forge_vix_carry built end-to-end — and the backtest killed it

Architect agent recommended VIX term-structure carry (long SVXY when
contango>=1.05 + VIX<20 + SPY 5d realized vol<15%) as the top new edge,
"Sharpe 0.9-1.4 plausible." Built it end-to-end:

- `helio/vix_term_structure.py`: pure-function decision logic (entry/exit/hold).
- `forge/vix_carry/runner.py`: --check, --evaluate, --loop, --backtest modes.
- `argus_flow/tests/test_vix_carry.py`: 17 tests (decision logic + runner smoke).
- `forge_vix_carry` added to `allocation_factors.json` at 0.0× (RESEARCH_ONLY).

**Then ran the backtest on real data (2021-2026 via yfinance).**

| Variant | n | WR | PF | Total | CAGR | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| baseline (contango>=1.05) | 15 | 27% | 1.01 | **-13.82%** | -3.07% | 33.1% |
| tighter entry 1.10 | 14 | 29% | 1.10 | -8.08% | -1.75% | 28.7% |
| looser exit VIX>=30 | 14 | 21% | 0.95 | -18.04% | -4.09% | 33.1% |
| looser exit contango<0.95 | 13 | 23% | 0.48 | -35.60% | -8.80% | 36.2% |
| tighter+looser combined | 10 | 20% | 0.54 | -30.80% | -7.42% | 38.4% |
| aggressive entry 1.15 | 10 | 20% | 0.46 | -33.29% | -8.13% | 41.1% |

**NO parameter variant produces positive return** over 2021-2026.
The Architect's "decades-validated Sharpe 0.9-1.4" was based on older
regime data (pre-2018). Post-2020 — with Volmageddon legacy, COVID,
2022 rate shock, Aug-2024 carry unwind — the edge isn't there at
n=10-15 trades. The Skeptic was more right than the Architect on this
specific candidate.

**Decision:** keep the code as a research artifact + template for the
next edge candidate. Allocation locked at 0.0. **Do NOT ship to live
paper trading.** Status documented in `forge/vix_carry/__init__.py`
docstring and in `allocation_factors.json` _kill_log.

**Why:** the operator's instinct (Path B from synthesis — build
textbook factors) needs every candidate backtest-validated before
shipping. The Architect agent was theorizing; the data overrules.
The infrastructure I built (runner+backtest+tests pattern) is the
reusable template for PEAD, XS-momentum, cointegration, CSP — the next
candidates from the Architect's list. Each must be backtested before
graduation from RESEARCH_ONLY.

## Capacity stress run — fleet has zero scaling headroom

Ran `ops/audit/run_capacity_stress.py` for the first time:

- **8 strategies fail at 1× their own configured cap** (jpy_pm_short,
  aud_asian_breakout, wick_gbpusd, mamba, tori, argus_gbpusd,
  argus_usdjpy, argus_cadjpy) — per-strategy cap is bigger than what
  cluster/single-instrument caps allow. This is the "NOTIONAL_CAP
  warnings every cycle" from the cull audit, finally quantified.
- **None of 18 strategies has 2× headroom.** Every strategy that
  passes at 1× fails at 2×.
- **The binding constraint is cluster caps, NOT per-strategy caps.**
  ROI optimizer's Lever #3 ("raise nq_overnight to 2 MNQ") won't work
  without first raising EQUITY_BETA from 2.5× to 4.0×.

**Implication for next 30 days:** if there's any scaling work, it
starts at the cluster cap layer, not the per-strategy layer. And the
8 "trips at 1×" strategies should either get their per-strategy caps
reduced (so they trade what they're actually allowed to trade) or
have their sizing math debugged so they don't request impossible sizes.

## Path C still applies — slightly tighter

Synthesis recommendation from the 5-agent audit:
- Don't shelve.
- Don't go real-money on 7/1 — extend to 2026-10-01+.
- Build one new textbook strategy at a time, validating each with a real backtest.
- The $10K stays in VTI/BIL until at least one strategy has 60+ clean trades.

Today's vix_carry backtest tightens this: **every new strategy gets a
multi-year backtest BEFORE it's allocated paper capital, let alone real.**
The vix_carry build is what the new template looks like. Next
candidates from Architect's list (in priority order): PEAD, XS-momentum,
cointegration pairs. Each one is a 2-4 hour build + backtest sprint.
If 1 of 3 survives, that's a real result. If 0 of 3 survive, the
Architect's framing of "untouched factor edges" is wrong and Path A
(shelve) becomes the honest call.

## Files changed today

- `helio/canonical_fills.py` — lineage_id propagation fix
- `helio/vix_term_structure.py` (new) — VIX carry decision logic
- `forge/vix_carry/__init__.py` (new) — strategy package + STATUS block
- `forge/vix_carry/runner.py` (new) — daily-check runner + backtest
- `ops/audit/run_capacity_stress.py` (new) — CLI for the harness
- `argus_flow/configs/allocation_factors.json` — vix_carry added at 0.0
- `argus_flow/tests/test_entry_write_smoke.py` (new) — end-to-end ENTRY write proof
- `argus_flow/tests/test_vix_carry.py` (new) — 17 tests
- `ops/reports/system_audit/capacity_stress.json` (artifact)

## Test scoreboard

113/113 green across batches 1-4. Today's additions:
- ENTRY write smoke test: 2/2
- vix_carry: 17/17

## Open items going into tomorrow

1. **Operator action: restart the fleet** so ENTRY canonical_fills
   writes + lineage_id stamps actually start producing rows.
2. **Next strategy candidate**: PEAD or XS-momentum. Build, backtest,
   ship-or-discard. Same template as vix_carry.
3. **Cluster cap audit**: 8 strategies trip at 1× — either reduce
   per-strategy caps to match cluster reality, or raise cluster caps
   if individual strategies have proven edge worth the concentration.
4. **The post-5/31 reset**: still the right plan. Pre-freeze epoch
   stays is_clean=False. The 30-day clean window 6/1→6/30 is the
   minimum to start interpreting any strategy's evidence.
