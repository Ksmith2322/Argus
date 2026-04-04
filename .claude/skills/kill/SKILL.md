---
name: kill
description: Emergency kill — stop all trading immediately. Creates KILL_SWITCH file to flatten all Argus positions and stops all Helio runners.
allowed-tools: Bash
disable-model-invocation: true
---

EMERGENCY KILL — stop everything immediately.

IMPORTANT: Confirm with the user before executing. This is destructive.

1. **Create KILL_SWITCH file** to trigger Argus emergency shutdown (flattens all positions):
   ```bash
   echo "manual_kill_$(date -u +%Y%m%dT%H%M%SZ)" > C:/Argus/repo/KILL_SWITCH
   ```

2. **Kill all Helio family runners**:
   ```bash
   powershell.exe -NoProfile -Command "Get-Process python* | ForEach-Object { try { $cmd=(Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.Id)\").CommandLine; if($cmd -match 'apollo|hermes|helio\.runner') { Stop-Process -Id $_.Id -Force } } catch {} }"
   ```

3. **Verify**: Check that no runner processes remain and KILL_SWITCH file exists.

4. **Report**: List any positions that were open at kill time.

To resume after kill: remove KILL_SWITCH file and use /restart all
