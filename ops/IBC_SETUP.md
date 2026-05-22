# IBC Setup — TWS Auto-Restart

**One-time setup, ~10 minutes operator work. After this, TWS babysitting goes away.**

IBC (Interactive Brokers Controller) wraps TWS to give us:

- Auto-login (no manual disclaimer accept after restart)
- Daily auto-restart at 23:55 local (handles IBKR's nightly server reset)
- Reconnect on connection drop
- Headless capability (TWS runs minimized)

Source: <https://github.com/IbcAlpha/IBC> (open-source, mature, in production at many small algo shops)

## Status

- IBC 3.23.0 downloaded + extracted at `C:\IBC`
- `config.ini` pre-configured for paper mode, port 7497, auto-restart at 23:55
- **Two manual steps remain** (you):
  1. Edit `C:\IBC\config.ini` and replace the credential placeholders
  2. Optionally register the nightly Windows scheduled task

## Step 1 — Add your paper-account credentials

Open `C:\IBC\config.ini` in any text editor. Find these two lines:

```ini
IbLoginId=PLACEHOLDER_YOUR_PAPER_USERNAME
IbPassword=PLACEHOLDER_YOUR_PAPER_PASSWORD
```

Replace with your IBKR paper-account credentials (the same ones you type
into the TWS login dialog). Save.

**Security note:** `config.ini` lives outside the git repo (under
`C:\IBC`, not `C:\Argus\repo`). It's never committed. File permissions
should be operator-only — verify with:

```powershell
icacls "C:\IBC\config.ini"
```

If anyone other than yourself has read access, restrict it:

```powershell
icacls "C:\IBC\config.ini" /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

## Step 2 — First-run test (with current TWS stopped)

The current TWS process must NOT be running when IBC starts (IBC owns
the lifecycle). The cleanest cutover:

```powershell
# 1. Close current TWS via the GUI (File -> Exit)
#    OR force-stop:
Get-Process tws -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. Launch via IBC
cd C:\Argus\repo
.\ops\start_tws_via_ibc.ps1
```

What should happen:

- IBC window opens (minimized to taskbar; minimal GUI noise)
- Login + disclaimer accepted automatically
- TWS comes up, port 7497 listens
- The script reports "TWS is up" within 60 seconds

If it stalls, look at the latest log under `C:\IBC\Logs\ibc_*.log`. Most
common first-run issues:

- **"Login failed"** — credentials in config.ini are wrong
- **"Existing session detected"** — old TWS not fully closed; wait 30s
  and retry, or check Task Manager for stray `tws.exe` or `java.exe`
- **"Disclaimer dialog stuck"** — `AcceptIncomingConnectionAction=accept`
  in config.ini needs to be set (already configured by setup)

## Step 3 — Nightly Windows scheduled task (optional, recommended)

IBC already handles the in-day reconnects and the daily 23:55 restart.
The Windows scheduled task is for **machine-reboot recovery** — if PC1
reboots overnight, the task brings TWS back without you logging in.

IBC ships a task template at `C:\IBC\Start TWS (autorestart).xml`. To
register it:

```powershell
# Import the IBC task template (creates an at-logon trigger)
schtasks /Create /TN "IBC TWS Auto-Restart" /XML "C:\IBC\Start TWS (autorestart).xml" /F
```

After registration:

- The task triggers on user logon
- It runs `C:\IBC\StartTWS.bat` automatically
- TWS comes up unattended after Windows boots

To check status:

```powershell
schtasks /Query /TN "IBC TWS Auto-Restart"
```

To remove it later:

```powershell
schtasks /Delete /TN "IBC TWS Auto-Restart" /F
```

## Daily operations after setup

| When | What | How |
|---|---|---|
| Normal day | Nothing | IBC auto-restarts TWS at 23:55 |
| TWS hangs mid-day | Nothing (IBC reconnects on its own) | Watch `C:\IBC\Logs\` for the reconnect entry |
| Machine reboot | Wait 1 min after logon | Scheduled task launches IBC; preflight passes |
| Update TWS version | Update via TWS itself (Help → Software Update) | IBC tolerates TWS minor updates without config change |
| Change paper password | Edit `config.ini` IbPassword field, restart IBC | Stop IBC then re-run `start_tws_via_ibc.ps1` |

## Reverting to manual TWS (if needed)

If IBC misbehaves, you can always go back to bare TWS:

```powershell
# 1. Stop IBC + child TWS
Get-Process tws,java -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. Remove the scheduled task (if you registered it)
schtasks /Delete /TN "IBC TWS Auto-Restart" /F

# 3. Launch TWS manually
C:\Jts\tws.exe
```

Nothing in the argus runner depends on IBC specifically — it just needs
TWS reachable on port 7497. IBC is a wrapper that goes away cleanly.

## What's in C:\IBC

| File | Purpose |
|---|---|
| `IBC.jar` | The IBC Java application |
| `config.ini` | All IBC settings (credentials live here) |
| `config.ini.original.bak` | Pristine factory config (for reference) |
| `StartTWS.bat` | The launcher IBC uses to start TWS |
| `Start TWS (autorestart).xml` | Scheduled task template |
| `Stop.bat` / `Restart.bat` / `ReconnectAccount.bat` | Manual control scripts |
| `userguide.pdf` | Full IBC documentation (advanced configuration) |
| `Logs/` | IBC log files (rotated per launch) |

## Done. Test by running `start_tws_via_ibc.ps1` after editing your credentials in.
