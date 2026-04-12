"""
GDELT v2 Doc API poller for Atlas event detection.

Polls the GDELT Global Event Database for keyword-matched articles
across monetary, energy, trade, geopolitical, and crisis categories.
Deduplicates by title hash (compatible with RSS poller hashes).

Usage:
    python -m forge.atlas.sources.gdelt_poller --test
"""

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GDELT query registry
# ---------------------------------------------------------------------------

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

GDELT_QUERIES = [
    # GDELT Doc API uses space-separated keywords (AND logic).
    # Use simple, focused queries — one concept per query.
    ("federal reserve", "MONETARY"),
    ("opec oil", "ENERGY"),
    ("tariff", "TRADE"),
    ("invasion missile strike", "GEOPOLITICAL"),
    ("sanctions imposed", "TRADE"),
    ("bank failure", "FINANCIAL"),
    ("inflation CPI", "ECONOMIC_DATA"),
    ("pandemic outbreak", "BLACK_SWAN"),
    ("strait hormuz", "ENERGY"),
    ("trade war", "TRADE"),
]

USER_AGENT = "Atlas/1.0 (news aggregator; +https://github.com/Ksmith2322/Argus)"
REQUEST_TIMEOUT = 15  # seconds
RATE_LIMIT_DELAY = 5.0  # seconds between queries (GDELT throttles aggressively)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_dedupe_hash(title: str) -> str:
    """SHA256 of lowercased, stripped, whitespace-normalized title.

    Matches the hash used by rss_poller so cross-source duplicates
    are caught when items reach the Atlas DB.
    """
    normalized = " ".join(title.lower().strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_gdelt_date(date_str: str) -> str:
    """Convert GDELT seendate (YYYYMMDDTHHmmSSZ) to ISO8601.

    Falls back to current UTC if parsing fails.
    """
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    try:
        # GDELT dates look like "20260411T143000Z"
        dt = datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        pass
    # Some GDELT dates may come without the trailing Z
    try:
        dt = datetime.strptime(date_str.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def poll_gdelt_query(
    query: str,
    max_records: int = 75,
    category_hint: str = "",
) -> list[dict]:
    """Query the GDELT Doc API and return normalized article list.

    Parameters
    ----------
    query : str
        GDELT query string (supports OR, +, sourcelang: etc.).
    max_records : int
        Maximum articles to return (API caps at 250).
    category_hint : str
        Atlas category tag attached to every result.

    Returns
    -------
    list[dict]
        Each dict has: title, url, published, source, category_hint, dedupe_hash
    """
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(min(max_records, 250)),
        "format": "json",
    }
    url = f"{GDELT_DOC_API}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        logger.error("GDELT HTTP %d for query '%s': %s", exc.code, query, exc.reason)
        return []
    except urllib.error.URLError as exc:
        logger.error("GDELT URL error for query '%s': %s", query, exc.reason)
        return []
    except Exception as exc:
        logger.error("GDELT request failed for query '%s': %s", query, exc)
        return []

    # Parse JSON response
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("GDELT JSON decode error for query '%s': %s", query, exc)
        return []

    # The Doc API returns {"articles": [...]} or an empty object
    articles = data.get("articles") or []
    if not isinstance(articles, list):
        logger.warning("GDELT unexpected response structure for query '%s'", query)
        return []

    results: list[dict] = []
    for article in articles:
        title = (article.get("title") or "").strip()
        if not title:
            continue
        results.append({
            "title": title,
            "url": article.get("url", ""),
            "published": _parse_gdelt_date(article.get("seendate", "")),
            "source": article.get("domain", "gdelt"),
            "category_hint": category_hint,
            "dedupe_hash": compute_dedupe_hash(title),
        })

    return results


def poll_all_gdelt(
    queries: Optional[list[tuple[str, str]]] = None,
    rate_limit: float = RATE_LIMIT_DELAY,
    max_records_per_query: int = 75,
) -> list[dict]:
    """Run all GDELT queries, merge results, and deduplicate.

    Parameters
    ----------
    queries : list of (query_str, category) tuples, optional
        Override default GDELT_QUERIES.
    rate_limit : float
        Seconds to sleep between API requests.
    max_records_per_query : int
        Max articles per query.

    Returns
    -------
    list[dict]
        Deduplicated articles sorted by published date (newest first).
    """
    queries = queries or GDELT_QUERIES
    all_items: list[dict] = []
    seen_hashes: set[str] = set()

    for idx, (query, category) in enumerate(queries):
        logger.info(
            "GDELT query %d/%d [%s]: %s",
            idx + 1, len(queries), category, query,
        )
        items = poll_gdelt_query(
            query=query,
            max_records=max_records_per_query,
            category_hint=category,
        )
        for item in items:
            if item["dedupe_hash"] not in seen_hashes:
                seen_hashes.add(item["dedupe_hash"])
                all_items.append(item)

        # Rate limit between requests (skip after last query)
        if idx < len(queries) - 1:
            time.sleep(rate_limit)

    # Sort newest first
    all_items.sort(key=lambda x: x["published"], reverse=True)
    logger.info(
        "GDELT polled %d queries -> %d unique articles",
        len(queries), len(all_items),
    )
    return all_items


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test_mode():
    """Poll all GDELT queries once and print results."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    items = poll_all_gdelt()
    print(f"\n{'='*80}")
    print(f"GDELT Total unique articles: {len(items)}")
    print(f"{'='*80}\n")

    # Category breakdown
    categories: dict[str, int] = {}
    for item in items:
        cat = item["category_hint"]
        categories[cat] = categories.get(cat, 0) + 1
    for cat, count in sorted(categories.items()):
        print(f"  {cat}: {count} articles")
    print()

    for item in items[:30]:  # Print first 30
        title = item['title'].encode('ascii', 'replace').decode('ascii')
        print(f"[{item['category_hint']}] {item['published']}")
        print(f"  {title}")
        print(f"  source: {item['source']}")
        print()


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _test_mode()
    else:
        print("Usage: python -m forge.atlas.sources.gdelt_poller --test")
        sys.exit(1)
