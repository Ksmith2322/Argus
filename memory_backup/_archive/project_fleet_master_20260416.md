---
name: Fleet Master Plan — Source of Truth (2026-04-16)
description: Complete status of all 12 trading systems. Replaces 20260408 snapshot. Fleet expanded from 5→12 systems in 7 days; AUDJPY killed; Apollo rebuilt; Forge family added.
type: project
originSessionId: 3a7a2a39-5348-43c1-8feb-cb0d2b0662ae
---
# Helio Fleet Master Plan
**Updated: 2026-04-16** (supersedes 20260408 snapshot)
**Goal:** Multi-strategy uncorrelated fleet. Fleet target: 15-30%/year on $10K. Prove edge → fund → compound.

## Major changes since 2026-04-09
- **AUDJPY killed** 2026-04-14 (kill discipline). 3 Argus pairs remain: USDJPY, GBPUSD, CADJPY
- **Apollo rebuilt** — NOT killed. Now an earnings/catalyst scanner (BB squeeze, post-earnings drift), not the old FX swing
- **Forge family added** — 5 new sub-systems (atlas, themis, mamba, tori, gdx_gld) + cuebanks incubating
- **Ares running** as signal-only sector rotation (was "not built")
- **Oracle running** but pivoted to **Polymarket** edge scanner (was earnings/catalyst per old plan)
- **Hermes running** (gap fill)
- Repo is **NOT** a git repo at C:\Argus\repo (no .git) — use file mtimes for history

## IBKR Constraints (unchanged)
- Max 32 clientIds, 100 market data lines, $53 real / $10K model equity per strategy

## Active Systems (10 in fleet_status.json + Oracle + Cuebanks)

| System | Type | Status | Path |
|--------|------|--------|------|
| **Argus** | FX intraday MTF + AI overlay | LIVE PAPER (3 pairs) | `argus_flow/` |
| **Titan** | Stock/commodity swing (4 sub-strats) | LIVE signal-only | `titan/` (root) |
| **Apollo** | Earnings/catalyst scanner (REBUILT) | LIVE alerts only | `apollo/` |
| **Hermes** | Daily gap fill | LIVE | `hermes/` |
| **Ares** | Sector rotation (RS, monthly) | LIVE signal-only | `ares/` |
| **Oracle** | Polymarket edge scanner (PIVOTED) | EXISTS | `oracle/` |
| **Forge.Atlas** | Macro events/RSS/regime | LIVE | `forge/atlas/` |
| **Forge.Themis** | Congressional trades signal | LIVE | `forge/themis/` |
| **Forge.Mamba** | NQ/YM NY-open breakout (9:25-10:30 EST) | LIVE | `forge/mamba/` |
| **Forge.Tori** | 4H trendline swings (PL/CL/GC/YM) | LIVE | `forge/tori/` |
| **Forge.GDX_GLD** | Pairs trading (miners/gold) | LIVE | `forge/gdx_gld_runner.py` |
| **Forge.Cuebanks** | TBD (rulebook only) | INCUBATING | `forge/cuebanks/` |
| Dashboard | Web UI | LIVE | `ops/dashboard.py` |

## Argus Detail
- **Active pairs:** USDJPY, GBPUSD, CADJPY (paper QA stage)
- **Killed:** AUDJPY (2026-04-14, 7 trades, +$43.99 PnL — killed by kill discipline)
- **Trade counts:** USDJPY 16, CADJPY 16, GBPUSD 0 (in QA, 1 invalid trade)
- **Promotion gate verdict (2026-04-16):** Both GBPUSD/USDJPY = NOT_READY (need 60 valid trades, only 1-3 logged; missing config_hash/git_sha fields in trades; 100% invalid rate on GBPUSD)
- **Real configs live:** 0 (zero live trading; all paper)
- **Trade storage:** `argus_flow/logs/<pair>/trades.csv` (NOT `argus_flow/data/trades/` — that's empty)

## AUDJPY stale-heartbeat (FIXED 2026-04-16)
- Edited `helio/fleet_monitor.py`: added `_argus_killed_symbols()` helper that reads `deployment_registry.json` and skips heartbeats for any symbol with `current_stage == "killed"`. Argus now reports OK instead of STALE.
- Config still on disk: `argus_flow/configs/audjpy_mtf_paper_v1.json` with `launch_enabled: false`
- See `argus_flow/logs/kill_discipline_report.json` for kill rationale

## Trade instrumentation (already in place — no edit needed)
- `runner_unified.py:1720-1724` already writes `experiment_valid, invalid_reason, config_hash, session_id, runtime_epoch, git_sha` for every trade. Verified: USDJPY/GBPUSD trades.csv have these columns populated.
- Promotion-gate complaint "no config_hash field found in valid trades" is a downstream symptom of "0 valid trades" — vacuously fails because the set is empty. Misleading but not a bug.

## GBPUSD signal-starvation diagnosis (2026-04-16)
- Only 9 candidate signals in last 7d; 4 of 5 blocks were REGIME_BLOCKED (config had `regime_gate: GATE` while USDJPY had `LOG_ONLY`)
- **Fix applied:** flipped `argus_flow/configs/gbpusd_range_paper_v1.json` regime_gate to LOG_ONLY (matches USDJPY's proven pattern)
- Historical SIZE_ZERO (153) and OPEN_RISK_LIMIT (247) counts in `opportunities.jsonl` are STALE — came from older config_hashes already replaced. Don't be misled by aggregate counts; always filter by date or current config_hash.
- **Restart-taint not the real bottleneck:** even with ~7 IBKR reconnects/week (nightly 23:45 UTC + spot failures), most happen FLAT. Signal frequency is the constraint. Don't add pre-disconnect flatten until signal rate ≥3/day per pair.
- **Open question:** replay_expectations claims 18.9 signals/day for GBPUSD range_accel; observed ~1.3/day. ~14x gap. Worth investigating after regime_gate change has 1 week of data.

## Forge family health (audit + follow-up 2026-04-16)
- **PRODUCTIVE:** atlas, mamba, cuebanks
- **themis:** reassessed — actually fine, just quiet. Process alive (pid 82104), heartbeat fresh, `new_signals_this_cycle: 0` means no new congressional trades to flag, not a crash. runner_bg.log empty because logging goes to DB/heartbeat. Don't restart.
- **gdx_gld:** recovered. Heartbeat + live z_score/spread data flowing as of 22:45. The Apr 15 ConnectionError was historic; runner retried past it.
- **oracle:** added to nightly `ops/run_cohort_report.ps1` (after Ares, before Titan scanner). Will run daily with `--dry-run`. Fleet_monitor's 26h artifact threshold will pass once first nightly run completes.
- **tori:** reassessed — actually fine. Scans every 4hr, writes heartbeat, prints results to stdout (captured to runner_bg.log). Just hasn't found A/A+ trendlines meeting bounce/break criteria lately. Same pattern as USDJPY's MTF gates — tight criteria, sparse signals. No code change.

## Atlas Discord ban (2026-04-16)
- Root cause: `forge/atlas/runner.py:224` looped and posted each high-severity event individually. With ~69 events/cycle, flooded Discord.
- Consequence: Cloudflare error 1010 (IP ban). Webhook GET returns 403.
- **Fix applied:** rewrote the loop to batch top-10 events into one summary message per cycle. Commit after Cloudflare ban lifts (IP bans are typically 24h).
- Webhook token itself is valid; only the IP is blocked. Can verify recovery later with `curl <webhook>` returning 200.

## Fleet monitor extension (2026-04-16)
- Added `artifact_glob` + `artifact_max_age_s` config keys for scheduled-task systems
- Added `no_restart` flag to skip auto-restart for externally-scheduled jobs
- Wired up `ares` (artifact: `ares/logs/signal_*.json`, 26h max) and `oracle` (`oracle/logs/scan_*.json`, 26h max)
- Verified: ares=OK (18.5h), oracle=STALE (6.85d — needs to run)

## GBPUSD signal pipeline truth (2026-04-16)
- 919 bars evaluated last 7d → **97.4% NO_TRIGGER** (strategy triggers don't fire on live bars)
- Of 24 bars that did attempt: 19 hour-filtered, 4 regime-blocked (now LOG_ONLY), 1 spread-blocked
- regime_gate flip unlocks ~4 signals/week. Helpful, not transformative.
- **Real bottleneck:** `range_accel` trigger thresholds (`range_pct_min: 0.001` = 10 pips) don't match live GBPUSD bar distributions. Replay's 18.9 signals/day claim is fantasy.
- **Actionable:** lower range_pct_min, OR retire range_accel for GBPUSD and switch to mtf_trend like USDJPY
- Decision deferred: needs 1 week of data post-regime-flip to baseline

## Strategic findings batch (2026-04-16 evening)

### CADJPY — KEEP_WATCHER
- 1 trade in 8.8 days (loss, -0.7 pips). Backtest PF only 1.01 ("thin edge" per own config note).
- Watcher→paper criteria: needs 50 observed signals, has 10. Needs 7 days, has 8.8. Frequency 1.14/day OK.
- Hard gate: 10/50 observed signals. NOT promotable. Status: COLLECTING.

### Apollo — NOISY (77 alerts/day, 0 trades ever)
- 83 signals/day, scoring inflated (10% scored 90+ but tickers repeat across days = noise)
- Same ticker re-flagged daily as BB/momentum noise oscillates around stable earnings dates
- Discord sends everything ≥45 score with no per-ticker dedup → user has trained themselves to ignore
- Post-ER drift code exists but dormant (zero plays last 14d)
- **Fixes needed:** raise score floor to 75, dedup per `symbol+earnings_date`, decide auto-execute vs alert-only

### Forge logging — 4 of 8 systems misconfigured
- HEALTHY (FileHandler + StreamHandler): atlas, gdx_gld, vix_revert, rebalance
- BROKEN (no FileHandler, logs lost when Popen redirects stdout): themis (0 bytes!), mamba, tori, cuebanks
- Discord webhook is shared — atlas spam took down notifications for the whole fleet today
- **Fix needed:** standard `setup_logging(name)` helper retrofitted into the 4 broken runners

## Edge-improvement changes (2026-04-16, post-debate)

### Apollo: noise reduction
- `apollo/runner.py:399`: added `_dedup_results()` — persists alert_history.json keyed on `symbol|earnings_date`, only re-alerts on first appearance OR ≥15-point score change. Killed the daily re-flag noise that trained operator to ignore Discord.
- Raised thresholds: imminent 60→75, watchlist 45→75, post_er 50→75
- `runner.py:622`: skip Discord post entirely if no actionable signals after dedup/threshold
- Expected impact: 77 alerts/day → maybe 5-15/day, all higher-conviction. If user STILL doesn't trade these, it's a strategy/execution problem, not signal noise.

### GBPUSD: data-driven threshold relaxation
- Was: `range_pct_min: 0.001` (10 pips). Last 7d: only 24 trigger candidates, 0 valid trades.
- Now: `range_pct_min: 0.0008` (8 pips).
- Justification: NO_TRIGGER bars had p90 range_pct = 0.00114, sitting just below gate. Triggered bars min = 0.00101. Threshold was clipping near-misses.
- Expected impact: ~8x more trigger candidates (24/week → ~190/week). Most will be filtered by downstream gates (regime now LOG_ONLY, risk gates, etc.). Realistic outcome: 5-15 actual trades/week vs current 0.
- **Watch for:** if win rate craters below 40%, threshold was too aggressive — bump back to 0.001 or step to 0.0009. If win rate holds while volume jumps, hold and accumulate cohort.

### What I deliberately did NOT do
- **Did not kill USDJPY/CADJPY** — they hold paper data + your strategic intent. Killing is irreversible.
- **Did not swap GBPUSD strategy** — relaxing one parameter is reversible; replacing the whole engine isn't.
- **Did not archive entire systems** — Skeptic's "concentrate to 2" is a strategic decision that needs your call.
- **Did not retrofit Forge logging/cohort yet** — it's a real day's work and competes with strategy iteration. User should pick: ops-first (Operator) or strategy-first (Strategist) sequencing.

## Code changes applied 2026-04-16 evening
- `argus_flow/ops/promotion_gate_v2.py`: added `_awaiting()` classification; when valid_trades is empty, downstream checks now report "awaiting valid trades" instead of cascading 14 false-failure messages. Verified: GBPUSD now shows 3 real blockers + 14 awaiting.
- `argus_flow/runner_unified.py:2592-2602`: added pre-disconnect flatten for paper/watcher forex configs at >=23:30 UTC. Exit reason: `pre_disconnect_flatten`. Real configs unaffected.
- `ops/run_cohort_report.ps1`: added Oracle daily run between Ares and Titan scanner.
- `forge/atlas/runner.py:223`: batched Discord events into single top-10 message per cycle (was 69+ individual posts).
- `helio/fleet_monitor.py`: added artifact_glob/artifact_max_age_s/no_restart for scheduled-task systems; killed-config heartbeat skip.

## Three-way debate synthesis (2026-04-16)
Three agents argued from Skeptic / Operator / Strategist perspectives. Strong consensus:

**Universal agreement (all 3 agents):**
1. **Apollo must change** — kill, auto-execute, or aggressively rate-limit. 77 alerts/0 trades is worse than nothing.
2. **Fleet is too large for trade-rate math** — at current 1-2 trades/week/pair, 60 valid trades = 30-60 weeks per system. 12 strategies × 60 weeks = never funded.
3. **Hostile portfolio review needed** — for each system: "would I deploy this fresh given current data?" Default to archive.

**2-of-3 agreement:**
- **Forge needs cohort instrumentation** (Operator + Strategist) — without config_hash/git_sha/session_id, Forge edge is unfalsifiable
- **GBPUSD replay-vs-live divergence is the central mystery** (Skeptic + Strategist) — replay said 18.9 sig/day, live shows 0.2. Either fix or kill.
- **CADJPY is questionable** (Skeptic says kill; Strategist agrees PF 1.01 backtest doesn't merit a slot)

**Real disagreement — sequencing:**
- Operator: fix ops first (logging, alerts, schema). Without instrumentation, strategy claims are unfalsifiable.
- Strategist: ops work is avoidance. The only question is "does anything make money on unseen data."
- Skeptic: both — concentrate to 2 systems, fix ops for those, freeze everything else.

**Honest user synthesis:** Apollo and Forge cohort instrumentation are independent of the sequencing debate — both should land regardless. The harder call is whether to kill Argus pairs now (Skeptic) vs reconcile them (Strategist). User decision needed.

## Four new fundable strategies productionized via label-first research (2026-04-16)

| # | Strategy | Path | Direction | Asset | PF (live-realistic) | Cadence/yr |
|---|---|---|---|---|---|---|
| 1 | wick_gbpusd | `forge/wick_gbpusd/` | Long | GBPUSD daily | 1.24 (PF 1.42 raw) | ~13 |
| 2 | gld_pm_long | `forge/gld_pm_long/` | Long | GLD intraday | **1.73** | ~250 |
| 3 | nq_overnight | `forge/nq_overnight/` | Long | NQ futures intraday | 1.23 | ~250-400 |
| 4 | jpy_pm_short | `forge/jpy_pm_short/` | Short | USDJPY+CADJPY 1h | 1.24 / 1.30 | ~250 each |

All four:
- Cohort tagging on every trade (config_hash, git_sha, session_id)
- Discovered via Phase 2 (combination search + walk-forward) or Phase 2C/D (hour-of-day session)
- Wired into `helio/fleet_monitor.py` as artifact-monitored systems (no auto-restart)
- In `ops/run_cohort_report.ps1` for nightly eval
- Have `--loop` mode for continuous hourly operation
- Currently paper-only

**Diversification:** 4 strategies across FX (2 - GBPUSD daily long + JPY pair short), commodity ETF (1 - GLD intraday long), index futures (1 - NQ overnight long). Largely uncorrelated.

**To capture full edge:** gld_pm_long, nq_overnight, jpy_pm_short need `--loop` running (or hourly Task Scheduler). Daily cohort report only catches one fire/day.

## Two new fundable strategies discovered + productionized (2026-04-16, label-first research)

**#1 `forge/wick_gbpusd/`** — GBPUSD daily wick reversal
- Discovery: label-first analysis on 22-year GBPUSD daily history (5,818 bars)
- Pattern: long upper wick + close near low + low BB width + high choppiness → predicts up move
- Walk-forward: PF 1.42 over 298 trades, 5/5 valid folds positive
- Live-realistic backtest with rolling quantiles: PF 1.24, expectancy +0.148 ATR
- Runs daily via run_cohort_report.ps1; ~13 trades/year cadence

**#2 `forge/gld_pm_long/`** — GLD afternoon intraday long (THE STANDOUT)
- Discovery: hour-of-day analysis on 5,050 GLD 1H bars (2.5 years)
- Pattern: long GLD at top of hours 18/19/20 UTC (2pm, 3pm, 4pm NY)
- Walk-forward: 6/6 folds positive at every hour, t-stats 6.2-7.3
- Per-hour PFs: 1.33 / 1.63 / 1.77 / 2.28
- Runner backtest: PF 1.73 over 530 trades / 2 years
- 250+ trades/year potential; faster funding-gate path than GBPUSD
- **Currently scheduled daily — needs hourly Task Scheduler entry OR --loop for full coverage**

Both: cohort tagging (config_hash, git_sha, session_id) on every trade, fleet_monitor integrated, paper mode only.

## Mamba investigation (2026-04-16, 4-iteration audit)
- **Initial backtest (2-conf floor, ATR stops):** 128 trades, PF 0.68, WR 15.6%, -$902
- **Iter 1 (3-conf floor):** 121 trades, PF 0.85, WR 21.5%, -$387. By-bucket: 3-conf=13% WR, 4-conf=26%, **5-conf=36%** (looked promising)
- **Iter 2 (5-conf + swing stops per rulebook):** 28 trades, PF ~0.85, WR 21%, -$61. Swing stops HURT — synth_1min detector picks tight refs that get whipsawed.
- **Iter 3 (5-conf, ATR stops):** 29 trades, PF 0.88, WR 24%, -$73. Confirmed 5-conf-only WR is 24%, not the 36% the prior subset showed. Earlier 36% was a **14-trade artifact** caused by daily-cap crowding.
- **Verdict:** No demonstrated edge in current implementation. **Subpattern hint:** MYM (Dow micros) shows 33% WR over 15 trades (+$117) vs MNQ 14% over 14 trades (-$190). Possible YM-only edge, sample too small.
- **Audit value:** the per-conviction-bucket lens is the right diagnostic tool, even when conclusion is "no edge." See conviction-tier insight below.
- **Funding truth:** Apr 30 dead. Realistic = mid-to-late May. Mamba not the path; needs MYM-only validation OR fundamentally different signal generation.

## Conviction-tier lens — methodology insight (2026-04-16)
User's strategic insight: "reverse engineer charts to know how to trade them rather than just copy methods."
- **Method:** for any strategy, segment trades/signals into conviction buckets (3 vs 4 vs 5 confluences; low vs mid vs high score; etc.) and look at per-bucket WR/expectancy. Edge usually lives in a small high-conviction subset, diluted to noise by lower-conviction inclusion.
- **Where it works:** mamba (showed 5-conf needed validation; revealed 36% was artifact)
- **Where it fails today:**
  - Argus pairs: 1-4 trades per pair = too small for tier analysis
  - Apollo: never records post-signal price outcomes — no way to validate "do sniper signals outperform mid?"
- **Required to scale this lens fleet-wide:**
  - Argus: ~20-30 trades per pair (months at current rate, OR a strategy that fires more)
  - Apollo: add T+1, T+3, T+5 forward-return tracking to scan files
  - Forge: add cohort tagging (config_hash, git_sha, session_id) — currently absent on most

## Forge logging fixed (2026-04-16)
- Created `forge/logging_setup.py` with `setup_logging(name)` — RotatingFileHandler (10MB × 5) + StreamHandler, idempotent
- Retrofitted: mamba, tori, cuebanks (one-line swap), themis (added log import + 12 print→log conversions)
- Verified: all 4 runners import cleanly and write to `forge/logs/<name>/runner.log`
- Themis log will fill on next 6h cycle; previously 0 bytes for 4+ days

## Monitoring-mode state (2026-04-16 evening)
All active development complete. System is hands-off pending data accumulation.
- **Argus paper:** USDJPY (2 valid trades, PF 0.88), CADJPY watcher (10/50 signals), GBPUSD post-threshold-flip (was 0 trades, expecting ~5-15/week now)
- **Forge:** atlas batched, mamba/tori/cuebanks/themis logging fixed, gdx_gld/vix_revert/rebalance healthy
- **Apollo:** noise reduced (dedup + threshold raise), waiting to see if user engages with cleaner signals
- **Oracle:** wired into nightly cohort report, will run daily
- **Ares:** scheduled task, healthy
- **Open external dependencies:** Atlas Discord IP ban (lifts ~24h), Apollo execution decision (auto-trade or archive), GBPUSD/USDJPY/CADJPY accumulating data

## Restart cadence reference
- IBKR maintenance disconnect: every night 23:45 UTC → ~1 reconnect/pair/night
- In-process reconnect handled by `runner_unified.py:4472-4498` (200 retries, 10s→120s backoff)
- DESIGN DOCTRINE at `runner_unified.py:4478-4484` (2026-03-25): trade-scoped tainting is intentional. Don't loosen without author sign-off.
- Tainted trades = restarts where pair has open position (rare)

## Forge Family Detail
- **Atlas:** RSS → event classification → regime tracking → cascade detection
- **Themis:** DB-driven congressional trade ingestion → signal scoring
- **Mamba:** NQ=F + YM=F dual-instrument, 5m bias / 1m structure, max 2 trades/day, NY open only
- **Tori:** PL/CL/GC/YM 4H trendlines, 3 setups (bounce/break/retest)
- **GDX_GLD:** 2-leg correlation pairs spread
- Rulebooks live in each `forge/<name>/*_RULEBOOK.md`

## Recently Modified (last 7 days)
- `argus_flow/runner_unified.py`, `promotion_gate_v2.py`, `weekly_pair_onboarding.py`
- `apollo/runner.py` + ops scripts (Apr 15)
- `ares/runner.py` (Apr 15)
- `forge/mamba/runner.py` (Apr 15)
- `forge/atlas/cascade/templates.py` (Apr 13)

## Key Operational Files
- Fleet status: `argus_flow/logs/fleet_status.json` (auto-refreshed)
- Deployment registry: `argus_flow/logs/deployment_registry.json`
- Kill discipline: `argus_flow/logs/kill_discipline_report.json`
- Promotion gates: `argus_flow/logs/promotion_gate_report.json`
- Argus runner log: `argus_flow/logs/runner_unified.log`
- Per-Forge heartbeats: `forge/logs/<name>/heartbeat.json`

## Build/Deployment Status (vs old plan)
- Phase A (build Ares + Hermes): **DONE**
- Phase B (backtest those): **partially** — running signal-only
- Phase C (Argus governor → GATE): **BLOCKED** — only 32 trades on validated pairs, need 30+ per kill discipline
- Phase D (Titan AI overlay + execution): **NOT DONE** — still scanner-only
- Phase E (Ares + Hermes execution): **NOT DONE** — signal-only
- Phase F (Oracle): pivoted to Polymarket
- **FUND:** blocked — 0 real configs across all systems

## Portfolio Risk Rules (unchanged)
- Max 2% portfolio risk per trade across ALL systems
- Max 6% open risk simultaneously
- Daily fleet loss -3% → 24hr halt
