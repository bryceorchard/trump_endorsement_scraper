# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A pipeline intended to run on a Raspberry Pi 5 that:

1. Continuously scrapes every source where Trump speaks or posts (Truth Social, Twitter/X, White House briefing room, RSS news feeds).
2. Runs each new item through a local LLM (Qwen3-8B via Ollama) to detect whether he's endorsing a company, brand, or financial asset (stock/crypto).
3. Logs/alerts on actionable detections (alerting itself is not yet implemented — see "Known gaps" below).

## Known gap: import paths don't match the directory layout

The source tree was reorganized into `src/{collectors,config,database,detector}/` subpackages, but the module code was never updated to match — every file still uses flat, top-level imports:

- `src/main.py` does `import config`, `from database import ...`, `from endorsement_detector import ...`, `from collectors import ...`
- `src/detector/endorsement_detector.py` does `import config`
- `src/collectors/*.py` do `import config` and `from .base import ...`

For any of this to run, `src/config/`, `src/database/`, `src/detector/` would need to be on `sys.path` directly (or the imports rewritten to `from config.config import ...` / `from database.database import ...` / `from detector.endorsement_detector import ...`, and `config` calls updated accordingly). **Don't assume `python src/main.py` works as-is** — check/fix import wiring before running or testing changes that touch cross-module calls. `docs/SETUP.md` describes commands (`python3 main.py`, `python3 endorsement_detector.py`) that assume the old flat layout (everything in one directory), not the current `src/` subpackage layout.

## Git & deployment workflow

Solo project. **Deployment is decoupled from git:** Syncthing syncs files (not commits) from
the Mac to the headless Pi, so whatever is on disk under the synced paths runs on the Pi
regardless of branch.

> ⚠️ **Syncthing ships an allowlist — only `src/`, `scripts/`, and `test/` reach the Pi.**
> Everything else at the repo root (docs, `.claude/`, `*.md`, root configs, any *new* top-level
> file or dir) is ignored and will **not** sync. If you add something the Pi needs at runtime,
> either put it under `src/`/`scripts/`/`test/`, **or** widen the allowlist in `.stignore` on
> *both* machines (Mac **and** Pi — `.stignore` doesn't sync itself); see
> [DEVLOG.md](DEVLOG.md) → "Syncthing deployment". A file placed outside the allowlist silently
> never reaches the Pi, which is an easy way to ship a broken deploy. The Pi builds its own
> `.venv` natively; `.git`/`.venv`/`__pycache__` never sync.

Because deployment doesn't depend on branch, git is purely for history + review, using a
**Hybrid** model:

- **Direct to `main`** for small, low-risk, self-contained changes (docs, config, single-file fixes).
- **Short-lived branch + squash-merge PR** for anything non-trivial or worth reviewing (features,
  multi-file refactors, detector/collector/schema logic) — this is where `/code-review` runs.

The `/commit` slash command (`.claude/commands/commit.md`) drives this: it reads the change
journal, picks/proposes the mode, commits, and clears the journal. Pushing and PR creation are
confirmed explicitly (outward-facing); local commits proceed on the `/commit` go-ahead.

## Change journal workflow

Maintain a running journal of uncommitted work in `.claude/pending-changes.md` (gitignored — git history is the permanent record once committed):

- **After each logical change**, append an entry: what changed, why, which files were touched, and current test status.
- **When committing**, read the journal, base the commit message on it, then clear the entries (leave the header) so the file only ever reflects work since the last commit.
- A `Stop` hook (`.claude/hooks/journal-reminder.sh`) is a backstop: it blocks the turn from ending if there are uncommitted changes newer than the journal, prompting a journal update. This is a reminder, not a substitute — write the entry as you go, don't wait for the nudge.

## Local devlog

`DEVLOG.md` (committed, persistent) logs **local usability changes** — tooling, workflow, and deployment plumbing set up *around* the code — **organized by subsystem**. Each chapter has a living **description + "how it works" overview** (kept current: edit it in place when the subsystem changes) and a collapsible **Changelog** of dated entries, newest first. When a local/usability/workflow/deployment change happens: update the relevant chapter's overview to reflect the new state *and* append a dated changelog entry; use discretion to extend an existing chapter or add a new one. It's distinct from the ephemeral `.claude/pending-changes.md` journal (gitignored, cleared each commit) and from this file (instructions/reference). Application-code changes do **not** go in `DEVLOG.md` — they belong in commits.

## Architecture

**Pipeline shape:** independent collectors write into Postgres; a separate detection pass reads unprocessed rows and runs them through a local LLM. Collection and detection are decoupled — they run on independent schedules and neither blocks the other.

- **`collectors/`** — one class per source, all subclassing `BaseCollector` (`collectors/base.py`). Each subclass implements only `collect() -> list[CollectedItem]`; the base class handles run logging (`collection_runs` table), upsert/dedup, and per-item error isolation (one bad item doesn't fail the run). Sources: `truth_social.py` (Mastodon-compatible public API, no auth), `twitter.py` (via `twscrape`, needs registered accounts, degrades to a no-op if `twscrape` isn't installed), `whitehouse.py` (BeautifulSoup scrape of three briefing-room sections), `rss.py` (feedparser, filtered to entries containing a keyword from `RSS_FILTER_KEYWORDS`).
- **`database/database.py`** — owns the Postgres schema (`sources`, `items`, `collection_runs`, `endorsements`) and is the only module that touches SQL. `init_db()` is idempotent (CREATE TABLE IF NOT EXISTS + seed sources via ON CONFLICT DO NOTHING) and is called unconditionally at the top of `main()` regardless of which CLI mode is selected. Dedup key is `(source_id, external_id)`; `external_id` is the platform's native ID where one exists, otherwise a SHA-256 hash of the URL (whitehouse, rss-without-link). The full original API/HTML payload is kept in `items.raw_json` so items can be re-analyzed later without re-fetching.
- **`detector/endorsement_detector.py`** — `detect_endorsement(text)` posts to a local Ollama instance (`OLLAMA_URL`) using `OLLAMA_MODEL` (default `qwen3:8b`) with `"think": false` (disables Qwen3 chain-of-thought — needed because it roughly doubles inference time and isn't useful for this structured-extraction task) and `temperature: 0.1`, then parses the model's JSON response into an `EndorsementResult` dataclass. Raises `RuntimeError` if Ollama isn't reachable (the caller in `main.py` treats this as "stop the detection loop, don't mark items processed" rather than a per-item failure). `is_actionable()` gates alerting on confidence (`high`/`medium`) and `endorsement_type != "none"`.
- **`main.py`** — CLI entry point and scheduler. `run_detection()` pulls a batch of unprocessed items (`processed_at IS NULL`), runs each through the detector, and persists results via `save_endorsement()`, which also stamps `items.processed_at` so items aren't reprocessed. A permanently-broken item (detector raises something other than `RuntimeError`) is still marked processed via a synthetic "no detection" result, specifically to avoid retrying it forever. In scheduled mode, each collector and the detection pass run as independent APScheduler interval jobs (`BlockingScheduler`), each configured to fire immediately on startup as well as on its interval.

## Configuration

All configuration is environment-variable driven through `config/config.py` (re-implemented with defaults — there's no `.env`-loading code in the module itself; load `.env` via `export $(cat .env | xargs)` or `python-dotenv` before running). `src/.env.example` documents the variables. Notable ones:

- `DETECTION_ENABLED=false` — run collectors without Ollama running (useful while Ollama isn't set up yet).
- `TWITTER_ACCOUNTS_JSON` — required for the Twitter collector; `twscrape` needs at least one real, already-registered Twitter account.
- `RSS_FEEDS_JSON` / `RSS_FILTER_KEYWORDS` — override the default feed list / Trump-relevance keyword filter.

## Commands

There is no test suite, build step, or linter configured in this repo yet.

```bash
pip install -r scripts/requirements.txt --break-system-packages   # or omit the flag off-Pi

# one-time Twitter/twscrape account registration — see docs/SETUP.md Step 5

python3 main.py --run-once              # run all collectors + detection once
python3 main.py --collector truth_social  # run a single named collector (truth_social|twitter|whitehouse|rss)
python3 main.py --detect-only           # run detection only, against whatever's already in the DB
python3 main.py                          # scheduled mode (blocking), per-source intervals from config
python3 endorsement_detector.py          # quick manual test of the detector against hardcoded sample text
```

(See "Known gap" above — these commands match the documented flat layout, not the current `src/` subpackage structure.)

Full setup instructions (Ollama/Qwen3-8B install, Postgres setup, systemd service, troubleshooting) are in `docs/SETUP.md`.
