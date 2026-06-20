"""
twitter.py — Collector for @realDonaldTrump tweets via twscrape.

twscrape scrapes Twitter using real Twitter accounts under the hood.
You need at least one Twitter account in TWITTER_ACCOUNTS_JSON.

Setup (one-time):
    pip install twscrape
    # Set TWITTER_ACCOUNTS_JSON in your .env, then run:
    python -c "
    import asyncio, json, config
    from twscrape import AccountsPool
    async def add():
        pool = AccountsPool()
        for acc in json.loads(config.TWITTER_ACCOUNTS_JSON):
            await pool.add_account(**acc)
        await pool.login_all()
    asyncio.run(add())
    "

Rate limits: twscrape respects Twitter's rate limits internally.
If you hit them, increase INTERVAL_TWITTER.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import config
from .base import BaseCollector, CollectedItem

logger = logging.getLogger(__name__)


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

        # Seed accounts from env if provided
        accounts = json.loads(config.TWITTER_ACCOUNTS_JSON)
        for acc in accounts:
            await api.pool.add_account(**acc)

        # Resolve user ID once
        user = await api.user_by_login(config.TWITTER_TARGET_USER)
        if user is None:
            logger.error("[twitter] could not resolve user: @%s", config.TWITTER_TARGET_USER)
            return []

        items: list[CollectedItem] = []
        count = 0
        async for tweet in api.user_tweets(user.id, limit=config.TWITTER_TWEET_LIMIT):
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
                    raw_json=tweet.dict(),
                )
            )
            count += 1

        logger.debug("[twitter] fetched %d tweets", count)
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
