#!/usr/bin/env bash
# Pre-commit secret guard. Blocks a git commit when a strictly-local secret
# file would be committed, or when the Finnhub API key value leaks into any
# staged content. Wired from .claude/settings.json (PreToolUse / git commit).
#
# Reads the hook payload on stdin and inspects the staged index — so it
# protects every commit made from inside a Claude Code session, on this
# branch or any future one.
set -euo pipefail

cmd="$(jq -r '.tool_input.command // ""' 2>/dev/null || echo "")"
echo "$cmd" | grep -qE 'git[[:space:]]+commit' || exit 0

# 1) Never let a local-secret file enter the index.
staged="$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)"
if echo "$staged" | grep -qE '(^|/)\.finnhub_key$|(^|/)\.env\.local$|\.secret$'; then
  echo "BLOCKED: a strictly-local secret file is staged (.finnhub_key / .env.local / *.secret)."
  echo "Unstage it before committing:  git restore --staged <file>"
  exit 1
fi

# 2) Never let the key VALUE leak into any staged diff (even pasted elsewhere).
root="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
for kf in "$root/.finnhub_key" ".finnhub_key"; do
  [ -f "$kf" ] || continue
  key="$(tr -d '[:space:]' < "$kf")"
  if [ -n "$key" ] && git diff --cached -U0 2>/dev/null | grep -Fq "$key"; then
    echo "BLOCKED: the Finnhub API key value was found in the staged diff."
    echo "Remove the literal key before committing (use the env var or .finnhub_key file)."
    exit 1
  fi
  break
done

echo "Secret scan clean."
exit 0
