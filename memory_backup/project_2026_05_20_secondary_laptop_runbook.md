---
name: Secondary-laptop (PC2) deployment runbook
description: How to stand up laptop2 as a paper-only data-collection node — separate IBKR paper account, separate TWS install, dedicated clientId range, no shared filesystem mount with PC1.
type: reference
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# Why PC2 exists

Activity-multiplication for bug-finding. PC1 runs "production" paper —
argus FX (until 5/31 sunset) then the 5 survivors. PC2 runs the wild
side: stress_injector + threshold-multiplied parameter variations +
longer backtests + chaos modes. Independence is the point — a PC2 crash
must not affect PC1, and vice versa. Results flow back to PC1 via git
push/pull, not a shared mount.

This is purely paper for the foreseeable future. No real-money flow
ever runs on PC2. Hard rule.

# One-time setup

## 1. IBKR paper account

Login at https://www.interactivebrokers.com/portal → create a SECOND
paper account (free; no funding needed). Note the new DU-prefix
account number. PC1 uses DUP472829; PC2 gets its own.

## 2. TWS install on PC2

Download Trader Workstation (paper). On first launch:
- Configure → API → Settings → Enable ActiveX and Socket Clients = ON
- Configure → API → Settings → Socket port = **7497** (paper)
- Configure → API → Settings → Master API client ID = empty
- Configure → API → Settings → Read-Only API = OFF
- Configure → API → Precautions → Bypass Order Precautions for API Orders = ON

DO NOT install live TWS. PC2 only ever sees paper.

## 3. Python env + repo clone

```powershell
# On PC2
cd C:\
git clone <repo-url> Argus
cd Argus
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Repo is via git, NOT a shared mount. Update via `git pull` whenever
PC1 ships fixes. Local PC2-only changes stay on a `pc2-experiments`
branch — never merged to main.

## 4. ClientId allocation (do not collide with PC1)

PC1 reserves:
- 1, 12, 51, 53 — argus
- 60, 70, 80, 90 — Greek family
- 101-117 — forge strategies
- 186 — flatten_eod
- 250-299 — stress harness (one of these is always running on PC1
  if stress mode is on)

PC2 reserves: **300-399**. Hard rule. Each runner on PC2 picks a
unique ID in this range. Currently used:
- 300 — stress_injector PC2 instance
- 301-310 — reserved for paper-only parameter sweep runners
- 311+ — ad-hoc

When launching anything on PC2, set the clientId explicitly. Don't rely
on defaults (defaults match PC1 production allocation).

## 5. Environment

In `pc2-experiments` branch, add a `.env.pc2.ps1` that sets:

```powershell
$env:IBKR_PORT = "7497"
$env:STRESS_INJECT_OK = "1"   # only if running stress harness
$env:PC2_NODE = "1"             # marker for any log filtering
$env:REAL_MONEY_ENABLED = ""    # explicitly empty
```

Run it before launching any process on PC2. Hard rule.

# What runs where

| Component | PC1 (prod) | PC2 (data-collection) |
|---|---|---|
| argus FX runner | YES (until 5/31 sunset) | NO |
| Forge survivors (5) | YES (post 5/31) | NO |
| stress_injector | optional, brief | YES, continuous |
| Threshold-multiplied paramater variants | NO | YES |
| Backtest sweeps | only on demand | YES, anytime |
| Dashboard | YES (port 8080) | optional read-only |
| Heartbeat watchers | YES | YES (independent set) |
| TWS install | paper port 7497 | paper port 7497 |
| IBKR account | DUP472829 | (PC2's own DUxxx) |
| Filesystem mount | local only | local only |

The "no shared filesystem mount" rule is critical. If both PCs wrote
to the same canonical_fills.jsonl, OS-level file locking under Windows
would race and corrupt the JSONL. Separate copies, manual merge via
git, no exception.

# Operational flow

## Daily

PC2 runs stress_injector in loop mode against EURUSD/AUDUSD. Either
runs continuously or on a cron at off-hours.

```powershell
. .\.env.pc2.ps1
python -m ops.stress_injector --loop 50 --cooldown 90 --chaos cancel_during_fill
```

PC2 runs paper-stress-multiplied variants of strategies, with the
`paper_stress_multiplier` set to e.g. 0.5 on pc2 configs. PC1 keeps
the multiplier at 1.0.

```powershell
# PC2 copy of usdjpy_mtf_paper_v1.json, with paper_stress_multiplier
# set to 0.5 inside the mtf block, runs an aliased clientId 311.
# This generates 3-5× more fires than PC1 — useful for surfacing race
# conditions that only manifest under volume.
```

## End of day

```powershell
# On PC2
git add argus_flow/logs/stress_injector.jsonl
git add argus_flow/logs/unfilled_orders.jsonl
git commit -m "PC2 data collection $(Get-Date -Format yyyy-MM-dd)"
git push origin pc2-experiments
```

```powershell
# On PC1, pull and review
git fetch origin pc2-experiments
git checkout origin/pc2-experiments -- argus_flow/logs/stress_injector.jsonl
# Run analysis scripts on the merged log
```

## When a bug is found on PC2

1. Reproduce on PC2 first (rerun stress_injector with same chaos mode)
2. Capture full log JSONL + screenshot of broker order state
3. File on the PC1 main branch as a regression test
4. Fix on PC1 main, PC2 pulls via `git pull origin main`

Never fix on PC2 and push to main. PC2 is downstream of PC1.

# Failure scenarios

**PC2 crashes mid-cycle:** stress_injector cycles are self-contained.
Restart picks up clean. Orphan paper position on PC2 broker = flatten
via `python -m ops.flatten_eod_executor --force` on PC2.

**PC1 crashes:** PC2 unaffected. PC1 recovery is separate.

**Network blip between PC2 and IBKR:** stress_injector's
`disconnect_after_submit` chaos mode is the simulated version; real
network blip behaves identically. The injector logs RECONNECTED or
RECONNECT_FAILED. Operator reviews log + re-flattens any orphan.

**PC2 git push conflicts with main:** PC2 only has its own
pc2-experiments branch. Conflicts on logs/ are resolved by always
preferring PC2's local copy (PC2 owns its log file). Conflicts on
code = PC2 was modified incorrectly; revert local PC2 changes.

# Don't do

- Don't run helio/runner_apollo, runner_hermes, etc. on PC2 — those
  scanner runners write to shared `forge/logs/<strategy>/` paths and
  would race with PC1.
- Don't share `canonical_fills.jsonl` between PCs. Each PC writes its
  own copy. Cross-PC reconciliation is manual via the operator.
- Don't install live TWS on PC2 ever. Even with real_money_enabled=False
  the port mismatch (7496 vs 7497) is a foot-gun.
- Don't run PC1 and PC2 on the same IBKR paper account. Each PC must
  have its OWN paper account. Two clients on one account share
  positions and the chaos modes interfere with each other.

# Bootstrapping checklist

Before declaring PC2 ready for data collection:

- [ ] Second IBKR paper account created and DU number recorded
- [ ] TWS installed, port 7497 enabled, ActiveX clients enabled
- [ ] Repo cloned to C:\Argus, pc2-experiments branch checked out
- [ ] Python venv created, requirements installed
- [ ] `.env.pc2.ps1` exists and sets correct env vars
- [ ] All 4 stress_injector safety locks verified (`pytest argus_flow/tests/test_stress_injector.py`)
- [ ] One smoke cycle run successful: `python -m ops.stress_injector --single`
- [ ] PC1 and PC2 confirmed using different clientId ranges (no collisions)
- [ ] Log push/pull confirmed working: PC2 → git → PC1

When all checkboxes pass, PC2 is live for data collection.
