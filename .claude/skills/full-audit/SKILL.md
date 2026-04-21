---
name: full-audit
description: Run the 10-lens comprehensive bot audit. Produces a dated report at docs/audits/<stamp>/ with JSON + markdown + diff vs previous. The "deep investigation" tool for finding issues that limit trades/performance.
allowed-tools: Bash Agent
argument-hint: '[--diff | --agents]'
---

Run the comprehensive bot audit.

## 1. Mechanical lenses (fast, deterministic)

```bash
cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m ops.full_audit --diff
```

This fires 10 automated lenses and writes a dated report to
`docs/audits/<YYYYMMDD_HHMM>/`:

- **fleet**     — broker, pairs, processes, heartbeats
- **risk**      — drawdown, correlation, exposure, pause state
- **trading**   — 24h/7d/30d trade counts + PnL per strategy
- **data**      — canonical vs per-strategy CSV divergence, reconciliation
- **strategy**  — dispositions, promotion gates, holdout freezes
- **signals**   — 24h signal→entry conversion + top block reasons
- **tasks**     — Argus* scheduled-task health
- **code**      — errors, stale locks, git status
- **deps**      — IBKR, yfinance, Discord health
- **drift**     — config on-disk vs runner-launched drift

The `--diff` flag adds a diff.md comparing against the most recent prior
audit (if any).

## 2. Judgment lenses (agent-based, optional)

If user passed `--agents`, also fire the 4-agent debate:
- Performance gap auditor (what's suppressing P&L?)
- Strategy architect (what coverage is missing?)
- Skill/workflow auditor (what operator workflows still hurt?)
- Architecture risk auditor (what breaks at scale?)

Use the Agent tool with subagent_type=Explore for each, running in parallel
(`run_in_background: true`). Prompt each with current fleet context + the
specific audit lens. When they return, synthesize with particular attention
to DISAGREEMENTS between the agents and the mechanical audit.

## 3. Output format

After running, present:
- **Top 3 critical findings** (anything marked CRITICAL, MISSING, or DRIFT)
- **Top 3 actionable items** (specific file/command user can take action on)
- **Diff highlights** (vs previous audit — metrics that improved or degraded)
- **Punchlist** (numbered list of suggested fixes)
- Link to the full report: `docs/audits/<stamp>/report.md`

## 4. Mental model

The mechanical audit catches schema drift, missing tasks, divergent sources,
stale data, and process deaths — objective problems with objective signals.
The agent debate catches judgment-call issues: strategy quality, design
flaws, performance gaps, architectural blind spots.

Run the mechanical version weekly or after major changes. Run the agent
version after significant session work or when something feels "off" that
you can't quantify.

Every audit run is permanent at `docs/audits/<stamp>/` — commit them so
trends over time are visible.
