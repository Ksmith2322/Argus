---
name: restart
description: Restart a specific strategy runner. Usage /restart argus, /restart helio, /restart apollo, /restart hermes, /restart all
allowed-tools: Bash
argument-hint: '[strategy]'
disable-model-invocation: true
---

Restart the $ARGUMENTS strategy runner. Valid targets: argus, helio, apollo, hermes, futures, all

IMPORTANT: Confirm with the user before restarting. Check for open positions first.

1. **Check for open positions** on the target strategy before killing
2. **Kill the target process**:
   - argus: processes matching 'runner_unified'
   - helio: processes matching 'helio.runner' (not apollo/hermes)
   - apollo: processes matching 'runner_apollo'
   - hermes: processes matching 'runner_hermes'
   - futures: the argus runner with --client-id 2
   - all: everything
3. **Wait 3 seconds** for clean shutdown
4. **Relaunch** using the appropriate command:
   - argus FX: `python -m argus_flow.runner_unified --configs argus_flow/configs/{eurusd,gbpusd,eurjpy,gbpjpy,usdjpy,audusd,cadjpy,audjpy}_*_paper_v1.json`
   - argus futures: `python -m argus_flow.runner_unified --client-id 2 --configs argus_flow/configs/{mnq,mes,m2k,mgc,mym,nkd}_*_paper_v1.json`
   - helio: `python -m helio.runner`
   - apollo: `python -m helio.runner_apollo`
   - hermes: `python -m helio.runner_hermes`
5. **Verify** the process is running and heartbeat is fresh
