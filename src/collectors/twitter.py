"""
twitter.py — Collector for @realDonaldTrump tweets via twscrape.

twscrape scrapes Twitter using real Twitter accounts under the hood.
You need at least one Twitter account in TWITTER_ACCOUNTS_JSON.

Setup (one-time): fill in TWITTER_ACCOUNTS_JSON in src/.env, then run
scripts/setup_twitter.sh — it registers the account(s) with twscrape and
logs them in (session tokens are cached in accounts.db next to main.py).

Rate limits: twscrape respects Twitter's rate limits internally.
If you hit them, increase INTERVAL_TWITTER.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from re import compile as re_compile
from typing import Optional

from config import config
from .base import BaseCollector, CollectedItem

logger = logging.getLogger(__name__)

# Many of Trump's tweets are media-only — rawContent is just a trailing t.co
# link with no text. Those carry nothing for the text-only detector, so we skip
# them (mirrors the media-only skip in truth_social.py).
_URL_RE = re_compile(r"https?://\S+")


def _has_analyzable_text(raw_content: str) -> bool:
    return bool(_URL_RE.sub("", raw_content or "").strip())

# twscrape's default HTTP backend (httpx) has a non-browser TLS fingerprint that
# X's Cloudflare rejects with a 403 — the same wall the Truth Social collector
# hits. Force twscrape's curl_cffi impersonation backend (curl_cffi is already a
# dependency). setdefault so an explicit TWS_HTTP_BACKEND in the env still wins.
os.environ.setdefault("TWS_HTTP_BACKEND", "curl")


def _run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class TwitterCollector(BaseCollector):
    source_name = "twitter"

    def __init__(self):
        try:
            from twscrape import API as TwAPI
            self._api_cls = TwAPI
        except ImportError:
            self._api_cls = None
            logger.warning(
                "twscrape not installed. Run: pip install twscrape\n"
                "Twitter collection will be skipped."
            )

    async def _fetch_tweets(self) -> list[CollectedItem]:
        from twscrape import API as TwAPI

        api = TwAPI()  # uses default accounts pool db

        # Accounts are registered + logged in once by scripts/setup_twitter.sh
        # and persist in accounts.db; only seed from env if the pool is empty.
        # (Re-adding an existing account every run just logs a noisy
        # "Account X already exists" warning and does nothing useful.)
        if not await api.pool.accounts_info():
            for acc in json.loads(config.TWITTER_ACCOUNTS_JSON):
                await api.pool.add_account(**acc)

        # Resolve user ID once
        user = await api.user_by_login(config.TWITTER_TARGET_USER)
        if user is None:
            logger.error("[twitter] could not resolve user: @%s", config.TWITTER_TARGET_USER)
            return []

        items: list[CollectedItem] = []
        skipped = 0
        async for tweet in api.user_tweets(user.id, limit=config.TWITTER_TWEET_LIMIT):
            if not _has_analyzable_text(tweet.rawContent):
                skipped += 1  # media-only / bare-link tweet — nothing to analyze
                continue

            published_at: Optional[datetime] = None
            if tweet.date:
                published_at = tweet.date.replace(tzinfo=timezone.utc) if tweet.date.tzinfo is None else tweet.date

            items.append(
                CollectedItem(
                    external_id=str(tweet.id),
                    content=tweet.rawContent,
                    url=f"https://x.com/{config.TWITTER_TARGET_USER}/status/{tweet.id}",
                    author=config.TWITTER_TARGET_USER,
                    published_at=published_at,
                    # tweet.dict() keeps datetime objects, which json.dumps
                    # chokes on later; .json() stringifies them (default=str)
                    raw_json=json.loads(tweet.json()),
                )
            )

        logger.debug("[twitter] kept %d tweets, skipped %d media-only", len(items), skipped)
        return items

    def collect(self) -> list[CollectedItem]:
        if self._api_cls is None:
            logger.warning("[twitter] skipping — twscrape not installed")
            return []

        try:
            return _run_async(self._fetch_tweets())
        except Exception as exc:
            logger.error("[twitter] fetch failed: %s", exc, exc_info=True)
            raise
