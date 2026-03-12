"""
argus_builder.py — Autonomous Argus development agent.

Usage:
    C:\\Argus\\.venv\\Scripts\\python.exe .\\ops\\argus_builder.py
    C:\\Argus\\.venv\\Scripts\\python.exe .\\ops\\argus_builder.py --phase 13
    C:\\Argus\\.venv\\Scripts\\python.exe .\\ops\\argus_builder.py --task "implement analytics/risk_model.py per roadmap Phase 13"
    C:\\Argus\\.venv\\Scripts\\python.exe .\\ops\\argus_builder.py --dry-run

The agent reads CLAUDE.md + roadmap.txt, determines what to build next,
implements it, and reports what was done. Each run is logged to ops/logs/builder_<timestamp>.log.
"""

import argparse
import sys
import datetime
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "ops", "logs")

SYSTEM_PROMPT = """
You are the Argus project architect and autonomous developer.

## Your Mission
Read roadmap.txt to understand current project state, identify the next concrete
unit of work, and implement it fully. One run = one complete, tested deliverable.

## Non-negotiable Rules (from CLAUDE.md and roadmap.txt)
1. TRUTH-SURFACE DOCTRINE: lower layers feed higher layers only. Analytics must
   never become hidden runtime control state. Every derived metric must be
   traceable to its canonical source artifact.
2. Never touch hazard files: main.py, run_main.py, backtest/run_backtest_LEGACY_DO_NOT_USE.py, logger.py
3. Use io_logs.py for ALL artifact writes — never write CSV/JSON artifacts directly.
4. Backtest always runs in ENGINE mode (_ensure_bt_cfg() enforces this — do not remove).
5. Sandbox rule: backtest must never touch live_events.csv or live_signals.csv.
6. Only build what the roadmap says. Do not add features, refactor code, or make
   improvements beyond what is explicitly scoped in the phase spec.
7. After implementing, validate: run the backtest or relevant test harness to
   confirm nothing is broken. Fix any regressions before declaring done.
8. Update roadmap.txt CURRENT STATE section when a phase milestone is reached.

## How to Start Each Run
1. Read CLAUDE.md for architectural context.
2. Read roadmap.txt, focusing on CURRENT STATE and the next incomplete phase.
3. Read the relevant source files before writing any code.
4. Implement the smallest complete unit described in the phase spec.
5. Run validation (backtest, harness, or import check as appropriate).
6. Report clearly: what was built, what was validated, what is next.

## Key Paths
- Repo root: C:\\Argus\\repo
- Python venv: C:\\Argus\\.venv\\Scripts\\python.exe
- Run backtest: C:\\Argus\\.venv\\Scripts\\python.exe -m backtest.runner
- Artifacts: C:\\Argus\\repo\\ops\\logs\\
- Config: .env in repo root
""".strip()

AUTONOMOUS_PROMPT = """
Read CLAUDE.md and roadmap.txt. Based on the CURRENT STATE section, identify the
next concrete unit of work that can be completed in this session and implement it.

Prioritize:
1. Any phase with status INFRA DONE / DATA PENDING → skip (blocked on data), move on
2. Any phase whose entry gate is satisfied → implement next deliverable
3. Build in roadmap order (Phase 11 before 12, etc.)

After implementing, run the backtest or relevant validation to confirm no regressions.
Report: what was built, what tests pass, what the updated CURRENT STATE should be.
""".strip()


def make_task_prompt(task: str) -> str:
    return f"""
Read CLAUDE.md and roadmap.txt for context, then complete the following task:

{task}

After implementing, validate (run backtest or relevant test harness).
Report what was done and whether validation passed.
""".strip()


def make_dry_run_prompt() -> str:
    return """
Read CLAUDE.md and roadmap.txt. Do NOT make any changes.

Report:
1. Current project state (summarize CURRENT STATE section)
2. What the next unit of work is and why
3. What files would need to be created or modified
4. What validation would be run

This is a planning pass only — no code changes.
""".strip()


def _fmt_tool_input(name: str, inp: dict) -> str:
    """Return a short human-readable summary of a tool call's input."""
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:120] + ("…" if len(cmd) > 120 else "")
    if name in ("Glob", "Grep"):
        return inp.get("pattern", inp.get("query", ""))
    return str(inp)[:120]


async def run_builder(prompt: str, max_turns: int, log_path: str) -> None:
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, ResultMessage,
        AssistantMessage, TextBlock, ToolUseBlock,
    )

    print(f"[argus_builder] Starting agent | log: {log_path}")
    print(f"[argus_builder] Repo: {REPO_ROOT}\n")
    print("=" * 70)

    result_text = None
    session_id = None
    turn = 0

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            cwd=REPO_ROOT,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
            max_turns=max_turns,
            setting_sources=["project"],  # loads CLAUDE.md
        ),
    ):
        if isinstance(message, AssistantMessage):
            turn += 1
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    # Print agent's reasoning/commentary
                    print(f"\n[turn {turn}] {block.text.strip()}")
                elif isinstance(block, ToolUseBlock):
                    summary = _fmt_tool_input(block.name, block.input)
                    print(f"\n[turn {turn}] >> {block.name}({summary})")

        elif isinstance(message, ResultMessage):
            result_text = message.result
            session_id = message.session_id
            print("\n" + "=" * 70)
            print("[argus_builder] RESULT:")
            print("=" * 70)
            print(result_text)
            print("=" * 70)
            print(f"\nStop reason: {message.stop_reason}")
            print(f"Turns: {message.num_turns} | Cost: ${message.total_cost_usd or 0:.4f}")
            print(f"Session: {session_id}")

    # Write log
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"argus_builder run: {datetime.datetime.now().isoformat()}\n")
        f.write(f"session_id: {session_id}\n")
        f.write(f"prompt:\n{prompt}\n\n")
        f.write("result:\n")
        f.write(result_text or "(no result)")
        f.write("\n")

    print(f"\n[argus_builder] Log saved: {log_path}")


def check_not_in_claude_session() -> None:
    if os.environ.get("CLAUDECODE"):
        print("ERROR: argus_builder cannot run inside an active Claude Code session.")
        print()
        print("Run this from a regular terminal (PowerShell / cmd), not from")
        print("within the VS Code Claude Code extension.")
        print()
        print("  1. Open a new PowerShell window")
        print("  2. cd C:\\Argus\\repo")
        print("  3. C:\\Argus\\.venv\\Scripts\\python.exe .\\ops\\argus_builder.py")
        sys.exit(1)


def main() -> None:
    check_not_in_claude_session()
    parser = argparse.ArgumentParser(description="Argus autonomous builder agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--task", metavar="TASK", help="Specific task to implement")
    group.add_argument("--phase", metavar="N", type=int, help="Focus on a specific phase number")
    group.add_argument("--dry-run", action="store_true", help="Plan only, no code changes")
    parser.add_argument("--max-turns", type=int, default=80, help="Max agent turns (default: 80)")
    args = parser.parse_args()

    if args.dry_run:
        prompt = make_dry_run_prompt()
    elif args.task:
        prompt = make_task_prompt(args.task)
    elif args.phase:
        prompt = make_task_prompt(
            f"Implement the next deliverable for Phase {args.phase} as specified in roadmap.txt."
        )
    else:
        prompt = AUTONOMOUS_PROMPT

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"builder_{ts}.log")

    import anyio
    try:
        anyio.run(run_builder, prompt, args.max_turns, log_path)
    except KeyboardInterrupt:
        print("\n[argus_builder] Interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
