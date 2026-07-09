#!/usr/bin/env bash
# run_once.sh — initialise the DB schema and run every collector + detection once.
# (init_db() runs unconditionally at startup, so this also sets up the schema.)
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
cd "$SRC_DIR"
exec "$PY" main.py --run-once "$@"
