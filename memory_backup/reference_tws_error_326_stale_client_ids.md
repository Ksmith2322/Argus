---
name: TWS Error 326 — stale client_id slots after runner crashes
description: Root cause for mamba/cuebanks/gdx_gld "connect failed" loops and a major contributor to the gdx_gld silent-death pattern. When a runner dies without graceful disconnect, TWS holds the client_id slot, rejecting reconnects with Error 326. Fix is TWS reset.
type: reference
originSessionId: ca6e24c7-7756-4b66-a0df-d339b1453b20
---
# TWS Error 326 — stale client_id slots

## Symptom

Specific runners log persistent connect failures while others connect fine:
```
[WARNING] mamba: IBKR connect failed: connect failed: TimeoutError: TimeoutError() (host=127.0.0.1 port=7497 client_id=114)
```

Or in a stderr capture:
```
Error 326, reqId -1: Unable to connect as the client id is already in use. Retry with a unique client id.
Peer closed connection. clientId X already in use?
API connection failed: TimeoutError()
```

The TimeoutError-with-empty-message is misleading — the real cause is TWS Error 326 during the connect handshake. Before 2026-05-08, `helio.ibkr_execution.connect()` swallowed the exception type and only showed `connect failed: connect failed:` (empty inner). Diagnostic logging now includes `type(exc).__name__` + `repr(exc)` so the cause is visible.

## Root cause

When a runner dies without calling `ib.disconnect()` cleanly (Force-kill, segfault, lock-acquisition crash, parent process died), TWS holds the API client_id slot open. Subsequent connection attempts with that same client_id get rejected with Error 326. The reject masquerades as a TimeoutError because ib_insync waits for a handshake response that never comes.

## Strategies affected (2026-05-07/08)

| Strategy | client_id |
|---|---|
| gdx_gld_runner | 101 |
| gld_pm_long | 102 |
| jpy_pm_short | 103 |
| nq_overnight | 104 |
| spy_mean_rev | 105 |
| vix_intraday | 106 |
| nq_london_close | 107 |
| aud_asian_breakout | 108 |
| multi_orb | 109 (KILLED) |
| fomc_drift | 110 |
| tom_international | 111 |
| wick_gbpusd | 112 |
| mamba | 114 |
| tori | 115 |
| cuebanks | 116 |
| spy_trend_follower | 118 |

The 3 confirmed-affected on 2026-05-08: **101 (gdx_gld), 114 (mamba), 116 (cuebanks)**. Others connect normally because their slots aren't stuck.

## Compound effect on gdx_gld silent-death

The 2026-05-07 lock-bug fix (`forge/gdx_gld_runner.py:449`, catch SystemError on Windows os.kill) lets gdx_gld _start up_ cleanly. But if it then dies for any reason, the next start hits Error 326 on its old client_id and silently fails to connect. So the silent-death pattern has **two compounding causes** now identified:
1. Stale lock file (Windows os.kill SystemError) — fixed in code
2. Stale TWS client_id slot — requires TWS reset

Together they made gdx_gld appear permanently broken.

## Fixes

### Operator-side (immediate)
**Restart TWS.** That clears all API client_id slots. Runners auto-reconnect cleanly on next cycle.

In TWS: File → Exit → relaunch. ~30 seconds. All runners stay running through the TWS restart and will reconnect on their next cycle (1-15 minutes depending on runner).

### Code-side (deferred work)
1. **Fallback client_id rotation**: on Error 326, retry with `client_id + 20`. Implement in `helio/ibkr_execution.connect()`. Risk: needs careful testing so we don't accidentally collide with other runners.
2. **Graceful shutdown handlers**: SIGTERM/SIGINT handlers on each runner that call `ib.disconnect()` before exit. Force-kills (Stop-Process -Force) bypass these but cooperative shutdowns would release slots cleanly.
3. **Pre-connect probe**: before connect, check if a different process is using our client_id (would require IBKR API introspection — not trivial).

## Detection

After 2026-05-08 logging fix, search for "Error 326" in any forge runner stderr or `IBKR connect failed: TimeoutError` in runner.log.

Quick check across the fleet:
```powershell
Select-String -Path "C:\Argus\repo\forge\logs\*\runner.log" -Pattern "TimeoutError.*client_id" | Select-Object -Last 20
```

## When to apply this memory

- Whenever a runner shows persistent `connect failed` with an empty inner exception
- Whenever the silent-death pattern recurs on gdx_gld (or any other runner)
- After any session that did `Stop-Process -Force` on runners — the killed runners may be leaving stuck slots
- Before deciding to KILL a strategy for "no trades" — verify it's not just a stuck TWS slot
