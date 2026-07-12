#!/usr/bin/env bash
# setup_twitter.sh — one-time twscrape account registration for the X/Twitter collector.
# Reads TWITTER_ACCOUNTS_JSON from src/.env; twscrape caches session tokens in
# src/accounts.db. Uses twscrape's curl_cffi backend so X's Cloudflare doesn't
# 403 the login by TLS fingerprint (the httpx default does).
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

if [ -z "${TWITTER_ACCOUNTS_JSON:-}" ] || [ "${TWITTER_ACCOUNTS_JSON}" = "[]" ]; then
    echo "TWITTER_ACCOUNTS_JSON is not set in src/.env — nothing to register." >&2
    echo "(Set it only if you intend to run the X/Twitter collector.)" >&2
    exit 1
fi

cd "$SRC_DIR"
export TWS_HTTP_BACKEND=curl   # browser-impersonation backend (see header)

echo "Registering Twitter account(s) with twscrape..."
# login_all() logs errors but does NOT raise, so we inspect account status
# afterwards and exit non-zero if nothing actually logged in.
if "$PY" -c "
import asyncio, json, sys
from config import config
from twscrape import AccountsPool

async def register():
    pool = AccountsPool()
    for acc in json.loads(config.TWITTER_ACCOUNTS_JSON):
        await pool.add_account(**acc)
    await pool.login_all()
    info = await pool.accounts_info()
    for a in info:
        flag = 'OK    ' if a.get('active') else 'FAILED'
        print(f\"  [{flag}] {a.get('username')}  {a.get('error_msg') or ''}\")
    return sum(1 for a in info if a.get('active'))

sys.exit(0 if asyncio.run(register()) else 2)
"; then
    echo "Done — account logged in; twscrape will reuse its session tokens."
else
    cat >&2 <<'MSG'

Login did not succeed. If the error above is a Cloudflare 403 the backend is
wrong; otherwise (e.g. "Could not log you in now", code 399) it's X's own
anti-automation, not a config problem. Two ways forward:

  1. Wait a while and re-run — code 399 is often a temporary throttle.
  2. Use browser cookies (most reliable): log in to x.com in a browser, copy the
     'auth_token' and 'ct0' cookies, and add a "cookies" field to the account in
     TWITTER_ACCOUNTS_JSON:
        "cookies":"auth_token=XXXX; ct0=YYYY"
     twscrape won't update an account that already exists, so remove
     src/accounts.db first, then re-run this script.

The X/Twitter collector is optional — the other three collectors run without it.
MSG
    exit 1
fi
