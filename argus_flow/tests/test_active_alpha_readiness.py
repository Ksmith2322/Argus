from __future__ import annotations

from ops.audit.run_active_alpha_readiness import _strategy_status


def test_strategy_status_blocks_on_red_preflight():
    status = _strategy_status(
        ["forge_xs_momentum"],
        {
            "forge_xs_momentum": {
                "checks": [
                    {"name": "live_evidence_n", "verdict": "RED"},
                ],
            },
        },
        {"per_strategy": {"forge_xs_momentum": {"exits": 50}}},
        {"by_strategy": {}},
    )

    row = status["forge_xs_momentum"]
    assert row["verdict"] == "BLOCKED"
    assert row["reasons"] == ["preflight_red:live_evidence_n"]


def test_strategy_status_surfaces_cap_and_data_blocks_after_evidence():
    status = _strategy_status(
        ["forge_xs_momentum"],
        {"forge_xs_momentum": {"checks": []}},
        {"per_strategy": {"forge_xs_momentum": {"exits": 25}}},
        {
            "by_strategy": {
                "forge_xs_momentum": {
                    "CAP_EXCEEDED": 2,
                    "DATA_UNAVAILABLE": 1,
                },
            },
        },
    )

    row = status["forge_xs_momentum"]
    assert row["verdict"] == "GATED_REVIEW"
    assert "cap_blocks_30d:2" in row["reasons"]
    assert "data_blocks_30d:1" in row["reasons"]
