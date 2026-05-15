#!/usr/bin/env python3
"""helio/edgar_catalysts.py — SEC EDGAR filings adapter.

Pulls structured filings data from SEC EDGAR for every ticker in a breakout
universe and produces a per-ticker timeline of filings. Used to:

  1. Auto-label the catalyst column on `all_breakouts_*.csv` (so we can
     re-run forward-return analysis CONDITIONAL on catalyst type — the
     unlock for finding signal that overall averaging hides).
  2. Power the S-3 dilution short scanner (Agent 4's highest-EV systematic
     play in the sub-$10 cohort).

EDGAR fair-access rules:
  - Max 10 requests/sec (we go slower to be polite).
  - User-Agent header REQUIRED with a contact email or SEC will block.
  - No API key needed for the JSON submissions endpoint.

Filing forms we care about (catalyst taxonomy):
  S-3, S-1, S-3ASR, S-1/A    → SHELF / dilution registration (lead time risk)
  424B5, 424B3, 424B2         → DILUTION (actual sale prospectus, near-term)
  8-K (item 2.02 = earnings)  → EARNINGS catalyst
  8-K (item 1.01 = material)  → M&A / contract event
  10-Q, 10-K                  → SCHEDULED earnings report
  SC 13D, SC 13G              → Institutional ownership change
  Form 4                      → Insider trading

Usage:
    python -m helio.edgar_catalysts --build-cik-map
    python -m helio.edgar_catalysts --fetch --universe sub10
    python -m helio.edgar_catalysts --enrich --suffix _sub10
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

REPO = Path(__file__).resolve().parents[1]
EDGAR_DIR = REPO / "helio" / "data" / "edgar"
CIK_MAP_PATH = EDGAR_DIR / "ticker_cik_map.json"
FILINGS_DIR = EDGAR_DIR / "filings"
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"

# Hard-coded operator email for the User-Agent per SEC fair-access policy.
# SEC requires a contact email; without it, requests return 403.
SEC_USER_AGENT = "Argus Research (ksmith2322@yahoo.com)"

# Throttle: SEC allows 10 req/s; we use 5 req/s to leave headroom.
REQUEST_DELAY_S = 0.2

# Form taxonomy — maps SEC form code to our catalyst category.
FORM_CATEGORY = {
    # Dilution / shelf
    "S-3": "shelf_registration",
    "S-3/A": "shelf_registration",
    "S-3ASR": "shelf_registration",
    "S-1": "primary_registration",
    "S-1/A": "primary_registration",
    "424B5": "dilution_prospectus",
    "424B3": "dilution_prospectus",
    "424B2": "dilution_prospectus",
    "424B4": "dilution_prospectus",
    # Earnings / current events
    "8-K": "current_event_8k",   # need item parsing for sub-category
    "10-Q": "quarterly_report",
    "10-K": "annual_report",
    # Ownership
    "SC 13D": "institutional_change",
    "SC 13G": "institutional_change",
    "SC 13D/A": "institutional_change",
    "SC 13G/A": "institutional_change",
    "4": "insider_transaction",
    "3": "insider_transaction",
}


@dataclass
class Filing:
    """One filing event."""
    ticker: str
    cik: int
    form: str
    filing_date: str          # YYYY-MM-DD
    accession_number: str
    category: str             # our taxonomy
    primary_doc: str = ""     # filename of the actual document (for items lookup later)


def _http_get_json(url: str, retries: int = 3) -> dict | None:
    """GET a JSON URL with SEC-compliant headers. Returns None on failure."""
    req = Request(url, headers={
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": url.split("//")[1].split("/")[0],
    })
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                # SEC sometimes serves gzip even without us asking
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
        except (URLError, json.JSONDecodeError, ValueError):
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def build_cik_map() -> dict[str, int]:
    """Download the SEC ticker -> CIK mapping. Cache locally; refresh on demand.
    Returns dict {TICKER_UPPER: cik_int}."""
    EDGAR_DIR.mkdir(parents=True, exist_ok=True)
    url = "https://www.sec.gov/files/company_tickers.json"
    print(f"Downloading ticker->CIK map from {url}...")
    data = _http_get_json(url)
    if not data:
        print("  FAILED to fetch CIK map.")
        return {}
    # SEC JSON format: {"0": {"cik_str": int, "ticker": "STR", "title": "..."}, ...}
    ticker_to_cik: dict[str, int] = {}
    for _, row in data.items():
        try:
            ticker_to_cik[str(row["ticker"]).upper()] = int(row["cik_str"])
        except (KeyError, ValueError, TypeError):
            continue
    CIK_MAP_PATH.write_text(json.dumps(ticker_to_cik, indent=2))
    print(f"  Saved {len(ticker_to_cik)} ticker->CIK mappings to {CIK_MAP_PATH}")
    return ticker_to_cik


def load_cik_map() -> dict[str, int]:
    """Load cached ticker->CIK map. Builds if missing."""
    if not CIK_MAP_PATH.exists():
        return build_cik_map()
    try:
        return json.loads(CIK_MAP_PATH.read_text())
    except Exception:
        return build_cik_map()


def fetch_filings_for_cik(cik: int) -> list[dict]:
    """Pull the SEC submissions JSON for a CIK. Returns list of filing rows
    (each row is a dict with form, filingDate, accessionNumber, primaryDocument).
    """
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    data = _http_get_json(url)
    if not data:
        return []
    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []
    # SEC submissions JSON returns parallel arrays — zip them
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    rows = []
    for i in range(min(len(forms), len(dates), len(accessions))):
        rows.append({
            "form": forms[i],
            "filing_date": dates[i],
            "accession_number": accessions[i],
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
        })
    return rows


def fetch_filings_for_ticker(ticker: str, cik_map: dict[str, int] | None = None,
                               since_date: str | None = None) -> list[Filing]:
    """Fetch SEC filings for a ticker. Returns list of Filing objects.

    since_date: ISO date string. Filings older than this are filtered out.
    Default = 3 years ago (covers our 2y breakout window + buffer).
    """
    if cik_map is None:
        cik_map = load_cik_map()
    cik = cik_map.get(ticker.upper())
    if cik is None:
        return []
    raw = fetch_filings_for_cik(cik)
    if since_date is None:
        # 3 years ago
        from datetime import timedelta
        since_date = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%d")
    filings: list[Filing] = []
    for r in raw:
        if r["filing_date"] < since_date:
            continue
        form = r["form"]
        category = FORM_CATEGORY.get(form, "other")
        filings.append(Filing(
            ticker=ticker.upper(),
            cik=cik,
            form=form,
            filing_date=r["filing_date"],
            accession_number=r["accession_number"],
            category=category,
            primary_doc=r.get("primary_document", ""),
        ))
    return filings


def bulk_fetch(tickers: list[str], output_path: Path | None = None) -> dict[str, list[Filing]]:
    """Fetch filings for many tickers (throttled). Returns {ticker: [Filings]}."""
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    cik_map = load_cik_map()
    results: dict[str, list[Filing]] = {}
    print(f"Fetching SEC filings for {len(tickers)} tickers (throttled to {1/REQUEST_DELAY_S:.0f} req/s)...")
    n_ok = 0
    n_no_cik = 0
    n_no_filings = 0
    for i, sym in enumerate(tickers, 1):
        time.sleep(REQUEST_DELAY_S)
        if sym.upper() not in cik_map:
            n_no_cik += 1
            results[sym] = []
            continue
        f_list = fetch_filings_for_ticker(sym, cik_map)
        if not f_list:
            n_no_filings += 1
        else:
            n_ok += 1
        results[sym] = f_list
        # Cache per-ticker so re-runs are fast
        per_ticker_path = FILINGS_DIR / f"{sym}_filings.json"
        per_ticker_path.write_text(json.dumps(
            [{"form": f.form, "filing_date": f.filing_date,
              "accession_number": f.accession_number, "category": f.category,
              "primary_doc": f.primary_doc, "cik": f.cik}
             for f in f_list], indent=2
        ))
        if i % 10 == 0:
            print(f"  {i}/{len(tickers)} done...")
    print(f"  Complete: {n_ok} OK, {n_no_cik} no CIK, {n_no_filings} no filings")
    if output_path:
        all_filings = {sym: [f.__dict__ for f in fl] for sym, fl in results.items()}
        output_path.write_text(json.dumps(all_filings, indent=2, default=str))
    return results


def label_breakout_catalyst(symbol: str, breakout_date: str,
                              window_days_before: int = 5,
                              window_days_after: int = 1,
                              filings_cache: dict[str, list[dict]] | None = None) -> str:
    """Look up filings around a breakout date and return the dominant catalyst.

    A breakout that fires within [breakout_date - window_days_before,
    breakout_date + window_days_after] of a relevant filing is tagged with
    that filing's category. Priority order (most-impactful first):
      dilution_prospectus > shelf_registration > current_event_8k >
      quarterly_report > annual_report > institutional_change > primary_registration > other

    If no filings within window, returns "none" (technical move, no catalyst).
    """
    # Load filings (from in-mem cache or disk)
    if filings_cache is None:
        path = FILINGS_DIR / f"{symbol}_filings.json"
        if not path.exists():
            return "unknown"
        try:
            filings = json.loads(path.read_text())
        except Exception:
            return "unknown"
    else:
        filings = filings_cache.get(symbol, [])

    if not filings:
        return "none"

    try:
        bd = datetime.strptime(breakout_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return "unknown"

    priority = [
        "dilution_prospectus",
        "shelf_registration",
        "current_event_8k",
        "quarterly_report",
        "annual_report",
        "institutional_change",
        "primary_registration",
        "insider_transaction",
        "other",
    ]

    found_categories: set[str] = set()
    for f in filings:
        try:
            fd = datetime.strptime(f["filing_date"][:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        delta_days = (bd - fd).days
        if -window_days_after <= delta_days <= window_days_before:
            found_categories.add(f.get("category", "other"))

    if not found_categories:
        return "none"
    for p in priority:
        if p in found_categories:
            return p
    return "other"


def enrich_breakouts_csv(suffix: str = "_sub10",
                          window_days_before: int = 5,
                          window_days_after: int = 1) -> Path:
    """Read all_breakouts{suffix}.csv, fill in catalyst_label column, save.

    A breakout's catalyst is set to the highest-priority SEC filing within
    [date - window_days_before, date + window_days_after]. Defaults: 5 days
    before, 1 day after. The 5-day-before captures S-3 file → 1-3 day fade
    pattern. The 1-day-after captures end-of-day filings that triggered the
    next-day move.
    """
    import csv
    in_path = RESULTS_DIR / f"all_breakouts{suffix}.csv"
    if not in_path.exists():
        raise FileNotFoundError(f"{in_path} not found")

    # Bulk-load every ticker's filings once
    print(f"Loading filings cache from {FILINGS_DIR}...")
    cache: dict[str, list[dict]] = {}
    for path in FILINGS_DIR.glob("*_filings.json"):
        sym = path.stem.replace("_filings", "")
        try:
            cache[sym] = json.loads(path.read_text())
        except Exception:
            cache[sym] = []
    print(f"  Loaded filings for {len(cache)} tickers")

    # Read input CSV
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"Enriching {len(rows)} breakouts with catalyst labels...")
    label_counts: dict[str, int] = {}
    for r in rows:
        lbl = label_breakout_catalyst(
            r["symbol"], r["date"],
            window_days_before=window_days_before,
            window_days_after=window_days_after,
            filings_cache=cache,
        )
        r["catalyst_label"] = lbl
        label_counts[lbl] = label_counts.get(lbl, 0) + 1

    # Save enriched CSV (overwrites the original)
    fieldnames = list(rows[0].keys()) if rows else []
    with open(in_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"  Saved enriched CSV: {in_path}")
    print(f"\nCatalyst distribution:")
    total = len(rows)
    for lbl, cnt in sorted(label_counts.items(), key=lambda x: -x[1]):
        pct = 100 * cnt / total if total else 0
        print(f"  {lbl:25s}  {cnt:6d}  ({pct:5.1f}%)")
    return in_path


def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR Catalyst Adapter")
    parser.add_argument("--build-cik-map", action="store_true",
                        help="Refresh ticker->CIK mapping from SEC")
    parser.add_argument("--fetch", action="store_true",
                        help="Fetch filings for the universe")
    parser.add_argument("--universe", choices=["mainline", "sub10", "all"], default="sub10",
                        help="Which universe to fetch filings for")
    parser.add_argument("--universe-file", default=None,
                        help="Path to a text file with one ticker per line. Overrides --universe.")
    parser.add_argument("--enrich", action="store_true",
                        help="Backfill catalyst_label on all_breakouts CSV")
    parser.add_argument("--suffix", default="_sub10",
                        help="Suffix on the breakout CSV (default: _sub10)")
    args = parser.parse_args()

    if args.build_cik_map:
        build_cik_map()

    if args.fetch:
        if args.universe_file:
            from pathlib import Path as _P
            u_path = _P(args.universe_file)
            tickers = [t.strip().upper() for t in u_path.read_text().splitlines() if t.strip()]
            universe_label = u_path.stem
        else:
            from helio.breakout_research import UNIVERSES
            u = UNIVERSES.get(args.universe, {})
            tickers = list(u.keys())
            universe_label = args.universe
        print(f"Fetching SEC filings for {universe_label} ({len(tickers)} tickers)")
        bulk_fetch(tickers, output_path=EDGAR_DIR / f"all_filings_{universe_label}.json")

    if args.enrich:
        enrich_breakouts_csv(suffix=args.suffix)

    if not any([args.build_cik_map, args.fetch, args.enrich]):
        parser.print_help()


if __name__ == "__main__":
    main()
