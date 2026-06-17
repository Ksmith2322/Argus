---
name: Terminal management — close old before opening new
description: Always close old/unneeded terminals before opening new ones to save resources
type: feedback
---

Before opening a new terminal or PowerShell window, close any existing terminals that are no longer needed.

**Why:** The user explicitly asked to conserve system resources on the Windows PC running live runners.

**How to apply:** When a task requires a new terminal (e.g., restarting the dashboard, running a script), first check if an existing terminal can be reused or closed. Kill stale background processes instead of leaving orphaned sessions accumulating.
