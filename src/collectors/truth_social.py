"""
truth_social.py — Collector for Trump's Truth Social posts.

Truth Social is built on Mastodon, so it exposes a standard Mastodon-compatible
REST API at https://truthsocial.com/api/v1/.

Public endpoints don't require authentication, but may be rate-limited.
If you get 429s, increase INTERVAL_TRUTH_SOCIAL in config.
"""

import logging
from datetime import datetime, timezone
from html import unescape
from re import compile as re_compile

import requests

from config import config
from .base import BaseCollector, CollectedItem

logger = logging.getLogger(__name__)

# Strip HTML tags from post content (Truth Social returns HTML-formatted text)
_TAG_RE = re_compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


class TruthSocialCollector(BaseCollector):
    source_name = "truth_social"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
        })

    def _get_statuses(self, max_id: str | None = None) -> list[dict]:
        """
        Fetch a page of statuses from the Mastodon-compatible API.
        https://docs.joinmastodon.org/methods/accounts/#statuses
        """
        url = (
            f"{config.TRUTH_SOCIAL_BASE_URL}/api/v1/accounts"
            f"/{config.TRUTH_SOCIAL_ACCOUNT_ID}/statuses"
        )
        params = {
            "limit": config.TRUTH_SOCIAL_LIMIT,
            "exclude_replies": "false",
            "exclude_reblogs": "false",
        }
        if max_id:
            params["max_id"] = max_id

        resp = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def collect(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []

        try:
            statuses = self._get_statuses()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.warning(
                    "Truth Social account ID %s not found. "
                    "Update TRUTH_SOCIAL_ACCOUNT_ID in config.",
                    config.TRUTH_SOCIAL_ACCOUNT_ID,
                )
            raise

        for status in statuses:
            content_html = status.get("content", "")
            content_text = _strip_html(content_html)

            # Include reblogs (re-truths) — unwrap the reblogged content
            if not content_text and status.get("reblog"):
                rb = status["reblog"]
                content_text = _strip_html(rb.get("content", ""))

            if not content_text:
                continue  # media-only post, skip

            published_raw = status.get("created_at")
            published_at = None
            if published_raw:
                try:
                    published_at = datetime.fromisoformat(
                        published_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            post_url = status.get("url") or (
                f"{config.TRUTH_SOCIAL_BASE_URL}/@realDonaldTrump/{status['id']}"
            )

            items.append(
                CollectedItem(
                    external_id=str(status["id"]),
                    content=content_text,
                    url=post_url,
                    author="realDonaldTrump",
                    published_at=published_at,
                    raw_json=status,
                )
            )

        logger.debug("[truth_social] fetched %d posts", len(items))
        return items
