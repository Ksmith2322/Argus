---
name: how to change the WiFi auto-on/off schedule
description: Two scheduled tasks control overnight WiFi disable/re-enable. One-line schtasks command changes the time. Logon mode is Interactive only (no stored password needed).
type: reference
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## The two tasks

| Task name | Purpose | Default time |
|---|---|---|
| `ArgusWiFiDisable` | Turn WiFi OFF for the night | **8:00 PM** (changed from 9pm 2026-04-24) |
| `ArgusWiFiEnable` | Turn WiFi back ON in the morning | 7:00 AM |

Both fire daily, both run as user `ksmit` in `Interactive only` logon mode (no stored password — runs under the active user session).

Underlying script: `C:\Argus\repo\ops\wifi_control.py` with `disable` or `enable` arg.

## How to change the time

```powershell
schtasks /Change /TN '<TaskName>' /ST HH:MM
```

`/ST` is start time in 24-hour format. Example: change WiFi-off to 10pm:

```powershell
schtasks /Change /TN 'ArgusWiFiDisable' /ST 22:00
```

**Ignore the "Please enter the run as password" warning** that appears on Interactive-only tasks — the change actually took effect, and the task doesn't need a password to fire. Verify with the Query command below.

## How to verify the change took

```powershell
schtasks /Query /TN '<TaskName>' /FO LIST /V | Select-String -Pattern 'TaskName|Next Run|Status|Logon Mode|Last Run|Last Result'
```

Should show:
- `Next Run Time` = the new time
- `Status` = Ready
- `Logon Mode` = Interactive only
- `Last Result` = 0 (last fire was successful)

## When to use this

Whenever the user says something like:
- "change WiFi to <time>"
- "WiFi schedule needs to shift"
- "turn off WiFi earlier/later"
- Anything similar about the overnight WiFi automation

Confirm which task they mean (off vs on) if ambiguous, then run the one-line schtasks command.

## IMPORTANT: WiFi off does NOT affect TWS or trading

**The PC is hardwired (Ethernet).** When `ArgusWiFiDisable` fires:
- The Ethernet link stays up
- TWS keeps its connection to IBKR servers
- Runners → TWS (localhost) is unaffected
- TWS → IBKR (Ethernet uplink) is unaffected
- Cohort reports, scheduled tasks, and everything trading-related continues to work normally

The WiFi schedule is purely for non-trading concerns (notifications, power, mobile devices on the same network). **Do NOT recommend changing the WiFi schedule to "fix" trading-side issues** — they're independent.

I (Claude) made this exact mistake on 2026-04-25 by suggesting that WiFi-off at 8pm would disconnect TWS from IBKR before the 11pm cohort report. User correctly pointed out the Ethernet hardwiring. Don't repeat.
