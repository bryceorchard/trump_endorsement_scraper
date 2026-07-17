"""
base.py — Abstract base class for all collectors.

Each collector implements collect() which returns a list of dicts matching
the upsert_item() signature. The base class handles the run logging,
deduplication calls, and error handling.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from html import unescape
from re import compile as re_compile
from typing import Optional
import logging

from database.database import upsert_item, log_run, finish_run, DatabaseError

logger = logging.getLogger(__name__)

# Shared HTML→plain-text for collectors whose sources return markup
# (Truth Social post HTML, RSS content:encoded). Lives here so peer
# collectors don't reach into each other's internals for it.
_TAG_RE = re_compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


class CollectedItem:
    """Lightweight typed container for a single scraped item."""
    __slots__ = ("external_id", "content", "url", "author", "published_at", "raw_json")

    def __init__(
        self,
        external_id: str,
        content: str,
        *,
        url: Optional[str] = None,
        author: Optional[str] = None,
        published_at: Optional[datetime] = None,
        raw_json=None,
    ):
        self.external_id = external_id
        self.content = content
        self.url = url
        self.author = author
        self.published_at = published_at
        self.raw_json = raw_json


class BaseCollector(ABC):
    """
    Subclass this and implement:
      - source_name (class attribute)
      - collect()   → list[CollectedItem]
    """

    source_name: str  # must match sources.name in DB

    def run(self) -> dict:
        """
        Execute a collection run. Returns summary dict:
          {"source": str, "found": int, "new": int, "error": str|None}
        """
        started = datetime.now(timezone.utc)
        try:
            run_id = log_run(self.source_name, started)
        except DatabaseError as exc:
            # DB down before we could even open the run — nothing can be
            # recorded, so skip cleanly instead of dumping a raw traceback.
            logger.error(
                "[%s] database unavailable — skipping this run; it will be "
                "retried next cycle: %s", self.source_name, exc,
            )
            return {"source": self.source_name, "found": 0, "new": 0,
                    "error": f"database unavailable: {exc}"}

        error_msg = None
        items: list[CollectedItem] = []

        try:
            items = self.collect()
        except Exception as exc:
            error_msg = str(exc)
            logger.error("[%s] collection failed: %s", self.source_name, exc, exc_info=True)

        new_count = 0
        for item in items:
            try:
                is_new = upsert_item(
                    self.source_name,
                    item.external_id,
                    item.content,
                    url=item.url,
                    author=item.author,
                    published_at=item.published_at,
                    raw_json=item.raw_json,
                )
                if is_new:
                    new_count += 1
                    logger.info("[%s] new item: %s", self.source_name, item.external_id)
            except DatabaseError as exc:
                # Systemic DB failure, not this item's fault — one message and
                # stop, rather than a warning per remaining item.
                error_msg = f"database error during upsert: {exc}"
                logger.error(
                    "[%s] %s — aborting this run's remaining upserts; the items "
                    "will be re-collected next cycle.", self.source_name, error_msg,
                )
                break
            except Exception as exc:
                logger.warning("[%s] failed to upsert %s: %s", self.source_name, item.external_id, exc)

        try:
            finish_run(run_id, len(items), new_count, error_msg)
        except DatabaseError as exc:
            logger.error(
                "[%s] database error recording the run result (run row stays "
                "open): %s", self.source_name, exc,
            )
            error_msg = error_msg or f"database unavailable: {exc}"

        summary = {
            "source":  self.source_name,
            "found":   len(items),
            "new":     new_count,
            "error":   error_msg,
        }
        logger.info("[%s] run complete — found=%d new=%d", self.source_name, len(items), new_count)
        return summary

    @abstractmethod
    def collect(self) -> list[CollectedItem]:
        """Fetch raw items from the source. Must be implemented by subclass."""
        ...
