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
    """Run an async coroutine from sync context.

    Collectors are always invoked from a plain sync context — an APScheduler
    worker thread or the CLI main thread — never from inside a running event
    loop, so a fresh loop per call is correct. (The old version caught
    RuntimeError around run_until_complete and retried via asyncio.run, which
    re-ran the whole coroutine when the *work* raised RuntimeError — a silent
    double fetch — and leaked the auto-created loop.)
    """
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

        # Accounts persist in accounts.db (registered + logged in by
        # scripts/setup_twitter.sh). Seed any env account that isn't already in
        # the pool — this picks up a newly-added/rotated account in
        # TWITTER_ACCOUNTS_JSON without forcing a manual accounts.db delete,
        # while skipping already-present ones (re-adding just warns and no-ops).
        env_accounts = json.loads(config.TWITTER_ACCOUNTS_JSON)
        pool_info = await api.pool.accounts_info()
        if not pool_info and not env_accounts:
            # Not configured at all — that's a valid setup (this collector is
            # optional), so skip cleanly instead of failing the run with a
            # twscrape traceback every cycle.
            logger.warning(
                "[twitter] skipping — no Twitter accounts configured. To enable this "
                "collector, set TWITTER_ACCOUNTS_JSON in src/.env and run "
                "scripts/setup_twitter.sh (docs/SETUP.md Step 5). The other "
                "collectors are unaffected."
            )
            return []

        existing = {a["username"] for a in pool_info}
        added = False
        for acc in env_accounts:
            if acc["username"] not in existing:
                await api.pool.add_account(**acc)
                added = True
                # New accounts still need a login pass to get session tokens;
                # setup_twitter.sh does that. Flag it so a silently-unusable
                # account is obvious rather than looking merely idle.
                logger.info(
                    "[twitter] added account @%s from env — run "
                    "scripts/setup_twitter.sh to log it in if not already done",
                    acc["username"],
                )
        if added:
            pool_info = await api.pool.accounts_info()

        if pool_info and not any(a.get("active") for a in pool_info):
            # Registered but never (successfully) logged in — every request
            # would fail with twscrape's "no account available". Say what's
            # wrong and what fixes it, then skip this run.
            logger.warning(
                "[twitter] skipping — %d account(s) registered but none are logged "
                "in. Run scripts/setup_twitter.sh; if password login is blocked by "
                "X (code 399), use browser cookies (docs/SETUP.md Step 5).",
                len(pool_info),
            )
            return []

        # Resolve user ID once
        user = await api.user_by_login(config.TWITTER_TARGET_USER)
        if user is None:
            logger.error("[twitter] could not resolve user: @%s", config.TWITTER_TARGET_USER)
            return []

        items: list[CollectedItem] = []
        skipped = 0
        async for tweet in api.user_tweets(user.id, limit=config.TWITTER_TWEET_LIMIT):
            raw = tweet.rawContent or ""
            # Expand t.co shortlinks to their destinations. A bare link to a
            # company/asset site is itself an endorsement signal, so we keep
            # such tweets and feed the detector the real URL instead of an
            # opaque t.co (or dropping them). getattr keeps this safe if a
            # twscrape version doesn't expose .links.
            link_urls = [
                l.url for l in (getattr(tweet, "links", None) or [])
                if getattr(l, "url", None)
            ]
            content = "\n".join([raw, *link_urls]).strip()

            # Skip only genuine media-only posts: no text AND no outbound link.
            if not _has_analyzable_text(raw) and not link_urls:
                skipped += 1
                continue

            published_at: Optional[datetime] = None
            if tweet.date:
                published_at = tweet.date.replace(tzinfo=timezone.utc) if tweet.date.tzinfo is None else tweet.date

            items.append(
                CollectedItem(
                    external_id=str(tweet.id),
                    content=content,
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
