---
name: Split large backtest queues between PC1 and PC2
description: When queuing large backtest batches (>50 runs), always split load between PC1 and PC2
type: feedback
---

Always split large backtest queues between PC1 and PC2 when the batch is large (>50 jobs).

**Why:** PC1 runs live runners 24/7 and dev work. Running 100+ backtests on PC1 alone takes 4+ hours and competes with live runners for resources. PC2 is available specifically for offloaded backtest work.

**How to apply:**
1. Generate the full queue as usual into `ops/backtest_queue.jsonl`
2. Split 50/50: first half stays in `ops/backtest_queue.jsonl` (for PC1), second half goes to `ops/backtest_queue_pc2.jsonl`
3. Commit and push both files
4. On PC2: `git reset --hard origin/phase6-hardening`, then `Copy-Item ops/backtest_queue_pc2.jsonl ops/backtest_queue.jsonl -Force`
5. Launch queue on PC1: `Start-Process powershell -ArgumentList '... .\ops\run_queue.ps1 ...'`
6. Launch queue on PC2 via SSH: `ssh "ksmith2322@yahoo.com@192.168.1.98" "powershell ... Start-Process powershell ... run_queue.ps1 ..."`

**Rule of thumb:** Any sweep >50 configs = split between machines.
