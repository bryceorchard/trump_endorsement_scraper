"""
whitehouse.py — Collector for White House speeches, remarks, and press briefings.

Scrapes the three main briefing-room sections:
  /briefing-room/speeches-remarks/
  /briefing-room/press-briefings/
  /briefing-room/statements-releases/

For each listing page it finds article links, then fetches the full text of
each article and stores it as a single item (deduplicated by URL).
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import config
from .base import BaseCollector, CollectedItem

logger = logging.getLogger(__name__)

SECTIONS = [
    "/briefing-room/speeches-remarks/",
    "/briefing-room/press-briefings/",
    "/briefing-room/statements-releases/",
]


class WhiteHouseCollector(BaseCollector):
    source_name = "whitehouse"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })

    def _get_soup(self, url: str) -> BeautifulSoup:
        resp = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def _article_links_from_section(self, section_path: str) -> list[str]:
        """Return up to WHITEHOUSE_LIMIT article URLs from a section listing page."""
        url = urljoin(config.WHITEHOUSE_BASE_URL, section_path)
        soup = self._get_soup(url)

        links: list[str] = []
        # The WH site wraps articles in <article> tags with an <a> inside the header
        for article in soup.select("article"):
            a = article.select_one("h2 a, h3 a, .news-item__title a")
            if a and a.get("href"):
                href = a["href"]
                full = urljoin(config.WHITEHOUSE_BASE_URL, href)
                links.append(full)
            if len(links) >= config.WHITEHOUSE_LIMIT:
                break

        # Fallback: scan all links containing the section path
        if not links:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if section_path.rstrip("/").split("/")[-1] in href and href != section_path:
                    full = urljoin(config.WHITEHOUSE_BASE_URL, href)
                    if full not in links:
                        links.append(full)
                if len(links) >= config.WHITEHOUSE_LIMIT:
                    break

        return links

    def _parse_article(self, url: str) -> Optional[CollectedItem]:
        """Fetch a single WH article page and extract title + body text."""
        try:
            soup = self._get_soup(url)
        except Exception as exc:
            logger.warning("[whitehouse] failed to fetch %s: %s", url, exc)
            return None

        # Title
        title_tag = soup.select_one("h1.page-header__title, h1.news-header__title, h1")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Published date
        published_at: Optional[datetime] = None
        time_tag = soup.select_one("time[datetime]")
        if time_tag:
            try:
                published_at = datetime.fromisoformat(
                    time_tag["datetime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Body — the main article content
        body_tag = soup.select_one(
            "div.body-content, div.page-content, article .entry-content, "
            "div[class*='body'], section.briefing-room__body"
        )
        if body_tag is None:
            body_tag = soup.select_one("article") or soup.select_one("main")

        body_text = ""
        if body_tag:
            # Remove nav, footer, sidebar noise
            for noise in body_tag.select("nav, footer, aside, script, style, .related-briefing"):
                noise.decompose()
            body_text = body_tag.get_text(separator="\n", strip=True)

        if not body_text:
            logger.debug("[whitehouse] no body text for %s", url)
            return None

        content = f"{title}\n\n{body_text}".strip() if title else body_text

        # Use URL hash as stable external_id (URL itself can be long)
        external_id = hashlib.sha256(url.encode()).hexdigest()[:24]

        return CollectedItem(
            external_id=external_id,
            content=content,
            url=url,
            author="White House",
            published_at=published_at,
            raw_json={"title": title, "url": url},
        )

    def collect(self) -> list[CollectedItem]:
        seen_urls: set[str] = set()
        items: list[CollectedItem] = []

        for section in SECTIONS:
            try:
                links = self._article_links_from_section(section)
            except Exception as exc:
                logger.warning("[whitehouse] failed to list section %s: %s", section, exc)
                continue

            logger.debug("[whitehouse] section %s → %d links", section, len(links))

            for url in links:
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                item = self._parse_article(url)
                if item:
                    items.append(item)

        logger.debug("[whitehouse] collected %d articles", len(items))
        return items
