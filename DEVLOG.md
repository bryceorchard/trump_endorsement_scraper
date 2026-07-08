# 🛠️ Workspace & Workflow Devlog

> A personal log of **local usability changes** — the tooling, workflow, and
> deployment plumbing set up *around* this project rather than inside its code.
> The application itself lives in [CLAUDE.md](CLAUDE.md) and git history; this
> file is the story of the workbench.
>
> **Organized by subsystem.** Each chapter's overview reflects the *current*
> state; its collapsible **Changelog** records how it got there, newest first.

<sub>Dev machine: MacBook Air · Deploy target: headless Raspberry Pi 5</sub>

---

## Contents

| Chapter | Last updated |
| --- | :---: |
| [1. Change journal](#1-change-journal) | 2026-07-06 |
| [2. Git workflow and /commit](#2-git-workflow-and-commit) | 2026-07-06 |
| [3. Syncthing deployment](#3-syncthing-deployment) | 2026-07-06 |
| [4. Local tooling](#4-local-tooling) | 2026-07-06 |
| [5. Pi environment (setup.sh)](#5-pi-environment-setupsh) | 2026-07-08 |

---

## 1. Change journal

An auto-maintained record of uncommitted work at `.claude/pending-changes.md`, backed by a `Stop` hook that nudges if a turn ends with unlogged changes.

**How it works.** Entries are appended as work happens and the file is cleared on commit, so it always reflects work since the last commit. The `Stop` hook (`.claude/hooks/journal-reminder.sh`, registered in `.claude/settings.local.json`) blocks turn-end when tracked changes are newer than the journal. The journal is gitignored — git history is the permanent record.

<details><summary>📋 Changelog</summary>

- **2026-07-06**
  - Created the journal, the `Stop` hook backstop, and its registration.

</details>

---

## 2. Git workflow and /commit

A **Hybrid** git model driven by the `/commit` slash command: small changes go straight to `main`, non-trivial ones get a branch and a squash-merge PR.

**How it works.** `/commit` (optionally `/commit main` | `/commit pr`) reads the change journal, recommends a mode, commits, and clears the journal — confirming before any push or PR. Because deployment is by Syncthing (files, not commits), git is purely history/review, which keeps the whole thing low-stakes.

| Mode | When | Result |
| --- | --- | --- |
| Direct to `main` | small, low-risk, self-contained | one commit on `main` |
| Branch + PR | non-trivial or worth reviewing | short-lived branch → squash-merge PR |

Defined in `.claude/commands/commit.md`; convention documented in [CLAUDE.md](CLAUDE.md).

<details><summary>📋 Changelog</summary>

- **2026-07-06**
  - Added the `/commit` command and the Hybrid workflow.
  - First run merged as PR #1.

</details>

---

## 3. Syncthing deployment

One-way file sync from the Mac to the headless Raspberry Pi 5: **Mac `sendonly` → Pi `receiveonly`**. Ships only the runtime deploy set (`src/`, `scripts/`, `test/`) — not the whole working tree — so the Pi stays lean and git/docs/dev-tooling stay Mac-only. One-way by design — no conflict files, and the Mac is always source of truth.

**How it works.**

- **Access the Pi:** `ssh brycepi5` (→ `192.168.2.10`); project at `/home/bryce/project`.
- **Syncthing GUI:** `https://192.168.2.10:8384` (TLS; accept the self-signed cert once).
- **Ignore rules — allowlist** (kept matching on both ends; `.stignore` doesn't sync between devices): ship only `/src`, `/scripts`, `/test`; ignore everything else via a trailing `**`. Junk (`.DS_Store`, `._*`, `__pycache__`, `*.pyc`) is ignored *first* with the `(?d)` prefix so it's cleaned even inside kept dirs. Future-proof — new dev files at the repo root are auto-excluded from the Pi.
- **Golden rule:** the Pi builds its **own** `.venv` natively (ARM64) — never sync a venv or `.git`.

<details><summary>📋 Changelog</summary>

- **2026-07-06**
  - Switched `.stignore` from a denylist to an **allowlist** — ship only `src/`, `scripts/`, `test/`; removed the now-excluded dev files (`.claude/`, `docs/`, `previous_conversations/`, top-level `*.md`, `.gitignore`, `.markdownlint.json`) from the Pi. Verified `13 == 13`, `pullErrors: 0`.
  - Added the missing `.stignore` on the Pi (it had none); matched patterns on both ends.
  - Added the `(?d)` prefix to junk patterns to resolve a stuck directory-delete pull error.
  - Cleared 9 receive-only conflicts; removed a stale 604K `.git/` and scattered `.DS_Store`/`._*` cruft.
  - Switched the Pi's Syncthing GUI from plain HTTP on `0.0.0.0` to HTTPS (password already set).
  - Verified end state: `pullErrors: 0`, fully in sync (`24 == 24`), idle.

</details>

---

## 4. Local tooling

Local CLI tools and repo lint configuration that support the workflow.

**How it works.**

- **GitHub CLI (`gh`)** — used by `/commit` to open PRs end-to-end.
- **`.markdownlint.json`** (local, gitignored) — disables the `MD033` (inline-HTML) and `MD013` (line-length) rules so this log's collapsible `<details>` Changelogs, `<sub>` small-text, and long prose lines don't trip the editor's markdown linter.

<details><summary>📋 Changelog</summary>

- **2026-07-06**
  - Added a local (gitignored) `.markdownlint.json` disabling `MD041` (first line h1), `MD040` (fenced block language), `MD033` (inline HTML) and `MD013` (line length), because they annoy me.
  - Installed **GitHub CLI (`gh`) 2.96.0** via Homebrew and authenticated it.

</details>

---

## 5. Pi environment (setup.sh)

`scripts/setup.sh` provisions a fresh Raspberry Pi 5 for the pipeline: Ollama + the `qwen3:8b` model, PostgreSQL (db/user/grants), the Python dependencies, a scaffolded `.env`, and a printed twscrape account-registration step. It's re-runnable — each step no-ops or reuses if already done.

**How it works.**

- **Self-locating:** resolves `SCRIPT_DIR`/`PROJECT_ROOT`/`SRC_DIR`/`VENV` from `${BASH_SOURCE[0]}`, so it runs correctly from any cwd (not just the dir it happens to sit in).
- **Project-root virtualenv:** Python deps install into `$PROJECT_ROOT/.venv` (`/home/bryce/project/.venv` on the Pi), **never** system Python. Root placement keeps the venv outside the Syncthing allowlist (`/src /scripts /test`), so ARM binaries never sync — this is the concrete mechanism behind Chapter 3's "the Pi builds its own `.venv`" golden rule. Run the app with `.venv/bin/python3` (systemd's `ExecStart` and the `docs/SETUP.md` commands do this).
- **`.env` scaffold:** copies `src/.env.example` → `src/.env` (the app reads env from `src/`); left for you to fill in.

<details><summary>📋 Changelog</summary>

- **2026-07-08**
  - Replaced `pip install --break-system-packages` (system Python, overrides PEP 668) with a project-root `.venv` built via `python3 -m venv`; added the `python3-venv` apt dependency. Merged as PR #3.
  - Made the script self-locating (`SCRIPT_DIR`/`PROJECT_ROOT`) so it no longer depends on cwd; corrected the `.env` path to `src/.env`; de-staled the printed run commands (`from config import config`, `python3 -m detector.endorsement_detector`, run from `src/` with the venv Python).
  - Kept `docs/SETUP.md` and `CLAUDE.md` in step (venv-based install, systemd `ExecStart` → `.venv/bin/python3`).

</details>

---

<sub>📝 **Maintaining this log:** when a local/usability/workflow/deployment change happens, update the relevant chapter's overview (description + "how it works") to reflect the new state *and* add a dated entry to its Changelog, newest first. Extend an existing chapter or start a new one as fits. Keep it to the workbench — application-code changes belong in commits, not here.</sub>
