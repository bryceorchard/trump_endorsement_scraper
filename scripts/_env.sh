#!/usr/bin/env bash
# _env.sh — shared bootstrap for the run/* wrapper scripts. SOURCE this, don't run it.
#
# Resolves the project's venv + src paths and loads src/.env into the environment,
# then leaves $PY / $SRC_DIR / $VENV set for the caller. Values containing JSON in
# .env must be quoted (see src/.env.example) so `source` preserves them intact.

_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$_ENV_DIR")"
SRC_DIR="$PROJECT_ROOT/src"
VENV="$PROJECT_ROOT/.venv"
PY="$VENV/bin/python3"

if [ ! -x "$PY" ]; then
    echo "No virtualenv at $VENV — run scripts/setup.sh first." >&2
    exit 1
fi
if [ ! -f "$SRC_DIR/.env" ]; then
    echo "No $SRC_DIR/.env — run scripts/setup.sh first." >&2
    exit 1
fi

# Export everything the file sets, then source it (KEY=value / quoted-JSON lines).
set -a
# shellcheck disable=SC1091
. "$SRC_DIR/.env"
set +a
