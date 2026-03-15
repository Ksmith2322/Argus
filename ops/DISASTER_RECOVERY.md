# Argus Disaster Recovery Runbook

Last updated: 2026-03-14

## Prerequisites

- GitHub repo: `Ksmith2322/Argus`, branch `phase6-hardening`
- USB backup: `E:\Argus_Backup\repo\` (nightly mirror via ArgusUSBBackup@03:00)
- Python 3.x installed on system
- Git installed and configured

---

## Scenario A: PC1 Hard Drive Failure (Full Restore)

Restore from USB backup + GitHub onto a fresh drive.

1. Install Windows, Python 3.x, Git
2. Create directory structure:
   ```powershell
   mkdir C:\Argus\repo
   ```
3. Clone repo from GitHub:
   ```powershell
   cd C:\Argus
   git clone -b phase6-hardening https://github.com/Ksmith2322/Argus.git repo
   ```
4. Restore state and logs from USB backup:
   ```powershell
   robocopy "E:\Argus_Backup\repo\state" "C:\Argus\repo\state" /MIR
   robocopy "E:\Argus_Backup\repo\ops\logs" "C:\Argus\repo\ops\logs" /MIR
   ```
5. Recreate Python venv:
   ```powershell
   cd C:\Argus
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r repo\requirements.txt
   ```
   If no requirements.txt, install manually:
   ```powershell
   pip install fastapi uvicorn sse-starlette
   ```
6. Verify .env exists (should be in git):
   ```powershell
   Test-Path C:\Argus\repo\.env
   ```
7. Restore Task Scheduler jobs:
   ```powershell
   schtasks /create /tn "ArgusGitBackup" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\auto_git_backup.ps1" /sc daily /st 02:00 /f
   schtasks /create /tn "ArgusRefreshCandles" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\refresh_candles.ps1" /sc weekly /d SUN /st 03:00 /f
   schtasks /create /tn "ArgusUSBBackup" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\backup_to_usb.ps1" /sc daily /st 03:00 /f
   schtasks /create /tn "ArgusVerifyBackup" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\verify_backup.ps1" /sc weekly /d SUN /st 04:00 /f
   ```
8. Set up auto-start (copy shortcuts to Startup folder):
   ```powershell
   # Create shortcuts for runner + dashboard in:
   # C:\Users\<user>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup
   # Pointing to ops\autostart_runner.ps1 and ops\autostart_dashboard.ps1
   ```
9. Start live runner:
   ```powershell
   cd C:\Argus\repo
   C:\Argus\.venv\Scripts\python.exe runner_live.py
   ```
10. Verify recovery:
    ```powershell
    # Check runner picks up state from state\ directory
    # Check dashboard at http://localhost:8080
    # Check Discord webhook works:
    C:\Argus\.venv\Scripts\python.exe ops\notify.py --test "PC1 restored"
    ```

---

## Scenario B: Repo Corruption (Git Restore)

Code is broken but state/logs are fine.

1. Back up current state and logs:
   ```powershell
   Copy-Item -Recurse C:\Argus\repo\state C:\Argus\_state_backup
   Copy-Item -Recurse C:\Argus\repo\ops\logs C:\Argus\_logs_backup
   ```
2. Delete and re-clone repo:
   ```powershell
   Remove-Item -Recurse -Force C:\Argus\repo
   cd C:\Argus
   git clone -b phase6-hardening https://github.com/Ksmith2322/Argus.git repo
   ```
3. Restore state and logs:
   ```powershell
   robocopy C:\Argus\_state_backup C:\Argus\repo\state /MIR
   robocopy C:\Argus\_logs_backup C:\Argus\repo\ops\logs /MIR
   ```
4. Restart runner:
   ```powershell
   C:\Argus\.venv\Scripts\python.exe runner_live.py
   ```
5. Clean up backups after verifying:
   ```powershell
   Remove-Item -Recurse C:\Argus\_state_backup
   Remove-Item -Recurse C:\Argus\_logs_backup
   ```

---

## Scenario C: State Corruption (USB State Restore)

Runner crashes or state files are corrupted. Code is fine.

1. Stop the runner (Ctrl+C or kill process)
2. Restore state from USB:
   ```powershell
   robocopy "E:\Argus_Backup\repo\state" "C:\Argus\repo\state" /MIR
   ```
3. Restart runner:
   ```powershell
   cd C:\Argus\repo
   C:\Argus\.venv\Scripts\python.exe runner_live.py
   ```
4. Runner will auto-recover from the restored snapshot. Check logs for:
   - `recovery_source` should show snapshot or fills.csv recovery
   - Position state should match last known state

Note: If USB backup is also stale, runner can recover from `fills.csv` alone (cold restore). Delete `state\` entirely and runner rebuilds from fill history.

---

## Scenario D: PC1 Dead, Failover to PC2

PC1 is completely down. Run everything on PC2 (DESKTOP-17CJMUP) temporarily.

1. On PC2, pull latest code:
   ```powershell
   cd C:\Argus\repo
   git pull origin phase6-hardening
   ```
2. PC2 does NOT have PC1's state or logs. Options:
   - **If USB drive available**: plug into PC2 and restore:
     ```powershell
     robocopy "E:\Argus_Backup\repo\state" "C:\Argus\repo\state" /MIR
     robocopy "E:\Argus_Backup\repo\ops\logs" "C:\Argus\repo\ops\logs" /MIR
     ```
   - **If no USB**: runner starts fresh (FLAT position, no history). Safe for paper trading.
3. Start runner on PC2:
   ```powershell
   cd C:\Argus\repo
   C:\Argus\.venv\Scripts\python.exe runner_live.py
   ```
4. Start dashboard:
   ```powershell
   C:\Argus\.venv\Scripts\python.exe ops\dashboard.py
   ```
5. When PC1 is restored, stop PC2 runner first, then start PC1 runner. Never run both simultaneously (would create duplicate orders).

---

## Verification Checklist (Post-Recovery)

- [ ] `runner_live.py` starts without errors
- [ ] Dashboard accessible at http://localhost:8080
- [ ] Discord test: `python ops/notify.py --test "Recovery OK"`
- [ ] Bot state matches expected (FLAT or correct position)
- [ ] Task Scheduler jobs exist and enabled
- [ ] Git push works: `git push origin phase6-hardening`
- [ ] USB backup runs: `.\ops\backup_to_usb.ps1 -DryRun`