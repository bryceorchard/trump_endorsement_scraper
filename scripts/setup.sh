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

# ── 0. Configuration ──────────────────────────────────────────────────────────
# Collect interactive input up front so the long install steps run unattended.
echo "Choose a password for the PostgreSQL user 'trump_tracker_user':"
while true; do
    read -rs -p "  Database password: " DB_PASSWORD; echo
    read -rs -p "  Confirm password:  " DB_PASSWORD_CONFIRM; echo
    if [ -z "$DB_PASSWORD" ]; then
        echo "  Password cannot be empty — try again."
    elif [ "$DB_PASSWORD" != "$DB_PASSWORD_CONFIRM" ]; then
        echo "  Passwords don't match — try again."
    else
        break
    fi
done
unset DB_PASSWORD_CONFIRM
# Escape single quotes for the SQL string literal; percent-encode for the DATABASE_URL.
DB_PASSWORD_SQL=${DB_PASSWORD//\'/\'\'}
DB_PASSWORD_URL=$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASSWORD")
echo ""

# ── 1. Ollama ─────────────────────────────────────────────────────────────────
echo "[1/5] Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh

echo "[1/5] Pulling qwen3:8b (~5.5 GB, this will take a few minutes)..."
ollama pull qwen3:8b

echo "[1/5] Verifying Ollama..."
ollama run qwen3:8b "Say hello" --nowordwrap
echo ""

# ── 2. PostgreSQL ─────────────────────────────────────────────────────────────
echo "[2/5] Setting up PostgreSQL..."
sudo apt-get install -y postgresql postgresql-contrib

# Start postgres if not already running
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Create the database and role (safe to re-run). The role's password is always
# (re)set via ALTER ROLE so a re-run picks up a newly chosen password.
sudo -u postgres psql -c "CREATE DATABASE trump_tracker;" 2>/dev/null || echo "  (database already exists, skipping)"
sudo -u postgres psql -c "CREATE ROLE trump_tracker_user LOGIN;" 2>/dev/null || echo "  (role already exists, updating password)"
sudo -u postgres psql -c "ALTER ROLE trump_tracker_user PASSWORD '$DB_PASSWORD_SQL';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE trump_tracker TO trump_tracker_user;" 2>/dev/null || true
echo "  PostgreSQL database 'trump_tracker' and role 'trump_tracker_user' ready; password set from your input."

# ── 3. Python dependencies (in a virtualenv) ──────────────────────────────────
echo "[3/5] Installing Python dependencies into a virtualenv..."
sudo apt-get install -y python3-venv
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
echo "  Virtualenv ready at $VENV"
echo ""

# ── 4. Environment file ───────────────────────────────────────────────────────
echo "[4/5] Setting up .env..."
if [ ! -f "$SRC_DIR/.env" ]; then
    cp "$SRC_DIR/.env.example" "$SRC_DIR/.env"
    # Point DATABASE_URL at the role/password we just configured (| delimiter: URL has /).
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://trump_tracker_user:${DB_PASSWORD_URL}@localhost/trump_tracker|" "$SRC_DIR/.env"
    echo "  Created $SRC_DIR/.env from .env.example, with DATABASE_URL set to your DB password."
    echo "  !! Still edit it to fill in your Twitter credentials before running."
else
    echo "  $SRC_DIR/.env already exists, leaving it untouched (DATABASE_URL not modified)."
fi
echo ""

# ── 5. Twitter / twscrape account setup ──────────────────────────────────────
echo "[5/5] Registering Twitter accounts with twscrape..."
echo "  This requires TWITTER_ACCOUNTS_JSON to be set in src/.env."
echo "  Run this step manually after editing src/.env:"
echo ""
echo "    cd \"$SRC_DIR\""
echo "    export \$(cat .env | xargs)"
echo "    \"$VENV/bin/python3\" -c \""
echo "    import asyncio, json"
echo "    from config import config"
echo "    from twscrape import AccountsPool"
echo "    async def add():"
echo "        pool = AccountsPool()"
echo "        for acc in json.loads(config.TWITTER_ACCOUNTS_JSON):"
echo "            await pool.add_account(**acc)"
echo "        await pool.login_all()"
echo "    asyncio.run(add())"
echo "    \""
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
echo "=== Setup complete ==="
echo ""
echo "Next steps (run the app from src/ with the venv's Python):"
echo "  1. Edit src/.env with your credentials"
echo "  2. Run the twscrape account registration above"
echo "  3. cd \"$SRC_DIR\""
echo "  4. Initialise the database:  \"$VENV/bin/python3\" main.py --run-once"
echo "  5. Test the detector:        \"$VENV/bin/python3\" -m detector.endorsement_detector"
echo "  6. Start the scheduler:      \"$VENV/bin/python3\" main.py"
