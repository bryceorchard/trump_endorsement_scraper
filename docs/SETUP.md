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

```bash
sudo apt-get install -y postgresql postgresql-contrib
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Create the database and user
sudo -u postgres psql -c "CREATE DATABASE trump_tracker;"
sudo -u postgres psql -c "CREATE USER trump_tracker_user WITH PASSWORD 'yourpassword';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE trump_tracker TO trump_tracker_user;"
```

---

## Step 3 — Install Python dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

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

twscrape needs at least one real Twitter account to scrape through. Set `TWITTER_ACCOUNTS_JSON` in `.env` first, then run:

Run from inside `src/` (the import root — see CLAUDE.md → "Module layout & import convention"):

```bash
cd src
export $(cat .env | xargs)

python3 -c "
import asyncio, json
from config import config
from twscrape import AccountsPool

async def add():
    pool = AccountsPool()
    for acc in json.loads(config.TWITTER_ACCOUNTS_JSON):
        await pool.add_account(**acc)
    await pool.login_all()

asyncio.run(add())
"
```

This is a one-time step. twscrape saves session tokens locally and reuses them.

---

## Step 6 — Verify the pipeline

Run from inside `src/` (the import root — see CLAUDE.md → "Module layout & import convention"):

```bash
cd src
export $(cat .env | xargs)

# 1. Initialise the database schema
python3 main.py --run-once

# 2. Test a single collector
python3 main.py --collector truth_social

# 3. Test the endorsement detector directly (requires Ollama running)
python3 -m detector.endorsement_detector

# 4. Test detection on whatever is already in the DB
python3 main.py --detect-only
```

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
# WorkingDirectory must be src/ — it's the import root, so `main.py` resolves the
# config/database/detector/collectors packages (see CLAUDE.md → "Module layout & import convention").
WorkingDirectory=<your_dir>/trump_stocks_project/code/src
EnvironmentFile=<your_dir>/trump_stocks_project/code/src/.env
ExecStart=<your_python_path>/python3 main.py
# Example: ExecStart=/usr/bin/python3 main.py
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
