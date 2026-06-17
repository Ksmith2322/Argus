"""Ken French data library loader — daily Fama-French 3-factor + Momentum.

The ETF-proxy factor model in helio/factor_decomposition.py is convenient
but imperfect: IWM-SPY isn't quite SMB, IWD-IWF isn't quite HML, etc.
For load-bearing decisions (e.g. whether to keep xs_momentum at 1.0× or
reduce it) we need the actual Ken French factors.

This module downloads the two ZIPs Ken French publishes:
  - F-F_Research_Data_Factors_daily.csv  (Mkt-RF, SMB, HML, RF)
  - F-F_Momentum_Factor_daily.csv         (Mom)

Both are CSV-inside-ZIP. Daily returns are in PERCENT (we convert to
fractions on parse). The files have several lines of header text plus
a closing copyright line that we strip.

Data is cached in helio/data_fama_french/ so repeated runs don't re-hit
Dartmouth's server. Cache freshness is checked on file mtime; if older
than `cache_max_age_days` (default 30), re-download.

USAGE:
    from helio.fama_french_data import load_factors_daily, load_factors_monthly
    df_daily = load_factors_daily()   # DataFrame indexed by date, cols Mkt-RF/SMB/HML/MOM/RF
    df_monthly = load_factors_monthly()  # same but aggregated to month-end
"""
from __future__ import annotations

import io
import socket
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO / "helio" / "data_fama_french"

URL_FACTORS_3 = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
    "ftp/F-F_Research_Data_Factors_daily_CSV.zip"
)
URL_MOMENTUM = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
    "ftp/F-F_Momentum_Factor_daily_CSV.zip"
)

CACHE_FACTORS_3 = CACHE_DIR / "ff_factors_daily.csv"
CACHE_MOMENTUM = CACHE_DIR / "ff_momentum_daily.csv"

DEFAULT_CACHE_MAX_AGE_DAYS = 30
USER_AGENT = "Argus/1.0 (factor decomposition; ksmith2322@yahoo.com)"


class FamaFrenchError(RuntimeError):
    pass


# ─── download + cache ────────────────────────────────────────────────

def _is_cache_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    age_seconds = datetime.now().timestamp() - path.stat().st_mtime
    return age_seconds < max_age_days * 86400


def _download_zip(url: str, *, timeout: int = 30) -> bytes:
    """Fetch a ZIP from Dartmouth's data library + return the inner
    CSV bytes (decompressed)."""
    socket.setdefaulttimeout(timeout)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
    except Exception as exc:
        raise FamaFrenchError(f"download failed for {url}: {exc}") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                raise FamaFrenchError(f"empty zip from {url}")
            return zf.read(names[0])
    except zipfile.BadZipFile as exc:
        raise FamaFrenchError(f"bad zip from {url}: {exc}") from exc


def _ensure_cached(
    url: str, cache_path: Path,
    *,
    cache_max_age_days: int,
    force_refresh: bool = False,
) -> None:
    if not force_refresh and _is_cache_fresh(cache_path, cache_max_age_days):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    csv_bytes = _download_zip(url)
    cache_path.write_bytes(csv_bytes)


# ─── parse Ken French CSV format ─────────────────────────────────────

def _parse_kf_csv(text: str, expected_cols: list[str]) -> pd.DataFrame:
    """Ken French CSVs have N lines of header text, then a row that starts
    with a comma + the column names (e.g. ",Mkt-RF,SMB,HML,RF"), then the
    daily rows. There's also typically a copyright footer plus optional
    annual/monthly aggregated sections that we want to STOP at.

    Strategy: find the column-header row — must be a row that starts with
    a comma (unnamed date column convention) AND has each expected_col as
    a whole comma-separated token. The "in" substring check used to fire
    on prose lines like "contains a momentum factor..." that happened to
    contain "mom". Then parse until we hit a blank line or a non-date row.
    """
    lines = text.splitlines()
    expected_lower = [c.strip().lower() for c in expected_cols]
    header_idx = -1
    for i, ln in enumerate(lines):
        # The data header row in F-F CSVs always starts with a comma
        # (date column is unnamed). Reject lines that don't.
        if not ln.startswith(","):
            continue
        cols = [c.strip().lower() for c in ln.split(",")]
        if all(want in cols for want in expected_lower):
            header_idx = i
            break
    if header_idx < 0:
        raise FamaFrenchError(
            f"could not find header row starting with ',' containing "
            f"{expected_cols} in CSV"
        )

    # Parse data rows starting from header_idx+1 until empty/non-date line
    parsed_rows = []
    for ln in lines[header_idx + 1:]:
        ln = ln.strip()
        if not ln:
            break  # end of daily data block
        first = ln.split(",")[0].strip()
        # Daily date format: YYYYMMDD (8 digits)
        if not (first.isdigit() and len(first) == 8):
            break
        parsed_rows.append(ln)
    if not parsed_rows:
        raise FamaFrenchError("no daily data rows found after header")
    # Build a DataFrame from the header line + parsed rows
    header_line = lines[header_idx]
    # The header has a leading comma (date col unnamed); name it "date"
    if header_line.startswith(","):
        header_line = "date" + header_line
    df = pd.read_csv(io.StringIO(header_line + "\n" + "\n".join(parsed_rows)))
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df = df.set_index("date")
    # F-F values are in PERCENT — divide by 100 to get fractions
    for c in df.columns:
        df[c] = df[c].astype(float) / 100.0
    return df


# ─── public loaders ──────────────────────────────────────────────────

def load_factors_daily(
    *,
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Return a daily DataFrame indexed by date with columns:
       Mkt-RF, SMB, HML, MOM, RF — all as decimal fractions
       (so 0.0123 = 1.23%/day)."""
    _ensure_cached(URL_FACTORS_3, CACHE_FACTORS_3,
                    cache_max_age_days=cache_max_age_days,
                    force_refresh=force_refresh)
    _ensure_cached(URL_MOMENTUM, CACHE_MOMENTUM,
                    cache_max_age_days=cache_max_age_days,
                    force_refresh=force_refresh)
    df_3 = _parse_kf_csv(
        CACHE_FACTORS_3.read_text(encoding="utf-8", errors="replace"),
        expected_cols=["Mkt-RF", "SMB", "HML", "RF"],
    )
    df_mom = _parse_kf_csv(
        CACHE_MOMENTUM.read_text(encoding="utf-8", errors="replace"),
        expected_cols=["Mom"],
    )
    # Standardise the momentum column name to 'MOM'
    df_mom = df_mom.rename(columns={c: "MOM" for c in df_mom.columns})
    merged = df_3.join(df_mom, how="inner")
    # Standardise column names
    return merged.rename(columns={"Mkt-RF": "MKT_RF"})


def load_factors_monthly(
    *,
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Aggregate daily factor returns to month-end. Factor portfolios ARE
    compoundable (each is a real long-short portfolio return), so monthly
    return = (1 + r_d).prod() - 1 across each calendar month."""
    daily = load_factors_daily(
        cache_max_age_days=cache_max_age_days,
        force_refresh=force_refresh,
    )

    # Per-column compound (one_plus is per-day; we want product per month)
    def _compound(g: pd.Series) -> float:
        return float((1.0 + g).prod() - 1.0)

    monthly = daily.resample("ME").apply(_compound)
    return monthly


def data_freshness() -> dict:
    """Diagnostic: when was the cache last updated, what date does the data
    extend through?"""
    out: dict = {"cache_dir": str(CACHE_DIR)}
    for label, path in (("factors_3", CACHE_FACTORS_3),
                         ("momentum", CACHE_MOMENTUM)):
        if not path.exists():
            out[label] = {"cached": False}
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        # Find last data date by reading the file
        text = path.read_text(encoding="utf-8", errors="replace")
        last_date = None
        for ln in reversed(text.splitlines()):
            first = ln.split(",")[0].strip()
            if first.isdigit() and len(first) == 8:
                last_date = first
                break
        out[label] = {
            "cached": True,
            "size_bytes": path.stat().st_size,
            "cache_mtime": mtime.isoformat(),
            "last_data_date": last_date,
        }
    return out
