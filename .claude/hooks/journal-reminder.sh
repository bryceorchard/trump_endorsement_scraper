#!/usr/bin/env bash
#
# Stop hook backstop for the change journal.
#
# Fires when Claude finishes a turn. If there are uncommitted changes that are
# newer than .claude/pending-changes.md, it blocks the stop and tells Claude to
# log the change. Once Claude updates the journal (making it the newest file),
# the condition clears and the turn is allowed to end.
#
# See CLAUDE.md "Change journal" for the workflow this enforces.

set -uo pipefail

input=$(cat)

# Avoid an infinite loop: if we already blocked once this turn, let it stop.
if printf '%s' "$input" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$dir" 2>/dev/null || exit 0

journal=".claude/pending-changes.md"

# Uncommitted work: unstaged + staged + untracked, excluding anything under .claude/.
changed=$( { git diff --name-only; \
             git diff --name-only --cached; \
             git ls-files --others --exclude-standard; } 2>/dev/null \
           | grep -vE '^\.claude/' | sort -u )

# Nothing to log -> allow stop.
[ -z "$changed" ] && exit 0

# Newest mtime among the changed files.
newest=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  m=$(stat -f %m "$f" 2>/dev/null || echo 0)
  [ "$m" -gt "$newest" ] && newest=$m
done <<< "$changed"

journal_m=0
[ -f "$journal" ] && journal_m=$(stat -f %m "$journal" 2>/dev/null || echo 0)

# Journal is up to date with the latest change -> allow stop.
[ "$newest" -le "$journal_m" ] && exit 0

cat <<'EOF'
{"decision":"block","reason":"There are uncommitted changes newer than .claude/pending-changes.md. Before finishing, append an entry to that change journal describing what changed and why, which files were touched, and the current test status. If everything is already logged, just re-save the journal so its timestamp reflects the latest change."}
EOF
exit 0
