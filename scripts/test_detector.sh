#!/usr/bin/env bash
# test_detector.sh — end-to-end test of the detection→alert path: runs a known
# endorsement sample through the detector, then posts the resulting alert to
# Discord. Requires Ollama running (see scripts/setup.sh step 1) and
# DISCORD_WEBHOOK_URL set in src/.env.
#
# To exercise the detector on its own (several sample cases, no Discord send):
#   cd src && ../.venv/bin/python3 -m detector.endorsement_detector
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
cd "$SRC_DIR"
exec "$PY" -m webhook.webhook "$@"
