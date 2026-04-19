# Profit-Max System Blueprint and Gap Audit

Generated: 2026-04-18
**Updated: 2026-04-18 - Three-way debate applied, then correctness-reviewed by Codex (see Section 18).**

> **READ SECTION 18 FIRST.** The body below, sections 1-17, is comprehensive reference material. Section 18 is the applied operating filter for a single operator, limited weekly development time, and a possible $10K live funding target. It also corrects several overly confident claims from the first debate pass: ROI is not proven, $/year forecasts are hypotheses only, PDT rules are in a 2026 transition window, and current promotion gates must not be bypassed.

Scope: this is a critical roadmap for turning the current Argus/Fleet/Forge/Apollo system into a higher-confidence, higher-return trading operation. It is not financial advice and it is not a promise of returns. The goal is to identify what is missing, why profitability is not yet where it should be, what to build, and how to test it before real production sizing.

## 1. Blunt Verdict

The system is much more observable than it was. That is good. But it is not yet a high-confidence profit engine.

Current live evidence says:

- Fleet health is OK across 19 systems.
- Entries are not currently blocked.
- Dynamic broker-equity sizing is mostly in place.
- Risk state is visible.
- Forward returns and fleet performance are now falsifiable.
- Promotion gate has 0 promoted strategies.
- Last-7-day USD roll-up is +$97.60 on a broker-equity anchor of about $1,002,591.71, or +0.0097%.
- All-time roll-up is +$9,172.47, or +0.9149%, but most of that is historical GDX/GLD back-fill, not current live performance.
- The old idea of "15% to 40% ROI per strategy" is not supported by current live evidence. It may be a research/backtest aspiration for some strategies, but it is not the current measured production expectation.

The practical conclusion: we are at "operational recovery plus early validation," not "production edge proven." The next job is not to add random strategies. The next job is to build a repeatable alpha factory, prove two to four sleeves, and only then scale risk.

## 2. Why The System Is Not More Profitable Yet

### 2.1 Too little clean live evidence

The promotion report is the strongest truth source right now:

- GBPUSD: NOT_READY, 0 valid trades, 100% invalid rate on the one observed trade, signal frequency ratio 0.00 versus replay.
- USDJPY: NOT_READY, 2 valid trades, expectancy negative after friction, profit factor 0.88 versus required 1.10, signal frequency ratio 0.14 versus replay, only one regime observed.
- CADJPY: present in fleet, but not contributing current live USD evidence.

This means the system cannot honestly claim strategy-level production profitability yet.

### 2.2 Backtest/live frequency collapse

The worst blocker is not just P&L. It is that replay expected more signals than live/paper is producing. A strategy that trades far less often live than it did in replay is not the same strategy in practice.

Required fix:

- Every strategy needs a live-vs-replay drift monitor.
- Drift must compare signal count, entry timestamps, feature values, spread, stop distance, and blocked-entry reasons.
- A strategy should not promote if it only works in replay.

### 2.3 The fleet is broad but not ranked by proof

The system has many named engines: Argus, Titan, Hermes, Apollo, Forge, Ares, Oracle, Atlas, Themis, Mamba, Tori, Cuebanks, and several one-shot strategies. But "running" is not the same as "validated."

The system needs a hard distinction:

- Production candidate: has clean paper/live evidence, USD P&L, fills, and promotion gates.
- Research candidate: has a thesis and backtest, no production sizing.
- Signal-only: can emit alerts, cannot trade.
- Retired/killed: cannot launch.

If a component is not in a USD roll-up and cannot state its edge, evidence, and kill rule, it should not count as profitable.

### 2.4 The current strategy mix is not diversified enough by edge

There are some good components, but the system is still heavily weighted toward narrow timing patterns, sparse event signals, and early-stage scanners. A best-in-class bot would deliberately cover multiple return sources:

- Intraday momentum/reversal.
- Multi-day swing momentum.
- Cross-asset trend following.
- Earnings and event drift.
- Relative value and statistical arbitrage.
- Volatility regime and VIX mean reversion.
- Calendar/rebalance/flow effects.
- Macro regime overlays.
- Execution alpha and cost control.
- Cash/collateral yield.

Argus has pieces of this, but not as a full portfolio with a unified allocator and measured edge inventory.

### 2.5 Capital allocation is still basic

The new fleet sizing is the correct direction: risk is based on broker equity and strategy confidence tiers. But the allocator is not yet a full portfolio manager.

Missing:

- Real broker buying power and margin impact before every order.
- Expected value per unit of risk by strategy.
- Correlation-adjusted risk allocation.
- Capacity/slippage model.
- Strategy decay detection.
- Drawdown-based de-risking by sleeve, not just fleet.
- Opportunity-cost logic: best current signal gets capital, weak correlated signal waits.

### 2.6 Static notional caps remain a useful guardrail, but not a final answer

`argus_flow/configs/fleet_sizing.json` still has static notional cap multipliers:

- stock: 2.0x
- etf: 2.0x
- fx: 20.0x
- micro_future: 5.0x

These are good fail-safe limits, but the production buying process should also query broker margin/buying-power truth. Broker-equity dynamic sizing answers "how much should I risk?" Broker margin truth answers "can this account actually carry the position under real rules?"

### 2.7 Execution truth is not yet the primary truth everywhere

The best system derives trades from fills and positions, not from intentions. The current system is closer than before, but any strategy that still treats a CSV trade row or signal as truth is not production ready.

Production truth hierarchy should be:

1. Broker executions/fills.
2. Broker positions/orders/account.
3. Local reconciled ledger.
4. Strategy state.
5. Dashboard.
6. Human-readable CSV/report.

### 2.8 Research is not yet controlled for multiple testing

When you test many ideas, the best-looking backtest is often the one that got lucky. This is especially dangerous if you fit chart patterns after looking at the chart.

Every strategy family needs trial accounting:

- What was tested.
- Parameter ranges.
- Number of variants.
- In-sample and out-of-sample split.
- Walk-forward folds.
- Transaction cost assumptions.
- Deflated Sharpe or similar multiple-testing adjustment.
- Rejection log.

### 2.9 Dashboard still needs to become a PM cockpit

The dashboard is much closer to truth because of canonical endpoints, but the visual goal should be clearer:

- Recent trades.
- Account balance curve.
- Realized and unrealized P&L.
- Open risk by strategy and asset class.
- Buying power and margin.
- Strategy tier and current risk percent.
- Promotion blockers.
- Entry blockers.
- Current exposure map.
- Live-vs-replay drift.
- Data freshness.
- Order/fill status.

If the dashboard cannot answer "why did we trade or not trade?" in 10 seconds, it is not yet operationally smooth.

## 3. What The Best Bot Would Actually Be

The best trading bot is not one magic strategy. It is a portfolio business:

1. A clean data layer.
2. A repeatable research lab.
3. A strategy factory.
4. A capital allocator.
5. An execution engine.
6. A risk supervisor.
7. An observability cockpit.
8. A post-trade learning loop.
9. A kill/degrade system.
10. A low-friction operating process.

The highest-return version is not reckless. It is aggressive only where it has evidence. It scales winners, shrinks uncertain strategies, kills decaying edges, and keeps enough liquidity to survive bad regimes.

## 4. Current Strategy Inventory and Confidence

Confidence scale:

- High: enough clean live/paper evidence for controlled production sizing.
- Medium: strong research or early live evidence, but needs more validation.
- Low: interesting, but not proven enough for production.
- Research-only: no production sizing.

| Strategy/system | Current status | Confidence | Main issue | Next move |
|---|---:|---:|---|---|
| Argus USDJPY | NOT_READY, 2 valid trades | Low | Negative live expectancy after friction, low signal frequency vs replay, one-regime sample | Diagnose replay/live drift before any scale-up |
| Argus GBPUSD | NOT_READY, 0 valid trades | Low | Invalid rate and no valid paper sample | Fix invalidity and signal-frequency gap |
| Argus CADJPY | Present, no current USD evidence | Low | Insufficient live evidence | Keep paper only until promotion report includes it cleanly |
| Forge GLD PM Long | 1 recent USD trade, +$199.15 | Medium-low | Best-looking near-term candidate but sample is one trade | Continue paper, add replay parity, require more trades/events |
| Forge JPY PM Short | 1 recent USD trade, -$100.00 | Low-medium | Sparse evidence | Continue paper, do not scale |
| Forge Wick GBPUSD | No recent USD trades | Low-medium research | Sparse live evidence | Validate frequency and execution assumptions |
| Forge NQ Overnight | No recent USD trades | Low research | Needs cost/slippage and futures-specific risk proof | Keep paper/signal-only until proven |
| Forge GDX/GLD | 87 historical trades, +$9,074.87 historical back-fill | Medium research | Historical performance is not current live production evidence | Convert to live fill-truth pipeline and paper-forward test |
| Apollo scanner | 50 forward-return records started | Research-only | Falsifiable now, but not an execution strategy yet | Build event strategy card and forward-return scoring |
| Titan/Hermes/Ares/Oracle | Heartbeats OK | Unclear | Not in current USD roll-up as production evidence | Classify each as production, research, signal-only, or retire |
| Cuebanks/Tori/Mamba | Research-only flag | Research-only | Live placeholders | Keep excluded from production roll-up |
| VIX/Rebalance/Atlas/Themis | Running/monitored | Research to low | Needs explicit strategy cards, backtests, and live tracking | Promote only after evidence pipeline |

## 5. The Missing Strategy Sleeves

Below is not a top-10 list. It is the full strategy map I would want if designing a serious multi-angle bot.

### 5.1 Intraday index futures sleeve

Markets: ES/MES, NQ/MNQ, RTY/M2K, YM/MYM.

Why it matters:

- High liquidity.
- Repeatable intraday structure.
- Good for fast feedback loops.
- Scales better than tiny FX patterns if execution is controlled.

Candidate strategies:

1. Opening range breakout.
2. Opening range failure/reversal.
3. VWAP reclaim/lose continuation.
4. VWAP stretch mean reversion.
5. First-hour trend day classifier.
6. Midday compression breakout.
7. Closing hour trend continuation.
8. Overnight inventory unwind.
9. Gap-and-go versus gap-fill classifier.
10. Volatility contraction to expansion.

Implementation:

- Build a shared intraday event-bar engine for 1m/5m/15m bars.
- Add session templates for RTH, overnight, premarket, and macro-event windows.
- Generate labels: next 5/15/30/60 minute return, max favorable excursion, max adverse excursion, stop-first/target-first sequence.
- Add a feature store: opening gap, prior day range, overnight range, VIX, realized vol, volume profile, distance to VWAP, distance to prior high/low, trend strength, time of day.
- Use simple rules first, then a meta-model to choose breakout versus fade.

Tests:

- Next-bar-open execution.
- Spread and commission.
- Stop-before-target sequencing.
- Event-day exclusion tests for CPI/FOMC/NFP.
- Walk-forward by month and volatility regime.
- Paper/live signal parity.
- Slippage by time of day.

Kill rules:

- Live signal frequency below 50% of replay for two weeks.
- Profit factor below 1.05 after 50 live/paper trades.
- Slippage exceeds modeled cost by 2x.
- One setup contributes more than 40% of P&L and then decays.

### 5.2 Intraday FX sleeve

Markets: major liquid pairs first: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD. Crosses only after proving majors.

Candidate strategies:

1. London breakout.
2. London breakout failure.
3. NY session continuation after London trend.
4. Asia range breakout.
5. Fixing-window mean reversion.
6. Post-news volatility compression.
7. Carry-aligned intraday continuation.

Why current Argus is not enough:

- Argus is seeing live signal-frequency collapse.
- Current promotion gate shows NOT_READY.
- There is no broad FX basket allocator yet.

Implementation:

- Treat each pair/session/setup as a separate strategy card.
- Add pip-value, notional, margin, and spread models by pair.
- Compare exact live features to replay features at every skipped and taken entry.
- Add "would have entered except blocked by X" records.

Tests:

- Replay/live drift.
- Spread widening around news.
- Weekend reopen/rollover behavior.
- Broker disconnect and partial fill tests.
- Session daylight-saving-time tests.

Kill rules:

- More than 10% invalid trades in any 20-trade window.
- Signal-frequency ratio below 0.5 versus replay after enough live hours.
- Realized spread worse than model by 2x.

### 5.3 Cross-asset trend following sleeve

Markets: equity index futures/ETFs, bonds/rates ETFs, currencies, gold, oil, commodities, sector ETFs.

Edge:

- Time-series momentum has strong published evidence across futures and asset classes.
- It is not meant to win every month. It is meant to diversify, catch persistent moves, and help during crisis regimes.

Implementation:

- Build a daily trend engine:
  - 1/3/6/12 month momentum.
  - Volatility targeting.
  - Correlation caps.
  - Crisis-vol de-risking.
  - Long/short support where borrow/margin is available.
- Start with liquid ETFs if futures margin handling is not ready.
- Add instrument universe health checks.

Tests:

- Walk-forward from at least 2006 onward, with regime splits.
- Include realistic ETF/futures costs.
- Compare long-only, long/flat, and long/short.
- Stress 2008, 2020, 2022, and 2025/2026 volatile regimes.
- Deflated Sharpe and parameter stability.

Kill rules:

- Trend sleeve correlation to existing strategies exceeds expected range.
- Live slippage or gap risk exceeds modeled risk.
- Drawdown exceeds walk-forward 95th percentile.

### 5.4 Equity/ETF momentum and sector rotation sleeve

Markets: sector ETFs, industry ETFs, liquid large-cap stocks.

Edge:

- Momentum and value premia have broad academic evidence across markets.
- Your Ares rotation is a starting point, but it needs a full promotion path and cost-aware allocator.

Implementation:

- Rank liquid sector/industry ETFs by 3/6/12 month momentum adjusted for volatility.
- Blend with value/quality for stock universes if data exists.
- Hold top N, rebalance weekly or monthly.
- Add market regime filter: risk-on, risk-off, choppy.

Tests:

- Survivorship-free universe for stocks.
- ETF inception bias handling.
- Rebalance slippage.
- Tax/turnover estimates if relevant.
- Capacity test.

Kill rules:

- Excess turnover with no net alpha after costs.
- Signal concentration in one sector without risk approval.

### 5.5 Earnings/event drift sleeve

Markets: liquid US equities and ETFs around earnings.

Edge:

- Post-earnings announcement drift is one of the classic event anomalies.
- Apollo now has forward returns, which is the right beginning.

Implementation:

- Convert Apollo from scanner into event research pipeline:
  - Earnings surprise.
  - Revenue surprise.
  - Guidance/profitability language if data exists.
  - Gap size.
  - Volume shock.
  - Options-implied move if available.
  - Analyst revisions if available.
  - Short interest if available.
- Label T+1/T+3/T+5/T+10 trading-day returns.
- Separate strategies:
  - Gap continuation.
  - Gap fade.
  - Post-earnings drift long.
  - Post-earnings drift short.
  - Volatility crush/no-trade filter.

Tests:

- No lookahead: use only data available at signal time.
- Trading-day horizon logic, already started in backfill.
- Liquidity and spread filters.
- Earnings-calendar correctness.
- Out-of-sample quarter splits.
- Avoid tiny sample winners.

Kill rules:

- Forward-return expectancy disappears after adding costs.
- Model only works on a handful of mega-cap names.
- Slippage/gaps exceed expected edge.

### 5.6 Relative value and statistical arbitrage sleeve

Markets: pairs and baskets where economic linkage is real.

Current seed:

- GDX/GLD is the best obvious seed.

Additional candidates:

1. GDX/GLD.
2. XLE/oil proxy.
3. Gold miners versus gold plus rates/USD.
4. Sector ETF versus top constituents.
5. Pairs inside same industry.
6. ETF residual mean reversion using sector/factor hedge.
7. FX basket residuals.

Implementation:

- Build a spread engine:
  - Hedge ratio.
  - Rolling z-score.
  - Half-life.
  - Cointegration/stability checks.
  - Borrow/margin availability.
  - Dollar-neutral P&L.
  - Leg-level fill truth.
- Avoid pairs selected only because they looked good recently.

Tests:

- Formation/trading window split.
- Rolling re-estimation.
- Transaction costs on both legs.
- Short availability.
- Stress tests for relationship breakdown.
- Legging risk and partial fill tests.

Kill rules:

- Spread no longer mean reverts within expected half-life.
- Hedge ratio instability.
- One leg cannot fill or borrow reliably.

### 5.7 VIX and volatility regime sleeve

Markets: VIX futures/ETNs/ETFs only if product risk is fully understood. Safer first step: use VIX as a regime input for index/ETF strategies.

Edge:

- Volatility tends to cluster and mean-revert, but vol products can be dangerous and path-dependent.

Implementation path:

1. Use VIX as a filter, not as a traded product.
2. Build VIX spike/reversion paper strategy.
3. Add VIX term structure: contango/backwardation.
4. Only later consider direct vol products or options.

Tests:

- 2018 volmageddon-like shock.
- 2020 crash.
- 2022 inflation/rates volatility.
- Product decay and roll yield.
- Gap-open losses.

Kill rules:

- Strategy loses more in vol spikes than it makes in calm periods.
- Product structure produces hidden decay.

### 5.8 Options sleeve, later only

Do not rush options. Options can create high ROI and high blow-up risk.

Candidate strategies:

1. Cash-secured put selling after volatility spikes.
2. Defined-risk credit spreads.
3. Earnings implied-move overpricing.
4. Protective overlays for existing positions.
5. Volatility crush after earnings.

Prerequisites:

- Options chain data.
- Greeks.
- Assignment handling.
- Early exercise risk.
- Expiration calendar.
- Multi-leg order/fill truth.
- Portfolio margin stress.

Tests:

- OCC/assignment simulation.
- IV surface replay.
- Liquidity filters.
- Worst-case gap tests.
- Pin risk handling.

Until that infrastructure exists, options should be research-only.

### 5.9 Calendar, rebalance, and flow sleeve

Candidate strategies:

1. Turn-of-month equity strength.
2. Month-end bond/equity rebalance flows.
3. Index rebalance additions/deletions.
4. Sector rebalance pressure.
5. Option expiration week behavior.
6. Holiday effects.
7. Quarter-end window dressing.

Implementation:

- Build a calendar event engine.
- Precompute event windows.
- Label before/during/after returns.
- Add liquidity and market-regime filters.

Tests:

- Use event date known before trade.
- Avoid overlapping event double-counting.
- Include slippage during crowded rebalance windows.

Kill rules:

- Edge vanishes after event becomes crowded.
- Too few annual events for confidence.

### 5.10 Macro regime sleeve

Candidate strategies:

1. Gold/rates/USD regime.
2. Oil and commodity exporter FX.
3. Dollar trend and risk appetite.
4. Rates momentum and equity duration sensitivity.
5. CPI/FOMC/NFP event filters.
6. Safe-haven flow basket.

Implementation:

- Create daily macro feature table:
  - DXY proxy.
  - 10Y yield.
  - Real yields if available.
  - Oil.
  - Gold.
  - VIX.
  - Credit spread ETF proxies.
  - Sector breadth.
- Use regime output to size/filter other strategies first.
- Only trade macro directly after filter value is proven.

Tests:

- Regime classification stability.
- Out-of-sample period robustness.
- Recession/crisis periods.
- False regime flip cost.

Kill rules:

- Regime filter reduces good trades more than bad trades.
- Regime labels are unstable around turning points.

### 5.11 News and text event sleeve

This can be powerful but is easy to overfit and operationally fragile.

Candidate events:

1. Earnings call sentiment changes.
2. SEC 8-K material events.
3. FDA/biotech decisions if data is reliable.
4. Analyst upgrades/downgrades.
5. M&A rumors and confirmed deals.
6. Macro headline shock filters.

Implementation:

- Use only timestamped data with known availability time.
- Store original document, parsed features, and signal decision.
- Start with post-event drift, not instant headline trading.

Tests:

- Timestamp correctness.
- Vendor outage behavior.
- False positive parser tests.
- No-lookahead audit.

Kill rules:

- Parser confidence low.
- Signal requires speed the current stack cannot deliver.

### 5.12 Cash and collateral sleeve

If the account is large, idle cash matters.

Implementation:

- Track cash, margin, sweep yield, and idle balance.
- Decide whether excess cash should sit in broker sweep, Treasury ETF, or short-term instruments.
- Never impair margin liquidity for active strategies.

Tests:

- Settlement timing.
- Liquidity.
- Margin impact.

Kill rule:

- Any cash-yield instrument that blocks trading liquidity or creates gap risk is not worth it.

## 6. Custom Chart Strategies: Good Tool, Bad Master

Custom chart strategies can help if they become systematic labels. They are dangerous if they become visual curve fitting.

Correct use:

- Turn the chart idea into measurable events.
- Define the signal before testing.
- Label forward returns.
- Test across symbols, periods, and regimes.
- Compare against simple baselines.
- Paper-forward before production.

Wrong use:

- Look at a chart, describe a pattern, fit thresholds to that chart, and assume it will repeat.

Recommendation:

- Build a "chart-to-label" workflow:
  - Human marks example setup.
  - System converts it to measurable features.
  - Research engine tests it across history.
  - If it survives, it becomes a strategy card.
  - If it fails, it goes into the rejection log.

## 7. Production Buying Process Target State

The buying process should be completely dynamic except for explicit policy limits such as maximum risk percent and kill-switch rules.

### 7.1 Required order pipeline

Every proposed order should pass through this path:

1. Strategy emits signal.
2. Feature snapshot is saved.
3. Strategy confidence tier is computed from live/paper evidence.
4. Risk percent is selected from `fleet_sizing.json`.
5. Broker account equity is read.
6. Broker buying power/margin impact is read or estimated from broker truth.
7. Stop distance and invalidation level are computed from current market structure/volatility.
8. Position size is computed from actual stop distance, not configured default.
9. Portfolio risk gate checks total open risk, correlated exposure, direction bias, family caps, and margin.
10. Order ticket is persisted before submit.
11. Order is submitted.
12. Broker ack, order id, status, partial fills, and final fills are persisted.
13. Position ledger reconciles broker truth.
14. Trade journal is derived from fills.
15. Dashboard updates from canonical ledger.

### 7.2 Dynamic sizing policy

Keep the current confidence tiers, but make the allocator smarter:

| Tier | Current risk pct | Required evidence before tier |
|---|---:|---|
| Unproven | 0.50% | Default paper/micro risk only |
| Emerging | 1.00% | 10+ valid trades, PF >= 1.00, no critical drift |
| Validated | 1.50% | 30+ valid trades, PF >= 1.20, stable execution |
| Promoted | 2.00% | 60+ valid trades, PF >= 1.30, regime diversity |
| Exceptional | 3.00% | 100+ valid trades, PF >= 1.50, drawdown within expected bounds |

Enhancements:

- Use lower confidence bound of expectancy, not just raw PF.
- Compute tier per strategy and per market regime.
- Decay old evidence if the last 30 trades underperform.
- Do not let historical backfills influence live sizing unless marked as research evidence only.
- Use fractional-Kelly style overlays only after enough evidence exists, and cap them at the policy ceiling.

### 7.3 Broker dynamic caps

Keep static notional caps as hard fail-safes, but add:

- `available_funds`
- `buying_power`
- `excess_liquidity`
- `initial_margin`
- `maintenance_margin`
- `day_trading_buying_power`, where applicable
- estimated post-trade margin
- estimated stress loss

Order should be blocked if:

- post-trade excess liquidity would fall below policy threshold.
- margin impact is unknown for the instrument.
- account is near a day-trading or house-margin restriction.
- broker/local positions disagree.

## 8. Dashboard Target

The dashboard should become the trading cockpit, not a report viewer.

Required first screen:

1. Account equity curve, last 7d/30d/all-time.
2. Current broker equity, cash, buying power, excess liquidity.
3. Fleet open risk and remaining risk budget.
4. Open positions with risk-to-stop and unrealized P&L.
5. Recent trades table with fills, strategy, tier, risk pct, P&L, and validity.
6. Strategy tiles:
   - status
   - mode
   - confidence tier
   - last signal
   - last trade
   - live P&L
   - promotion blockers
   - data freshness
7. Entry blockers panel.
8. Risk drift panel.
9. Live-vs-replay drift panel.
10. Alerts panel.

Important visual principle:

- Do not show historical back-fill P&L in the same visual weight as live-window P&L.
- Default to live-window performance.
- Let all-time/research evidence be visible but clearly labeled.

## 9. Test Matrix Before Any Strategy Is Considered Production

### 9.1 Data tests

- No missing bars during market hours.
- Timezone correctness.
- Daylight-saving-time handling.
- Corporate actions adjusted correctly.
- Futures roll logic if futures are used.
- T+1/T+3/T+5 labels use trading days, not calendar days.
- Data availability timestamp is stored.

### 9.2 Backtest correctness tests

- Next-bar-open entry.
- Stop-before-target same-bar pessimism.
- Slippage and spread model.
- Commission.
- Borrow and short constraints where relevant.
- No lookahead.
- No survivorship bias.
- No duplicate signals.
- Parameter stability.

### 9.3 Walk-forward tests

- Train/test split by time.
- Multiple regimes.
- Parameter freeze per fold.
- Compare against simple baseline.
- Reject if edge only appears in one fold.

### 9.4 Paper/live parity tests

- Live features match replay features.
- Signal count ratio within policy.
- Entry/skip reasons logged.
- Broker spread matches model.
- Order sizing matches expected formula.
- Dashboard matches artifacts.

### 9.5 Execution tests

- Partial fill.
- Cancel/replace.
- Rejected order.
- Broker disconnect before fill.
- Broker disconnect after partial fill.
- Process restart mid-position.
- Duplicate fill event.
- Out-of-order fill event.

### 9.6 Risk tests

- Max per-trade risk.
- Max fleet risk.
- Correlation cap.
- Directional bias cap.
- Family cap.
- Margin cap.
- Drawdown brake.
- Kill switch.
- Manual unlock.
- Unknown symbol fail-closed.

### 9.7 Operations tests

- Fleet monitor crash-loop.
- Hash mismatch.
- Discord/webhook failure retry.
- Stale heartbeat.
- FATAL log scan.
- PAUSE_ENTRIES age.
- Risk-state drift.
- Scheduled one-shot freshness.
- Dashboard stale source warning.

### 9.8 Model-risk tests

- Deflated Sharpe or equivalent multiple-testing adjustment.
- Minimum track record length.
- Bootstrap confidence interval for expectancy.
- Monte Carlo trade-order reshuffle.
- Stress transaction costs.
- Stress lower win rate and worse payoff ratio.

## 10. The Critical Implementation Roadmap

### Phase 0: Do not regress the operating system

Goal: keep the fleet running and honest.

Tasks:

- Restart components that still need the latest code paths.
- Finish fleet-monitor fault-injection tests.
- Confirm all 19 systems stay visible after restart.
- Confirm dashboard canonical endpoints read fresh files.
- Confirm risk drift detection flags both directions.

Exit criteria:

- No hidden entry blockers.
- No stale dashboard truth.
- No untested alert pathway.

### Phase 1: Make trade truth canonical

Goal: every P&L number comes from reconciled fills.

Tasks:

- Add canonical order/fill/position ledger.
- Derive trade CSVs from fills.
- Add broker account snapshot on every order decision.
- Add post-trade reconciliation event.
- Add `order_intent_id` linking signal, risk decision, broker order, fill, and trade.

Tests:

- Partial fill test.
- Restart mid-order test.
- Duplicate fill test.
- Broker/local mismatch blocks new entries.

Exit criteria:

- Dashboard recent trades and account curve are fill-derived.

### Phase 2: Build the alpha lab

Goal: all strategy ideas go through the same proof machine.

Tasks:

- Create `research/strategy_cards/`.
- Create one common backtest report schema.
- Add trial registry for every tested idea.
- Add walk-forward summary.
- Add cost/capacity model.
- Add deflated Sharpe or equivalent model-risk metric.
- Add rejection log.

Exit criteria:

- No new strategy can bypass a strategy card and validation report.

### Phase 3: Promote two near-term candidates properly

Best candidates:

1. Forge GLD PM Long.
2. GDX/GLD relative value.
3. Wick GBPUSD only if replay/live drift is fixed.
4. Apollo earnings drift only after forward-return sample grows.

Tasks:

- For each, write edge thesis, rules, risk, tests, and kill rule.
- Paper-forward with live feature snapshots.
- Add to promotion gate with strategy-specific sample requirements.

Exit criteria:

- At least one strategy reaches promoted or validated status with clean evidence.

### Phase 4: Add missing major sleeves

Priority order:

1. Cross-asset trend following.
2. Intraday futures ORB/VWAP sleeve.
3. Earnings/event drift.
4. Relative-value spread engine.
5. VIX/regime filter.

Reason:

- These provide different kinds of edge.
- They reduce dependence on one narrow setup.
- They give the allocator more chances to deploy capital when one market is dead.

### Phase 5: Build the allocator

Goal: best risk-adjusted current opportunities get capital.

Tasks:

- Strategy expected value estimate.
- Confidence-adjusted risk.
- Correlation-adjusted risk.
- Regime-specific tier.
- Margin-aware order cap.
- Drawdown de-risking.
- Capital queue for simultaneous signals.

Exit criteria:

- Two correlated weak signals cannot crowd out one stronger independent signal.

## 11. What To Stop Doing

Stop treating these as success:

- Many systems running.
- Many backtests.
- One good live trade.
- Historical back-fill P&L.
- Alerts firing.
- Strategies that are "OK" because they have a heartbeat.

Start treating these as success:

- Clean live valid trade count.
- Signal parity.
- Fill-derived P&L.
- Strategy-specific expectancy after cost.
- Risk-adjusted return with confidence interval.
- Surviving fault injection.
- Fast diagnosis of blocked entries.
- Killing weak strategies quickly.

## 12. What Would Put The System Ahead Of Competition

The advantage is not one secret indicator. It is process speed plus truth quality.

Build advantages:

1. Faster idea-to-test loop.
2. Better data hygiene.
3. Better execution realism.
4. Better post-trade learning.
5. Better strategy death process.
6. Better regime awareness.
7. Better capital allocation.
8. Better operational reliability.
9. Better dashboard clarity.
10. Better discipline about not scaling unproven edge.

Competitors lose money because they overfit, oversize, under-measure costs, ignore operations, and keep broken strategies alive. The way to beat them is to be colder about evidence and faster about iteration.

## 13. Concrete Work Items

### P0: before claiming production readiness

- Finish fleet-monitor fault-injection tests.
- Add broker dynamic margin/buying-power checks to order gate.
- Add fill-derived recent-trades dashboard.
- Add account equity/balance tracking from broker snapshots.
- Add live-vs-replay drift report per strategy.
- Classify every running system as production candidate, research-only, signal-only, or retired.
- Update old governance docs so required valid trades and active instruments match current promotion rules.

### P1: highest ROI engineering

- Canonical order/fill/position ledger.
- Strategy card schema.
- Common backtest report schema.
- Cost/slippage/capacity model.
- GDX/GLD live-forward promotion path.
- GLD PM Long expanded validation.
- Apollo event-drift strategy conversion.
- Dashboard PM cockpit.

### P2: highest ROI research

- Cross-asset trend following.
- Intraday futures opening range/VWAP.
- Earnings drift.
- Relative-value spread engine.
- VIX regime filter.
- Sector momentum/rotation.

### P3: later, only after infrastructure

- Options strategies.
- Faster news/text trading.
- Futures calendar spreads.
- ML meta-allocator.
- Alternative data.

## 14. Strategy Card Template

Every strategy should have this file before it gets production sizing:

```text
strategy_id:
owner:
mode: research_only | paper | micro_live | live
asset_class:
instruments:
edge_hypothesis:
why_this_edge_should_exist:
entry_rules:
exit_rules:
stop_logic:
position_sizing:
expected_trade_frequency:
known_failure_modes:
data_sources:
feature_availability_times:
backtest_period:
walk_forward_periods:
transaction_cost_model:
slippage_model:
live_forward_start:
promotion_gate:
kill_rules:
dashboard_fields:
runbook:
```

## 15. Current Confidence Summary

System operations confidence: medium and rising.

Reason:

- Fleet is visible.
- Risk state is visible.
- Entry blocks are visible.
- Dynamic sizing is in place.
- But fault-injection tests remain incomplete.

Strategy profitability confidence: low to medium-low.

Reason:

- Promotion gate has 0 promoted strategies.
- Last-7-day live USD sample is tiny.
- Some research candidates look promising, especially GLD PM Long and GDX/GLD, but evidence is not enough for high confidence.

Portfolio ROI confidence: low.

Reason:

- Last-7-day measured return is tiny.
- All-time return includes historical back-fill.
- No annualized ROI should be claimed from current live evidence.

Best near-term path to higher confidence:

1. Keep current fleet running in monitoring/paper mode.
2. Finish operational fault testing.
3. Make fills and broker snapshots canonical.
4. Promote only the best two to four strategy candidates.
5. Add cross-asset trend and intraday futures research sleeves.
6. Build the allocator after strategies have comparable evidence.

## 16. External Research And Regulatory Anchors

These are not proof that our implementation works. They are anchors for strategy families and operational constraints:

- Time-series momentum: Moskowitz, Ooi, and Pedersen document cross-asset return persistence in futures and diversification benefits. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463
- Long-run trend following: Hurst, Ooi, and Pedersen study trend following across global markets since 1880 and report positive average returns across decades with crisis-period usefulness. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2993026
- Post-earnings announcement drift: Bernard and Thomas document delayed price response/risk-premium debate around earnings drift. Source: https://econpapers.repec.org/RePEc:bla:joares:v:27:y:1989:i::p:1-36
- Pairs trading: Gatev, Goetzmann, and Rouwenhorst study relative-value pairs trading with historical profitability after conservative cost estimates in much of their sample. Source: https://www.nber.org/papers/w7032
- Value and momentum across assets: Asness, Moskowitz, and Pedersen document value and momentum premia across asset classes and their common factor structure. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2174501
- Technical pattern recognition: Lo, Mamaysky, and Wang propose systematic technical-pattern recognition and find some indicators contain incremental information in their sample. Source: https://www.nber.org/papers/w7613
- Backtest overfitting risk: Bailey and Lopez de Prado describe the Deflated Sharpe Ratio to correct for selection bias, multiple testing, and non-normality. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Kelly/fractional Kelly: MacLean, Thorp, and Ziemba survey capital growth theory and note full Kelly can create painful short-term losses, with fractional Kelly reducing risk at the cost of lower expected long-run wealth. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1797366
- Day trading constraints: FINRA's investor page still describes the $25,000 pattern-day-trader framework and buying-power limits, while the SEC page for SR-FINRA-2025-017 shows an April 14, 2026 order granting accelerated approval to replace FINRA Rule 4210 day-trading provisions with intraday margin standards. Treat this as a transition item: verify the effective date and the broker's implementation before any stock/options day-trading plan. Sources: https://www.finra.org/investors/investing/investment-products/stocks/day-trading and https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking/sr-finra-2025-017
- Broker margin reality: Interactive Brokers notes margin requirements depend on residence, exchange, product, house requirements, and high-risk exposure fees. Source: https://www.interactivebrokers.com/en/trading/margin-requirements.php
- Market access risk controls: SEC Rule 15c3-5 requires risk controls designed to limit financial exposure and prevent erroneous orders before market access. Source: https://www.sec.gov/rules-regulations/2011/06/risk-management-controls-brokers-or-dealers-market-access
- T+1 settlement: SEC moved standard settlement for most broker-dealer transactions from T+2 to T+1 on May 28, 2024. Source: https://www.sec.gov/newsroom/press-releases/2024-62

## 17. Final Recommendation (Original - Superseded By Section 18)

> Original recommendation preserved below for reference. **Read Section 18 for the applied plan.**

Do not go into passive monitoring mode as if the money machine is proven.

Go into controlled monitoring mode with a clear sprint plan:

1. Prove operational survivability.
2. Make broker/fill truth canonical.
3. Upgrade dashboard visuals for account balance and recent trades.
4. Promote the best current candidates only after clean evidence.
5. Build the missing strategy sleeves through the alpha lab.
6. Scale risk only as evidence improves.

The system is now close enough to start learning seriously. It is not close enough to size aggressively.

---

## 18. Corrected Debate Outcome and Applied Scope (2026-04-18)

This section supersedes sections 1-17 where they conflict. The original blueprint is retained above as reference.

### 18.1 Correctness pass in one sentence

The scope-reduction argument is correct, but the first Section 18 draft was too confident in a few places: it treated $/year estimates as if they were forecasts, implied a $10K live move could happen before the current gates are satisfied, understated the current 2026 PDT transition, and risked bypassing the existing 60-valid-trade promotion standard.

### 18.2 Verified current state

As of the local artifacts reviewed on 2026-04-18:

- `argus_flow/logs/fleet_status.json`: 19 systems are visible and OK, Argus entries are not blocked, `PAUSE_ENTRIES`, `RESET_DRAWDOWN`, and `KILL_SWITCH` are absent, and risk state is GREEN. The guard still warns about Apollo concentration and directional bias.
- `argus_flow/logs/fleet_perf_summary.json`: broker-equity anchor is about $1,002,591.71. Last-7-day USD roll-up is +$97.60, or +0.0097%. All-time USD roll-up is +$9,172.47, but this is dominated by historical GDX/GLD back-fill.
- `argus_flow/logs/promotion_gate_report.json`: 0 promoted strategies, 2 NOT_READY Argus runners. GBPUSD has 0 valid trades. USDJPY has 2 valid trades, negative expectancy after friction, PF 0.88, and severe replay/live frequency drift.
- `apollo/logs/forward_returns.jsonl`: 88 records exist. T+1 has 88 observations, 52.3% positive, average +0.286%. T+3 has 50 observations, 66.0% positive, average +1.162%. T+5 has 17 observations, 70.6% positive, average +2.004%. This is useful research evidence, not an executable strategy yet.
- `research/label_first/reports/FINDINGS_v3.md`: GLD PM Long backtest PF 1.73 over 530 trades / 2 years. Current live/paper USD evidence is still only 1 recent trade.
- `argus_flow/configs/fleet_sizing.json`: sizing is broker-equity dynamic with $10K fallback, but current paper broker equity is about $1M. Any $10K live plan must use a $10K-specific profile or risk-normalized paper sizing so paper results are not psychologically or operationally misleading.

### 18.3 What the debate got right

The important conclusions still hold:

1. Scope creep is the real risk. The fleet grew faster than the evidence base.
2. Sections 1-17 describe an institutional-quality destination, not a 4-week single-operator sprint.
3. VIX direct products, options, standalone macro, news/text, cash/collateral, broad sector rotation, and new intraday sleeves should be deferred. They may be future research, but they should not enter paper or micro-live until one existing candidate is resolved.
4. Fractional-Kelly overlays should stay disabled. No current strategy has enough clean live evidence for Kelly inputs.
5. The 15-step canonical order pipeline is directionally right but too large for the next sprint. The near-term version is fill-derived recent trades plus broker account/equity snapshots.
6. The useful reusable pieces are: Section 11, Section 14 strategy cards, Section 2.2 signal-frequency drift, and Section 6 chart-to-label workflow.

### 18.4 Corrected live-capital position

Do not fund live trading merely because the doc says a $10K micro-live account is "educational." Current evidence does not prove a live edge.

A $10K micro-live move is only defensible after these conditions:

1. Gateway/fleet operational tests pass.
2. Fill-derived recent trades and broker equity curve are visible on the dashboard.
3. The strategy has a completed strategy card.
4. The strategy is PDT-safe or the broker confirms the applicable intraday-margin rules are active for that account.
5. The account profile caps risk at 0.25% to 0.50% per trade. Current unproven tier is 0.50%, so a 0.25% "tuition mode" requires an explicit config/profile change.
6. The operator accepts that early live trading is data collection, not proven compounding.

The safer default remains paper/monitoring until a strategy-specific review says otherwise.

### 18.5 Corrected ROI target

The doc should not imply 8-15% is the base expectation today. Current live evidence supports "unknown, low confidence."

Use these ranges:

| State | Planning assumption |
|---|---:|
| Current unproven state | 0-3% annual, including a real chance of negative return |
| After one validated edge | 3-8% annual target |
| After promoted edge and stable execution | 8-15% annual stretch target |
| 15-40% per strategy | Not supported by current live evidence |

Reference math for a $10K account:

| Scenario | Math | Annualized result |
|---|---|---:|
| Conservative learning | $10K * 0.25% risk * 25 trades * 0.2R | +$125 / +1.25% before mistakes/cost drift |
| Realistic if edge exists | $10K * 0.50% risk * 50 trades * 0.3R | +$750 / +7.50% |
| Strong but still plausible | $10K * 0.50% risk * 75 trades * 0.4R | +$1,500 / +15.0% |
| Aggressive, not a base case | $10K * 1.00% risk * 100 trades * 0.4R | +$4,000 / +40.0% |

These are scenario math, not forecasts. The table also shows why 15-40% is not impossible in arithmetic, but it requires a real edge, enough trade frequency, stable execution, and risk levels that are not justified by the current evidence. If live expectancy is zero or negative, more trades only compounds churn.

### 18.6 Corrected strategy shortlist

Ranked by best near-term evidence-to-effort ratio, not by guaranteed profit:

1. `forge_gdx_gld`: Best historical seed. It has 87 historical trades and dollar-neutral pair logic, but the live-window view has 0 recent trades. Gate: fill-truth path, paper-forward tracking, and no use of historical back-fill for live sizing.
2. `forge_gld_pm_long`: Strongest nearby strategy research. Backtest PF 1.73 over 530 trades / 2 years, but current USD evidence is one recent trade. Gate: 10+ more clean paper/live trades for a first review, and current promotion standards for actual promotion.
3. Apollo earnings drift: Research evidence is now falsifiable. T+3 currently has 50 observations, 66.0% positive, average +1.162%. Gate: build an executable strategy card and order path or explicitly keep it scanner-only. T+5 looks better but only has 17 observations, so treat it as thin evidence.
4. Argus FX pairs: Keep running for evidence, but do not prioritize for production until live-vs-replay drift is fixed. Current promotion gate says GBPUSD and USDJPY are NOT_READY.

Do not present annual dollar forecasts for these strategies until they have forward paper/live evidence under the intended account size and sizing profile.

### 18.7 Corrected PDT and margin note

This area changed during April 2026 and must be handled carefully.

- FINRA's public day-trading page still describes the old pattern-day-trader framework: four or more day trades in five business days, more than 6% of account activity, and a $25,000 minimum equity requirement.
- The SEC page for SR-FINRA-2025-017 shows that on April 14, 2026 the SEC issued Release 34-105226, granting accelerated approval to amend FINRA Rule 4210 and replace the day-trading margin provisions with intraday margin standards.
- Practical rule for this system: before any stock/options day-trading plan, verify the broker's actual effective date, account eligibility, and house rules. Do not assume the PDT constraint is gone in the account just because the SEC approved the rule change.
- Overnight stock/ETF strategies remain simpler from a PDT perspective because they are not same-day round trips.
- Cash forex is outside the stock/options PDT framework, but still has broker leverage, margin, spread, and rollover risks.
- Futures do not use the PDT framework, but initial/maintenance/day margins can be large relative to a $10K account and can change quickly. Use broker margin truth, not a fixed "$500 per contract" assumption.

### 18.8 Applied 4-week plan

This is still the right near-term scope:

| Week | Hours | Ship |
|---|---:|---|
| 1 | 8h | IBC/gateway resilience work and recovery behavior review |
| 1 | 12h | Fault-injection tests: hash mismatch, crash loop, stale heartbeat, Discord failure |
| 2 | 8h | Fill-derived recent-trades dashboard panel from broker fills where available |
| 2 | 6h | Account equity curve widget from broker/account snapshots |
| 3 | 10h | Live-vs-replay drift report for `forge_gdx_gld` and `forge_gld_pm_long` first |
| 3 | 6h | Strategy cards for `forge_gdx_gld`, `forge_gld_pm_long`, `forge_wick_gbpusd`, and Apollo |
| 4 | 5h | Observation week and one promotion-readiness review |
| Buffer | 10h | Gateway noise, broker login issues, unexpected fires |

Total: about 65 hours. Zero new strategy families. No allocator. No options. No broad alpha lab.

### 18.9 Governance rule against sprawl

Proposed hard rule for `project_validation_charter.md`:

> No new strategy family enters `paper` or `micro_live` mode until:
> (a) one existing strategy reaches a formal review point and is either promoted, demoted, or killed,
> (b) the new strategy has a completed Section 14 strategy card,
> (c) the fleet master file is updated with the decision,
> (d) the dashboard/reporting path can show its live-window USD P&L or explicitly mark it research-only.

The constraint is not ideas. The constraint is finishing.

### 18.10 Stop-iterating threshold

Do not suspend all research because two strategies have 30 combined trades. That can hide one weak strategy behind one good one.

Use this instead:

- If one strategy reaches the current promotion gate, follow the gate.
- If a strategy reaches 30 clean valid trades with PF >= 1.20 and positive expectancy after costs, stop adding new strategy families for 30 days and focus on observation, execution quality, and risk discipline.
- If a strategy reaches the existing 60-valid-trade promoted gate with PF >= 1.30 and stable execution, suspend new research for 90 days and compound/monitor the proven sleeve.

This preserves discipline without weakening the current promotion system.

### 18.11 Revised phase plan

**Phase 0+1, weeks 1-4: Operational stability plus visible fill/account truth.**

Ship the 4-week plan in Section 18.8. Exit criteria: gateway recovery is stable, fault-injection tests pass, fill-derived recent trades are visible, account equity curve is visible, and four strategy cards exist.

**Phase 2, weeks 5-12: Observe and review.**

Run `forge_gdx_gld` and `forge_gld_pm_long` under the intended paper/live sizing profile. Thirty combined trades is a review point, not a promotion override. Promotion must still satisfy the current promotion gate unless the gate itself is deliberately changed in code and governance docs.

**Phase 3, months 4-6: Micro-live only if evidence permits.**

Fund or trade a $10K live profile only after a candidate has passed the relevant review, dashboard truth is fill/account-derived, broker rules are verified, and risk is capped at an explicit micro-live level. Monitor 30 live trades before increasing risk.

Phases 4 and 5 from the original blueprint remain parked until Phase 3 produces evidence.

### 18.12 Honest bottom line

The updated Section 18 is directionally right after these corrections: narrow scope, stop sprawl, prioritize GDX/GLD and GLD PM Long, keep Apollo falsifiable, and do not scale Argus FX until drift is explained.

The system is closer than it was. The remaining gap is not a missing exotic strategy. It is verified execution truth, clean evidence, and the discipline to finish one candidate before adding five more.
