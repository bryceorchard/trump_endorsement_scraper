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

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import config
from database.database import item_exists
from .base import BaseCollector, CollectedItem
# feedparser entries share rss.py's shape, so reuse its helpers rather than
# re-implementing feed fetch/parse, id-hashing, and content extraction.
from .rss import _parse_date, _parse_feed, _hash_external_id, _entry_body_html

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
            # None of the known containers matched — most likely another
            # whitehouse.gov redesign (the same class of breakage that forced
            # the RSS rewrite). Warn loudly so it surfaces instead of silently
            # degrading every excerpt-only item to title-only.
            logger.warning(
                "[whitehouse] no known article body container in %s — "
                "site markup may have changed; extracting title only", url,
            )
            return ""
        for noise in body.select("nav, footer, aside, script, style"):
            noise.decompose()
        return body.get_text(separator="\n", strip=True)

    def collect(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        seen_urls: set[str] = set()

        for path in FEED_PATHS:
            feed_url = urljoin(config.WHITEHOUSE_BASE_URL, path)
            feed = _parse_feed(self.session, feed_url, "whitehouse")
            if feed is None:
                continue

            for entry in feed.entries[: config.WHITEHOUSE_LIMIT]:
                # Isolate each entry: one malformed post (or a failed article
                # fetch that raises) must not discard the rest of the run.
                try:
                    link = getattr(entry, "link", None)
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)

                    external_id = _hash_external_id(link)
                    # Already stored — skip before the (expensive) article-page
                    # fallback so we don't re-fetch/re-parse it every run. The
                    # feeds overlap and mostly carry seen items, so this saves
                    # the bulk of the per-run HTTP work on the Pi.
                    if item_exists(self.source_name, external_id):
                        continue

                    title = getattr(entry, "title", "").strip()

                    body_html = _entry_body_html(entry)
                    body_text = self._html_to_text(body_html) if body_html else ""
                    if len(body_text) < _MIN_FULL_TEXT:
                        body_text = self._fetch_article_text(link) or body_text
                    if not body_text and not title:
                        continue

                    content = f"{title}\n\n{body_text}".strip() if title else body_text

                    items.append(
                        CollectedItem(
                            external_id=external_id,
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
                except Exception as exc:
                    logger.warning("[whitehouse] skipping malformed entry: %s", exc)
                    continue

        logger.debug("[whitehouse] collected %d items", len(items))
        return items
