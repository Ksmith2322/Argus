---
name: Multi-Coin Parallel Architecture
description: 3-coin parallel runner setup (ETH, BTC, SOL) with per-coin log dirs and shared .env base config
type: project
---

Multi-coin trading via parallel runner instances (Option A) confirmed 2026-03-15.

**Why:** User wants to trade ETH, BTC, SOL simultaneously. Single-symbol architecture means one runner per coin. Parallel instances require zero code changes — just env var overrides.

**How to apply:**
- Launcher: `.\ops\launch_multi.ps1` — starts 3 PowerShell windows, one per coin
- Each runner gets: `PRODUCT_ID=<COIN>-USD` + `ARGUS_LOG_DIR=ops/logs/<coin>/` as env overrides
- State files auto-separate by symbol: `state/runtime_state_<COIN>_USD.json`
- Per-coin .env reference files: `.env.eth`, `.env.btc`, `.env.sol` (documentation, not loaded directly)
- Candle data: `data/eth_usd_1m.csv`, `data/btc_usd_1m.csv`, `data/sol_usd_1m.csv` (all 43K+ rows)
- Capital: $500 per coin, $1,500 total starting balance

**Automation domino chain (daily @ 01:30 AM):**
1. `ArgusRefreshCandles` task → `ops/refresh_candles.ps1 -RunBacktest`
2. Downloads all 3 coins (45 days history) from Coinbase public API
3. Validates candle data via `ops/validate_candles.py`
4. Git commits + pushes updated data
5. Chains into `ops/run_backtest.ps1 -SingleRun` (ETH backtest)
6. Old `Argus_Nightly_Backtest` task DISABLED (replaced by chain)

**Future (Option B):** Portfolio Runner with shared cash pool, per-symbol allocation caps, cross-symbol risk. Needs `PortfolioRunner` layer above existing single-symbol bot. Not built yet.
