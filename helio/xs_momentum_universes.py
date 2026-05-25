"""Universe registry for cross-sectional momentum sweeps.

The existing `helio.xs_momentum.DEFAULT_UNIVERSE` is the production
broad-8 ETF set (PF 3.30, CI [1.86, 6.30] over 20y). This module
adds candidate universes for the disciplined-gate sweep.

Each universe is a tuple of liquid ETFs/instruments that span a
distinct cross-sectional dispersion source. Cross-sectional
momentum needs *dispersion across the universe*; a tight basket
(e.g. only mega-caps) produces no edge.

USAGE
=====
    from helio.xs_momentum_universes import CANDIDATE_UNIVERSES
    for name, tickers in CANDIDATE_UNIVERSES.items():
        result = backtest(universe_override=list(tickers), ...)
"""
from __future__ import annotations


# ─── Universe registry ──────────────────────────────────────────────

# 11 SPDR sector ETFs — within-equity rotation. Test whether sector
# dispersion drives an xs_momentum edge on its own (previously failed
# in 2026-05 testing; re-tested here at v2 fidelity).
SECTORS_SPDR_11 = (
    "XLK",  "XLF",  "XLE",  "XLV",  "XLY",
    "XLP",  "XLU",  "XLB",  "XLI",  "XLC",
    "XLRE",
)

# 7 commodity ETFs — broad commodity rotation. Tests whether
# commodity-specific momentum (rotating between metals, energy,
# agricultural) survives the gate.
COMMODITIES_7 = (
    "GLD",   # Gold
    "SLV",   # Silver
    "USO",   # WTI Crude
    "UNG",   # Natural gas
    "DBA",   # Agriculture broad
    "DBC",   # Commodity broad
    "PDBC",  # Optimum yield commodity (active rolling)
)

# 10 developed/emerging country ETFs — geographic momentum.
COUNTRIES_10 = (
    "EWJ",   # Japan
    "EWG",   # Germany
    "EWZ",   # Brazil
    "EWA",   # Australia
    "EWU",   # UK
    "EWC",   # Canada
    "EWS",   # Singapore
    "EWY",   # South Korea
    "EWT",   # Taiwan
    "EWW",   # Mexico
)

# 7 bond duration ETFs — fixed-income rotation. Tests whether
# the cross-sectional engine works on rates dispersion.
BONDS_DURATION_7 = (
    "TLT",   # Long Treasuries
    "IEF",   # 7-10y Treasuries
    "SHY",   # 1-3y Treasuries
    "AGG",   # Aggregate bond
    "LQD",   # Investment-grade corporate
    "HYG",   # High-yield corporate
    "EMB",   # Emerging-market bonds
)

# 8 style-factor ETFs — within-equity factor rotation (value/growth/
# momentum/quality/low-vol/etc.). Tests whether xs_momentum can
# rotate between FACTORS rather than instruments.
STYLE_FACTORS_8 = (
    "VTV",   # Vanguard Value
    "VUG",   # Vanguard Growth
    "VYM",   # High dividend
    "VIG",   # Dividend-growth
    "MTUM",  # iShares momentum
    "QUAL",  # iShares quality
    "USMV",  # iShares low-vol
    "VLUE",  # iShares value
)

# 7 real-asset ETFs — REITs + commodities. Different inflation-
# hedge thesis.
REAL_ASSETS_7 = (
    "VNQ",   # US REITs
    "VNQI",  # International REITs
    "GLD",   # Gold
    "SLV",   # Silver
    "USO",   # Crude
    "DBC",   # Broad commodity
    "DBA",   # Agriculture
)

# 8 international equity slices — developed/emerging/frontier
# variants. Tests whether international dispersion alone produces
# an edge (orthogonal to the broad-8 which only uses 2 international).
INTERNATIONAL_EQUITY_8 = (
    "EFA",   # MSCI EAFE (developed)
    "EEM",   # MSCI Emerging
    "VEA",   # Vanguard developed ex-US
    "VWO",   # Vanguard emerging
    "IEMG",  # iShares core emerging
    "SCHF",  # Schwab developed ex-US
    "IXUS",  # iShares total int'l ex-US
    "ACWX",  # All-country world ex-US
)

# 10 sector + 5 country mix — the legacy 15-ticker universe that
# was the live default before the 5/22 swap to broad-8. Replayed
# here so the sweep produces a head-to-head comparison.
LEGACY_SECTORS_COUNTRIES_15 = (
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP",
    "XLU", "XLB", "XLI", "XLC",
    "EWJ", "EWG", "EEM", "EFA", "DIA",
)


CANDIDATE_UNIVERSES: dict[str, tuple[str, ...]] = {
    "sectors_spdr_11":        SECTORS_SPDR_11,
    "commodities_7":          COMMODITIES_7,
    "countries_10":           COUNTRIES_10,
    "bonds_duration_7":       BONDS_DURATION_7,
    "style_factors_8":        STYLE_FACTORS_8,
    "real_assets_7":          REAL_ASSETS_7,
    "international_equity_8": INTERNATIONAL_EQUITY_8,
    "legacy_sectors_countries_15": LEGACY_SECTORS_COUNTRIES_15,
}


def get_universe(name: str) -> tuple[str, ...]:
    """Lookup a universe by name; raises KeyError if unknown."""
    return CANDIDATE_UNIVERSES[name]
