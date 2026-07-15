"""
config.py — All configuration pulled from environment variables.

Copy src/.env.example to src/.env and fill in values. The scripts/ helpers load
it for you (scripts/_env.sh does `set -a; . src/.env; set +a`). JSON values in
.env must be wrapped in single quotes so sourcing preserves the inner double
quotes — see src/.env.example.
"""

import json as _json
import os


def _json_env(name: str, default: str):
    """Parse a JSON-valued env var, failing with the variable name and value
    instead of an anonymous JSONDecodeError from deep inside json.

    A variable that is set but blank (e.g. `TWITTER_ACCOUNTS_JSON=` in .env to
    disable a feature) falls back to the default rather than erroring."""
    raw = os.getenv(name) or default
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Environment variable {name} is not valid JSON: {raw!r}\n"
            f"In src/.env, wrap the whole value in single quotes, e.g.\n"
            f"    {name}='[\"a\",\"b\"]'\n"
            f"so shell sourcing keeps the inner double quotes intact "
            f"(see src/.env.example)."
        ) from exc

# ── Database ────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/trump_tracker")

# ── Truth Social ─────────────────────────────────────────────────────────────
# Trump's public account ID on Truth Social (Mastodon-compatible API)
TRUTH_SOCIAL_ACCOUNT_ID = os.getenv("TRUTH_SOCIAL_ACCOUNT_ID", "107780257626128497")
TRUTH_SOCIAL_BASE_URL    = os.getenv("TRUTH_SOCIAL_BASE_URL", "https://truthsocial.com")
TRUTH_SOCIAL_LIMIT       = int(os.getenv("TRUTH_SOCIAL_LIMIT", "40"))

# ── X / Twitter ──────────────────────────────────────────────────────────────
# twscrape needs at least one Twitter account. Add credentials as JSON list:
# '[{"username":"u","password":"p","email":"e","email_password":"ep"}]'
TWITTER_ACCOUNTS_JSON = os.getenv("TWITTER_ACCOUNTS_JSON", "[]")
# Parsed form (list of account dicts) with blank/invalid handled up front —
# collectors should use this, not re-parse the raw string.
TWITTER_ACCOUNTS: list = _json_env("TWITTER_ACCOUNTS_JSON", "[]")
TWITTER_TARGET_USER   = os.getenv("TWITTER_TARGET_USER", "realDonaldTrump")
TWITTER_TWEET_LIMIT   = int(os.getenv("TWITTER_TWEET_LIMIT", "40"))

# ── White House ───────────────────────────────────────────────────────────────
WHITEHOUSE_BASE_URL   = os.getenv("WHITEHOUSE_BASE_URL", "https://www.whitehouse.gov")
WHITEHOUSE_LIMIT      = int(os.getenv("WHITEHOUSE_LIMIT", "20"))  # articles per run

# ── RSS / News ────────────────────────────────────────────────────────────────
# Default feeds — override by setting RSS_FEEDS_JSON to a JSON list of URLs.
# (Reuters shut its public RSS feeds down and c-span.org/rss/ is 410 Gone —
# all of these were verified live 2026-07.)
_default_feeds = _json.dumps([
    "https://feeds.foxnews.com/foxnews/politics",
    "http://rss.cnn.com/rss/cnn_allpolitics.rss",
    "https://feeds.npr.org/1014/rss.xml",            # NPR Politics
    "https://rss.politico.com/politics-news.xml",
    "https://thehill.com/feed/",
    "https://abcnews.go.com/abcnews/politicsheadlines",
    "https://www.cbsnews.com/latest/rss/politics",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://www.theguardian.com/us-news/us-politics/rss",
])
RSS_FEEDS: list[str] = _json_env("RSS_FEEDS_JSON", _default_feeds)
# Keywords used to filter RSS items — only items containing at least one are saved
RSS_FILTER_KEYWORDS: list[str] = _json_env(
    "RSS_FILTER_KEYWORDS", '["trump", "donald"]'
)

# ── Scheduler intervals (seconds) ─────────────────────────────────────────────
INTERVAL_TRUTH_SOCIAL = int(os.getenv("INTERVAL_TRUTH_SOCIAL", "300"))   #  5 min
INTERVAL_TWITTER      = int(os.getenv("INTERVAL_TWITTER",      "600"))   # 10 min
INTERVAL_WHITEHOUSE   = int(os.getenv("INTERVAL_WHITEHOUSE",   "900"))   # 15 min
INTERVAL_RSS          = int(os.getenv("INTERVAL_RSS",          "600"))   # 10 min

# ── Scheduler intervals (detection) ──────────────────────────────────────────
INTERVAL_DETECTION = int(os.getenv("INTERVAL_DETECTION", "120"))  # 2 min

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; trump-tracker/1.0; +https://github.com/local/trump-tracker)"
)

# ── Ollama / Endorsement detection ───────────────────────────────────────────
# Set DETECTION_ENABLED=false to skip LLM analysis (e.g. while Ollama isn't running)
DETECTION_ENABLED    = os.getenv("DETECTION_ENABLED", "true").lower() == "true"
OLLAMA_URL           = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL         = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT       = int(os.getenv("OLLAMA_TIMEOUT", "180"))  # seconds per inference —
# generous because the first call after idle also loads the model into RAM,
# which on a Pi 5 can take a minute by itself
DETECTION_BATCH_SIZE = int(os.getenv("DETECTION_BATCH_SIZE", "10"))  # items per detection run
# How many times a single item may time out in the detector before we give up
# and mark it processed. Bounds retries so a transient cold-model-load timeout
# gets another chance, but a genuinely-too-long "poison" item can't retry
# forever and starve newer items out of the batch.
DETECTION_MAX_ATTEMPTS = int(os.getenv("DETECTION_MAX_ATTEMPTS", "3"))
