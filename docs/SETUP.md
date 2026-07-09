# trump_tracker — Setup Guide

Tested on Raspberry Pi 5 (16 GB) running Raspberry Pi OS (64-bit).

---

## Prerequisites

- Python 3.11+
- PostgreSQL 14+
- An internet connection for the initial model pull (~5.5 GB)
- At least one real Twitter/X account (for twscrape)

---

## Quickstart

```bash
chmod +x setup.sh
./setup.sh
```

The script handles steps 1–4 below automatically. Read on if you prefer to run steps manually or need to troubleshoot.

---

## Step 1 — Install Ollama and pull the model

```bash
# Install Ollama (ARM64 binary, works on Pi 5 out of the box)
curl -fsSL https://ollama.com/install.sh | sh

# Pull Qwen3-8B (Q4_K_M quantization by default, ~5.5 GB)
ollama pull qwen3:8b

# Verify it works
ollama run qwen3:8b "Say hello"
```

Ollama starts automatically as a systemd service after install. It listens on `http://localhost:11434` by default. The endorsement detector talks to this endpoint directly — no configuration needed unless you move Ollama to another machine.

**Why Qwen3-8B?**
At 4-bit quantization it uses ~5.5 GB of RAM, leaving plenty of headroom on the 16 GB Pi. `"think": False` is set in the detector to disable Qwen3's chain-of-thought mode — you don't need it for structured extraction and it roughly doubles inference time.

---

## Step 2 — Set up PostgreSQL

> `scripts/setup.sh` does all of this for you — it prompts for the DB password, detects an existing
> database (offering keep / reset-password / drop-and-recreate), and writes `DATABASE_URL` into
> `src/.env`. The commands below are the manual equivalent.

```bash
sudo apt-get install -y postgresql postgresql-contrib
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Create the database and role
sudo -u postgres psql -c "CREATE DATABASE trump_tracker;"
sudo -u postgres psql -c "CREATE ROLE trump_tracker_user LOGIN PASSWORD 'yourpassword';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE trump_tracker TO trump_tracker_user;"
# PostgreSQL 15+ (bookworm's default) needs an explicit grant to create tables in schema public;
# this must run inside the target database:
sudo -u postgres psql -d trump_tracker -c "GRANT ALL ON SCHEMA public TO trump_tracker_user;"
```

Then set `DATABASE_URL` in `src/.env` to match (`postgresql://trump_tracker_user:yourpassword@localhost/trump_tracker`).

---

## Step 3 — Install Python dependencies

Install into a project-root virtualenv (kept out of the Syncthing allowlist, so the Pi builds its
own native `.venv` — never install into system Python with `--break-system-packages`):

```bash
sudo apt-get install -y python3-venv
python3 -m venv .venv          # from the project root
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r scripts/requirements.txt
```

Then invoke the app with `.venv/bin/python3` (the commands in Steps 5–6 assume this).

---

## Step 4 — Configure .env

```bash
cp .env.example .env
nano .env   # or your editor of choice
```

Required fields:

| Variable                | Description                                                                 |
| ----------------------- | --------------------------------------------------------------------------- |
| `DATABASE_URL`          | e.g. `postgresql://trump_tracker_user:yourpassword@localhost/trump_tracker` |
| `TWITTER_ACCOUNTS_JSON` | JSON array with at least one Twitter account (see below)                    |

Optional fields (defaults are sensible for a Pi):

| Variable                | Default                               | Description                                                  |
| ----------------------- | ------------------------------------- | ------------------------------------------------------------ |
| `DETECTION_ENABLED`     | `true`                                | Set `false` to skip LLM analysis while testing collectors    |
| `OLLAMA_URL`            | `http://localhost:11434/api/generate` | Change if Ollama is on another machine                       |
| `OLLAMA_MODEL`          | `qwen3:8b`                            | Model name as shown in `ollama list`                         |
| `OLLAMA_TIMEOUT`        | `60`                                  | Seconds to wait for inference (increase if Pi is under load) |
| `DETECTION_BATCH_SIZE`  | `10`                                  | Items analyzed per detection run                             |
| `INTERVAL_TRUTH_SOCIAL` | `300`                                 | Collection interval in seconds                               |
| `INTERVAL_TWITTER`      | `600`                                 |                                                              |
| `INTERVAL_WHITEHOUSE`   | `900`                                 |                                                              |
| `INTERVAL_RSS`          | `600`                                 |                                                              |
| `INTERVAL_DETECTION`    | `120`                                 |                                                              |

---

## Step 5 — Register Twitter accounts (twscrape)

twscrape needs at least one real Twitter account to scrape through. Set `TWITTER_ACCOUNTS_JSON` in
`src/.env` first, then run the helper (it loads `src/.env` and uses the venv's Python for you):

```bash
scripts/setup_twitter.sh
```

This is a one-time step. twscrape saves session tokens locally and reuses them.

---

## Step 6 — Verify the pipeline

The helper scripts each load `src/.env`, `cd` into `src/`, and run with the venv's Python:

```bash
scripts/run_once.sh        # initialise the DB schema + run all collectors + detection once
scripts/test_detector.sh   # test the endorsement detector directly (requires Ollama running)
```

For finer-grained checks you can still call the CLI directly (from `src/`, with the venv active and
`src/.env` loaded), e.g. `python3 main.py --collector truth_social` or `python3 main.py --detect-only`.

---

## Step 7 — Run as a service (recommended)

Create `/etc/systemd/system/trump-tracker.service`:

```ini
[Unit]
Description=Trump Statement Tracker
After=network.target postgresql.service ollama.service

[Service]
Type=simple
User=<user_name>
# start.sh cd's into src/, loads src/.env, and runs the scheduler with the venv's Python — so no
# WorkingDirectory / EnvironmentFile / interpreter path is needed here, and .env is loaded the same
# (bash source) way as everything else.
ExecStart=<your_dir>/trump_stocks_project/code/scripts/start.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable trump-tracker
sudo systemctl start trump-tracker

# Check logs
sudo journalctl -u trump-tracker -f
```

---

## Troubleshooting

**`RuntimeError: Ollama is not running`**

```bash
ollama serve          # start manually
# or
sudo systemctl start ollama
```

**twscrape returns no tweets:**
Twitter sessions expire. Re-run the account registration command in Step 5.

**Truth Social returns 404:**
Trump's account ID may have changed. Find the current ID:

```bash
curl "https://truthsocial.com/api/v1/accounts/lookup?acct=realDonaldTrump" | python3 -m json.tool | grep '"id"'
```

Update `TRUTH_SOCIAL_ACCOUNT_ID` in `.env`.

**Detection is slow:**
Normal on first run — the model loads into RAM. Subsequent calls are faster. If it's consistently slow, reduce `DETECTION_BATCH_SIZE` to `5` or increase `OLLAMA_TIMEOUT`.
