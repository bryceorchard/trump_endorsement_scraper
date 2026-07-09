#!/usr/bin/env bash
# setup_twitter.sh — one-time twscrape account registration for the X/Twitter collector.
# Reads TWITTER_ACCOUNTS_JSON from src/.env; twscrape saves session tokens locally.
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

if [ -z "${TWITTER_ACCOUNTS_JSON:-}" ]; then
    echo "TWITTER_ACCOUNTS_JSON is not set in src/.env — nothing to register." >&2
    echo "(Set it only if you intend to run the X/Twitter collector.)" >&2
    exit 1
fi

cd "$SRC_DIR"
echo "Registering Twitter account(s) with twscrape..."
"$PY" -c "
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
echo "Done. twscrape has saved session tokens and will reuse them."
