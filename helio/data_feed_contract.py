"""Production data-feed contract registry (Codex gap #1).

Each daily-bar strategy declares an explicit contract:
  - primary source (where live runs pull from)
  - fallback source (where backtests + cache-recovery pull from)
  - required universe (which tickers MUST have data)
  - freshness budget (max age before the data is considered RED)
  - schema (which columns are required; auto_adjust setting)

A contract is the offline, machine-checkable answer to:
    "Is the data this strategy depends on actually present, fresh,
    and shaped the way we promised?"

This is intentionally SEPARATE from `runner.data_diagnostics()`:
    - data_diagnostics runs at runtime, hits the live source, and is
      best-effort — yfinance going down counts as YELLOW.
    - verify_contract runs offline against the on-disk fallback cache.
      The cache is the production source of truth: if the cache is
      empty/stale/missing tickers, the strategy CANNOT survive a live
      data outage. RED here means "you have no insurance."

USAGE
=====
    from helio.data_feed_contract import (
        CONTRACTS, verify_contract, verify_all_contracts,
    )
    verdict = verify_contract(CONTRACTS["forge_xs_momentum"])

OUTPUT shape per contract:
    {
        "strategy": "forge_xs_momentum",
        "verdict": "GREEN" | "YELLOW" | "RED",
        "reasons": [...],
        "universe_required": [tickers],
        "universe_present": [tickers],
        "universe_missing": [tickers],
        "stale_tickers": [{"ticker": t, "age_days": d}],
        "max_age_days": 7,
    }
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd


_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_CACHE_ROOT = _REPO / "helio" / "data_yfinance"


# ─── Contract dataclass ─────────────────────────────────────────────

@dataclass(frozen=True)
class DataFeedContract:
    """A strategy's machine-checkable data-feed contract.

    primary_source: identifier of the live data source ("yfinance",
        "ibkr_bars", "massive", etc.). Informational — the verifier
        does NOT hit the network.

    fallback_cache_root: directory holding the offline CSV cache. The
        verifier checks <root>/<TICKER>_daily.csv exists and is recent
        enough.

    universe: list of tickers the strategy NEEDS. Missing any of these
        is a RED — the strategy cannot make a complete decision.

    max_age_days: any cached file with a most-recent Date older than
        this is "stale" and counts toward the RED tally. Defaults to 7
        (covers a long weekend + a market holiday).

    required_columns: must exist in the CSV. Default Close-only since
        most ranking strategies just need adjusted/raw closes.

    auto_adjust: documents the auto_adjust setting under which the
        promotion_gate baseline was computed. The verifier does NOT
        check this — it's a pin so the operator can audit it.

    notes: free-form context (which baseline this matches, why these
        tickers, etc.).
    """

    strategy: str
    primary_source: str
    fallback_cache_root: Path
    universe: tuple[str, ...]
    max_age_days: int = 7
    required_columns: tuple[str, ...] = ("Close",)
    auto_adjust: bool = False
    notes: str = ""

    def cache_path_for(self, ticker: str) -> Path:
        safe = ticker.upper().replace("/", "_").replace("=", "_").replace("^", "_")
        return self.fallback_cache_root / f"{safe}_daily.csv"


# ─── Registry ───────────────────────────────────────────────────────

# Universe pinned to PARAMS['universe'] in forge/xs_momentum/runner.py
# Broad-8 cross-asset ETFs (the disciplined-gate survivor universe per
# project_2026_05_22_backtest_factory_findings.md).
_XS_MOMENTUM_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT",
)

# 2026-05-24 universe-expansion variants — each surviving universe is
# importable from helio.xs_momentum_universes. The variant runners
# share the same cache root because every universe ultimately resolves
# to yfinance daily ETF closes.
try:
    from helio.xs_momentum_universes import (
        SECTORS_SPDR_11 as _SECTORS_SPDR_11,
        STYLE_FACTORS_8 as _STYLE_FACTORS_8,
        LEGACY_SECTORS_COUNTRIES_15 as _LEGACY_15,
    )
except Exception:  # registry not yet importable in some test contexts
    _SECTORS_SPDR_11 = ()
    _STYLE_FACTORS_8 = ()
    _LEGACY_15 = ()


CONTRACTS: dict[str, DataFeedContract] = {
    "forge_xs_momentum": DataFeedContract(
        strategy="forge_xs_momentum",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=_XS_MOMENTUM_UNIVERSE,
        max_age_days=7,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "Baseline CI=[1.86,6.30] computed at auto_adjust=False; "
            "live runner must stay consistent. See helio/yfinance_data.py "
            "for the repo-wide auto_adjust convention block."
        ),
    ),
    "forge_gld_pm_long": DataFeedContract(
        strategy="forge_gld_pm_long",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=("GLD",),
        max_age_days=3,
        required_columns=("Close",),
        auto_adjust=False,
        notes="GLD daily close drives the PM-long entry/exit gate.",
    ),
    "forge_tom_spy": DataFeedContract(
        strategy="forge_tom_spy",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=("SPY",),
        max_age_days=3,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "PENDING_OPT_IN. Turn-of-month SPY (last 3-4 trading days + "
            "first 1-2). PARTIAL_PASS 6/9 layers; CI lower 1.22 @ 10bp. "
            "Recommended allocation 0.3x once activated."
        ),
    ),
    "forge_nov_spy": DataFeedContract(
        strategy="forge_nov_spy",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=("SPY",),
        max_age_days=3,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "PENDING_OPT_IN. November-only SPY (single-month strategy). "
            "MARGINAL_PASS 8/9 layers; CI lower 1.75 @ 10bp. First "
            "live action 2026-11-02; allocation 0.2x recommended."
        ),
    ),
    "forge_xs_momentum_sectors": DataFeedContract(
        strategy="forge_xs_momentum_sectors",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=_SECTORS_SPDR_11,
        max_age_days=7,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "20y disciplined gate: PF 2.50 CI [1.44, 4.53], DD 50%. "
            "Same engine as broad-8 xs_momentum, sector universe (11 SPDRs)."
        ),
    ),
    "forge_xs_momentum_style": DataFeedContract(
        strategy="forge_xs_momentum_style",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=_STYLE_FACTORS_8,
        max_age_days=7,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "20y disciplined gate: PF 3.78 CI [1.85, 8.55], DD 34% "
            "(best risk profile of the variants). Style factors: "
            "VTV/VUG/VYM/VIG/MTUM/QUAL/USMV/VLUE."
        ),
    ),
    "forge_xs_momentum_legacy15": DataFeedContract(
        strategy="forge_xs_momentum_legacy15",
        primary_source="yfinance",
        fallback_cache_root=_DEFAULT_CACHE_ROOT,
        universe=_LEGACY_15,
        max_age_days=7,
        required_columns=("Close",),
        auto_adjust=False,
        notes=(
            "20y disciplined gate: PF 2.22 CI [1.38, 3.75], DD 65%. "
            "15-ticker pre-5/22 default universe (10 sectors + 5 country). "
            "Accept the higher DD because the universe size enables larger "
            "trade count (n=165 over 20y)."
        ),
    ),
}


# ─── Verification ────────────────────────────────────────────────────

@dataclass
class ContractVerdict:
    strategy: str
    verdict: str  # GREEN / YELLOW / RED
    reasons: list[str] = field(default_factory=list)
    universe_required: list[str] = field(default_factory=list)
    universe_present: list[str] = field(default_factory=list)
    universe_missing: list[str] = field(default_factory=list)
    stale_tickers: list[dict] = field(default_factory=list)
    max_age_days: int = 0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "universe_required": list(self.universe_required),
            "universe_present": list(self.universe_present),
            "universe_missing": list(self.universe_missing),
            "stale_tickers": list(self.stale_tickers),
            "max_age_days": self.max_age_days,
        }


def _latest_date_in_csv(path: Path) -> datetime | None:
    """Read just the last data row of the CSV to get the most-recent
    bar Date without parsing the whole file."""
    if not path.exists():
        return None
    try:
        # Lightweight: read CSV with pandas (idiomatic + handles header
        # well). The files are bounded (~10y * 252 ≈ 2520 rows).
        df = pd.read_csv(path, usecols=["Date"], parse_dates=["Date"])
        if df.empty:
            return None
        latest = df["Date"].max()
        if pd.isna(latest):
            return None
        ts = pd.Timestamp(latest)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def _has_required_columns(path: Path, required: Iterable[str]) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        return all(col in header for col in required)
    except Exception:
        return False


def verify_contract(
    contract: DataFeedContract,
    *,
    now: datetime | None = None,
) -> ContractVerdict:
    """Offline check of one contract against its fallback cache.

    Returns GREEN if every ticker in the universe has a present, fresh,
    schema-correct CSV in the fallback cache. YELLOW if some are stale
    but at least one is fresh. RED if any tickers are missing entirely
    or the cache is uniformly stale (no insurance against live outage).
    """
    now = now or datetime.now(timezone.utc)
    required = list(contract.universe)
    present: list[str] = []
    missing: list[str] = []
    stale: list[dict] = []
    reasons: list[str] = []

    for ticker in required:
        path = contract.cache_path_for(ticker)
        if not path.exists():
            missing.append(ticker)
            continue
        if not _has_required_columns(path, contract.required_columns):
            missing.append(ticker)
            reasons.append(
                f"{ticker}: cache missing required columns "
                f"{list(contract.required_columns)}"
            )
            continue
        latest = _latest_date_in_csv(path)
        if latest is None:
            missing.append(ticker)
            reasons.append(f"{ticker}: cache exists but no readable Date")
            continue
        age_days = max(0.0, (now - latest).total_seconds() / 86400.0)
        present.append(ticker)
        if age_days > contract.max_age_days:
            stale.append({"ticker": ticker, "age_days": round(age_days, 2)})

    if missing:
        verdict = "RED"
        reasons.append(
            f"{len(missing)} of {len(required)} tickers missing from cache "
            f"({', '.join(sorted(missing))})"
        )
    elif stale and len(stale) == len(required):
        verdict = "RED"
        reasons.append(
            f"all {len(required)} cached tickers stale (>{contract.max_age_days}d)"
        )
    elif stale:
        verdict = "YELLOW"
        reasons.append(
            f"{len(stale)} of {len(required)} cached tickers stale "
            f"(>{contract.max_age_days}d)"
        )
    else:
        verdict = "GREEN"

    return ContractVerdict(
        strategy=contract.strategy,
        verdict=verdict,
        reasons=reasons,
        universe_required=required,
        universe_present=present,
        universe_missing=missing,
        stale_tickers=stale,
        max_age_days=contract.max_age_days,
    )


def verify_all_contracts(
    *,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Verify every contract in the registry. Returns
    {strategy_name: verdict_dict}."""
    return {
        name: verify_contract(c, now=now).to_dict()
        for name, c in CONTRACTS.items()
    }
