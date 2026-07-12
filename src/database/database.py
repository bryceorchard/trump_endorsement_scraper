"""
database.py — PostgreSQL schema + helper for trump_tracker.

Tables:
  sources          — known data sources (truth_social, twitter, whitehouse, rss)
  items            — every collected post/transcript (deduplicated by source+external_id)
  collection_runs  — log of each collector invocation
  endorsements     — LLM detection results, one row per analyzed item
"""

import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config import config

DATABASE_URL = config.DATABASE_URL

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- e.g. 'truth_social', 'twitter', 'whitehouse', 'rss'
    display_name TEXT,
    base_url    TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS items (
    id              SERIAL PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    external_id     TEXT NOT NULL,         -- platform-native ID or URL hash
    url             TEXT,
    content         TEXT NOT NULL,
    author          TEXT,
    published_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    raw_json        JSONB,                 -- full original payload for LLM later
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id          SERIAL PRIMARY KEY,
    source_name TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    items_found INTEGER DEFAULT 0,
    items_new   INTEGER DEFAULT 0,
    error       TEXT
);

-- Track which items have been run through the endorsement detector
ALTER TABLE items ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
-- Count detector timeouts per item so a poison item can be given up on after a
-- bounded number of retries instead of being retried forever.
ALTER TABLE items ADD COLUMN IF NOT EXISTS detection_attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_items_published  ON items (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_source     ON items (source_id);
CREATE INDEX IF NOT EXISTS idx_items_fetched    ON items (fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_unprocessed ON items (id) WHERE processed_at IS NULL;

-- Endorsement detection results — one row per analyzed item
CREATE TABLE IF NOT EXISTS endorsements (
    id               SERIAL PRIMARY KEY,
    item_id          INTEGER NOT NULL REFERENCES items(id) UNIQUE,
    detected         BOOLEAN NOT NULL,
    company          TEXT,
    ticker           TEXT,
    confidence       TEXT,
    quote            TEXT,
    endorsement_type TEXT,
    analyzed_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_endorsements_detected ON endorsements (detected, analyzed_at DESC);
CREATE INDEX IF NOT EXISTS idx_endorsements_ticker   ON endorsements (ticker) WHERE ticker IS NOT NULL;
"""

SEED_SOURCES = [
    ("truth_social", "Truth Social",     "https://truthsocial.com"),
    ("twitter",      "X / Twitter",      "https://x.com"),
    ("whitehouse",   "White House",      "https://www.whitehouse.gov"),
    ("rss",          "News / RSS Feeds", None),
]


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


@contextmanager
def db_cursor():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables and seed sources if needed."""
    with db_cursor() as cur:
        cur.execute(SCHEMA_SQL)
        for name, display, url in SEED_SOURCES:
            cur.execute(
                """
                INSERT INTO sources (name, display_name, base_url)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, display, url),
            )
    print("Database initialised.")


_source_id_cache: dict[str, int] = {}


def get_source_id(source_name: str) -> int:
    if source_name in _source_id_cache:
        return _source_id_cache[source_name]
    with db_cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE name = %s", (source_name,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Unknown source: {source_name}")
        _source_id_cache[source_name] = row["id"]
        return row["id"]


def item_exists(source_name: str, external_id: str) -> bool:
    """True if an item with this (source, external_id) is already stored.

    Cheap indexed lookup on the UNIQUE(source_id, external_id) constraint —
    lets a collector skip expensive work (e.g. fetching an article page) for
    items it has already seen, instead of relying on upsert_item to dedup after
    the work is done.
    """
    source_id = get_source_id(source_name)
    with db_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM items WHERE source_id = %s AND external_id = %s",
            (source_id, external_id),
        )
        return cur.fetchone() is not None


def upsert_item(
    source_name: str,
    external_id: str,
    content: str,
    *,
    url: Optional[str] = None,
    author: Optional[str] = None,
    published_at: Optional[datetime] = None,
    raw_json=None,
) -> bool:
    """
    Insert item if not already present. Returns True if new, False if duplicate.
    """
    import json

    source_id = get_source_id(source_name)
    # default=str keeps a stray datetime/Decimal in a payload from failing the
    # whole upsert — raw_json is archival, lossy stringification is fine.
    raw = json.dumps(raw_json, default=str) if raw_json is not None else None

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO items
                (source_id, external_id, url, content, author, published_at, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (source_id, external_id) DO NOTHING
            RETURNING id
            """,
            (source_id, external_id, url, content, author, published_at, raw),
        )
        return cur.fetchone() is not None  # True = newly inserted


def log_run(source_name: str, started_at: datetime) -> int:
    """Open a collection run log row, return its id."""
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO collection_runs (source_name, started_at) VALUES (%s, %s) RETURNING id",
            (source_name, started_at),
        )
        return cur.fetchone()["id"]


def finish_run(run_id: int, items_found: int, items_new: int, error: Optional[str] = None):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE collection_runs
            SET finished_at = %s, items_found = %s, items_new = %s, error = %s
            WHERE id = %s
            """,
            (datetime.now(timezone.utc), items_found, items_new, error, run_id),
        )


# ── Endorsement detection helpers ─────────────────────────────────────────────

def get_unprocessed_items(batch_size: int = 50) -> list[dict]:
    """
    Return up to batch_size items that have not yet been run through the
    endorsement detector (processed_at IS NULL), oldest first.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.content, i.url, i.author, i.published_at, s.name AS source_name
            FROM items i
            JOIN sources s ON s.id = i.source_id
            WHERE i.processed_at IS NULL
            ORDER BY i.fetched_at ASC
            LIMIT %s
            """,
            (batch_size,),
        )
        return [dict(row) for row in cur.fetchall()]


def record_detection_attempt(item_id: int) -> int:
    """Increment the item's detector-timeout counter and return the new count.

    Used to bound retries of items that keep timing out (see run_detection).
    """
    with db_cursor() as cur:
        cur.execute(
            "UPDATE items SET detection_attempts = detection_attempts + 1 "
            "WHERE id = %s RETURNING detection_attempts",
            (item_id,),
        )
        return cur.fetchone()["detection_attempts"]


def save_endorsement(item_id: int, result) -> None:
    """
    Persist an EndorsementResult (from endorsement_detector) and mark the
    parent item as processed. Uses an upsert in case of retries.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO endorsements
                (item_id, detected, company, ticker, confidence, quote, endorsement_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_id) DO UPDATE SET
                detected         = EXCLUDED.detected,
                company          = EXCLUDED.company,
                ticker           = EXCLUDED.ticker,
                confidence       = EXCLUDED.confidence,
                quote            = EXCLUDED.quote,
                endorsement_type = EXCLUDED.endorsement_type,
                analyzed_at      = NOW()
            """,
            (
                item_id,
                result.endorsement_detected,
                result.company,
                result.ticker,
                result.confidence,
                result.quote,
                result.endorsement_type,
            ),
        )
        cur.execute(
            "UPDATE items SET processed_at = NOW() WHERE id = %s",
            (item_id,),
        )


def get_recent_endorsements(limit: int = 20) -> list[dict]:
    """Return the most recent actionable endorsements for logging/alerting."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                e.analyzed_at,
                e.company,
                e.ticker,
                e.confidence,
                e.endorsement_type,
                e.quote,
                i.url,
                s.name AS source_name,
                i.published_at
            FROM endorsements e
            JOIN items i   ON i.id = e.item_id
            JOIN sources s ON s.id = i.source_id
            WHERE e.detected = TRUE
            ORDER BY e.analyzed_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
