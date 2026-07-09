#!/usr/bin/env bash
# start.sh — run the scheduler (blocking): all collectors + detection on their intervals.
# Same program the trump-tracker systemd service runs (main.py); handy for a manual
# foreground run with src/.env loaded.
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
cd "$SRC_DIR"
exec "$PY" main.py "$@"
