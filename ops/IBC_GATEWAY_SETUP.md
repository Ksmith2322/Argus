# IB Gateway Migration (via IBC) — Unattended Production Path

**Goal:** Move the production runners from TWS (port 7497) to IB Gateway
(port 4002 paper / 4001 live). Keep TWS installed for manual emergency
intervention but stop running it 24/7.

**Why:** 10× less memory, ~5× faster restart, no UI to crash, designed
for headless algo trading. Power-cut recovery goes from minutes to
seconds. See decision context in `docs/AUDIT_2026_05_25_PART2/SYNTHESIS.md`.

**Time estimate:** ~30 min operator work + ~15 min verification.

## Operator actions

### Step 1 — Download + install IB Gateway

Download from <https://www.interactivebrokers.com/en/index.php?f=16457>
("Stable" channel, latest). Install with default options. Default install
path: `C:\Jts\ibgateway\<version>\ibgateway.exe`.

Verify:

```powershell
Get-ChildItem C:\Jts\ibgateway\ -Directory | Select-Object Name, LastWriteTime
```

You should see one or more version directories (e.g. `1030.2t`).

### Step 2 — Copy IBC config for Gateway

IBC supports both TWS and Gateway via the same `config.ini` with one
field flipped. The simplest path is a parallel config file.

```powershell
Copy-Item C:\IBC\config.ini C:\IBC\config_gateway.ini
```

Then edit `C:\IBC\config_gateway.ini`:

```ini
# Set Gateway mode (was: TWS)
FIX=no
IbDir=C:\Jts\ibgateway\<VERSION>          # match your install path

# Set Gateway paper-mode port (was: 7497)
OverrideTwsApiPort=4002

# (keep IbLoginId + IbPassword the same — same paper account)
# (keep TradingMode=paper)
# (keep AcceptIncomingConnectionAction=accept)
# (keep AutoRestartTime=23:55)
```

The TWS config (`C:\IBC\config.ini`) stays as-is; you'll keep it for
manual TWS launches when you need the UI.

### Step 3 — First-run Gateway launch

The TWS Gateway runner script will be created in **Step 5** below; for
the first manual test, run IBC against Gateway directly:

```powershell
# Stop any running TWS (port 7497) first to avoid login collision
Get-Process tws -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process ibgateway -ErrorAction SilentlyContinue | Stop-Process -Force

# Launch Gateway via IBC with the new config
cd C:\IBC
java -cp "C:\IBC\IBC.jar;C:\Jts\ibgateway\<VERSION>\jars\*" `
     ibcalpha.ibc.IbcGateway C:\IBC\config_gateway.ini
```

What should happen in ~10 seconds:

- Gateway window opens (small status panel, NOT the full TWS UI)
- Login + disclaimer accepted automatically (same credentials as TWS)
- Port 4002 starts listening

Verify:

```powershell
Get-NetTCPConnection -LocalPort 4002 -State Listen
```

If this lists a connection, Gateway is up.

### Step 4 — Confirm Helio can connect

Without changing any production code yet, verify a basic Python connect:

```powershell
cd C:\Argus\repo
$env:IBKR_PORT='4002'
C:\Argus\.venv\Scripts\python.exe -c "from helio import ibkr_execution as ibkr; ib = ibkr.connect(client_id=999); print('connected:', ib.isConnected()); ib.disconnect()"
```

Expected: `connected: True`. If False, check the Gateway window for an
error dialog (usually a stuck disclaimer or a wrong API setting).

### Step 5 — Confirm with me

Once Step 4 returns `connected: True`, tell me. I will then:

1. Create `ops/start_gateway_via_ibc.ps1` (modeled on
   `ops/start_tws_via_ibc.ps1` but for Gateway + port 4002)
2. Update the runner-launch scripts in `ops/` to read `IBKR_PORT`
   from env (default 4002 once we cut over) instead of hardcoding 7497
3. Do a one-runner cutover test on `forge_tail_hedge` (smallest live
   strategy, lowest blast radius)
4. If clean, cut over the rest in one batch + commit

I deliberately won't make code changes until you've confirmed Gateway
works on your machine — so the currently-running argus pairs (waiting
to reconnect to TWS port 7497) don't get stranded between worlds.

## After cutover — daily operations

| Scenario | Action | Where |
|---|---|---|
| Normal day | Nothing | IBC auto-restarts Gateway at 23:55 |
| Gateway hangs | Auto-reconnect via IBC | `C:\IBC\Logs\` |
| Manual flatten / inspect a position | Launch TWS via existing `ops/start_tws_via_ibc.ps1` | Port 7497, manual UI |
| Machine reboot | Wait 1 min after logon | Scheduled task (see Step 6 below) |
| Update Gateway version | Update via Gateway itself + update `IbDir=` in `config_gateway.ini` | One config edit |

### Step 6 — Optional: nightly scheduled task

After Step 5 ships, register a Windows scheduled task for boot-recovery:

```powershell
schtasks /Create /TN "IBC Gateway Auto-Restart" `
  /TR "powershell.exe -File C:\Argus\repo\ops\start_gateway_via_ibc.ps1 -Hidden" `
  /SC ONLOGON /F
```

This brings Gateway up automatically after Windows reboots.

## Reverting if something goes wrong

The TWS-based path is unchanged and fully reversible:

```powershell
# 1. Stop Gateway + IBC
Get-Process ibgateway, java -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. Restart TWS via the existing path
C:\Argus\repo\ops\start_tws_via_ibc.ps1

# 3. Flip the IBKR_PORT env var back
$env:IBKR_PORT='7497'

# 4. Restart any runners that had been pointing at 4002
# (the per-strategy restart commands are in TUESDAY_20260526_OPEN_CHECKLIST.md)
```

If we've committed the IBKR_PORT default change in Step 5, the revert
also needs a code revert — that commit will be tagged `gateway-cutover`
for clean rollback.

## Why TWS stays installed

- **Emergency manual flatten:** if a position gets stuck and the bot
  is fighting itself (5/19 CADJPY cascade pattern), the TWS UI is the
  fastest way to *manually* close it. Gateway has no UI.
- **Spot-check trades visually:** when you want to see what the bot is
  doing in real time, TWS is the human-facing tool.
- **IBKR data downloads:** the `download_ibkr_bars.py` tool prefers
  TWS-style historical data requests; keep TWS available for ad-hoc
  data pulls.

Recommended pattern after migration:
- **Production (24/7):** Gateway via IBC, port 4002
- **Manual sessions (ad-hoc):** start TWS via existing script when
  needed, stop it when done. Different client_id namespace, no
  conflict with Gateway.

## Decision points still open (operator)

1. **Live Gateway port 4001 — do we want to set up the live-money path
   now too?** Probably not; defer until first real-money deployment
   approaches. Adding 4001 later is one config-file change.
2. **Schedule daily IBC log rotation?** IBC writes one log per launch;
   over months they accumulate. A 30-day delete schedule is reasonable.
   Defer until annoyance materializes.
