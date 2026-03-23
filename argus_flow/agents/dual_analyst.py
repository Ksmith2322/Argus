"""Dual-Agent Analysis System — Two Claude instances collaborate on strategy analysis.

Architecture:
  Agent 1 (Analyst): Reads data, produces initial analysis
  Agent 2 (Reviewer): Reads Agent 1's analysis + raw data, challenges/validates

Communication: File-based handoff via shared analysis directory.
Each agent writes its output, the other reads it for the next round.

Usage (with Anthropic API key):
    python -m argus_flow.agents.dual_analyst --mode analyze
    python -m argus_flow.agents.dual_analyst --mode review
    python -m argus_flow.agents.dual_analyst --mode full    # both in sequence

Without API key (manual mode):
    python -m argus_flow.agents.dual_analyst --mode collect  # gather data for analysis
    Then paste the output into two separate Claude conversations
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ANALYSIS_DIR = Path("argus_flow/agents/analysis")
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

RUNNER_LOGS = {
    "eurusd": Path("argus_flow/logs/eurusd"),
    "gbpusd": Path("argus_flow/logs/gbpusd"),
    "mnq": Path("argus_flow/logs/mnq"),
}

CONFIGS = {
    "eurusd": Path("argus_flow/configs/eurusd_t4_paper_v1.json"),
    "gbpusd": Path("argus_flow/configs/gbpusd_range_paper_v1.json"),
    "mnq": Path("argus_flow/configs/mnq_vol_burst_paper_v1.json"),
}


def collect_runner_data() -> dict:
    """Gather current state from all runners into a structured report."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runners": {},
    }

    for name, log_dir in RUNNER_LOGS.items():
        runner = {"name": name, "signals": [], "trades": [], "config": {}}

        # Config
        cfg_path = CONFIGS.get(name)
        if cfg_path and cfg_path.exists():
            runner["config"] = json.loads(cfg_path.read_text())

        # Signals (last 50)
        sig_file = log_dir / "signals.csv"
        if sig_file.exists():
            with open(sig_file, "r") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                runner["total_signals"] = len(rows)
                runner["signals"] = rows[-50:]

                # Compute signal stats
                if rows:
                    actions = [r.get("action", "") for r in rows]
                    runner["entry_count"] = sum(1 for a in actions if a == "ENTRY")
                    runner["no_trigger_count"] = sum(1 for a in actions if a == "NO_TRIGGER")

                    # Feature distributions from recent signals
                    recent = rows[-100:] if len(rows) >= 100 else rows
                    for feat in ["range_pct", "vol_z", "range_accel", "vol_burst_z", "dist_from_low"]:
                        vals = [float(r[feat]) for r in recent if feat in r and r[feat]]
                        if vals:
                            runner[f"{feat}_mean"] = sum(vals) / len(vals)
                            runner[f"{feat}_min"] = min(vals)
                            runner[f"{feat}_max"] = max(vals)

        # Trades
        trade_file = log_dir / "trades.csv"
        if trade_file.exists():
            with open(trade_file, "r") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                runner["total_trades"] = len(rows)
                runner["trades"] = rows

                if rows:
                    pnls = [float(r.get("pnl_pips", r.get("pnl_pts", 0))) for r in rows]
                    runner["total_pnl"] = sum(pnls)
                    runner["win_count"] = sum(1 for p in pnls if p > 0)
                    runner["loss_count"] = sum(1 for p in pnls if p <= 0)
                    runner["win_rate"] = runner["win_count"] / len(pnls) if pnls else 0

        # State
        state_file = log_dir / "state.json"
        if state_file.exists():
            runner["state"] = json.loads(state_file.read_text())

        report["runners"][name] = runner

    return report


def build_analyst_prompt(report: dict) -> str:
    """Build the prompt for Agent 1 (Analyst)."""
    return f"""You are the ANALYST agent for the Argus trading system.

Your job is to analyze the current state of three IBKR paper trading runners and provide:
1. Operational health assessment (is each runner working correctly?)
2. Signal quality analysis (are features computing as expected?)
3. Strategy behavior check (are triggers firing at expected rates?)
4. Early performance indicators (if any trades have occurred)
5. Concerns or anomalies that need attention
6. Comparison to replay expectations from configs

Be specific. Use numbers. Flag anything that diverges from expectations.

Here is the current runner data:

```json
{json.dumps(report, indent=2, default=str)}
```

Provide your analysis in a structured format with clear sections and specific metrics.
"""


def build_reviewer_prompt(report: dict, analyst_output: str) -> str:
    """Build the prompt for Agent 2 (Reviewer)."""
    return f"""You are the REVIEWER agent for the Argus trading system.

Agent 1 (Analyst) has provided an analysis of the current trading system state.
Your job is to:
1. Challenge the Analyst's conclusions — what did they miss or get wrong?
2. Identify risks the Analyst didn't flag
3. Check if the Analyst's numbers match the raw data
4. Provide a second opinion on strategy health
5. Recommend specific actions (if any)

Be adversarial but constructive. Don't agree for the sake of agreeing.
If the Analyst is right, say so — but explain WHY, don't just defer.

ANALYST'S REPORT:
{analyst_output}

RAW DATA (same data the Analyst received):
```json
{json.dumps(report, indent=2, default=str)}
```

Provide your review in a structured format. End with a clear AGREE/DISAGREE/PARTIALLY AGREE
on each of the Analyst's main conclusions.
"""


def run_with_api(report: dict, mode: str):
    """Run agents using the Anthropic API."""
    try:
        import anthropic
    except ImportError:
        print("ERROR: pip install anthropic")
        print("Or set ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        print("Get one at: https://console.anthropic.com/")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if mode in ("analyze", "full"):
        print("=" * 60)
        print("AGENT 1: ANALYST")
        print("=" * 60)

        prompt = build_analyst_prompt(report)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        analyst_output = response.content[0].text
        print(analyst_output)

        # Save
        (ANALYSIS_DIR / "analyst_report.md").write_text(analyst_output)
        (ANALYSIS_DIR / "analyst_report.json").write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": "claude-sonnet-4-6",
            "output": analyst_output,
        }, indent=2))
        print(f"\nSaved: {ANALYSIS_DIR / 'analyst_report.md'}")

    if mode in ("review", "full"):
        # Load analyst output if running review separately
        if mode == "review":
            analyst_file = ANALYSIS_DIR / "analyst_report.md"
            if not analyst_file.exists():
                print("ERROR: No analyst report found. Run --mode analyze first.")
                sys.exit(1)
            analyst_output = analyst_file.read_text()

        print("\n" + "=" * 60)
        print("AGENT 2: REVIEWER")
        print("=" * 60)

        prompt = build_reviewer_prompt(report, analyst_output)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        reviewer_output = response.content[0].text
        print(reviewer_output)

        # Save
        (ANALYSIS_DIR / "reviewer_report.md").write_text(reviewer_output)
        (ANALYSIS_DIR / "reviewer_report.json").write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": "claude-sonnet-4-6",
            "output": reviewer_output,
        }, indent=2))
        print(f"\nSaved: {ANALYSIS_DIR / 'reviewer_report.md'}")


def run_manual(report: dict):
    """Output data and prompts for manual copy-paste into Claude conversations."""
    print("=" * 60)
    print("MANUAL MODE — Copy these into separate Claude conversations")
    print("=" * 60)

    # Save the data package
    data_file = ANALYSIS_DIR / "runner_data.json"
    data_file.write_text(json.dumps(report, indent=2, default=str))

    prompt1_file = ANALYSIS_DIR / "prompt_analyst.md"
    prompt1_file.write_text(build_analyst_prompt(report))

    print(f"\n1. Data collected: {data_file}")
    print(f"2. Analyst prompt: {prompt1_file}")
    print(f"\nTo use manually:")
    print(f"  - Open Claude conversation #1, paste contents of {prompt1_file}")
    print(f"  - Copy the response, save to {ANALYSIS_DIR / 'analyst_report.md'}")
    print(f"  - Then run: python -m argus_flow.agents.dual_analyst --mode build-review")
    print(f"  - Open Claude conversation #2, paste the reviewer prompt")


def build_review_prompt_from_saved(report: dict):
    """Build reviewer prompt from saved analyst report."""
    analyst_file = ANALYSIS_DIR / "analyst_report.md"
    if not analyst_file.exists():
        print(f"ERROR: Save analyst output to {analyst_file} first")
        sys.exit(1)

    analyst_output = analyst_file.read_text()
    prompt = build_reviewer_prompt(report, analyst_output)

    prompt_file = ANALYSIS_DIR / "prompt_reviewer.md"
    prompt_file.write_text(prompt)
    print(f"Reviewer prompt saved: {prompt_file}")
    print(f"Paste into a separate Claude conversation for independent review.")


def main():
    parser = argparse.ArgumentParser(description="Dual-Agent Analysis System")
    parser.add_argument("--mode", choices=["collect", "analyze", "review", "full", "build-review"],
                        default="collect", help="Mode: collect data, run analyst, run reviewer, or full pipeline")
    args = parser.parse_args()

    report = collect_runner_data()

    if args.mode == "collect":
        run_manual(report)
    elif args.mode == "build-review":
        build_review_prompt_from_saved(report)
    elif args.mode in ("analyze", "review", "full"):
        run_with_api(report, args.mode)


if __name__ == "__main__":
    main()