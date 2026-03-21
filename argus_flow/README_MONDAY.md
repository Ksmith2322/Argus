# Cascade Monday Execution Plan

## Pre-flight (already done)
- [x] Kraken API keys in `.env`
- [x] Connectivity test: 9/9 passed
- [x] $50 USD balance confirmed
- [x] All pipeline scripts built and tested

## Step 1 — Download BTC trades (14 days)

```bash
C:\Argus\.venv\Scripts\python.exe -m argus_flow.capture.ingest_kraken_trades --pair XBTUSD --days 14
```

Output: `argus_flow/data/kraken_btcusd_trades.csv`

**Expected**: ~500K-2M rows depending on activity. Progress logged every 10 batches.
Checkpoint saves every 50 batches — safe to interrupt and resume.

## Step 2 — Validate data

```bash
C:\Argus\.venv\Scripts\python.exe -m argus_flow.capture.validate_trades --trades argus_flow/data/kraken_btcusd_trades.csv
```

Check: no gaps, no nulls, monotonic timestamps, sane aggressor distribution.

## Step 3 — Run full pipeline (one command)

```bash
C:\Argus\.venv\Scripts\python.exe -m argus_flow.run_pipeline --trades argus_flow/data/kraken_btcusd_trades.csv
```

This chains: validate → build bars → baseline replay → 9-sweep → classify → delay simulation.

## Step 4 — Read the verdict

Check `argus_flow/replay_out/edge_classification.json`:

| Classification | Action |
|---|---|
| **DEAD** | Kill the idea. Move on. |
| **ILLUSION** | Overfit. Don't trust it. |
| **WEAK_BUT_REAL** | Edge exists but thin. Investigate further. |
| **PROMISING** | Proceed to shadow mode. |
| **INSUFFICIENT_DATA** | Need more data. Extend capture. |

## Key files to inspect

- `argus_flow/replay_out/baseline/summary.json` — baseline metrics
- `argus_flow/replay_out/sweep_summary.json` — all 9 configs compared
- `argus_flow/replay_out/edge_classification.json` — automated verdict
- `argus_flow/replay_out/delay_summary.json` — does edge survive Kraken latency?

## Kill conditions (decide BEFORE looking at data)

- Post-cost expectancy <= 0 across most configs → **DEAD**
- False breakout rate > 65% → **DEAD**
- Median MAE > median MFE → **DEAD**
- Edge only positive in 1-2 configs → **ILLUSION**
- Edge dies with 1-3 bar delay → **won't survive Kraken execution**

## Friction assumptions

- Kraken taker fee: 0.40% (20bps per side)
- Slippage estimate: 5bps per side
- Total round-trip: ~50bps

## What NOT to do

- Do not optimize thresholds to make it work
- Do not re-run until results look good
- Do not overweight a few big winners
- Do not mix BTC and ETH in the same analysis