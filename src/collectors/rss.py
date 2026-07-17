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
from .base import BaseCollector, CollectedItem, strip_html

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


def _is_relevant(text: str) -> bool:
    """True if the (already HTML-stripped) text contains a filter keyword.

    Takes stripped text, not the entry: matching against raw HTML would let a
    keyword inside an href/attribute admit an irrelevant article — and every
    kept item costs a full LLM inference later.
    """
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in config.RSS_FILTER_KEYWORDS)


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
            # Isolate each entry: one malformed entry must not discard the rest
            # of the run's items (mirrors the whitehouse/truth_social guards).
            try:
                title = getattr(entry, "title", "").strip()
                # Strip markup BEFORE filtering and storing: raw feed HTML
                # ('<p><a href=…') would otherwise both admit keyword hits
                # hiding inside hrefs and burn inference tokens on tags.
                summary = strip_html(getattr(entry, "summary", ""))
                body = strip_html(_entry_body_html(entry))

                if not _is_relevant(f"{title} {summary} {body}"):
                    continue

                # Prefer full content over summary; both are already stripped,
                # so a markup-only body (image/embed) falls back to the summary.
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
            except Exception as exc:
                logger.warning("[rss] skipping malformed entry in %s: %s", feed_url, exc)
                continue

        logger.debug("[rss] feed %s → %d relevant items", feed_url, len(items))
        return items

    def collect(self) -> list[CollectedItem]:
        all_items: list[CollectedItem] = []
        for feed_url in config.RSS_FEEDS:
            all_items.extend(self._fetch_feed(feed_url))
        logger.debug("[rss] total items across all feeds: %d", len(all_items))
        return all_items
