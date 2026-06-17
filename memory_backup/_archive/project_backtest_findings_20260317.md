---
name: Backtest Findings 2026-03-17/18 — Fee-corrected sweep results
description: 34-run initial sweep + 333-run systematic sweep. Fee-corrected results: ALL TP=3-5% configs unprofitable at 60bps. Phase 2 sweep queued with TP=6-12%.
type: project
---

## Backtest Marathon (2026-03-17) — 34 runs across ETH/BTC/SOL

### Winning Config (deployed to live)
- CONFLUENCE_MIN_SCORE=88, ML_GOVERNOR_THRESHOLD=0.38, ML_GOVERNOR_MODE=GATE
- TAKE_PROFIT_PCT=0.015, EXIT_BREAKEVEN_TRIGGER_PCT=0.003
- MAX_HOLD_SECONDS=3600 (60min), COMPOUND_SIZE_PCT=0.05 (5%)
- USE_STRUCTURE=false, USE_TRENDLINES=true, USE_ADAPTIVE_CONFLUENCE=true

### Key Results

| Test | PF | WR% | Trades | DD% |
|------|----|-----|--------|-----|
| **45d ETH no_struct+tl 5%** | **1.64** | 34.0% | 100 | 0.37% |
| 45d ETH no_struct no_tl 5% | 1.39 | 38.5% | 109 | 0.54% |
| 90d ETH no_struct+tl 5% | 1.01 | 27.6% | 152 | 0.90% |
| 90d ETH current config | 0.87 | 31.7% | 199 | 2.88% |
| 90d BTC loose (80/30) | 1.13 | 41.2% | 51 | 0.39% |
| 90d BTC optimal (88/38) | 1.02 | 39.3% | 28 | 0.24% |
| 90d SOL any config | 0.53-0.57 | 24-29% | -- | 3%+ |

### Critical Findings
1. **Structure module harmful**: Removing it = biggest improvement (PF +0.5). It adds false support/resistance signals that cause bad entries.
2. **Trendlines help when structure OFF**: PF 1.39 → 1.64 by keeping trendlines on.
3. **Adaptive confluence essential**: Removing it drops PF from 1.11 to 0.81.
4. **Governor essential**: OFF = PF 0.78 (worst). GATE 38-45 = PF 1.1+.
5. **60min hold optimal**: 45min close, 90min too long.
6. **5% compound safest**: 0.37% DD vs 1.24% (10%) vs 2.08% (15%).
7. **Stop-loss irrelevant**: SL 1.5% and 3% produced identical results — time-stop fires first.
8. **BTC needs loose params**: score80/gate30 works; ETH-tuned (88/38) too tight.
9. **SOL consistently unprofitable**: All configs PF <0.57. Deprioritize.
10. **90d validation thinner**: 45d edge (PF 1.64) dilutes to ~1.01 over 90d. Older data (Dec-Jan) was unfavorable.

### ML Governor Notes
- Retrain with mixed-config data FAILED (ROC-AUC 0.944 → 0.500). Restored original model.
- For proper retrain: extract features only from runs with SAME config, not mixed.
- Governor now evaluates EARLY (before gate chain) — stamps all entry signals, not just ones that pass upstream gates. Fix deployed 2026-03-17.

**Why:** Systematic parameter sweep to find optimal live config before real money.
**How to apply:** Structure OFF is non-negotiable. BTC may need per-coin config. SOL should be deprioritized or disabled. ML retrain needs single-config dataset approach.

## Sweep Phase 2 — Fee-Corrected Results (2026-03-18) — CRITICAL

### Cross-coin guard bug (now fixed)
- 78 of 136 labeled sweep runs had 0 trades due to correlation_guard.py reading live state files
- Fix: bypass guard when ARGUS_MODE=bt. Committed b3f5a66.

### Phase 1 sweep (TP=3-5%, 60bps fees) — ALL FAILED
- 60 clean post-fix ETH runs: **0 profitable** (PF < 0.5)
- Best: PF=0.478 (tp4_sl2_s88_g38_h10800)
- WR stuck at 20-27% across all tested params
- Root cause: 60bps round-trip fee requires WR=60%+ to profit at TP=4%, which is impossible for momentum

### Fee math (60bps = 1.2% round-trip)
| Config | WR needed to break even |
|--------|--------------------------|
| TP=4%, SL=2% | 53% WR |
| TP=5%, SL=2% | 45% WR |
| TP=8%, SL=1% | 28% WR ← achievable |
| TP=10%, SL=1% | 22% WR ← achievable |

### Phase 2 sweep (2026-03-18) — QUEUED
- 144 configs: TP=6/8/10/12%, SL=1/1.5/2%, Hold=2h/4h/8h, Score=85/88, FEE_BPS=60
- ETH + BTC, all using 90d candle data
- Labels: sweep_{COIN}_tp{N}_sl{N0}_s{N}_g38_h{N}

### $500/coin viability
- $500 is ENOUGH (5% compound = $25/trade, $0.30 fees/trade = manageable)
- Strategy profitability is the blocker, not capital size
- Do NOT go live until Phase 2 sweep finds PF>1.0 config with real fees
