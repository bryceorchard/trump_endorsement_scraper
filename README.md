# Trump Endorsement Scraper

A self-hosted pipeline that watches everywhere Donald Trump speaks or posts, and uses a
local LLM to flag when he endorses or promotes a company, brand, or financial asset
(stock/crypto). Designed to run continuously and unattended on a Raspberry Pi 5.

Nothing leaves the machine: scraping, storage, and inference all run locally. There is no
paid API and no cloud LLM.

## How it works

The pipeline has two decoupled halves that run on independent schedules — neither blocks
the other:

1. **Collection.** Independent collectors poll each source and write new items into
   Postgres. Deduplication is handled centrally, so re-polling a source is cheap and safe.
2. **Detection.** A separate pass reads unprocessed items, runs each through a local LLM
   (Qwen3-8B via [Ollama](https://ollama.com/)), and stores a structured endorsement
   verdict. Because the full original payload is retained, items can be re-analyzed later
   without re-fetching.

```
                         ┌─────────────┐
  Truth Social ─┐        │             │        ┌──────────────┐
  Twitter / X  ─┼──────► │  Postgres   │ ─────► │  Ollama LLM  │ ─► endorsements
  White House  ─┤ collect│  (items)    │ detect │  (Qwen3-8B)  │
  RSS news     ─┘        │             │        └──────────────┘
                         └─────────────┘
```

### Sources

| Collector | Source | Notes |
| --- | --- | --- |
| `truth_social` | Truth Social (Mastodon-compatible public API) | No auth. Fetched with `curl_cffi` Chrome TLS impersonation because Cloudflare 403s plain `requests`. |
| `twitter` | Twitter/X via [`twscrape`](https://github.com/vladkens/twscrape) | Needs at least one registered account. Degrades to a no-op if `twscrape` isn't installed. Falls back to cookie auth when X blocks password login. |
| `whitehouse` | whitehouse.gov per-section WordPress RSS (`/news/`, `/remarks/`, `/briefings-statements/`, `/presidential-actions/`) | Full text from `content:encoded`, with an article-page fallback. |
| `rss` | Arbitrary news RSS feeds (`feedparser`) | Filtered to entries containing a Trump-relevance keyword. |

### Detection output

Each analyzed item produces a structured verdict:

```json
{
  "endorsement_detected": true,
  "company": "Apple",
  "ticker": "AAPL",
  "confidence": "high",
  "quote": "the amazing people at Apple are doing incredible things",
  "endorsement_type": "implicit"
}
```

`endorsement_type` is one of `explicit` (says to buy/invest/support), `implicit` (praise
that implies support), `financial` (references a stock/crypto/financial product), or
`none`. A detection is considered **actionable** when confidence is `high` or `medium` and
the type isn't `none`.

> **Note:** actionable detections are logged, but alerting/notifications are not yet
> implemented.

## Requirements

- Python 3.11+ (developed on 3.12)
- PostgreSQL
- [Ollama](https://ollama.com/) with the `qwen3:8b` model pulled (only needed when
  detection is enabled)

## Quick start

```bash
# 1. Create a project-root virtualenv (never system Python) and install deps
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# 2. Configure
cp src/.env.example src/.env
# edit src/.env — at minimum set DATABASE_URL; add TWITTER_ACCOUNTS_JSON to enable Twitter

# 3. Run (from inside src/, which is the import root)
cd src
python3 main.py --run-once
```

The schema is created automatically on first run (`init_db()` is idempotent). Set
`DETECTION_ENABLED=false` in `.env` to run collectors without Ollama — items simply queue
up unprocessed until detection is turned on.

### CLI

Run from inside `src/`:

```bash
python3 main.py --run-once                 # all collectors + detection, once
python3 main.py --collector truth_social   # a single collector (truth_social|twitter|whitehouse|rss)
python3 main.py --detect-only              # detection only, against what's already in the DB
python3 main.py                            # scheduled mode (blocking); per-source intervals from config
python3 -m detector.endorsement_detector   # quick manual detector test against sample text
```

The `scripts/` directory has convenience wrappers (`run_once.sh`, `start.sh`,
`test_detector.sh`, …) that load `src/.env` for you.

## Configuration

All configuration is environment-variable driven and documented in
[`src/.env.example`](src/.env.example). Highlights:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. |
| `DETECTION_ENABLED` | Set `false` to collect without running the LLM. |
| `TWITTER_ACCOUNTS_JSON` | Required to enable the Twitter collector. |
| `RSS_FEEDS_JSON` / `RSS_FILTER_KEYWORDS` | Override the default feed list / relevance keywords. |
| `INTERVAL_*` | Per-source polling intervals (seconds) for scheduled mode. |
| `OLLAMA_MODEL` / `OLLAMA_TIMEOUT` | Model name and per-inference timeout. |

## Project layout

```
src/                       import root (run main.py from here)
├── main.py                CLI entry point + APScheduler-based scheduler
├── collectors/            one class per source, all subclassing BaseCollector
├── database/database.py   the only module that touches SQL (schema + upsert/dedup)
├── detector/              detect_endorsement() → Ollama → EndorsementResult
└── config/config.py       env-var-driven configuration with defaults
scripts/                   setup + run helpers (setup.sh builds the Pi's venv)
docs/SETUP.md              full setup: Ollama, Postgres, systemd, Twitter, troubleshooting
```

Collectors subclass `BaseCollector`, which handles run logging, upsert/dedup, and
per-item error isolation (one bad item never fails a whole run). Each subclass only
implements `collect() -> list[CollectedItem]`.

## Deployment

The intended target is a headless Raspberry Pi 5 running the app under systemd as the
`trump-tracker` service. Full setup instructions — Ollama/Qwen3-8B, PostgreSQL, the
systemd unit, and Twitter/`twscrape` account registration — are in
[`docs/SETUP.md`](docs/SETUP.md).

## Status

Working: all four collectors, Postgres storage/dedup, and local LLM detection.

Not yet implemented: alerting/notifications on actionable detections.
