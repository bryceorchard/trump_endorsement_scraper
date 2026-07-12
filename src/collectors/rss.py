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


def _hash_external_id(value: str) -> str:
    """Stable dedup key for items with no native ID: truncated SHA-256 of the
    given string (usually the item URL). Shared by the rss and whitehouse
    collectors so their dedup keys can't drift apart."""
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _entry_body_html(entry) -> str:
    """First <content:encoded> block's HTML from a feedparser entry, or ""."""
    content_blocks = getattr(entry, "content", None) or []
    return content_blocks[0].get("value", "") if content_blocks else ""


def _parse_feed(session, feed_url: str, log_prefix: str):
    """Fetch a feed via `session` (with a real timeout — feedparser.parse(url)
    does an untimed urllib fetch that can hang the run) and parse it. Returns the
    feedparser result, or None if the fetch failed or the feed is malformed-and-empty.
    Shared by the rss and whitehouse collectors."""
    try:
        resp = session.get(feed_url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        logger.warning("[%s] failed to fetch/parse feed %s: %s", log_prefix, feed_url, exc)
        return None

    if feed.get("bozo") and not feed.entries:
        logger.warning(
            "[%s] malformed/empty feed (status=%s): %s",
            log_prefix, feed.get("status"), feed_url,
        )
        return None

    return feed


def _is_relevant(entry) -> bool:
    """Return True if the entry contains at least one filter keyword (case-insensitive)."""
    text = " ".join([
        getattr(entry, "title", ""),
        getattr(entry, "summary", ""),
        _entry_body_html(entry),
    ]).lower()
    return any(kw.lower() in text for kw in config.RSS_FILTER_KEYWORDS)


class RSSCollector(BaseCollector):
    source_name = "rss"

    def __init__(self):
        # Fetch feeds ourselves so we control the timeout — feedparser.parse(url)
        # does its own urllib fetch with NO timeout, so a stalled feed body hangs
        # the whole collector thread forever (and the scheduler then coalesces it
        # away, silently killing this source until restart).
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def _fetch_feed(self, feed_url: str) -> list[CollectedItem]:
        """Parse a single RSS/Atom feed and return relevant items."""
        feed = _parse_feed(self.session, feed_url, "rss")
        if feed is None:
            return []

        feed_title = feed.feed.get("title", feed_url)
        items: list[CollectedItem] = []

        for entry in feed.entries:
            if not _is_relevant(entry):
                continue

            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            body = _entry_body_html(entry)

            # Prefer full content over summary
            text = body or summary
            if not text and not title:
                continue

            content = f"{title}\n\n{text}".strip() if title else text

            # Use entry link as canonical ID; fall back to a hash of title+date
            link = getattr(entry, "link", None)
            if link:
                external_id = _hash_external_id(link)
            else:
                external_id = _hash_external_id(
                    f"{feed_url}:{title}:{getattr(entry, 'published', '')}"
                )

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
