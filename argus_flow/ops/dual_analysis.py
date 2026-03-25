"""Phase 22C — Dual-Model Adversarial Analysis.
Generates structured prompts for parallel Claude + GPT analysis.

Usage:
    python -m argus_flow.ops.dual_analysis --type strategy_review
    python -m argus_flow.ops.dual_analysis --type backtest_review --data FILE
    python -m argus_flow.ops.dual_analysis --type parameter_tuning --pair PAIR
"""
import argparse, csv, json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ANALYSIS_TYPES = {
    "strategy_review": "Review current FX strategy performance and suggest improvements",
    "backtest_review": "Analyze backtest results — is this edge real or overfit?",
    "parameter_tuning": "Suggest parameter adjustments based on trade data",
    "risk_assessment": "Evaluate current risk exposure and recommend changes",
    "kill_decision": "Should this pair be killed? Analyze the evidence",
}


def _load_pair_data(pair: str) -> dict:
    """Load current data for a pair."""
    log_dir = REPO / "argus_flow" / "logs" / pair.lower()
    data = {"pair": pair, "trades": [], "signals_summary": {}, "config": {}}

    # Trades
    trade_file = log_dir / "trades.csv"
    if trade_file.exists():
        with open(trade_file) as f:
            data["trades"] = list(csv.DictReader(f))

    # Config
    cfg_map = {"EURUSD": "eurusd_t4_paper_v1.json", "GBPUSD": "gbpusd_range_paper_v1.json", "EURJPY": "eurjpy_t4_paper_v1.json"}
    cfg_path = REPO / "argus_flow" / "configs" / cfg_map.get(pair.upper(), "")
    if cfg_path.exists():
        data["config"] = json.loads(cfg_path.read_text())

    return data


def _load_backtest_data(path: str) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def generate_prompt(analysis_type: str, pair: str = None, data_path: str = None) -> str:
    """Generate a structured analysis prompt."""

    header = f"""# Dual-Model Analysis Request
Type: {analysis_type} — {ANALYSIS_TYPES.get(analysis_type, '')}
Generated: {datetime.now(timezone.utc).isoformat()}

## Instructions
You are analyzing an FX trading strategy (IBKR, paper trading phase).
Be specific, data-driven, and adversarial — challenge assumptions.
If you see signs of overfitting, curve-fitting, or insufficient data, say so clearly.

"""

    context = "## Context\n"

    if pair:
        data = _load_pair_data(pair)
        context += f"Pair: {pair}\n"
        context += f"Strategy: {data['config'].get('strategy', 'unknown')}\n"
        context += f"Total trades: {len(data['trades'])}\n"

        if data['trades']:
            valid = [t for t in data['trades'] if t.get('experiment_valid', '').lower() == 'true']
            pnl_field = 'pnl_pips' if 'pnl_pips' in data['trades'][0] else 'pnl_pts'
            pnls = [float(t.get(pnl_field, 0)) for t in valid]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]

            context += f"Valid trades: {len(valid)}\n"
            context += f"Win rate: {len(wins)/len(pnls)*100:.1f}%\n" if pnls else ""
            context += f"Total PnL: {sum(pnls):.1f} pips\n" if pnls else ""
            context += f"Avg win: {sum(wins)/len(wins):.1f} pips\n" if wins else ""
            context += f"Avg loss: {sum(losses)/len(losses):.1f} pips\n" if losses else ""

            # Exit reason breakdown
            reasons = {}
            for t in valid:
                r = t.get('exit_reason', 'unknown')
                reasons[r] = reasons.get(r, 0) + 1
            context += f"Exit reasons: {reasons}\n"

        # Config summary
        risk = data['config'].get('risk', {})
        trigger = data['config'].get('trigger', {})
        context += f"\nConfig:\n"
        context += f"  Stop: {risk.get('stop_pips', risk.get('stop_bps', '?'))} | Target: {risk.get('target_pips', risk.get('target_bps', '?'))}\n"
        context += f"  Timeout: {risk.get('timeout_minutes', '?')} min\n"
        context += f"  Session: {trigger.get('session_start_utc', '?')}-{trigger.get('session_end_utc', '?')} UTC\n"
        context += f"  Range min: {trigger.get('range_pct_min', '?')} | Accel min: {trigger.get('range_accel_min', '?')}\n"

    if data_path:
        bt_data = _load_backtest_data(data_path)
        context += f"\nBacktest data: {len(bt_data)} trades from {data_path}\n"
        if bt_data:
            pnls = [float(t.get('pnl', 0)) for t in bt_data]
            wins = [p for p in pnls if p > 0]
            context += f"  WR: {len(wins)/len(pnls)*100:.1f}% | PnL: {sum(pnls):.1f} | Trades: {len(pnls)}\n"

    questions = {
        "strategy_review": """
## Questions to Answer
1. Based on the data, is this strategy showing a real edge or noise?
2. What is the biggest risk to this strategy failing in live trading?
3. Are the stop/target/timeout parameters appropriate for this pair?
4. What would you change and why?
5. Is there enough data to make a confident assessment?
""",
        "backtest_review": """
## Questions to Answer
1. Does this backtest show signs of overfitting? Why or why not?
2. Is the win rate and profit factor sustainable out-of-sample?
3. What is the expected degradation moving from backtest to live?
4. Are there any red flags in the exit reason distribution?
5. Would you deploy this to paper trading? What conditions?
""",
        "parameter_tuning": """
## Questions to Answer
1. Should stops be wider or tighter based on the data?
2. Is the timeout too short or too long?
3. Should the session window be adjusted?
4. Are the trigger thresholds appropriate?
5. What single parameter change would most improve performance?
""",
        "risk_assessment": """
## Questions to Answer
1. What is the maximum expected drawdown for this strategy?
2. Is position sizing appropriate for the account size?
3. Are correlation risks being managed?
4. What is the worst-case scenario and how do we survive it?
""",
        "kill_decision": """
## Questions to Answer
1. Does the data justify killing this pair?
2. Is the negative performance structural or just variance?
3. How many more trades would you need to be confident?
4. If not killing, what changes would you make?
""",
    }

    prompt = header + context + questions.get(analysis_type, "\n## Provide your analysis.\n")

    prompt += """
## Response Format
Please structure your response as:
1. **Assessment** (1 sentence verdict)
2. **Evidence** (specific data points supporting your verdict)
3. **Risks** (what could go wrong)
4. **Recommendations** (specific, actionable)
5. **Confidence** (low/medium/high and why)
"""

    return prompt


def main():
    parser = argparse.ArgumentParser(description="Dual-Model Analysis Prompt Generator")
    parser.add_argument("--type", required=True, choices=list(ANALYSIS_TYPES.keys()), help="Analysis type")
    parser.add_argument("--pair", help="FX pair (EURUSD, GBPUSD, EURJPY)")
    parser.add_argument("--data", help="Path to backtest results CSV")
    args = parser.parse_args()

    prompt = generate_prompt(args.type, args.pair, args.data)

    # Save to file
    out_dir = REPO / "argus_flow" / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"{args.type}_{ts}.md"
    out_path.write_text(prompt)

    print(f"Analysis prompt generated: {out_path}")
    print(f"Paste into BOTH Claude and GPT-4, then compare responses.")
    print(f"\n{'=' * 60}")
    print(prompt)


if __name__ == "__main__":
    main()
