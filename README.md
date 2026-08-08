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
| `twitter` | Twitter/X via [`twscrape`](https://github.com/vladkens/twscrape) | Needs at least one registered account. X's anti-automation frequently blocks password login, so in practice you'll often need to supply that account's browser **cookies** (`auth_token` + `ct0`) for it to work — see [docs/SETUP.md](docs/SETUP.md) Step 5. Degrades to a no-op if `twscrape` isn't installed. |
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

Actionable detections are logged and, when `DISCORD_WEBHOOK_URL` is set, posted to a
Discord channel. Alerting is best-effort and isolated, so a failed send never disturbs
the saved detection; unset the webhook to log only.

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
#   (Twitter often needs browser cookies, not just a password — see docs/SETUP.md Step 5)

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
python3 main.py --detect-only --drain      # …and keep going until the whole queue is analyzed
python3 main.py                            # scheduled mode (blocking); per-source intervals from config
python3 -m detector.endorsement_detector   # quick manual detector test against sample text
```

Detection works **newest content first**, one batch of `DETECTION_BATCH_SIZE` (default 10)
per pass — the run tells you how much of the queue that covers. Add `--drain` to
`--run-once`/`--detect-only` to process the entire backlog in one invocation (each item
costs an LLM inference, ~30 s on a Pi, so a big backlog takes a while).

The `scripts/` directory has convenience wrappers (`run_once.sh`, `start.sh`,
`test_detector.sh`, …) that load `src/.env` for you.

## Configuration

All configuration is environment-variable driven with sensible defaults, and is
documented inline in [`src/.env.example`](src/.env.example) — copy that to `src/.env`
and edit. Most deployments only need `DATABASE_URL` (plus `TWITTER_ACCOUNTS_JSON` to
enable Twitter); everything else has a working default. The full set:

### Database

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://localhost/trump_tracker` | Postgres connection string. |

### Source options

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRUTH_SOCIAL_ACCOUNT_ID` | `107780257626128497` | Trump's Mastodon-compatible account ID. |
| `TRUTH_SOCIAL_BASE_URL` | `https://truthsocial.com` | Truth Social API host. |
| `TRUTH_SOCIAL_LIMIT` | `40` | Posts fetched per Truth Social run. |
| `TWITTER_ACCOUNTS_JSON` | _(empty)_ | JSON array of twscrape accounts. **Required to enable the Twitter collector** — empty means it's skipped. Often needs browser cookies, not just a password (see [docs/SETUP.md](docs/SETUP.md) Step 5). |
| `TWITTER_TARGET_USER` | `realDonaldTrump` | Twitter/X handle to scrape. |
| `TWITTER_TWEET_LIMIT` | `40` | Tweets fetched per Twitter run. |
| `WHITEHOUSE_BASE_URL` | `https://www.whitehouse.gov` | White House site host. |
| `WHITEHOUSE_LIMIT` | `20` | Articles fetched per White House run. |
| `RSS_FEEDS_JSON` | _(9 politics feeds)_ | JSON array of RSS feed URLs to poll. |
| `RSS_FILTER_KEYWORDS` | `["trump","donald"]` | Only RSS entries containing one of these keywords are saved. |

### Scheduling

Scheduled mode only, in seconds. Each job also fires once on startup.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INTERVAL_TRUTH_SOCIAL` | `300` | Truth Social poll interval. |
| `INTERVAL_TWITTER` | `600` | Twitter poll interval. |
| `INTERVAL_WHITEHOUSE` | `900` | White House poll interval. |
| `INTERVAL_RSS` | `600` | RSS poll interval. |
| `INTERVAL_DETECTION` | `120` | Detection-pass interval (no job scheduled when `DETECTION_ENABLED=false`). |

### HTTP

| Variable | Default | Purpose |
| --- | --- | --- |
| `REQUEST_TIMEOUT` | `30` | Per-request HTTP timeout (seconds). |
| `USER_AGENT` | `Mozilla/5.0 (compatible; trump-tracker/1.0; …)` | User-Agent header for outbound requests. |

### Detection (Ollama)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DETECTION_ENABLED` | `true` | Set `false` to collect without the LLM — items queue up unprocessed until it's re-enabled. |
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama generate endpoint. |
| `OLLAMA_MODEL` | `qwen3:8b` | Model used for detection. |
| `OLLAMA_TIMEOUT` | `180` | Seconds per inference — generous, because the first call after idle also loads the model into RAM. |
| `DETECTION_BATCH_SIZE` | `10` | Items analyzed per detection pass (newest content first). |
| `DETECTION_MAX_ATTEMPTS` | `3` | Times a single item may time out before it's given up on and marked processed. |
| `DETECTION_RETRY_COOLDOWN` | `600` | Seconds before a timed-out item becomes eligible for retry. |

### Alerting

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | _(empty)_ | Discord webhook that actionable endorsements are posted to. **Secret** — set it in `src/.env`, never commit it. Unset = detections are logged only. |

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

Working: all four collectors, Postgres storage/dedup, local LLM detection, and
Discord alerting on actionable detections (best-effort, gated on `DISCORD_WEBHOOK_URL`).
