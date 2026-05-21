---
name: Scope-down sibling artifacts — 2026-04-20 comprehensive session
description: Eight scope_down sibling strategies built today. Each surfaces a subset edge hidden in the union backtest. Plus drawdown + walk-forward + status-override infrastructure.
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
Large session on 2026-04-20. Built a full scope-down analysis playbook and applied it across the fleet. **8 sibling strategies now track validated subsets**, the dashboard surfaces drawdown + walk-forward, and the promotion gate respects disposition rulings.

**Why:** several strategies had weak union PF but hidden structural subsets with real edge (classic signal-in-noise). Running on the union would underperform or lose; running on the subset could promote.

**How to apply:** before recommending any strategy go live, check both the main artifact AND the `_validated` sibling. The union often has a disposition pointing at the sibling — the sibling is the operational truth. Never promote a strategy with `disposition.status in {kill, shelve, scope_down}` without re-reviewing.

## The 8 scope_down siblings (all backed by `strategy_confidence/*_validated.json` + `_ym.json`)

| Strategy | Filter | n | PF | P(exp>0) | DD% | WF |
|---|---|---|---|---|---|---|
| **Titan Validated** | `strategy=='TREND_FOLLOW' AND direction=='long'` | 174 | 2.09 | 1.00 | 19.7% | 4/4 |
| **Tori Validated** | `name=='Dow' AND direction=='LONG'` | 78 | 3.65 | 1.00 | 17.4% | 4/4 |
| **Hermes Validated** | `score>=80 AND long AND GAP_DOWN` | 92 | 2.06 | 1.00 | 28.2% | 4/4 |
| **Cue Banks Validated** | `factors contains 'S/D supply zone'` | 30 | 3.63 | 0.998 | 10.4% | 4/4 |
| **Ares Validated** | `exit_reason == 'rotation'` | 27 | 4.63 | 1.00 | 0.0% | 3/4 |
| **Apollo Validated** | `surprise 10-20% + gap 2%+` | 15 | 4.59 | 0.986 | 413% | 3/3 |
| **Mamba YM** | `ticker == 'YM=F'` | 15 | 1.98 | 0.84 | 81% | 2/2 |
| **Index Rebal Validated** | `action == 'ADD'` | 19 | 7.04 | 0.998 | 116% | 2/3 |

**Titan Validated and Tori Validated both clear the promotion bar on backtest** (n≥60, PF≥1.3). Live evidence is the last gate.

## Non-obvious structural findings (watch for these)

1. **Cue Banks: higher confluence_score is WORSE.** Score≥5 is PF 0.64, score≥4 is PF 1.29. The strategy's own ranking signal inverts. Real edge lives in `S/D supply zone` factor (PF 3.63).

2. **Ares `risk_off` exits are net-negative (PF 0.26 on n=8).** The regime-override panic exit destroys value. `exit_reason=='rotation'` (the normal monthly rebalance) is the intended edge.

3. **Index Rebal DELETE is anti-edge** (PF 0.49, net -$159). All alpha lives in S&P 500 **ADDs** — the classic index-inclusion effect.

4. **Tori SHORT is PF 1.74 vs LONG PF 3.36.** US-equity upward drift asymmetry. Dow specifically is the dominant instrument (PF 2.48 vs PL 1.52 / CL 1.57 / Gold 1.84). Dow+LONG compounds both.

5. **Hermes runner already enforces** `score>=80 AND long AND GAP_DOWN` — prior memory note about the filter being unclear is resolved. Runner is at [hermes/runner.py:62,127](hermes/runner.py#L127).

## Infrastructure built this session

1. **`Drawdown` pydantic model** in `helio/strategy_confidence.py` — tracks real peak-to-trough DD on the actual trade sequence (distinct from MC-shuffle worst-case).

2. **`_max_drawdown(pnls, starting_equity_usd)`** helper in `helio/fleet_state.py`.

3. **`Disposition` status-override** — dashboard row's Status column now reflects `disposition.status` (KILLED, SHELVED, SCOPE_DOWN, RESEARCH_ONLY) instead of its hardcoded value. Fixes a prior mismatch (Sector Rot used to show BUILT while actually killed).

4. **Walk-forward column on dashboard** — shows "stable_folds/total_folds". Easy visual for edge stability.

5. **Disposition-aware promotion gate** — `helio/promotion_readiness.py` now blocks any strategy whose artifact carries `status in {kill, shelve, scope_down}` regardless of live stats.

6. **Portfolio correlation endpoint** at `/api/portfolio_correlation` — fleet naturally diversified, no pair above 0.7. Strongest: Tori↔GDX/GLD 0.53.

## Honest read on the fleet

- **Only Tori clears the 8% DD funding gate today (7.78%).** Everything else will need conservative sizing or a tighter scope.
- **17 of 18 strategy rows have computed confidence.** Only Themis stays hardcoded (signal-only system, no trading PnL).
- **Mamba runner scoped to YM=F only** (see `forge/mamba/runner.py:48`). When it goes live it'll only trade the validated subset.
- **Sector Rot killed** — not an alpha strategy, just long-equity beta.
- **No strategy at promotion bar on LIVE evidence yet** — 5 Argus trades is the live total. Accumulation is the path.

## Known loose ends

- **Hermes 0 overlap in portfolio correlation** — date-parsing bug in `_gather_backtest_trades_by_strategy` (probably the trades_*.csv date column format). Non-blocking.
- **DD% basis is peak-relative by default** — for strategies with near-zero peak equity (Apollo, Mamba) the pct reads as 100%+ which is mathematically correct but confusing. Consider wiring `starting_equity_usd=1000` into every writer for consistent anchoring.
- **Runner code hasn't picked up the scope filters** — only Mamba's TICKERS was flipped. Tori/Ares/Apollo/Cue Banks/Hermes/Index Rebal/Titan runners still take all setups. Wiring the filters into runners is the next wave of work when promotion is pursued.
