"""
whitehouse.py — Collector for White House remarks, briefings/statements, and
presidential actions.

whitehouse.gov is WordPress and publishes a standard RSS feed per section.
The old /briefing-room/* listing pages this collector originally scraped were
removed in the 2025 site redesign, and the replacement listing pages render
their lists with JavaScript — so the section feeds are the reliable way in.

Feed items usually carry the full post text in <content:encoded>; when an
entry doesn't, we fetch the article page and extract `div.entry-content`
(selector verified against the live site, 2026-07).
"""

import hashlib
import logging
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

from config import config
from .base import BaseCollector, CollectedItem
from .rss import _parse_date  # feedparser entries — same date fields as rss.py

logger = logging.getLogger(__name__)

# /news/feed/ aggregates most sections (releases, fact-sheets,
# briefings-statements, presidential-actions); the explicit section feeds add
# depth (30 items each) and /remarks/ coverage. Overlap is fine — items dedup
# by URL hash within the source.
FEED_PATHS = [
    "/news/feed/",
    "/remarks/feed/",
    "/briefings-statements/feed/",
    "/presidential-actions/feed/",
]

# Entries whose <content:encoded> is shorter than this are assumed to be
# excerpts, so the full article page gets fetched instead.
_MIN_FULL_TEXT = 200


class WhiteHouseCollector(BaseCollector):
    source_name = "whitehouse"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })

    @staticmethod
    def _html_to_text(html: str) -> str:
        return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)

    def _fetch_article_text(self, url: str) -> str:
        """Fallback for excerpt-only feed entries: extract the article body."""
        try:
            resp = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[whitehouse] failed to fetch %s: %s", url, exc)
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        body = (
            soup.select_one("div.entry-content")
            or soup.select_one("article")
            or soup.select_one("main")
        )
        if body is None:
            return ""
        for noise in body.select("nav, footer, aside, script, style"):
            noise.decompose()
        return body.get_text(separator="\n", strip=True)

    def collect(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        seen_urls: set[str] = set()

        for path in FEED_PATHS:
            feed_url = urljoin(config.WHITEHOUSE_BASE_URL, path)
            try:
                # Fetch via requests (with a timeout) rather than letting
                # feedparser.parse(url) do an untimed urllib fetch that can hang
                # the whole run on a stalled feed.
                resp = self.session.get(feed_url, timeout=config.REQUEST_TIMEOUT)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except Exception as exc:
                logger.warning("[whitehouse] failed to fetch/parse feed %s: %s", feed_url, exc)
                continue

            if feed.get("bozo") and not feed.entries:
                logger.warning(
                    "[whitehouse] malformed/empty feed (status=%s): %s",
                    feed.get("status"), feed_url,
                )
                continue

            for entry in feed.entries[: config.WHITEHOUSE_LIMIT]:
                link = getattr(entry, "link", None)
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                title = getattr(entry, "title", "").strip()

                content_blocks = getattr(entry, "content", None) or []
                body_html = content_blocks[0].get("value", "") if content_blocks else ""
                body_text = self._html_to_text(body_html) if body_html else ""
                if len(body_text) < _MIN_FULL_TEXT:
                    body_text = self._fetch_article_text(link) or body_text
                if not body_text and not title:
                    continue

                content = f"{title}\n\n{body_text}".strip() if title else body_text

                items.append(
                    CollectedItem(
                        # Same scheme as before: URL hash (URLs are long)
                        external_id=hashlib.sha256(link.encode()).hexdigest()[:24],
                        content=content,
                        url=link,
                        author="White House",
                        published_at=_parse_date(entry),
                        raw_json={
                            "feed_url": feed_url,
                            "title": title,
                            "entry_id": getattr(entry, "id", None),
                        },
                    )
                )

        logger.debug("[whitehouse] collected %d items", len(items))
        return items
