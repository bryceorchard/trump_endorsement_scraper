#!/usr/bin/env bash
# setup.sh — One-time setup for trump_tracker on Raspberry Pi 5
# Run as your normal user (not root). sudo is used only where required.
# Safe to run from anywhere — paths are resolved relative to this script.
set -e

# Resolve locations from the script itself, so cwd doesn't matter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$PROJECT_ROOT/src"
VENV="$PROJECT_ROOT/.venv"   # project-root venv: outside the Syncthing allowlist, so ARM
                             # binaries never sync — the Pi keeps its own native .venv.

echo "=== trump_tracker setup ==="
echo ""

# Prompt (hidden, confirmed, non-empty) for a DB password into the global DB_PASSWORD.
# Called only when we actually need one — see the PostgreSQL step.
prompt_db_password() {
    while true; do
        read -rs -p "  Database password: " DB_PASSWORD; echo
        read -rs -p "  Confirm password:  " _db_password_confirm; echo
        if [ -z "$DB_PASSWORD" ]; then
            echo "  Password cannot be empty — try again."
        elif [ "$DB_PASSWORD" != "$_db_password_confirm" ]; then
            echo "  Passwords don't match — try again."
        else
            break
        fi
    done
    unset _db_password_confirm
}

# ── 1. Ollama ─────────────────────────────────────────────────────────────────
# Install only if missing, so a re-run doesn't reinstall/restart (and re-race).
if ! command -v ollama >/dev/null 2>&1; then
    echo "[1/5] Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "[1/5] Ollama already installed, skipping install."
fi

# The installer starts Ollama as a systemd service; wait until it accepts
# connections before pulling (avoids a "could not connect" race on a fresh start).
echo "[1/5] Waiting for the Ollama server..."
for _ in $(seq 1 30); do
    ollama list >/dev/null 2>&1 && break
    sleep 1
done

# Pull the model only if it isn't already downloaded.
if ollama list 2>/dev/null | grep -q '^qwen3:8b'; then
    echo "[1/5] qwen3:8b already present, skipping pull."
else
    echo "[1/5] Pulling qwen3:8b (~5.5 GB, this will take a few minutes)..."
    ollama pull qwen3:8b
fi

echo "[1/5] Verifying Ollama..."
ollama run qwen3:8b "Say hello" --nowordwrap
echo ""

# ── 2. PostgreSQL ─────────────────────────────────────────────────────────────
echo "[2/5] Setting up PostgreSQL..."
# Install only if psql isn't already on the system.
command -v psql >/dev/null 2>&1 || sudo apt-get install -y postgresql postgresql-contrib

# Start postgres if not already running (both are idempotent no-ops if it is).
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Detect what already exists so we don't clobber a live database unprompted.
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='trump_tracker'" 2>/dev/null || true)
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='trump_tracker_user'" 2>/dev/null || true)

# DB_PASSWORD is set only when we (re)set the password below; it gates both the
# ALTER ROLE and the DATABASE_URL rewrite in the .env step.
DB_PASSWORD=""

if [ -n "$DB_EXISTS" ] || [ -n "$ROLE_EXISTS" ]; then
    echo "  Existing 'trump_tracker' database and/or 'trump_tracker_user' role detected."
    echo "    1) Keep as-is (default)"
    echo "    2) Reset the role password"
    echo "    3) Drop & recreate the database (DELETES ALL COLLECTED DATA)"
    read -rp "  Choice [1/2/3]: " db_choice
    case "$db_choice" in
        2)
            prompt_db_password
            ;;
        3)
            read -rp "  This DELETES all data in 'trump_tracker'. Type DROP to confirm: " confirm_drop
            if [ "$confirm_drop" = "DROP" ]; then
                prompt_db_password
                sudo -u postgres psql -c "DROP DATABASE IF EXISTS trump_tracker;"
                DB_EXISTS=""   # force recreation below
            else
                echo "  Not confirmed — keeping the existing database and password."
            fi
            ;;
        *)
            echo "  Keeping the existing database and password."
            ;;
    esac
else
    echo "  No existing database/role — creating a new one."
    prompt_db_password
fi

# Create database / role only if missing (safe to re-run).
[ -z "$DB_EXISTS" ]   && sudo -u postgres psql -c "CREATE DATABASE trump_tracker;"
[ -z "$ROLE_EXISTS" ] && sudo -u postgres psql -c "CREATE ROLE trump_tracker_user LOGIN;"

# Apply the password only when we collected one (fresh, reset, or recreate).
if [ -n "$DB_PASSWORD" ]; then
    DB_PASSWORD_SQL=${DB_PASSWORD//\'/\'\'}   # double single quotes for the SQL literal
    sudo -u postgres psql -c "ALTER ROLE trump_tracker_user PASSWORD '$DB_PASSWORD_SQL';"
fi

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE trump_tracker TO trump_tracker_user;" 2>/dev/null || true
echo "  PostgreSQL ready (database 'trump_tracker', role 'trump_tracker_user')."

# ── 3. Python dependencies (in a virtualenv) ──────────────────────────────────
echo "[3/5] Setting up the Python virtualenv..."
# Build the venv only if it doesn't exist yet (installing python3-venv only then).
if [ ! -d "$VENV" ]; then
    sudo apt-get install -y python3-venv
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip
    echo "  Created virtualenv at $VENV"
else
    echo "  Virtualenv already exists at $VENV, reusing it."
fi

# (Re)install dependencies only when requirements.txt changed since the last
# successful install — tracked by a hash stamp inside the venv.
REQ_HASH=$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$SCRIPT_DIR/requirements.txt")
REQ_STAMP="$VENV/.requirements-sha256"
if [ "$(cat "$REQ_STAMP" 2>/dev/null)" = "$REQ_HASH" ]; then
    echo "  Dependencies already up to date, skipping install."
else
    echo "  Installing Python dependencies..."
    "$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
    echo "$REQ_HASH" > "$REQ_STAMP"
fi
echo ""

# ── 4. Environment file ───────────────────────────────────────────────────────
echo "[4/5] Setting up .env..."
if [ ! -f "$SRC_DIR/.env" ]; then
    cp "$SRC_DIR/.env.example" "$SRC_DIR/.env"
    if [ -n "$DB_PASSWORD" ]; then
        # Point DATABASE_URL at the password we just set (| delimiter: URL has /).
        DB_PASSWORD_URL=$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASSWORD")
        sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://trump_tracker_user:${DB_PASSWORD_URL}@localhost/trump_tracker|" "$SRC_DIR/.env"
        echo "  Created $SRC_DIR/.env from .env.example, with DATABASE_URL set to your DB password."
    else
        echo "  Created $SRC_DIR/.env from .env.example — set DATABASE_URL's password yourself"
        echo "    (kept the existing DB password, which this script doesn't know)."
    fi
    echo "  !! Still edit it to fill in your Twitter credentials before running."
else
    echo "  $SRC_DIR/.env already exists, leaving it untouched (DATABASE_URL not modified)."
fi
echo ""

# ── 5. Twitter / twscrape account setup ──────────────────────────────────────
echo "[5/5] Twitter/twscrape account registration is a separate step."
echo "  After filling in TWITTER_ACCOUNTS_JSON in src/.env, run: scripts/setup_twitter.sh"
echo "  (Skip it if you don't need the X/Twitter collector.)"
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
echo "=== Setup complete ==="
echo ""
echo "Next steps — edit src/.env with your credentials, then use the helper scripts:"
echo "  scripts/setup_twitter.sh   # one-time X/Twitter account registration (optional)"
echo "  scripts/run_once.sh        # initialise the DB + run all collectors + detection once"
echo "  scripts/test_detector.sh   # quick manual test of the detector (needs Ollama)"
echo "  scripts/start.sh           # run the scheduler in the foreground"
