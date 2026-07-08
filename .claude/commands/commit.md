---
description: Turn uncommitted work into a commit (and optionally a PR) using the change journal, following the Hybrid main-vs-PR workflow.
argument-hint: "[main|pr]  — optional, forces the mode; otherwise inferred"
---

You are running the project's commit helper. Deployment to the Pi is handled by
Syncthing (it syncs files, not git), so the branch-vs-main choice below is purely
about history and review — it does **not** affect what runs on the Pi.

## 1. Gather context

- Run `git status` and `git diff --stat` (include staged + unstaged + untracked).
- Read `.claude/pending-changes.md` — this is the intended source of the commit message.
- If the journal has no substantive entries but there are real code changes,
  reconstruct the summary from the diff yourself, and note that the journal was empty.
- Never stage build/junk paths (`.venv/`, `__pycache__/`, `.DS_Store`); they're gitignored — keep it that way.

## 2. Choose the mode (Hybrid)

If `$ARGUMENTS` is `main` or `pr`, use that. Otherwise infer and state your pick in one line with the reason:

- **Direct to main** — small, low-risk, self-contained: docs, config tweaks, comments,
  a single-file bugfix, trivial edits.
- **Branch + PR** — non-trivial or worth reviewing: new features, multi-file refactors
  (e.g. the import-path fix), changes to detector/collector/schema logic, anything risky,
  or anything you'd want `/code-review` on.

When genuinely on the line, prefer a PR — it's cheap here and gives a review surface.

## 3. Confirm before anything outward-facing

- Show the proposed commit message (and branch name / PR title+body if applicable).
- **Local commit** is fine to make on the user's `/commit` go-ahead — that's the authorization,
  including committing directly to `main` in direct mode.
- **Pushing and opening a PR are outward-facing** — confirm explicitly before `git push` or `gh pr create`.
  Do not auto-push in direct-to-main mode; ask whether to push (Syncthing already handles deployment).

## 4. Execute

**Commit message:** imperative subject ≤72 chars, body explaining the *why* from the journal. End the message with:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

**Direct to main:**

1. `git add` the intended paths, `git commit`.
2. Ask whether to `git push` (don't assume).

**Branch + PR:**

1. `git switch -c <type>/<slug>` (`feat`/`fix`/`chore`/`docs` + short kebab slug).
2. Commit as above.
3. On confirmation: `git push -u origin <branch>`, then `gh pr create` (title = subject, body from journal).
   End the PR body with:

   ```
   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   ```
  
4. Offer to run `/code-review` on the PR. Squash-merge is the intended finish — remind the user, don't auto-merge.

## 5. Clear the journal

After a successful commit, remove the logged entries from `.claude/pending-changes.md`,
leaving the header block intact. The journal should only ever hold work since the last commit.
