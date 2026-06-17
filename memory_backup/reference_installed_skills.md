---
name: installed Claude Code skills (personal + project scope)
description: Inventory of skills installed in this user's Claude Code, plus install date + reason. Useful when wondering "does this user have X skill?" or when adding more.
type: reference
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Personal scope (`C:\Users\ksmit\.claude\skills\`)

These load across all Claude Code projects, not just Argus.

**Installed 2026-04-24** — `obra/superpowers` workflow skills (14):
- `verification-before-completion` — gate on completion claims; "evidence before assertions, always." High value: would have caught my "all 22 strategies converted" overclaim before atlas/themis were noticed as non-trading scanners.
- `systematic-debugging` — 4-phase root-cause investigation framework
- `using-git-worktrees` — parallel branches without disturbing main checkout
- `subagent-driven-development` — agent-coordinated work patterns
- `writing-plans` / `executing-plans` — Socratic refinement + implementation strategies
- `brainstorming` — structured ideation for design phases (penny stocks, valuation screener)
- `dispatching-parallel-agents` — multi-agent task orchestration
- `requesting-code-review` / `receiving-code-review` — review lifecycle
- `test-driven-development` — TDD methodology (not currently used by user, but available)
- `finishing-a-development-branch` — merge guidance
- `writing-skills` / `using-superpowers` — meta-skills for the collection itself

Source: `https://github.com/obra/superpowers` (clone + cp into personal scope, no plugin installer needed).

## Project scope (`c:\Argus\repo\.claude\skills\`)

26 custom domain skills hand-built for Argus trading system. Listed in `project_execution_conversion_20260424.md` and visible via `ls c:\Argus\repo\.claude\skills\`. Trading-specific (health, audit, backtest, sweep, kill, promote, today, trades, etc.) — none overlap with the obra workflow skills.

## Considered + skipped

- `farion1231/cc-switch` — desktop app for switching between AI CLIs. User runs single-tool (Claude Code only). Not relevant.
- `wshobson/incident-runbook-templates` — repo doesn't exist (404, may have been mis-cited in third-party tweets).
- `mattpocock/git-guardrails-claude-code` — blocks ALL `git push` unconditionally including non-destructive ones. User pushes 5+/day with explicit approval; the guard would create constant friction. Wrong fit for this workflow.
- `learn-claude-code` (53k stars) — beginner onboarding. User is past this stage.
- `karpathy-skills` — coding philosophy, not concrete tooling.
- `self-improving-agent` — auto memory curation. Would conflict with the existing Anthropic memory system + the disciplined approach this user has built up.
- All cloud/infra/devops skills (k8s, terraform, AWS, Azure, Cloudflare, Pulumi, docker, ansible) — wrong stack.
- All marketing/PM/design/legal/health/genealogy/video/3D/game-dev skills — irrelevant.

## How to apply this memory

When user asks about adding a Claude Code skill:
1. Check this file first to see if already installed or considered+skipped
2. Don't recommend anything that overlaps with the 26 Argus-specific skills
3. Don't recommend anything with `git push` or scheduled-task interception (those would fight user's normal workflow)
4. Default verdict on most public skills is "skip" — user wants lean tooling, not skill bloat
