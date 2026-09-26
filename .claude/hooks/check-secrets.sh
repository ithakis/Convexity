#!/usr/bin/env bash
# Pre-commit secret guard. Blocks a git commit when a strictly-local secret
# file would be committed, or when the Finnhub API key value leaks into any
# staged content. Wired from .claude/settings.json (PreToolUse / git commit).
#
# Reads the hook payload on stdin and inspects the staged index — so it
# protects every commit made from inside a Claude Code session, on this
# branch or any future one.
set -euo pipefail

# Decide whether this invocation is a git commit. Fail CLOSED: if jq is
# missing or the payload can't be parsed, we cannot read the command, so we
# still run the secret checks rather than silently skipping them. (The hook
# is also gated to `git commit` by .claude/settings.json's `if` clause.)
if command -v jq >/dev/null 2>&1; then
  cmd="$(jq -r '.tool_input.command // ""' 2>/dev/null || echo "")"
  if [ -n "$cmd" ] && ! echo "$cmd" | grep -qE 'git[[:space:]]+commit'; then
    exit 0  # parsed a command and it is clearly not a commit — nothing to guard
  fi
fi

# 1) Never let a local-secret file enter the index.
staged="$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)"
if echo "$staged" | grep -qE '(^|/)\.finnhub_key$|(^|/)\.openrouter_key$|(^|/)\.nvidia_key$|(^|/)\.env\.local$|\.secret$'; then
  echo "BLOCKED: a strictly-local secret file is staged (.finnhub_key / .openrouter_key / .nvidia_key / .env.local / *.secret)."
  echo "Unstage it before committing:  git restore --staged <file>"
  exit 1
fi

# 1b) The repo is public (CLAUDE.md §18): runtime state holds real holdings,
#     and settings.local.json holds machine-specific permissions/paths.
if echo "$staged" | grep -qE '(^|/)\.(convexity|portfolio_tracker)_[a-z_]+\.json$|(^|/)settings\.local\.json$|(^|/)\.dashboard\.pid$|\.sqlite(-shm|-wal)?$'; then
  echo "BLOCKED: runtime state / local settings are staged (holdings, watchlists, caches, settings.local.json)."
  echo "These must never be published. Unstage:  git restore --staged <file>"
  exit 1
fi

# 1c) No absolute home-directory paths in added lines — they leak the local
#     username and machine layout. Use ~ / \$HOME / repo-relative paths.
if git diff --cached -U0 2>/dev/null | grep -E '^\+[^+]' | grep -qE '/Users/[A-Za-z0-9._-]+/|/home/[a-z][a-z0-9._-]*/'; then
  echo "BLOCKED: an absolute home-directory path (/Users/<name>/ or /home/<name>/) is in the staged diff."
  git diff --cached -U0 | grep -nE '^\+[^+].*(/Users/[A-Za-z0-9._-]+/|/home/[a-z][a-z0-9._-]*/)' | head -5
  exit 1
fi

# 2) Never let any key VALUE leak into any staged diff (even pasted elsewhere).
root="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
staged_diff="$(git diff --cached -U0 2>/dev/null || true)"
for kf in "$root/.finnhub_key" ".finnhub_key"; do
  [ -f "$kf" ] || continue
  key="$(tr -d '[:space:]' < "$kf")"
  if [ -n "$key" ] && echo "$staged_diff" | grep -Fq -- "$key"; then
    echo "BLOCKED: the Finnhub API key value was found in the staged diff."
    echo "Remove the literal key before committing (use the env var or .finnhub_key file)."
    exit 1
  fi
  break
done
for kf in "$root/.openrouter_key" ".openrouter_key"; do
  [ -f "$kf" ] || continue
  key="$(tr -d '[:space:]' < "$kf")"
  if [ -n "$key" ] && echo "$staged_diff" | grep -Fq -- "$key"; then
    echo "BLOCKED: the OpenRouter API key value was found in the staged diff."
    echo "Remove the literal key before committing (use the env var or .openrouter_key file)."
    exit 1
  fi
  break
done
for kf in "$root/.nvidia_key" ".nvidia_key"; do
  [ -f "$kf" ] || continue
  key="$(tr -d '[:space:]' < "$kf")"
  if [ -n "$key" ] && echo "$staged_diff" | grep -Fq -- "$key"; then
    echo "BLOCKED: the NVIDIA API key value was found in the staged diff."
    echo "Remove the literal key before committing (use the env var or .nvidia_key file)."
    exit 1
  fi
  break
done

echo "Secret scan clean."
exit 0
