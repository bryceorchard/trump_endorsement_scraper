#!/usr/bin/env bash
# setup.sh — One-time setup for trump_tracker on Raspberry Pi 5
# Run as your normal user (not root). sudo is used only where required.
set -e

echo "=== trump_tracker setup ==="
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

# Create the database (safe to run if it already exists)
sudo -u postgres psql -c "CREATE DATABASE trump_tracker;" 2>/dev/null || echo "  (database already exists, skipping)"
sudo -u postgres psql -c "CREATE USER trump_tracker_user WITH PASSWORD 'changeme';" 2>/dev/null || echo "  (user already exists, skipping)"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE trump_tracker TO trump_tracker_user;" 2>/dev/null || true
echo ""

# ── 3. Python dependencies ────────────────────────────────────────────────────
echo "[3/5] Installing Python dependencies..."
pip install -r requirements.txt --break-system-packages
echo ""

# ── 4. Environment file ───────────────────────────────────────────────────────
echo "[4/5] Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from .env.example."
    echo "  !! Edit .env and fill in your DATABASE_URL and Twitter credentials before running."
else
    echo "  .env already exists, skipping."
fi
echo ""

# ── 5. Twitter / twscrape account setup ──────────────────────────────────────
echo "[5/5] Registering Twitter accounts with twscrape..."
echo "  This requires TWITTER_ACCOUNTS_JSON to be set in your .env."
echo "  Run this step manually after editing .env:"
echo ""
echo "    export \$(cat .env | xargs)"
echo "    python3 -c \""
echo "    import asyncio, json, config"
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
echo "Next steps:"
echo "  1. Edit .env with your credentials"
echo "  2. Run the twscrape account registration above"
echo "  3. Initialise the database:  python3 main.py --run-once"
echo "  4. Test the detector:        python3 endorsement_detector.py"
echo "  5. Start the scheduler:      python3 main.py"
