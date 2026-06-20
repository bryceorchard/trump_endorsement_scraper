# Session Summary — trump_stocks_project
**Date:** 2026-06-20
**Model:** Claude Sonnet 4.6 (Cowork mode)

---

## Project Goal

Build a system that runs on a **Raspberry Pi 5 (16 GB)** which:
1. Continuously scrapes every source where Trump speaks or posts
2. Passes each new item through a **local LLM** (Qwen3-8B via Ollama) to detect company/stock endorsements
3. Alerts the user whenever an actionable endorsement is detected

The scraping/collection layer was the focus of this session. The LLM detection layer was partially provided by the user (`endorsement_detector.py`) and integrated here.

---

## Project Structure (as reorganised by user)

```
trump_stocks_project/
├── Past Conversations/       ← session summaries (this file)
├── docs/                     ← documentation
├── guidelines/               ← project guidelines
├── scripts/                  ← utility scripts
├── skills/                   ← Claude Code skills
│   ├── debug-issue/
│   ├── explore-codebase/
│   ├── refactor-safely/
│   └── review-changes/
├── src/                      ← (user reorganised source here)
│   ├── collectors/
│   ├── config/
│   ├── database/
│   └── detector/
├── test/                     ← tests
└── code/                     ← original working directory (files written here first)
    ├── main.py
    ├── database.py
    ├── config.py
    ├── endorsement_detector.py
    ├── requirements.txt
    ├── setup.sh
    ├── SETUP.md
    ├── .env.example
    └── collectors/
        ├── __init__.py
        ├── base.py
        ├── truth_social.py
        ├── twitter.py
        ├── whitehouse.py
        └── rss.py
```

> **Note:** The user reorganised the project into `src/` subdirectories during this session. It is not confirmed whether the files in `code/` were moved into `src/` — verify with the user at the start of the next session.

---

## What Was Built This Session

### `database.py`
PostgreSQL schema and helper functions. Tables:
- `sources` — known data sources (truth_social, twitter, whitehouse, rss)
- `items` — every collected post/transcript, deduplicated by `(source_id, external_id)`. Has a `processed_at` column to track what's been through the LLM.
- `collection_runs` — log of each collector invocation (timing, counts, errors)
- `endorsements` — one row per analyzed item, stores full `EndorsementResult` fields

Key functions: `init_db()`, `upsert_item()`, `log_run()`, `finish_run()`, `get_unprocessed_items()`, `save_endorsement()`, `get_recent_endorsements()`

### `config.py`
All settings via environment variables with sensible defaults. Covers:
- Database URL
- Truth Social account ID and base URL
- Twitter credentials (JSON array for twscrape)
- White House scrape limit
- RSS feed list and keyword filter
- Scheduler intervals for each collector + detection
- Ollama: URL, model name, timeout, batch size, enabled flag

### `collectors/base.py`
Abstract `BaseCollector` class. Subclasses implement `collect() -> list[CollectedItem]`. The base class handles run logging, deduplication calls, and error handling automatically.

### `collectors/truth_social.py`
Uses Truth Social's **Mastodon-compatible public API** (`/api/v1/accounts/{id}/statuses`). No auth required for public accounts. Strips HTML from post content. Default account ID: `107780257626128497`.

> **Watch out:** This ID can change. If you get 404s, look up the current ID with:
> `curl "https://truthsocial.com/api/v1/accounts/lookup?acct=realDonaldTrump" | python3 -m json.tool | grep '"id"'`

### `collectors/twitter.py`
Uses **twscrape** — scrapes Twitter without the paid API by using real Twitter accounts under the hood. Targets `@realDonaldTrump`. Requires one-time account registration (see SETUP.md). Gracefully skips if twscrape isn't installed.

### `collectors/whitehouse.py`
Scrapes three sections of `whitehouse.gov/briefing-room/`:
- `/speeches-remarks/`
- `/press-briefings/`
- `/statements-releases/`

Fetches full article text for each listing. Uses URL hash as `external_id` for stable deduplication.

### `collectors/rss.py`
Reads standard RSS/Atom feeds via `feedparser`. Filters items to only those containing keywords from `RSS_FILTER_KEYWORDS` (default: `["trump", "donald"]`). Default feeds: Reuters, Fox News, CNN, NPR Politics, Politico, The Hill, C-SPAN.

### `endorsement_detector.py` (user-provided, patched this session)
Uses **Ollama + Qwen3-8B** locally. Takes any text string, returns a structured `EndorsementResult` dataclass with:
- `endorsement_detected` (bool)
- `company`, `ticker` (str or None)
- `confidence` (high/medium/low)
- `quote` (the specific endorsing phrase)
- `endorsement_type` (explicit/implicit/financial/none)

Key settings: `"think": False` disables Qwen3's chain-of-thought mode (roughly doubles inference time if enabled). `temperature: 0.1` keeps JSON output consistent.

**Patch applied this session:** Was hardcoding `OLLAMA_URL` and `MODEL`. Now imports from `config.py` so `.env` values are respected.

### `main.py`
Entry point. Uses **APScheduler** (`BlockingScheduler`) to run all collectors and detection on configurable intervals. CLI flags:
- `python main.py` — run scheduler (blocking)
- `python main.py --run-once` — run all collectors + detection once and exit
- `python main.py --collector <name>` — run one collector and exit
- `python main.py --detect-only` — run detection on unprocessed items and exit

Detection flow in `run_detection()`:
1. Calls `get_unprocessed_items(batch_size=N)` from DB
2. Passes each item's content to `detect_endorsement()`
3. Calls `save_endorsement()` to persist result and mark item processed
4. Logs a `WARNING`-level alert for any `is_actionable()` result (high/medium confidence, non-none type)
5. Stops loop gracefully if Ollama isn't running (RuntimeError)

### `setup.sh`
One-command setup script for the Pi. Covers: Ollama install + model pull, PostgreSQL setup, pip deps, `.env` creation. Prints the twscrape registration command (must be run manually after filling in `.env`).

### `SETUP.md`
Full step-by-step setup guide including: Ollama install, PostgreSQL setup, `.env` variable reference table, twscrape registration, pipeline verification commands, systemd service file, and troubleshooting section.

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Best library support for scraping + later LLM integration |
| Database | PostgreSQL | User's preference; good for structured querying later |
| Scheduling | APScheduler (BlockingScheduler) | Simple, no separate process needed |
| Truth Social | Mastodon REST API | Public, no auth required |
| Twitter | twscrape | No paid API needed |
| White House | BeautifulSoup scraping | Simple HTML parsing |
| RSS/News | feedparser | Standard, reliable |
| LLM | Qwen3-8B via Ollama | Runs locally on Pi 5, ~5.5 GB RAM at Q4_K_M |

---

## What Still Needs to Be Done

### High priority
1. **Alerting system** — nothing actually sends a notification yet. `main.py` logs to stdout only. Need an `alerter.py` module wired into `run_detection()`. Good options for Pi: `ntfy.sh` (self-hostable push notifications), Telegram bot, Pushover, or email via SMTP.

2. **Confirm file locations after reorganisation** — user reorganised project into `src/` subdirectories. Verify imports still resolve correctly if files were moved.

### Medium priority
3. **Retry/backoff on rate limits** — collectors raise immediately on 429. Simple exponential backoff would make them more robust long-term.

4. **Smoke test script** — a `test_pipeline.py` that hits one real endpoint and prints results (no Postgres needed) would make debugging on the Pi easier.

5. **`.env.example` is slightly stale** — missing `INTERVAL_DETECTION` (though all other new vars are present). Minor.

### Lower priority
6. **systemd service** — template is in SETUP.md but the `.service` file hasn't been written as an actual file in the repo yet.

---

## Key Decisions Made

- **`raw_json` column** stores the full original API payload for every item. The LLM can re-analyze it later without re-fetching.
- **`processed_at` on items** (not a separate table) tracks detection state — simple and queryable.
- **`DETECTION_ENABLED` flag** in config allows running collectors without Ollama running — useful during initial setup/testing.
- **Batch detection** runs separately from collection on its own interval (default 2 min), not inline with each collector run. Keeps concerns separated and allows catching up after downtime.
- Detection errors on a single item still mark it as processed (via `_make_error_result`) to avoid infinite retry loops on permanently broken items.
