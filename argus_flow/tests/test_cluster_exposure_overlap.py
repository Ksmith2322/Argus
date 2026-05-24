from __future__ import annotations

import json


def test_cluster_exposure_reads_xs_current_picks(tmp_path, monkeypatch):
    import helio.cluster_exposure as ce

    hb_dir = tmp_path / "forge" / "logs" / "xs_momentum"
    hb_dir.mkdir(parents=True)
    (hb_dir / "heartbeat.json").write_text(
        json.dumps({
            "system": "xs_momentum",
            "current_picks": {
                "GLD": {"entry_px": 200.0, "qty": 2},
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(ce, "REPO", tmp_path)
    expo = ce.compute_cluster_exposure()

    assert expo["by_symbol"]["GLD"] == 400.0
    assert any(p["system"] == "xs_momentum" and p["symbol"] == "GLD"
               for p in expo["positions"])


def test_xs_momentum_gld_overlap_cap_blocks_combined_active_sleeves(
    tmp_path, monkeypatch
):
    import helio.cluster_exposure as ce
    import helio.fleet_sizing as fs

    gld_dir = tmp_path / "forge" / "logs" / "gld_pm_long"
    gld_dir.mkdir(parents=True)
    (gld_dir / "heartbeat.json").write_text(
        json.dumps({
            "system": "gld_pm_long",
            "open_trade": {
                "symbol": "GLD",
                "direction": "long",
                "entry_px": 300.0,
                "position_size": 1,
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(ce, "REPO", tmp_path)
    monkeypatch.setattr(fs, "get_sizing_anchor_usd", lambda: 1000.0)

    breach = ce.would_breach_strategy_overlap_cap(
        "GLD",
        "long",
        200.0,
        strategy_label="forge_xs_momentum",
    )

    assert breach == "OVERLAP:GLD_ACTIVE_SLEEVES"


def test_xs_momentum_gld_overlap_cap_ignores_other_symbols(tmp_path, monkeypatch):
    import helio.cluster_exposure as ce
    import helio.fleet_sizing as fs

    monkeypatch.setattr(ce, "REPO", tmp_path)
    monkeypatch.setattr(fs, "get_sizing_anchor_usd", lambda: 1000.0)

    assert ce.would_breach_strategy_overlap_cap(
        "TLT",
        "long",
        5000.0,
        strategy_label="forge_xs_momentum",
    ) is None
