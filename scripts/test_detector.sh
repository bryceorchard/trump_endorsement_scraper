#!/usr/bin/env bash
# test_detector.sh — quick manual test of the endorsement detector against sample text.
# Requires Ollama to be running (see scripts/setup.sh step 1).
set -e
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
cd "$SRC_DIR"
exec "$PY" -m detector.endorsement_detector "$@"
