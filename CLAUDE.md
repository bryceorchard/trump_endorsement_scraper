# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A pipeline intended to run on a Raspberry Pi 5 that:

1. Continuously scrapes every source where Trump speaks or posts (Truth Social, Twitter/X, White House briefing room, RSS news feeds).
2. Runs each new item through a local LLM (Qwen3-8B via Ollama) to detect whether he's endorsing a company, brand, or financial asset (stock/crypto).
3. Logs/alerts on actionable detections (alerting itself is not yet implemented — see "Known gaps" below).

## Module layout & import convention

`src/` is the import root (not itself a package — it has no `__init__.py`). Its subdirectories
`collectors/`, `config/`, `database/`, `detector/` are all packages (each has an `__init__.py`).
Running `python3 main.py` from inside `src/` puts `src/` on `sys.path[0]`, so these packages
resolve without any `PYTHONPATH`/`sys.path` fiddling.

Cross-module imports are **package-qualified** — match this convention, don't reintroduce flat
top-level imports:

- `from config import config` — imports the `config.py` module from the `config` package and binds
  the name `config`, so `config.FOO` attribute access works everywhere.
- `from database.database import ...`, `from detector.endorsement_detector import ...`.
- Within `collectors/`, sibling imports are relative (`from .base import ...`).

> The detector's manual-test entry point is `python3 -m detector.endorsement_detector` (run from
> `src/`), not the file directly; `python3 main.py` is likewise run from `src/`. `docs/SETUP.md`
> reflects this.

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

### Running things on the Pi

The deployment target is a headless Raspberry Pi 5. To run or inspect anything there:

> ⚠️ **LAN-only — no remote access.** The Pi is reachable only on the local network
> (`192.168.2.10`). There is currently no VPN/overlay/tunnel, so **off-LAN, `ssh brycepi5` and the
> Syncthing GUI just time out** — don't retry, and defer any Pi commands until back on the local
> network (or until a remote path is set up). Note that Syncthing still deploys code whenever both
> machines are next online together; it's only *interactive* Pi access that requires the LAN.

- **SSH (on-LAN):** `ssh brycepi5` — alias in `~/.ssh/config` → `192.168.2.10`, user `bryce`, key
  `~/.ssh/brycepi5` (aarch64, key-based). For non-interactive/scripted commands add
  `-o BatchMode=yes`.
- **Project path on Pi:** `/home/bryce/project` (the synced deploy set); the app's import root is
  `/home/bryce/project/src` — run it from there with the Pi's own root venv:
  `cd ~/project/src && ../.venv/bin/python3 main.py` (see "Module layout & import convention" and
  `docs/SETUP.md`). `scripts/setup.sh` creates that `.venv`.
- **Service:** it runs under systemd as `trump-tracker`. Manage it with
  `sudo systemctl {status,restart,stop} trump-tracker` and tail logs with
  `journalctl -u trump-tracker -f`.
- **Syncthing GUI:** `https://192.168.2.10:8384` (TLS, self-signed — accept once).

See [DEVLOG.md](DEVLOG.md) → "Syncthing deployment" for the full deployment picture.

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

- **`collectors/`** — one class per source, all subclassing `BaseCollector` (`collectors/base.py`). Each subclass implements only `collect() -> list[CollectedItem]`; the base class handles run logging (`collection_runs` table), upsert/dedup, and per-item error isolation (one bad item doesn't fail the run). Sources: `truth_social.py` (Mastodon-compatible public API, no auth — but fetched via `curl_cffi` Chrome TLS impersonation because Cloudflare 403s plain `requests` regardless of User-Agent), `twitter.py` (via `twscrape`, needs registered accounts, degrades to a no-op if `twscrape` isn't installed; forces twscrape's `curl_cffi` backend via `TWS_HTTP_BACKEND=curl` since X's Cloudflare 403s its default `httpx` client — cookie-based auth is the fallback when X's login-flow anti-automation blocks password login, see `docs/SETUP.md` Step 5), `whitehouse.py` (WordPress per-section RSS feeds — `/news/`, `/remarks/`, `/briefings-statements/`, `/presidential-actions/` — full text from `content:encoded` with an article-page fallback; the old `/briefing-room/*` listing pages were removed in the 2025 redesign and the new listings are JS-rendered), `rss.py` (feedparser, filtered to entries containing a keyword from `RSS_FILTER_KEYWORDS`).
- **`database/database.py`** — owns the Postgres schema (`sources`, `items`, `collection_runs`, `endorsements`) and is the only module that touches SQL. `init_db()` is idempotent (CREATE TABLE IF NOT EXISTS + seed sources via ON CONFLICT DO NOTHING) and is called unconditionally at the top of `main()` regardless of which CLI mode is selected. Dedup key is `(source_id, external_id)`; `external_id` is the platform's native ID where one exists, otherwise a SHA-256 hash of the URL (whitehouse, rss-without-link). The full original API/HTML payload is kept in `items.raw_json` so items can be re-analyzed later without re-fetching.
- **`detector/endorsement_detector.py`** — `detect_endorsement(text)` posts to a local Ollama instance (`OLLAMA_URL`) using `OLLAMA_MODEL` (default `qwen3:8b`) with `"think": false` (disables Qwen3 chain-of-thought — needed because it roughly doubles inference time and isn't useful for this structured-extraction task) and `temperature: 0.1`, then parses the model's JSON response into an `EndorsementResult` dataclass. Raises `RuntimeError` for transport-level failures (unreachable, HTTP error such as model not pulled), which the caller in `main.py` treats as "stop the detection loop, don't mark items processed"; a per-call timeout instead raises `DetectionTimeout`, which `main.py` retries per-item — deferring the item for the rest of the current run — up to `DETECTION_MAX_ATTEMPTS` (tracked in `items.detection_attempts`) before marking it processed. Unparseable model *output* (`ValueError`) counts against the item itself. Sends `keep_alive: 30m` so the model stays resident between detection cycles (a cold load on the Pi costs ~a minute). `is_actionable()` gates alerting on confidence (`high`/`medium`) and `endorsement_type != "none"`.
- **`main.py`** — CLI entry point and scheduler. `run_detection()` pulls a batch of unprocessed items (`processed_at IS NULL`, **newest content first** — `published_at DESC NULLS LAST` — so fresh posts always beat backlog), runs each through the detector, and persists results via `save_endorsement()`, which also stamps `items.processed_at` so items aren't reprocessed. One call processes a single `DETECTION_BATCH_SIZE` batch and logs how much of the queue that covers; `--drain` (with `--run-once`/`--detect-only`) loops batches until the queue is empty, and one-shot modes exit non-zero if detection couldn't run (Ollama unreachable). A permanently-broken item (unparseable model output, or one that exhausts its `DETECTION_MAX_ATTEMPTS` timeout retries) is still marked processed via a synthetic "no detection" result, specifically to avoid retrying it forever; database-transport errors stop the run instead of counting against any item. In scheduled mode, each collector and the detection pass run as independent APScheduler interval jobs (`BlockingScheduler`), each configured to fire immediately on startup as well as on its interval.

## Configuration

All configuration is environment-variable driven through `config/config.py` (re-implemented with defaults — there's no `.env`-loading code in the module itself). The `scripts/` helper scripts load `src/.env` for you (via `scripts/_env.sh`, which does `set -a; . src/.env; set +a`); to run the app by hand instead, load it the same way or use `python-dotenv` before running. `src/.env.example` documents the variables (JSON values are single-quoted so they survive sourcing). Notable ones:

- `DETECTION_ENABLED=false` — run collectors without Ollama running (useful while Ollama isn't set up yet).
- `TWITTER_ACCOUNTS_JSON` — required for the Twitter collector; `twscrape` needs at least one real, already-registered Twitter account.
- `RSS_FEEDS_JSON` / `RSS_FILTER_KEYWORDS` — override the default feed list / Trump-relevance keyword filter.

## Commands

There is no test suite, build step, or linter configured in this repo yet.

```bash
# install into a project-root virtualenv (never system Python); scripts/setup.sh does this on the Pi
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# one-time Twitter/twscrape account registration — see docs/SETUP.md Step 5

# run from inside src/ (it's the import root — see "Module layout & import convention")
python3 main.py --run-once              # run all collectors + detection once
python3 main.py --collector truth_social  # run a single named collector (truth_social|twitter|whitehouse|rss)
python3 main.py --detect-only           # run detection only, against whatever's already in the DB
python3 main.py                          # scheduled mode (blocking), per-source intervals from config
python3 -m detector.endorsement_detector  # quick manual test of the detector against hardcoded sample text
```

Full setup instructions (Ollama/Qwen3-8B install, Postgres setup, systemd service, troubleshooting) are in `docs/SETUP.md`.
