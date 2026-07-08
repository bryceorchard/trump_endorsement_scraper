"""
rss.py — Collector for news RSS feeds filtered to Trump-related items.

Uses feedparser to read standard RSS/Atom feeds and only stores entries
that contain at least one keyword from RSS_FILTER_KEYWORDS (default: "trump", "donald").

Add more feeds by setting RSS_FEEDS_JSON in your .env.
"""

import hashlib
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests

from config import config
from .base import BaseCollector, CollectedItem

logger = logging.getLogger(__name__)


def _parse_date(entry) -> Optional[datetime]:
    """Try several feedparser date fields and return a timezone-aware datetime."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                import time as _time
                ts = _time.mktime(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass

    # Fallback: raw string fields
    for field in ("published", "updated"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except Exception:
                pass

    return None


def _is_relevant(entry) -> bool:
    """Return True if the entry contains at least one filter keyword (case-insensitive)."""
    text = " ".join([
        getattr(entry, "title", ""),
        getattr(entry, "summary", ""),
        getattr(entry, "content", [{}])[0].get("value", "") if hasattr(entry, "content") else "",
    ]).lower()
    return any(kw.lower() in text for kw in config.RSS_FILTER_KEYWORDS)


class RSSCollector(BaseCollector):
    source_name = "rss"

    def __init__(self):
        # feedparser uses its own HTTP stack but respects User-Agent via a kwarg
        self._ua = config.USER_AGENT

    def _fetch_feed(self, feed_url: str) -> list[CollectedItem]:
        """Parse a single RSS/Atom feed and return relevant items."""
        try:
            feed = feedparser.parse(
                feed_url,
                agent=self._ua,
                request_headers={"User-Agent": self._ua},
            )
        except Exception as exc:
            logger.warning("[rss] failed to parse feed %s: %s", feed_url, exc)
            return []

        if feed.get("bozo") and not feed.entries:
            logger.warning("[rss] malformed feed (bozo): %s", feed_url)
            return []

        feed_title = feed.feed.get("title", feed_url)
        items: list[CollectedItem] = []

        for entry in feed.entries:
            if not _is_relevant(entry):
                continue

            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            content_blocks = getattr(entry, "content", [])
            body = content_blocks[0].get("value", "") if content_blocks else ""

            # Prefer full content over summary
            text = body or summary
            if not text and not title:
                continue

            content = f"{title}\n\n{text}".strip() if title else text

            # Use entry link as canonical ID; fall back to a hash of title+date
            link = getattr(entry, "link", None)
            if link:
                external_id = hashlib.sha256(link.encode()).hexdigest()[:24]
            else:
                raw_id = f"{feed_url}:{title}:{getattr(entry, 'published', '')}"
                external_id = hashlib.sha256(raw_id.encode()).hexdigest()[:24]

            published_at = _parse_date(entry)

            items.append(
                CollectedItem(
                    external_id=external_id,
                    content=content,
                    url=link,
                    author=feed_title,
                    published_at=published_at,
                    raw_json={
                        "feed_url":   feed_url,
                        "feed_title": feed_title,
                        "entry_id":   getattr(entry, "id", None),
                        "title":      title,
                    },
                )
            )

        logger.debug("[rss] feed %s → %d relevant items", feed_url, len(items))
        return items

    def collect(self) -> list[CollectedItem]:
        all_items: list[CollectedItem] = []
        for feed_url in config.RSS_FEEDS:
            all_items.extend(self._fetch_feed(feed_url))
        logger.debug("[rss] total items across all feeds: %d", len(all_items))
        return all_items
