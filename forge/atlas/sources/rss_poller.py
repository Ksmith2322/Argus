"""
RSS feed polling engine for Atlas event detection.

Polls financial/geopolitical news feeds, normalizes entries,
and deduplicates by title hash.

Usage:
    python -m forge.atlas.sources.rss_poller --test
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import feedparser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

FEEDS = {
    "reuters_world": "https://feeds.reuters.com/Reuters/worldNews",
    "reuters_business": "https://feeds.reuters.com/Reuters/businessNews",
    "cnbc_economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "cnbc_finance": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "bbc_world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "marketwatch_top": "http://feeds.marketwatch.com/marketwatch/topstories/",
    "ft_world": "https://www.ft.com/rss/home/uk",
    "google_fomc": "https://news.google.com/rss/search?q=FOMC+federal+reserve&hl=en-US&gl=US&ceid=US:en",
    "google_opec": "https://news.google.com/rss/search?q=OPEC+oil+production&hl=en-US&gl=US&ceid=US:en",
    "google_tariff": "https://news.google.com/rss/search?q=tariff+trade+war&hl=en-US&gl=US&ceid=US:en",
    "google_geopolitical": "https://news.google.com/rss/search?q=war+conflict+sanctions&hl=en-US&gl=US&ceid=US:en",
    "google_cpi": "https://news.google.com/rss/search?q=CPI+inflation+consumer+price&hl=en-US&gl=US&ceid=US:en",
}

POLL_TIMEOUT = 10  # seconds
RATE_LIMIT_DELAY = 1.0  # seconds between feed requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_dedupe_hash(title: str) -> str:
    """SHA256 of lowercased, stripped, whitespace-normalized title."""
    normalized = " ".join(title.lower().strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_timestamp(entry) -> str:
    """Convert feedparser time struct to UTC ISO8601 string.

    Falls back to current UTC time if the entry has no parseable date.
    """
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        try:
            dt = datetime(*time_struct[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def _clean_html(text: str) -> str:
    """Strip basic HTML tags from summary text."""
    import re
    clean = re.sub(r"<[^>]+>", "", text or "")
    return " ".join(clean.split()).strip()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def poll_feed(feed_name: str, feed_url: str) -> list[dict]:
    """Poll a single RSS/Atom feed and return normalized entries.

    Returns list of dicts with keys:
        title, link, published, summary, source, dedupe_hash
    """
    try:
        parsed = feedparser.parse(feed_url, request_headers={
            "User-Agent": "Atlas/1.0 (news aggregator)"
        })
        # feedparser doesn't natively support a socket timeout; we rely on
        # urllib's default or the OS timeout.  For robustness we check the
        # bozo flag but still try to use partial results.
        if parsed.bozo and not parsed.entries:
            logger.warning("Feed %s returned bozo with no entries: %s",
                           feed_name, parsed.bozo_exception)
            return []
    except Exception as exc:
        logger.error("Failed to fetch feed %s: %s", feed_name, exc)
        return []

    results: list[dict] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        results.append({
            "title": title,
            "link": entry.get("link", ""),
            "published": _normalize_timestamp(entry),
            "summary": _clean_html(entry.get("summary", "")),
            "source": feed_name,
            "dedupe_hash": compute_dedupe_hash(title),
        })
    return results


def poll_all_feeds(
    feeds: Optional[dict[str, str]] = None,
    rate_limit: float = RATE_LIMIT_DELAY,
    max_age_hours: int = 48,
) -> list[dict]:
    """Poll all feeds, merge results, and deduplicate by title hash.

    Parameters
    ----------
    feeds : dict, optional
        Override feed registry (defaults to FEEDS).
    rate_limit : float
        Seconds to sleep between feed requests.
    max_age_hours : int
        Discard items older than this many hours (default 48).

    Returns
    -------
    list[dict]
        Deduplicated list of news items sorted by published date (newest first).
    """
    feeds = feeds or FEEDS
    all_items: list[dict] = []
    seen_hashes: set[str] = set()

    # Compute age cutoff
    cutoff = ""
    if max_age_hours > 0:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

    for idx, (name, url) in enumerate(feeds.items()):
        logger.info("Polling feed %s (%d/%d)...", name, idx + 1, len(feeds))
        items = poll_feed(name, url)
        for item in items:
            if item["dedupe_hash"] in seen_hashes:
                continue
            # Skip old articles
            if cutoff and item.get("published", "") < cutoff:
                continue
            seen_hashes.add(item["dedupe_hash"])
            all_items.append(item)
        # Rate limit between requests (skip after last feed)
        if idx < len(feeds) - 1:
            time.sleep(rate_limit)

    # Sort newest first
    all_items.sort(key=lambda x: x["published"], reverse=True)
    logger.info("Polled %d feeds -> %d unique items (max_age=%dh)", len(feeds), len(all_items), max_age_hours)
    return all_items


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test_mode():
    """Poll all feeds once and print results."""
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    items = poll_all_feeds()
    print(f"\n{'='*80}")
    print(f"Total unique items: {len(items)}")
    print(f"{'='*80}\n")

    for item in items[:30]:  # Print first 30
        print(f"[{item['source']}] {item['published']}")
        print(f"  {item['title']}")
        print(f"  {item['link']}")
        if item["summary"]:
            print(f"  Summary: {item['summary'][:120]}...")
        print()

    # Also dump full JSON for piping
    print(f"\n--- Full JSON ({len(items)} items) ---")
    print(json.dumps(items, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _test_mode()
    else:
        print("Usage: python -m forge.atlas.sources.rss_poller --test")
        sys.exit(1)
