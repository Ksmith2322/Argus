#!/usr/bin/env python3
"""ops/generate_map.py -- Generate Mermaid architecture diagram of the Argus system.

Scans all .py files, extracts intra-project imports, and generates a dependency graph
grouped by architectural layer.

Usage:
    python ops/generate_map.py              # generate ARGUS_MAP.mermaid
    python ops/generate_map.py --stdout     # print to stdout instead of file
"""
import ast
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Modules to skip (legacy/unused/test)
SKIP = {
    "main", "run_main", "logger",
    "backtest.run_backtest_LEGACY_DO_NOT_USE",
    "ops.test_phase16_ops",
    "__init__",
}

# Architectural layers
LAYERS = {
    "Config & Data": [
        "config", "candles", "indicators",
        "backtest.loader", "backtest.feed",
        "backtest.download_candles",
    ],
    "Strategy & Signals": [
        "confluence", "adaptive_confluence", "regime", "session",
        "structure", "liquidity", "trendlines", "risk",
        "strategy_phase2", "decisions",
    ],
    "Engine": [
        "engine", "state",
    ],
    "Execution": [
        "ledger", "io_logs",
        "execution.adapter", "execution.paper_adapter",
        "execution.intent", "execution.exec_io",
        "execution.checkpoint", "execution.recovery",
    ],
    "Lifecycle": [
        "runner_live", "trade_journal", "trade_tracker",
        "reconciliation", "pnl_shadow",
        "backtest.runner", "backtest.results",
        "backtest.friction_injector", "backtest.stress_runner",
        "backtest.walk_forward", "backtest.from_live_events",
    ],
    "Analytics & Reporting": [
        "analytics.attribution", "analytics.friction_report",
        "analytics.research_report", "analytics.risk_model",
        "reporting.generate_report",
    ],
    "Ops & Infra": [
        "ops.health", "ops.watchdog", "ops.alerting",
        "ops.run_manifest", "ops.artifact_integrity",
        "ops.invariants", "ops.auto_throttle",
        "ops.backup_restore", "ops.dashboard",
        "ops.notify", "ops.auto_compare",
        "ops.compare_runs", "ops.analyze_scores",
        "ops.validate_candles", "ops.argus_builder",
        "runtime_mode", "feed_coinbase", "utils",
        "notify",
    ],
}


def module_name(path: Path) -> str:
    """Convert file path to dotted module name relative to repo root."""
    rel = path.relative_to(REPO).with_suffix("")
    return str(rel).replace(os.sep, ".").replace("/", ".")


def find_modules() -> dict[str, Path]:
    """Find all .py files and return {module_name: path}."""
    modules = {}
    for py in REPO.rglob("*.py"):
        rel = py.relative_to(REPO)
        # Skip non-source directories
        parts = rel.parts
        if any(p in ("ops/logs", ".venv", "__pycache__", "tests", "archive") for p in parts):
            continue
        if "archive" in str(rel):
            continue
        name = module_name(py)
        if name in SKIP or any(name.endswith(f".{s}") for s in SKIP):
            continue
        modules[name] = py
    return modules


def extract_imports(path: Path, all_modules: set[str]) -> set[str]:
    """Extract intra-project imports from a Python file."""
    imports = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                full = alias.name
                if full in all_modules:
                    imports.add(full)
                elif name in all_modules:
                    imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module
                if mod in all_modules:
                    imports.add(mod)
                elif mod.split(".")[0] in all_modules:
                    # Try full dotted name
                    parts = mod.split(".")
                    for i in range(len(parts), 0, -1):
                        candidate = ".".join(parts[:i])
                        if candidate in all_modules:
                            imports.add(candidate)
                            break
    return imports


def mermaid_id(name: str) -> str:
    """Convert module name to valid Mermaid ID."""
    return name.replace(".", "_")


def generate_mermaid(modules: dict[str, Path], deps: dict[str, set[str]]) -> str:
    """Generate Mermaid flowchart string."""
    lines = ["graph TD"]

    # Group modules into subgraphs by layer
    assigned = set()
    for layer_name, layer_modules in LAYERS.items():
        members = [m for m in layer_modules if m in modules]
        if not members:
            continue
        safe_layer = layer_name.replace(" ", "_").replace("&", "and")
        lines.append(f"    subgraph {safe_layer}[\"{layer_name}\"]")
        for m in members:
            mid = mermaid_id(m)
            short = m.split(".")[-1]
            lines.append(f"        {mid}[\"{short}\"]")
            assigned.add(m)
        lines.append("    end")
        lines.append("")

    # Any unassigned modules
    unassigned = [m for m in modules if m not in assigned]
    if unassigned:
        lines.append("    subgraph Other[\"Other\"]")
        for m in unassigned:
            mid = mermaid_id(m)
            short = m.split(".")[-1]
            lines.append(f"        {mid}[\"{short}\"]")
        lines.append("    end")
        lines.append("")

    # Edges
    lines.append("    %% Dependencies")
    for src, targets in sorted(deps.items()):
        if src not in modules:
            continue
        src_id = mermaid_id(src)
        for tgt in sorted(targets):
            if tgt not in modules:
                continue
            tgt_id = mermaid_id(tgt)
            lines.append(f"    {src_id} --> {tgt_id}")

    return "\n".join(lines) + "\n"


def main():
    stdout_mode = "--stdout" in sys.argv

    modules = find_modules()
    all_names = set(modules.keys())

    # Extract dependencies
    deps: dict[str, set[str]] = {}
    for name, path in modules.items():
        imports = extract_imports(path, all_names)
        imports.discard(name)  # no self-references
        if imports:
            deps[name] = imports

    mermaid = generate_mermaid(modules, deps)

    if stdout_mode:
        print(mermaid)
    else:
        out_path = REPO / "ARGUS_MAP.mermaid"
        out_path.write_text(mermaid, encoding="utf-8")
        print(f"Generated {out_path} ({len(modules)} modules, {sum(len(v) for v in deps.values())} edges)")


if __name__ == "__main__":
    main()