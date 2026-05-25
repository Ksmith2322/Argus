"""Tests for ops.operational_vetting._duplicate_process_advisory.

Specifically pin the variant-aware grouping so future regressions don't
re-introduce the false-positive flagging of xs_momentum variants as
duplicates of the baseline.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _stub_subprocess_run(proc_list: list[dict]):
    """Build a subprocess.run replacement that returns synthetic
    PowerShell JSON output for a given process list."""
    class Result:
        def __init__(self, payload):
            self.stdout = json.dumps(payload) if isinstance(payload, list) else json.dumps([payload])
    def _run(*args, **kwargs):
        return Result(proc_list)
    return _run


def test_advisory_treats_shim_real_pair_as_one_runner():
    """venv shim + base interpreter = one runner, not two."""
    procs = [
        {"ProcessId": 100, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --loop'},
        {"ProcessId": 200, "ParentProcessId": 100,
         "CreationDate": "/Date(1779000001000)/",
         "CommandLine": '"C:\\Users\\ksmit\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" -m forge.xs_momentum.runner --loop'},
    ]
    with patch("subprocess.run",
               side_effect=_stub_subprocess_run(procs)):
        from ops.operational_vetting import _duplicate_process_advisory
        result = _duplicate_process_advisory()
    assert result["status"] == "ok"
    assert result["n_total_python_processes"] == 2
    assert result["n_duplicate_modules"] == 0, (
        f"shim/real pair should NOT count as duplicate, got "
        f"{result.get('duplicates')}"
    )


def test_advisory_groups_xs_momentum_variants_separately():
    """Each --variant flag = different runner instance."""
    procs = [
        {"ProcessId": 100, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --loop'},
        {"ProcessId": 200, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --variant sectors --loop'},
        {"ProcessId": 300, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --variant style --loop'},
        {"ProcessId": 400, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --variant legacy15 --loop'},
    ]
    with patch("subprocess.run",
               side_effect=_stub_subprocess_run(procs)):
        from ops.operational_vetting import _duplicate_process_advisory
        result = _duplicate_process_advisory()
    assert result["status"] == "ok"
    assert result["n_modules_running"] == 4, (
        "baseline + 3 variants = 4 distinct module instances"
    )
    assert result["n_duplicate_modules"] == 0, (
        f"variants on different --variant flags should NOT be flagged "
        f"as duplicates, got {result.get('duplicates')}"
    )


def test_advisory_flags_real_double_launch():
    """Two independent process trees for the SAME module + variant =
    real duplicate. Must be flagged."""
    procs = [
        # Tree 1: shell A launched baseline
        {"ProcessId": 100, "ParentProcessId": 1,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --loop'},
        # Tree 2: shell B ALSO launched baseline — DIFFERENT parent
        {"ProcessId": 200, "ParentProcessId": 2,
         "CreationDate": "/Date(1779000000000)/",
         "CommandLine": '"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner --loop'},
    ]
    with patch("subprocess.run",
               side_effect=_stub_subprocess_run(procs)):
        from ops.operational_vetting import _duplicate_process_advisory
        result = _duplicate_process_advisory()
    assert result["n_duplicate_modules"] == 1, (
        "independent trees on the same module should be flagged"
    )
    dup = result["duplicates"]
    assert "forge.xs_momentum.runner" in dup
    assert len(dup["forge.xs_momentum.runner"]) == 2


def test_advisory_does_not_flag_variants_with_shim_real_pairs():
    """Realistic scenario: baseline + 3 variants, each with its shim/real
    pair = 8 python processes. Advisory should flag 0 duplicates."""
    procs = []
    pid = 100
    for variant in [None, "sectors", "style", "legacy15"]:
        cmd_suffix = f" --variant {variant}" if variant else ""
        # venv shim
        procs.append({
            "ProcessId": pid,
            "ParentProcessId": 1,
            "CreationDate": "/Date(1779000000000)/",
            "CommandLine": f'"C:\\Argus\\.venv\\Scripts\\python.exe" -m forge.xs_momentum.runner{cmd_suffix} --loop',
        })
        # base-Python child
        procs.append({
            "ProcessId": pid + 1,
            "ParentProcessId": pid,
            "CreationDate": "/Date(1779000001000)/",
            "CommandLine": f'"C:\\Users\\ksmit\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" -m forge.xs_momentum.runner{cmd_suffix} --loop',
        })
        pid += 2

    with patch("subprocess.run",
               side_effect=_stub_subprocess_run(procs)):
        from ops.operational_vetting import _duplicate_process_advisory
        result = _duplicate_process_advisory()
    assert result["n_total_python_processes"] == 8
    assert result["n_modules_running"] == 4
    assert result["n_duplicate_modules"] == 0
