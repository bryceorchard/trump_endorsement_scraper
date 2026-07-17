"""
truth_social.py — Collector for Trump's Truth Social posts.

Truth Social is built on Mastodon, so it exposes a standard Mastodon-compatible
REST API at https://truthsocial.com/api/v1/. The endpoints need no auth, but
Cloudflare rejects plain `requests` clients by TLS fingerprint (HTTP 403
regardless of User-Agent), so we use curl_cffi's Chrome impersonation.

Rate limiting: if you get 429s, increase INTERVAL_TRUTH_SOCIAL in config.
"""

import logging
from datetime import datetime, timezone

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # collector degrades to plain requests (Cloudflare will 403)
    cffi_requests = None

from config import config
from .base import BaseCollector, CollectedItem, strip_html

logger = logging.getLogger(__name__)


class TruthSocialCollector(BaseCollector):
    source_name = "truth_social"

    def __init__(self):
        if cffi_requests is not None:
            # impersonate="chrome" matches a real browser TLS fingerprint,
            # which is what gets us past Cloudflare — not the User-Agent.
            self.session = cffi_requests.Session(impersonate="chrome")
        else:
            logger.warning(
                "curl_cffi not installed — Truth Social requests will almost "
                "certainly be blocked by Cloudflare (403). "
                "Run: pip install curl_cffi"
            )
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.session.headers.update({"Accept": "application/json"})

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
        except Exception as e:
            # requests and curl_cffi raise different HTTPError classes; both
            # carry a .response with .status_code.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                logger.warning(
                    "Truth Social account ID %s not found. "
                    "Update TRUTH_SOCIAL_ACCOUNT_ID in config.",
                    config.TRUTH_SOCIAL_ACCOUNT_ID,
                )
            elif status == 403:
                logger.warning(
                    "Truth Social returned 403 — Cloudflare blocked the request "
                    "(is curl_cffi installed?)"
                )
            raise

        # A 200 carrying a Cloudflare challenge or an error object isn't the
        # expected list of statuses. Raise (rather than returning []) so the run
        # is recorded as failed in collection_runs — otherwise a persistent
        # challenge looks like a healthy run that just found nothing, hiding the
        # outage.
        if not isinstance(statuses, list):
            raise RuntimeError(
                f"unexpected Truth Social API response (not a list): {type(statuses).__name__}"
            )

        for status in statuses:
            # Isolate each status: one malformed post must not discard the rest.
            try:
                content_html = status.get("content", "")
                content_text = strip_html(content_html)

                # Include reblogs (re-truths) — unwrap the reblogged content
                if not content_text and status.get("reblog"):
                    rb = status["reblog"]
                    content_text = strip_html(rb.get("content", ""))

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
            except Exception as exc:
                logger.warning("[truth_social] skipping malformed status: %s", exc)
                continue

        logger.debug("[truth_social] fetched %d posts", len(items))
        return items
