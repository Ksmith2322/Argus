---
name: Windows venv redirector pattern — two python.exe per invocation is NORMAL
description: Python 3.12 venv on Windows produces a redirector shim + real interpreter pair. Don't pattern-match "duplicate processes" without checking PPID first.
type: reference
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
On Windows, when a venv is created with `python -m venv` (Python 3.12+), `.venv\Scripts\python.exe` is a 270KB **redirector shim**, not a copy of the real interpreter. When invoked, it:

1. Spawns the real Python from `base_prefix` (e.g. `C:\Users\ksmit\AppData\Local\Programs\Python\Python312\python.exe`) as a child process
2. Waits on the child via `WaitForSingleObject`
3. Exits when the child exits

Both processes appear in `Get-CimInstance Win32_Process -Filter "name='python.exe'"` — but they're **one logical Python invocation**.

## How to verify a venv shim

```bash
ls -la c:/Argus/.venv/Scripts/python.exe          # ~270KB = shim
C:/Argus/.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)"
# If sys.base_prefix != sys.prefix, it's a shim with redirector behavior.
```

## The 2026-04-28 incident

While building 5/1 ceremony prep tools, I noticed every fleet python.exe had a "duplicate" with a different interpreter path:
- `.venv\Scripts\python.exe -m forge.X.runner` (PID A)
- `Python312\python.exe -m forge.X.runner` (PID B, parent = A)

I assumed this was [reference_failure_modes.md](reference_failure_modes.md) failure mode #1 (duplicate-daemon). Killed all 26 "system" processes thinking they were duplicates. **22 of the 26 venv "parents" died as collateral** — because they were redirector shims waiting on those exact children.

Result: fleet went from 26 strategies running to 4. Required emergency relaunch via [ops/emergency_relaunch.ps1](../../../../Argus/repo/ops/emergency_relaunch.ps1).

## How to detect REAL duplicates

The advisory in `ops/operational_vetting.py` now identifies tree ROOTS — processes whose parent is NOT another process in the same module group. Multiple roots running the same module = real duplicate launches. A single tree (shim + real) = one logical invocation.

## How to apply this memory

**Why:** confusing the redirector shim for a duplicate process led to a fleet outage. Real duplicates and redirector-shim pairs LOOK identical in `Get-CimInstance` output unless you check PPID relationships.

**How to apply:**
- Before claiming duplicate processes exist, check if one process is a CHILD of the other (PPID == other PID). If yes, it's the redirector pattern, not a duplicate.
- Real duplicates would have INDEPENDENT parents (e.g. both spawned by the same launcher script that ran twice).
- Use `ops/operational_vetting.py`'s `_duplicate_process_advisory()` for accurate detection — it already filters out redirector pairs.
- Do not Stop-Process the "system Python" sibling of a venv shim. They are not independent.

## Alternative: avoid the shim entirely

If desired, recreate the venv with `--copies`:
```
python -m venv --copies C:\Argus\.venv
```
This copies the full real Python interpreter into venv/Scripts instead of a redirector. One process per invocation. Slightly more disk, much less confusion.

For now, the existing redirector venv is fine — just don't mistake the pair for duplicates.
