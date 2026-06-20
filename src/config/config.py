"""
config.py — All configuration pulled from environment variables.
Copy .env.example to .env and fill in values, then:
    export $(cat .env | xargs) && python main.py
"""

import os

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
TWITTER_TARGET_USER   = os.getenv("TWITTER_TARGET_USER", "realDonaldTrump")
TWITTER_TWEET_LIMIT   = int(os.getenv("TWITTER_TWEET_LIMIT", "40"))

# ── White House ───────────────────────────────────────────────────────────────
WHITEHOUSE_BASE_URL   = os.getenv("WHITEHOUSE_BASE_URL", "https://www.whitehouse.gov")
WHITEHOUSE_LIMIT      = int(os.getenv("WHITEHOUSE_LIMIT", "20"))  # articles per run

# ── RSS / News ────────────────────────────────────────────────────────────────
# Default feeds — override by setting RSS_FEEDS_JSON to a JSON list of URLs
import json as _json
_default_feeds = _json.dumps([
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://feeds.foxnews.com/foxnews/politics",
    "http://rss.cnn.com/rss/cnn_allpolitics.rss",
    "https://feeds.npr.org/1014/rss.xml",            # NPR Politics
    "https://rss.politico.com/politics-news.xml",
    "https://thehill.com/feed/",
    "https://www.c-span.org/rss/",
])
RSS_FEEDS: list[str] = _json.loads(os.getenv("RSS_FEEDS_JSON", _default_feeds))
# Keywords used to filter RSS items — only items containing at least one are saved
RSS_FILTER_KEYWORDS: list[str] = _json.loads(
    os.getenv("RSS_FILTER_KEYWORDS", '["trump", "donald"]')
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
OLLAMA_TIMEOUT       = int(os.getenv("OLLAMA_TIMEOUT", "60"))   # seconds per inference
DETECTION_BATCH_SIZE = int(os.getenv("DETECTION_BATCH_SIZE", "10"))  # items per detection run
