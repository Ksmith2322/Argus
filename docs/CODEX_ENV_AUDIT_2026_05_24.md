# Codex Environment Audit - 2026-05-24

Focus: offense/defense trading posture after killing dead or poor-performing strategies.

## Executive Verdict

The strategic roster is now much cleaner: 2 ACTIVE, 28 KILLED, 2 PENDING_OPT_IN, 0 LIMBO. The kill registry and allocation file agree, and broker orphan audit is clean.

Before this audit, runtime did not match roster intent:

- `forge_xs_momentum` was ACTIVE but not actually running.
- KILLED runners were still alive: `argus_flow.runner_unified`, `forge_nq_overnight`, `forge_pead`, and `forge_spy_trend_follower`.
- KILLED state phantoms remained in `forge_nq_overnight` and `forge_spy_mean_rev`.
- The active `xs_momentum` loop could sleep for ~24 hours without refreshing heartbeat.

After this audit:

- Only active strategy runners are alive: `forge_gld_pm_long.runner` and `forge_xs_momentum.runner`.
- Dashboard remains alive.
- Killed runner processes were stopped.
- `xs_momentum --loop` was started.
- Killed-state phantoms were cleared.
- `ops/start_post_reset_runners.ps1` now launches only the active roster and refuses blocked/killed/pending runners.
- `xs_momentum` now refreshes heartbeat while sleeping.

## Active Roster

### Offense

- `forge_xs_momentum`
- Allocation: `1.0`
- Role: tactical factor beta / cross-sectional momentum.
- Runtime: running in `--loop`.
- Current blocker: market data download is failing through `yfinance`, so `--check` returns empty ranking/picks.

### Defense

- `forge_gld_pm_long`
- Allocation: `0.5`
- Role: metals diversifier / defensive sleeve.
- Runtime: running in `--loop`.
- Current blocker: preflight says disciplined gate fails at realistic slippage and live evidence is still zero in the clean epoch.

## Killed Strategy Enforcement

Allocation factors are aligned with the kill registry. `ops.audit.run_roster_state` reports:

- ACTIVE: 2
- KILLED: 28
- PENDING_OPT_IN: 2
- LIMBO: 0
- ABANDONED: 0

Runtime before cleanup included killed strategies. I stopped:

- `argus_flow.runner_unified`
- `forge.nq_overnight.runner`
- `forge.pead.runner`
- `forge.spy_trend_follower.runner`

Runtime after cleanup:

- `forge.gld_pm_long.runner --loop`
- `forge.xs_momentum.runner --loop`
- `ops/dashboard.py --port 8080`

Note: on this Windows venv, each runner appears as a venv parent process plus a system-Python child process. That paired-process shape is expected.

## Phantoms And Orphans

Broker orphan audit:

- `orphans: []`
- `ok: []`

Killed-state phantom audit initially found:

- `forge_nq_overnight`: stale signal-only open trade from `2026-05-22 20:00:00+00:00`
- `forge_spy_mean_rev`: stale signal-only open trade from `2026-04-30 19:55:00+00:00`

Both `open_trade` fields were cleared to `null`. Re-run result:

- `n_phantoms_found: 0`

## Real-Money Preflight

Real-money preflight is correctly BLOCKED for both active strategies.

`forge_gld_pm_long`:

- GREEN: 6
- YELLOW: 2
- RED: 4
- Main red reasons: disciplined gate fail, zero live evidence, no live trade source, real-money allowlist disabled.

`forge_xs_momentum`:

- GREEN: 7
- YELLOW: 2
- RED: 3
- Main red reasons: zero live evidence, no live trade source, real-money allowlist disabled.

This is good defensive behavior. The bot is not ready for real-money promotion. It is ready for clean paper evidence collection after the data-feed issue is resolved.

## Offense Blocker: `xs_momentum` Data Feed

Direct command:

```powershell
C:\Argus\.venv\Scripts\python.exe -m forge.xs_momentum.runner --check
```

returned:

```json
{
  "as_of": null,
  "ranking": [],
  "picks": []
}
```

with failed downloads for:

- `SPY`
- `DIA`
- `TLT`
- `QQQ`
- `IWM`
- `GLD`
- `EEM`
- `EFA`

Error shape:

```text
TypeError("'NoneType' object is not subscriptable")
```

This is the most important remaining offense blocker. If this persists at the next rebalance wake, the main active offense sleeve will not produce picks.

Recommended next fix:

- Add a data-feed preflight specifically for active strategies.
- Cache last successful ETF close panel.
- Fail loudly if `xs_momentum` ranking is empty.
- Prefer a broker/paid data fallback over yfinance for production paper/live trading.

## Launcher Fix

`ops/start_post_reset_runners.ps1` was stale. It still launched:

- dormant Argus FX
- killed `nq_overnight`
- killed `pead`
- killed `spy_trend_follower`

It now launches only:

- `forge.gld_pm_long.runner --loop`
- `forge.xs_momentum.runner --loop`

and refuses to launch if blocked modules are alive, including killed and pending-opt-in runners.

Dry run result:

- skipped both active runners because both were already running.
- no blocked runner was alive.

## Evidence State

Clean epoch:

- `current_epoch_id`: `post_reset_20260522`
- `is_clean`: true

Canonical fills:

- `argus_flow/logs/canonical_fills.jsonl` currently has `0` rows.

This is expected after reset, but it means there is no clean post-reset evidence yet. Any promotion or capital confidence must wait for actual clean ENTRY/EXIT lifecycle rows.

## Data-Feed Follow-Up Applied

`xs_momentum` no longer depends on a single direct `yf.download()` path. The runner now:

- handles both common yfinance MultiIndex column shapes,
- falls back to the vetted CSV cache in `helio/data_yfinance`,
- raises loudly if no ticker history is available,
- returns non-zero from `--check` if ranking is empty.

Verification:

```powershell
C:\Argus\.venv\Scripts\python.exe -m forge.xs_momentum.runner --check
```

now returns current picks from cache:

- `GLD`
- `EEM`

The latest cached bar is `2026-05-21`, so the sleeve is operational again but should still refresh cache/data before the next rebalance.

## Remaining Red Items

1. yfinance live downloads are still failing, but `xs_momentum` now falls back to cached closes and produces picks.
2. `real_money_preflight` blocks both active strategies due missing live evidence and disabled allowlist.
3. `gld_pm_long` remains questionable at realistic slippage.
4. Discord alert send failed with local connection refused during health check.
5. `SESSION_2026_05_24.md` is untracked and left untouched.

## Tests / Verification

Passed:

```powershell
C:\Argus\.venv\Scripts\python.exe -m pytest `
  argus_flow\tests\test_static_safety_invariants.py `
  argus_flow\tests\test_guards_fail_closed.py -q
```

Result:

```text
8 passed
```

AST parse passed for:

- `forge/xs_momentum/runner.py`

`test_xs_momentum_runner.py` could not complete because pytest hit Windows temp-directory ACL errors during fixture setup/cleanup, even with explicit `--basetemp`. Six tests passed before the ACL error.

## Bottom Line

Defense is materially better after this audit: killed strategies are stopped, phantoms are cleared, orphan audit is clean, and the launcher now refuses dead runners.

Offense is usable again after the cache fallback patch, but live yfinance remains unreliable. The next highest-value hardening step is replacing yfinance as the production source of truth for active ETF sleeves or adding a scheduled cache refresh with alerting.
